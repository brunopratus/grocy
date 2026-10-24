from datetime import date

from app import usage
from tests.conftest import make_services


def _call(db, ts, task, cost):
    db.log_claude_call(task, "claude-sonnet-5-5", 1000, 100, 0, 0, cost, True)
    db._conn.execute("UPDATE claude_calls SET ts = ? WHERE id = (SELECT MAX(id) FROM claude_calls)", (ts,))


def test_summary_days_tasks_and_credit(settings, catalog, fake_grocy):
    svc = make_services(settings, catalog, fake_grocy)
    _call(svc.db, "2026-09-30T10:00:00", "receipt_photo", 0.03)  # previous month
    _call(svc.db, "2026-10-20T10:00:00", "label_photo", 0.013)
    _call(svc.db, "2026-10-24T09:00:00", "receipt_photo", 0.025)
    _call(svc.db, "2026-10-24T09:05:00", "shelf_life", 0.0003)
    s = usage.summary(svc, today=date(2026, 10, 24))
    assert len(s["days"]) == 30 and s["days"][-1] == {"date": "2026-10-24", "cost_usd": 0.0253, "calls": 2}
    assert s["month_usd"] == 0.0383 and s["calls_30_days"] == 4
    assert [t["name"] for t in s["by_task"]][:2] == [
        "Reading receipts",
        "Reading labels".replace("labels", "nutrition labels"),
    ]
    assert s["by_task"][0]["cost_usd"] == 0.055 and s["by_task"][0]["avg_usd"] == 0.0275
    assert s["credit"] is None

    svc.db.set_kv("credit_balance_usd", "5")
    svc.db.set_kv("credit_since", "2026-10-24T00:00:00")
    s = usage.summary(svc, today=date(2026, 10, 24))
    assert s["credit"]["spent_since_usd"] == 0.0253 and s["credit"]["remaining_usd"] == 4.97
    assert not s["credit"]["low"]
    svc.db.set_kv("credit_balance_usd", "1.5")
    assert usage.summary(svc, today=date(2026, 10, 24))["credit"]["low"]


def test_set_credit(settings, catalog, fake_grocy):
    svc = make_services(settings, catalog, fake_grocy)
    s = usage.set_credit(svc, usage.CreditIn(balance_usd=10))
    assert s["credit"]["balance_usd"] == 10 and s["credit"]["remaining_usd"] == 10
