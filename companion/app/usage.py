"""Claude API usage as the companion sees it, and an estimate of the credit left.

Anthropic doesn't expose the prepaid balance to a normal API key, so the household enters the balance
shown in console.anthropic.com after a top-up; the companion subtracts what it spent since then.
Calls made with the same key outside the companion are not counted.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from .deps import Services

LOW_CREDIT_USD = 2.0

TASK_NAMES = {
    "receipt_photo": "Reading receipts",
    "label_photo": "Reading nutrition labels",
    "recipe_import": "Reading recipes",
    "mealplan": "Week plans",
    "shelf_life": "Shelf life",
    "storage_advice": "Storage advice",
    "due_date": "Reading due dates",
    "food_pick": "Matching market produce",
    "produce_translate": "Naming market produce",
    "english_names": "English product names",
}


class CreditIn(BaseModel):
    balance_usd: float = Field(ge=0, le=10000, description="Credit balance shown in console.anthropic.com now")


def set_credit(svc: Services, body: CreditIn) -> dict[str, Any]:
    svc.db.set_kv("credit_balance_usd", str(body.balance_usd))
    svc.db.set_kv("credit_since", datetime.now().isoformat(timespec="seconds"))
    return summary(svc)


def summary(svc: Services, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    start_30 = today - timedelta(days=29)
    daily = {r["day"]: r for r in svc.db.claude_daily(start_30.isoformat())}
    days = []
    for i in range(30):
        d = (start_30 + timedelta(days=i)).isoformat()
        row = daily.get(d, {})
        days.append({"date": d, "cost_usd": round(row.get("cost_usd") or 0, 4), "calls": row.get("calls") or 0})
    month = svc.db.claude_usage(today.replace(day=1).isoformat())
    last30 = svc.db.claude_usage(start_30.isoformat())
    by_task: dict[str, dict[str, Any]] = {}
    for r in last30["by_task"]:
        t = by_task.setdefault(
            r["task"], {"task": r["task"], "name": TASK_NAMES.get(r["task"], r["task"]), "calls": 0, "cost_usd": 0.0}
        )
        t["calls"] += r["calls"]
        t["cost_usd"] += r["cost_usd"] or 0
    tasks = sorted(by_task.values(), key=lambda t: -t["cost_usd"])
    for t in tasks:
        t["cost_usd"] = round(t["cost_usd"], 4)
        t["avg_usd"] = round(t["cost_usd"] / t["calls"], 4) if t["calls"] else 0

    credit = None
    balance, since = svc.db.get_kv("credit_balance_usd"), svc.db.get_kv("credit_since")
    if balance is not None and since:
        spent = svc.db.claude_usage(since)["total_cost_usd"]
        remaining = round(float(balance) - spent, 2)
        credit = {
            "balance_usd": float(balance),
            "since": since,
            "spent_since_usd": round(spent, 4),
            "remaining_usd": remaining,
            "low": remaining < LOW_CREDIT_USD,
        }
    return {
        "today_usd": days[-1]["cost_usd"],
        "month_usd": month["total_cost_usd"],
        "last_30_days_usd": last30["total_cost_usd"],
        "calls_30_days": sum(d["calls"] for d in days),
        "days": days,
        "by_task": tasks,
        "credit": credit,
        "low_credit_usd": LOW_CREDIT_USD,
    }
