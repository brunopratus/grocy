from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.claude import ClaudeError, estimate_cost, inline_schema
from app.main import create_app
from app.products import DueDate
from app.stock import WeighIn, weigh

from .conftest import make_services


class Inner(BaseModel):
    x: int


class Outer(BaseModel):
    inner: Inner
    items: list[Inner]


def test_inline_schema_resolves_refs():
    s = inline_schema(Outer.model_json_schema())
    assert "$defs" not in s
    assert s["properties"]["inner"]["properties"]["x"]["type"] == "integer"
    assert s["properties"]["items"]["items"]["properties"]["x"]["type"] == "integer"


def test_cost_estimate():
    assert estimate_cost("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(3.0)
    assert estimate_cost("claude-haiku-5-5", 10_000, 1_000) == pytest.approx(0.0015)


@pytest.mark.asyncio
async def test_structured_retries_once_then_validates(settings, catalog, fake_grocy):
    svc = make_services(
        settings,
        catalog,
        fake_grocy,
        [
            {"date": "31.12.2026", "kind": "use_by"},  # invalid date format -> retry
            {"date": "2026-12-31", "kind": "use_by", "confidence": "high"},
        ],
    )
    out = await svc.claude.structured(
        task="due_date", schema=DueDate, system="s", content=[{"type": "text", "text": "x"}]
    )
    assert out.date == "2026-12-31"
    fake = svc.claude._client
    assert len(fake.requests) == 2
    retry_msgs = fake.requests[1]["messages"]
    assert retry_msgs[-1]["content"][0]["type"] == "tool_result"
    assert retry_msgs[-1]["content"][0]["is_error"] is True
    assert fake.requests[0]["tool_choice"] == {"type": "auto"}
    assert fake.requests[0]["fallbacks"] == "default"
    usage = svc.db.claude_usage()
    assert usage["by_task"][0]["calls"] == 2 and usage["total_cost_usd"] > 0


@pytest.mark.asyncio
async def test_structured_gives_up_after_retry(settings, catalog, fake_grocy):
    svc = make_services(
        settings,
        catalog,
        fake_grocy,
        [
            [SimpleNamespace(type="text", text="no tool")],
            {"date": "nope"},
        ],
    )
    with pytest.raises(ClaudeError):
        await svc.claude.structured(task="due_date", schema=DueDate, system="s", content=[], cheap=True)
    assert "fallbacks" not in svc.claude._client.requests[0]  # not offered for the Haiku model


@pytest.mark.asyncio
async def test_weigh_with_tare(settings, catalog, fake_grocy):
    fake_grocy.seed(
        "products", {"id": 4, "name": "Honey", "qu_id_stock": 1, "enable_tare_weight_handling": 1, "tare_weight": 250}
    )
    fake_grocy.product_stock[4] = 400
    svc = make_services(settings, catalog, fake_grocy)
    res = await weigh(svc, 4, WeighIn(grams_measured=550))  # jar included by default
    assert ("inventory", 4, 550) in fake_grocy.calls  # Grocy expects the gross weight
    assert res["amount"] == 300 and res["difference"] == -100


@pytest.mark.asyncio
async def test_weigh_without_tare_needs_container(settings, catalog, fake_grocy):
    from app.products import CaptureError

    fake_grocy.seed("products", {"id": 5, "name": "Rice", "qu_id_stock": 2})  # kg stock unit
    fake_grocy.product_stock[5] = 1.0
    svc = make_services(settings, catalog, fake_grocy)
    with pytest.raises(CaptureError):
        await weigh(svc, 5, WeighIn(grams_measured=900, includes_container=True))
    res = await weigh(svc, 5, WeighIn(grams_measured=900, includes_container=True, container_g=150))
    assert res["amount"] == 0.75 and res["unit"] == "kg"
    assert ("inventory", 5, 0.75) in fake_grocy.calls


def test_auth(settings, catalog, fake_grocy):
    svc = make_services(settings, catalog, fake_grocy)
    with TestClient(create_app(svc, start_background=False)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/events").status_code == 401
        assert client.get("/events", headers={"X-Companion-Token": "wrong"}).status_code == 401
        assert client.get("/events", headers={"Authorization": "Bearer secret"}).status_code == 200
        assert client.post("/login", json={"token": "nope"}).status_code == 401
        r = client.post("/login", json={"token": "secret"})
        assert r.status_code == 200 and "companion_token" in r.cookies
        assert client.get("/events").status_code == 200  # cookie
        assert client.get("/bank/matches").status_code == 501
