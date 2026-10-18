"""Daily ntfy alert: opened items and items due within N days."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .deps import Services
from .units import fmt_amount

log = logging.getLogger(__name__)


def _days_text(days: int | None) -> str:
    if days is None:
        return "no due date"
    if days < 0:
        return f"{-days} day{'s' if days != -1 else ''} overdue"
    if days == 0:
        return "due today"
    return f"{days} day{'s' if days != 1 else ''} left"


async def open_and_due(svc: Services, due_days: int | None = None) -> list[dict[str, Any]]:
    """Opened products and products due within ``due_days`` days, soonest first."""
    due_days = svc.settings.alert_due_days if due_days is None else due_days
    today = date.today()
    items: dict[int, dict[str, Any]] = {}

    for s in await svc.grocy.stock():
        pid = int(s["product_id"])
        unit = svc.catalog.unit_of(s["product"].get("qu_id_stock"))
        opened = float(s.get("amount_opened") or 0)
        if opened <= 0:
            continue
        entries = [e for e in await svc.grocy.stock_entries(pid) if int(e.get("open") or 0) == 1]
        dues = [
            e["best_before_date"]
            for e in entries
            if e.get("best_before_date") and not e["best_before_date"].startswith("2999")
        ]
        due = min(dues) if dues else None
        items[pid] = {
            "product_id": pid,
            "name": s["product"]["name"],
            "opened": True,
            "opened_date": min((e.get("opened_date") or "" for e in entries), default="")[:10] or None,
            "amount": opened,
            "unit": unit,
            "amount_text": fmt_amount(opened, unit),
            "due": due,
            "days_left": (date.fromisoformat(due) - today).days if due else None,
        }

    volatile = await svc.grocy.volatile(due_soon_days=due_days)
    for key in ("expired_products", "overdue_products", "due_products"):
        for v in volatile.get(key, []):
            pid = int(v["product_id"])
            due = v.get("best_before_date")
            if not due or due.startswith("2999"):
                continue
            days_left = (date.fromisoformat(due) - today).days
            if days_left > due_days:
                continue
            if pid in items:
                if items[pid]["days_left"] is None or days_left < items[pid]["days_left"]:
                    items[pid].update(due=due, days_left=days_left)
                continue
            prod = v.get("product") or {}
            unit = svc.catalog.unit_of(prod.get("qu_id_stock"))
            amount = float(v.get("amount") or 0)
            items[pid] = {
                "product_id": pid,
                "name": prod.get("name", f"#{pid}"),
                "opened": False,
                "opened_date": None,
                "amount": amount,
                "unit": unit,
                "amount_text": fmt_amount(amount, unit),
                "due": due,
                "days_left": days_left,
            }
    return sorted(items.values(), key=lambda i: (i["days_left"] if i["days_left"] is not None else 9999, i["name"]))


def format_alert(items: list[dict[str, Any]]) -> tuple[str, str]:
    if not items:
        return "Grocy+: nothing to use up", "No opened or soon-due items. 👍"
    lines = []
    for i in items:
        icon = (
            "🔴"
            if (i["days_left"] is not None and i["days_left"] <= 0)
            else ("🟠" if i["days_left"] is not None and i["days_left"] <= 1 else "🟡")
        )
        what = f"opened, {i['amount_text']} left" if i["opened"] else i["amount_text"]
        lines.append(f"{icon} {i['name']} — {what}, {_days_text(i['days_left'])}")
    return f"Grocy+: {len(items)} item{'s' if len(items) != 1 else ''} to use up", "\n".join(lines)


async def preview(svc: Services) -> dict[str, Any]:
    items = await open_and_due(svc)
    title, body = format_alert(items)
    return {"title": title, "body": body, "items": items}


async def send(svc: Services, force: bool = False) -> dict[str, Any]:
    data = await preview(svc)
    if not svc.settings.ntfy_topic_url:
        return {**data, "sent": False, "reason": "NTFY_TOPIC_URL not set"}
    if not data["items"] and not force:
        return {**data, "sent": False, "reason": "nothing to report"}
    resp = await svc.http.post(
        svc.settings.ntfy_topic_url,
        content=data["body"].encode("utf-8"),
        headers={
            "Title": data["title"].encode("ascii", "ignore").decode(),
            "Tags": "shopping_cart",
            "Priority": "default",
        },
        timeout=20.0,
    )
    ok = resp.status_code < 300
    if not ok:
        log.warning("ntfy returned %s: %s", resp.status_code, resp.text[:200])
    svc.db.add_event("alert", data["title"], data={"sent": ok, "count": len(data["items"])})
    return {**data, "sent": ok}
