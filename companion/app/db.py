"""Companion SQLite store: events, Claude call log, generated plans, small key/value state."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,              -- opened | used | weighed | added | created | cooked | plan | alert
    product_id INTEGER,
    product_name TEXT,
    recipe_id INTEGER,
    amount REAL,
    unit TEXT,
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS claude_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    note TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    week_start TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    plan TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bug_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    description TEXT NOT NULL,
    screen TEXT,
    version TEXT,
    context TEXT NOT NULL,           -- JSON: actions, errors, device, server log lines, checklist
    screenshot TEXT,                 -- file name under <data>/bugs/
    github_url TEXT,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def add_event(
        self,
        kind: str,
        message: str,
        *,
        product_id: int | None = None,
        product_name: str | None = None,
        recipe_id: int | None = None,
        amount: float | None = None,
        unit: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, kind, product_id, product_name, recipe_id, amount, unit, message, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._now(),
                    kind,
                    product_id,
                    product_name,
                    recipe_id,
                    amount,
                    unit,
                    message,
                    json.dumps(data) if data is not None else None,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def add_bug(self, description: str, screen: str | None, version: str, context: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bug_reports (ts, description, screen, version, context) VALUES (?, ?, ?, ?, ?)",
                (self._now(), description, screen, version, json.dumps(context, ensure_ascii=False)),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def update_bug(self, bug_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE bug_reports SET {cols} WHERE id = ?", [*fields.values(), bug_id])  # noqa: S608
            self._conn.commit()

    def list_bugs(self, status: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM bug_reports", []
        if status:
            sql, args = sql + " WHERE status = ?", [status]
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id DESC", args).fetchall()
        return [{**dict(r), "context": json.loads(r["context"])} for r in rows]

    def list_events(self, limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        args: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"]) if d["data"] else None
            out.append(d)
        return out

    def log_claude_call(
        self,
        task: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cost_usd: float,
        ok: bool,
        note: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO claude_calls (ts, task, model, input_tokens, output_tokens, cache_read_tokens,"
                " cache_write_tokens, cost_usd, ok, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._now(),
                    task,
                    model,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cost_usd,
                    int(ok),
                    note,
                ),
            )
            self._conn.commit()

    def claude_usage(self, since: str | None = None) -> dict[str, Any]:
        sql = (
            "SELECT task, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd FROM claude_calls"
        )
        args: list[Any] = []
        if since:
            sql += " WHERE ts >= ?"
            args.append(since)
        sql += " GROUP BY task, model ORDER BY cost_usd DESC"
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(sql, args).fetchall()]
        return {"by_task": rows, "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4)}

    def claude_daily(self, since: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS calls, SUM(cost_usd) AS cost_usd FROM claude_calls"
                " WHERE ts >= ? GROUP BY day ORDER BY day",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_plan(self, week_start: str, dry_run: bool, plan: dict[str, Any], summary: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO plans (ts, week_start, dry_run, plan, summary) VALUES (?, ?, ?, ?, ?)",
                (self._now(), week_start, int(dry_run), json.dumps(plan), json.dumps(summary)),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def get_kv(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()
