"""Open checklists (receipts, lists without a receipt, the home stock-take), shared by the household's phones.

Each list is stored on the server with a version number. A phone saves with the version it last saw; if
another phone saved in between, the save is refused (409) with the newer list, so nothing is silently lost.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .deps import Services

PREFIX = "list:"
MAX_BYTES = 1_000_000


class ListSave(BaseModel):
    version: int = Field(0, ge=0, description="The version this phone last saw (0: new list)")
    device: str | None = Field(None, max_length=60, description="Which phone saved it, e.g. 'iPhone'")
    data: dict[str, Any]


def _load(svc: Services, list_id: str) -> dict[str, Any] | None:
    raw = svc.db.get_kv(PREFIX + list_id)
    return json.loads(raw) if raw else None


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    data = row["data"]
    lines = [ln for ln in data.get("lines", []) if ln.get("include") and not ln.get("done")]
    return {
        "id": row["id"],
        "mode": data.get("mode") or "receipt",
        "store": data.get("store"),
        "date": data.get("date"),
        "open_lines": len(lines),
        "version": row["version"],
        "updated": row["updated"],
        "device": row.get("device"),
    }


def all_lists(svc: Services) -> list[dict[str, Any]]:
    rows = [json.loads(v) for v in svc.db.kv_prefix(PREFIX).values() if v]
    return sorted((_summary(r) for r in rows), key=lambda s: s["updated"], reverse=True)


def get(svc: Services, list_id: str) -> dict[str, Any]:
    row = _load(svc, list_id)
    if row is None:
        raise HTTPException(404, "This list is finished or was discarded")
    return row


def save(svc: Services, list_id: str, body: ListSave) -> dict[str, Any]:
    if not list_id.replace("-", "").isalnum() or len(list_id) > 40:
        raise HTTPException(400, "Bad list id")
    row = _load(svc, list_id)
    if row is not None and body.version != row["version"]:
        raise HTTPException(409, {"message": "Changed on another phone", "current": row})
    new = {
        "id": list_id,
        "version": (row["version"] if row else 0) + 1,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "device": body.device,
        "data": body.data,
    }
    raw = json.dumps(new, ensure_ascii=False)
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "List too large")
    svc.db.set_kv(PREFIX + list_id, raw)
    return {"id": list_id, "version": new["version"], "updated": new["updated"]}


def delete(svc: Services, list_id: str) -> dict[str, Any]:
    svc.db.set_kv(PREFIX + list_id, "")
    return {"deleted": list_id}
