from __future__ import annotations

import itertools
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.claude import ClaudeClient
from app.config import get_settings
from app.db import Database
from app.deps import Services
from app.grocy import GrocyClient, GrocyError
from app.setup import Catalog

QU = {"g": 1, "kg": 2, "ml": 3, "l": 4, "piece": 5, "pack": 6}


@pytest.fixture
def settings():
    return replace(
        get_settings(),
        grocy_url="http://grocy.test",
        grocy_api_key="k",
        companion_token="secret",
        db_path=":memory:",
        anthropic_api_key="test",
        ntfy_topic_url="",
    )


@pytest.fixture
def catalog() -> Catalog:
    c = Catalog()
    c.qu_by_unit = dict(QU)
    c.unit_by_qu = {v: k for k, v in QU.items()}
    c.locations = {"fridge": 1, "freezer": 2, "pantry": 3}
    c.shopping_locations = {"maxima": 1, "rimi": 2, "lidl": 3, "norfa": 4, "iki": 5, "turgus": 6}
    c.meal_sections = {"breakfast": 1, "lunch": 2, "dinner": 3}
    return c


class FakeGrocy(GrocyClient):
    """In-memory stand-in for the parts of the Grocy API the companion uses."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[int, dict[str, Any]]] = {}
        self.uf: dict[tuple[str, int], dict[str, Any]] = {}
        self.ids = itertools.count(100)
        self.calls: list[tuple[str, Any]] = []
        self.product_stock: dict[int, float] = {}

    async def aclose(self) -> None:  # pragma: no cover
        pass

    def seed(self, entity: str, row: dict[str, Any]) -> dict[str, Any]:
        self.tables.setdefault(entity, {})[int(row["id"])] = row
        return row

    @staticmethod
    def _match(row: dict[str, Any], q: str) -> bool:
        for op in (">=", "<=", "="):
            if op in q:
                k, v = q.split(op, 1)
                val = str(row.get(k, ""))
                return {"=": val == v, ">=": val >= v, "<=": val <= v}[op]
        return True

    async def objects(self, entity: str, query: list[str] | None = None) -> list[dict[str, Any]]:
        rows = list(self.tables.get(entity, {}).values())
        for q in query or []:
            rows = [r for r in rows if self._match(r, q)]
        return [dict(r) for r in rows]

    async def object(self, entity: str, object_id: int) -> dict[str, Any]:
        try:
            return dict(self.tables[entity][int(object_id)])
        except KeyError as e:
            raise GrocyError(404, "not found", f"/objects/{entity}/{object_id}") from e

    async def add_object(self, entity: str, data: dict[str, Any]) -> int:
        new_id = next(self.ids)
        defaults = {"recipes": {"type": "normal"}}.get(entity, {})
        self.tables.setdefault(entity, {})[new_id] = {"id": new_id, **defaults, **data}
        self.calls.append(("add", entity, data))
        return new_id

    async def edit_object(self, entity: str, object_id: int, data: dict[str, Any]) -> None:
        self.tables[entity][int(object_id)].update(data)
        self.calls.append(("edit", entity, object_id, data))

    async def delete_object(self, entity: str, object_id: int) -> None:
        del self.tables[entity][int(object_id)]
        self.calls.append(("delete", entity, object_id))

    async def userfields(self, entity: str, object_id: int) -> dict[str, Any]:
        return dict(self.uf.get((entity, int(object_id)), {}))

    async def set_userfields(self, entity: str, object_id: int, values: dict[str, Any]) -> None:
        self.uf.setdefault((entity, int(object_id)), {}).update(values)

    async def product_details(self, product_id: int) -> dict[str, Any]:
        return {
            "product": await self.object("products", product_id),
            "stock_amount": self.product_stock.get(product_id, 0),
            "last_price": 0,
        }

    async def product_by_barcode(self, barcode: str) -> dict[str, Any] | None:
        for row in self.tables.get("product_barcodes", {}).values():
            if row["barcode"] == barcode:
                return await self.product_details(int(row["product_id"]))
        return None

    async def stock(self) -> list[dict[str, Any]]:
        return [
            {
                "product_id": pid,
                "product": self.tables["products"][pid],
                "amount": amount,
                "best_before_date": "2999-12-31",
            }
            for pid, amount in self.product_stock.items()
            if amount > 0
        ]

    async def stock_entries(self, product_id: int) -> list[dict[str, Any]]:
        return [dict(e) for e in self.tables.get("stock", {}).values() if e["product_id"] == product_id]

    async def open_stock(self, product_id: int, amount: float, stock_entry_id: str | None = None) -> Any:
        self.calls.append(("open", product_id, amount, stock_entry_id))
        for e in self.tables.get("stock", {}).values():
            if e["stock_id"] == stock_entry_id:
                e.update({"open": 1, "best_before_date": "2026-10-27"})
        return [{"transaction_id": "tx-open"}]

    async def volatile(self, due_soon_days: int | None = None) -> dict[str, Any]:
        return {"due_products": [], "overdue_products": [], "expired_products": [], "missing_products": []}

    async def add_stock(self, product_id: int, amount: float, **extra: Any) -> Any:
        self.calls.append(("add_stock", product_id, amount, extra))
        self.product_stock[product_id] = self.product_stock.get(product_id, 0) + amount
        return [{"transaction_id": "tx-add"}]

    async def inventory(self, product_id: int, new_amount: float, **extra: Any) -> Any:
        self.calls.append(("inventory", product_id, new_amount))
        return [{"transaction_id": "tx-inv"}]

    async def recipe_fulfillment(self, recipe_id: int) -> dict[str, Any]:
        return {"costs": 3.0, "costs_per_serving": 1.5, "calories": 0, "need_fulfilled": 0}

    async def add_missing_to_shoppinglist(self, recipe_id: int) -> Any:
        self.calls.append(("shoppinglist", recipe_id))

    async def upload_file(self, group: str, file_name: str, data: bytes) -> None:
        self.calls.append(("upload", group, file_name))


class FakeClaude:
    """Returns queued tool inputs (or raw content lists) from beta.messages.stream()."""

    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        out = self.outputs.pop(0)
        if isinstance(out, list):
            content = out
        else:
            content = [SimpleNamespace(type="tool_use", name="submit_result", id=f"tu_{len(self.requests)}", input=out)]
        msg = SimpleNamespace(
            content=content,
            stop_reason="tool_use" if not isinstance(out, list) else "end_turn",
            model=kwargs["model"],
            usage=SimpleNamespace(
                input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0
            ),
        )

        class _Ctx:
            async def __aenter__(self_inner) -> Any:
                return SimpleNamespace(get_final_message=self_inner._final)

            async def _final(self_inner) -> Any:
                return msg

            async def __aexit__(self_inner, *a: Any) -> None:
                return None

        return _Ctx()


@pytest.fixture
def fake_grocy() -> FakeGrocy:
    return FakeGrocy()


def make_services(settings, catalog, grocy, claude_outputs: list[Any] | None = None) -> Services:
    db = Database(":memory:")
    return Services(
        settings=settings,
        grocy=grocy,
        claude=ClaudeClient(settings, db, client=FakeClaude(claude_outputs or [])),
        db=db,
        catalog=catalog,
        http=httpx.AsyncClient(),
    )
