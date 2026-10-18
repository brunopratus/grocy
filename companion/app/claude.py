"""Claude API wrapper: structured output through a tool, validated with Pydantic.

Every call:
  * sends images/PDFs as base64 content blocks,
  * asks Claude to answer by calling one tool whose ``input_schema`` is the
    JSON schema of a Pydantic model,
  * validates the tool input with Pydantic and retries once on failure,
  * logs tokens and estimated cost in the companion DB.

Note on ``tool_choice``: Claude Sonnet 5.5 / Opus 5.5 reject forced tool use
(``{"type": "tool"}`` / ``{"type": "any"}``) with a 400, so the tool is offered with
``tool_choice: auto`` plus an explicit instruction, and a missing tool call counts
as a validation failure (retried once).
"""

from __future__ import annotations

import base64
import copy
import io
import json
import logging
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

TOOL_NAME = "submit_result"

# USD per million tokens: (input, output). Cache writes 1.25x input, reads 0.1x input.
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-fable-5-1": (10.0, 50.0),
}

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_IMAGE_EDGE = 1568


class ClaudeError(RuntimeError):
    pass


def estimate_cost(
    model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0
) -> float:
    p_in, p_out = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    return (
        input_tokens * p_in + output_tokens * p_out + cache_write * p_in * 1.25 + cache_read * p_in * 0.1
    ) / 1_000_000


def inline_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref``s into ``$defs`` so the tool schema is self-contained."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return resolve({**target, **extra})
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def image_block(data: bytes, media_type: str | None = None) -> dict[str, Any]:
    """Base64 image block; large phone photos are downscaled to keep tokens low."""
    try:
        from PIL import Image, ImageOps

        img: Image.Image = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img) or img
        if max(img.size) > MAX_IMAGE_EDGE or (media_type or "") not in ("image/jpeg", "image/png"):
            img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            data, media_type = buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - fall back to sending the original bytes
        media_type = media_type or "image/jpeg"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type or "image/jpeg",
            "data": base64.standard_b64encode(data).decode(),
        },
    }


def pdf_block(data: bytes) -> dict[str, Any]:
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(data).decode()},
    }


class ClaudeClient:
    def __init__(self, settings: Settings, db: Database, client: Any | None = None) -> None:
        self.settings = settings
        self.db = db
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)

    async def structured(
        self,
        *,
        task: str,
        schema: type[T],
        content: list[dict[str, Any]],
        system: str,
        cheap: bool = False,
        effort: str = "medium",
        max_tokens: int = 8000,
        tool_description: str = "Submit the final structured result.",
    ) -> T:
        model = self.settings.claude_model_cheap if cheap else self.settings.claude_model
        tool = {
            "name": TOOL_NAME,
            "description": tool_description,
            "input_schema": inline_schema(schema.model_json_schema()),
        }
        instruction = {
            "type": "text",
            "text": f"Answer only by calling the `{TOOL_NAME}` tool exactly once with the complete result.",
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": [*content, instruction]}]

        last_error = "no attempt"
        for attempt in range(2):
            msg = await self._call(task, model, system, messages, tool, effort, max_tokens)
            if msg.stop_reason == "refusal":
                raise ClaudeError(f"Claude declined the request ({task})")
            tool_use = next(
                (b for b in msg.content if getattr(b, "type", None) == "tool_use" and b.name == TOOL_NAME),
                None,
            )
            if tool_use is None:
                last_error = f"no {TOOL_NAME} call (stop_reason={msg.stop_reason})"
                messages.append({"role": "assistant", "content": msg.content})
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"You did not call `{TOOL_NAME}`. Call it now with the result."},
                        ],
                    }
                )
                continue
            try:
                return schema.model_validate(tool_use.input)
            except ValidationError as e:
                last_error = str(e)
                log.warning("Claude %s output failed validation (attempt %d): %s", task, attempt + 1, e)
                messages.append({"role": "assistant", "content": msg.content})
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "is_error": True,
                                "content": f"Validation failed, fix these errors and call {TOOL_NAME} again:\n{e}",
                            }
                        ],
                    }
                )
        raise ClaudeError(f"Claude output for '{task}' invalid after retry: {last_error[:500]}")

    async def _call(
        self,
        task: str,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        effort: str,
        max_tokens: int,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": [tool],
            "tool_choice": {"type": "auto"},
            "output_config": {"effort": effort},
        }
        # Server-side refusal fallback (not offered for Haiku models)
        if self.settings.claude_fallbacks and "haiku" not in model:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            async with self._client.beta.messages.stream(**kwargs) as stream:
                msg = await stream.get_final_message()
        except anthropic.APIError as e:
            self.db.log_claude_call(task, model, 0, 0, 0, 0, 0.0, False, str(e)[:300])
            raise ClaudeError(f"Claude API error during '{task}': {e}") from e

        usage = msg.usage
        in_t = int(getattr(usage, "input_tokens", 0) or 0)
        out_t = int(getattr(usage, "output_tokens", 0) or 0)
        c_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        c_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        served_by = getattr(msg, "model", model) or model
        cost = estimate_cost(served_by, in_t, out_t, c_read, c_write)
        self.db.log_claude_call(task, served_by, in_t, out_t, c_read, c_write, cost, msg.stop_reason != "refusal")
        log.info("claude %s model=%s in=%d out=%d cost=$%.4f", task, served_by, in_t, out_t, cost)
        return msg


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
