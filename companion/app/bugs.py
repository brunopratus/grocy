"""Bug reports from the phone: what the user saw plus what the app knows, ready for whoever fixes it.

Each report is stored in the companion DB (screenshot under <data>/bugs/). When BUG_GITHUB_REPO and
BUG_GITHUB_TOKEN are set it also becomes a GitHub issue (screenshot committed to that repo), so the
reports can be read and fixed later from a coding session.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .deps import Services

log = logging.getLogger(__name__)

SERVER_LOG_LINES = 80


class LogBuffer(logging.Handler):
    """Keeps the last server log lines in memory so a bug report can include them."""

    def __init__(self, size: int = 400) -> None:
        super().__init__(level=logging.INFO)
        self.lines: collections.deque[str] = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):  # logging must never fail the app
            self.lines.append(self.format(record))


LOG_BUFFER = LogBuffer()


def install_log_buffer() -> None:
    # uvicorn's loggers don't propagate to the root logger
    for name in ("", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if LOG_BUFFER not in logger.handlers:
            logger.addHandler(LOG_BUFFER)


def _issue_body(bug_id: int, description: str, screen: str | None, version: str, ctx: dict[str, Any]) -> str:
    def block(title: str, value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
        return f"<details><summary>{title}</summary>\n\n```\n{text[:20000]}\n```\n</details>\n"

    return (
        f"**What happened** (reported in the app)\n\n{description}\n\n"
        f"| | |\n|---|---|\n| Screen | `{screen or '-'}` |\n| App version | `{version}` |\n"
        f"| Device | {ctx.get('device', {}).get('userAgent', '-')} |\n| Companion bug | #{bug_id} |\n\n"
        + block("Last actions in the app", ctx.get("actions", []))
        + block("Errors seen in the app", ctx.get("errors", []))
        + block("Server log (last lines)", "\n".join(ctx.get("server_log", [])))
        + (block("Receipt checklist at the time", ctx["receipt"]) if ctx.get("receipt") else "")
    )


async def _to_github(
    svc: Services, bug_id: int, description: str, screen: str | None, ctx: dict[str, Any], screenshot: bytes | None
) -> str | None:
    repo, token = svc.settings.bug_github_repo, svc.settings.bug_github_token
    if not (repo and token):
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{repo}"
    body = _issue_body(bug_id, description, screen, svc.settings.version, ctx)
    try:
        if screenshot:
            path = f"screenshots/bug-{bug_id}.jpg"
            r = await svc.http.put(
                f"{api}/contents/{path}",
                headers=headers,
                json={"message": f"Screenshot for bug {bug_id}", "content": base64.b64encode(screenshot).decode()},
                timeout=30.0,
            )
            if r.status_code < 300:
                body = f"![screenshot](https://github.com/{repo}/blob/HEAD/{path}?raw=true)\n\n" + body
        title = "Bug: " + (description.strip().splitlines() or ["(no description)"])[0][:80]
        r = await svc.http.post(
            f"{api}/issues",
            headers=headers,
            json={"title": title, "body": body, "labels": ["bug-report"]},
            timeout=30.0,
        )
        r.raise_for_status()
        return str(r.json()["html_url"])
    except httpx.HTTPError as e:
        log.warning("bug %s not sent to GitHub: %s", bug_id, e)
        return None


async def report(
    svc: Services, description: str, screen: str | None, context: dict[str, Any], screenshot: bytes | None
) -> dict[str, Any]:
    ctx = {**context, "server_log": list(LOG_BUFFER.lines)[-SERVER_LOG_LINES:]}
    bug_id = svc.db.add_bug(description.strip(), screen, svc.settings.version, ctx)
    if screenshot:
        folder = Path(svc.settings.db_path).parent / "bugs"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"bug-{bug_id}.jpg"
        (folder / name).write_bytes(screenshot)
        svc.db.update_bug(bug_id, screenshot=name)
    url = await _to_github(svc, bug_id, description, screen, ctx, screenshot)
    if url:
        svc.db.update_bug(bug_id, github_url=url)
    log.info("bug report %s saved%s", bug_id, f" ({url})" if url else "")
    return {"id": bug_id, "github_url": url}
