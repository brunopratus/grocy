"""Cook / open logic: pure diffing plus the full flow against a respx-mocked Grocy HTTP API."""

import json
from datetime import date

import httpx
import pytest
import respx

from app.grocy import GrocyClient
from app.stock import cook, plan_opens

from .conftest import make_services


def entry(eid, amount, open_=0, price=0.0015, stock_id=None, bbd="2027-03-01"):
    return {
        "id": eid,
        "amount": amount,
        "open": open_,
        "price": price,
        "stock_id": stock_id or f"s{eid}",
        "best_before_date": bbd,
    }


def test_partly_used_entry_is_opened():
    plan = plan_opens({1: [entry(5, 1000)]}, {1: [entry(5, 500)]})
    assert [(u.entry_id, u.amount) for u in plan.usages] == [(5, 500)]
    assert len(plan.opens) == 1
    assert (plan.opens[0].stock_id, plan.opens[0].remaining) == ("s5", 500)


def test_fully_used_entry_is_not_opened():
    plan = plan_opens({1: [entry(5, 500)]}, {1: []})
    assert plan.usages[0].amount == 500
    assert plan.opens == [] and plan.already_open == []


def test_already_open_entry_is_reported_not_reopened():
    plan = plan_opens({1: [entry(5, 800, open_=1)]}, {1: [entry(5, 300, open_=1)]})
    assert plan.opens == []
    assert plan.already_open[0].remaining == 300


def test_untouched_and_multiple_entries():
    before = {1: [entry(5, 200, open_=1), entry(6, 1000)], 2: [entry(9, 6)]}
    after = {1: [entry(6, 900)], 2: [entry(9, 6)]}
    plan = plan_opens(before, after)
    assert sorted((u.entry_id, u.amount) for u in plan.usages) == [(5, 200), (6, 100)]
    assert [o.entry_id for o in plan.opens] == [6]


@pytest.mark.asyncio
@respx.mock(base_url="http://grocy.test/api")
async def test_cook_flow_opens_remaining_pasta(respx_mock, settings, catalog):
    pasta = {"id": 1, "name": "Spaghetti", "qu_id_stock": 1, "enable_tare_weight_handling": 0, "disable_open": 0}
    recipe = {"id": 7, "name": "Lentil bolognese", "base_servings": 2, "desired_servings": 2}
    entries_calls = iter(
        [
            [entry(5, 1000)],  # before consume
            [entry(5, 500)],  # after consume
            [entry(5, 500, open_=1, bbd="2026-12-01")],  # after open: due date recalculated
        ]
    )
    respx_mock.get("/objects/recipes/7").respond(json=recipe)
    respx_mock.get("/objects/recipes_pos_resolved").respond(json=[{"recipe_id": 7, "product_id": 1}])
    respx_mock.get("/objects/products").respond(json=[pasta])
    respx_mock.get("/stock/products/1/entries").mock(
        side_effect=lambda req: httpx.Response(200, json=next(entries_calls))
    )
    consume = respx_mock.post("/recipes/7/consume").respond(204)
    open_route = respx_mock.post("/stock/products/1/open").respond(json=[{"transaction_id": "t1"}])
    respx_mock.get("/userfields/products/1").respond(
        json={"kcal_100": "359", "protein_100": "12.5", "fat_100": "1.5", "carbs_100": "71"}
    )

    grocy = GrocyClient("http://grocy.test/api", "k")
    svc = make_services(settings, catalog, grocy)
    res = await cook(svc, 7)

    assert consume.called
    sent = json.loads(open_route.calls.last.request.content)
    assert sent == {"amount": 500.0, "stock_entry_id": "s5"}
    today = date.today().isoformat()
    assert res["notices"] == [f"Spaghetti: opened {today} for 'Lentil bolognese', 500 g left, use by 2026-12-01"]
    assert res["cost_total_eur"] == 0.75
    assert res["cost_per_serving_eur"] == 0.38
    assert res["nutrition_per_serving"]["kcal"] == round(359 * 5 / 2)
    kinds = [e["kind"] for e in svc.db.list_events()]
    assert "opened" in kinds and "cooked" in kinds
    await grocy.aclose()


@pytest.mark.asyncio
@respx.mock(base_url="http://grocy.test/api", assert_all_called=False)
async def test_cook_tare_product_not_opened(respx_mock, settings, catalog):
    jar = {"id": 2, "name": "Honey", "qu_id_stock": 1, "enable_tare_weight_handling": 1, "disable_open": 0}
    respx_mock.get("/objects/recipes/8").respond(json={"id": 8, "name": "Tea", "desired_servings": 1})
    respx_mock.get("/objects/recipes_pos_resolved").respond(json=[{"recipe_id": 8, "product_id": 2}])
    respx_mock.get("/objects/products").respond(json=[jar])
    seq = iter([[entry(3, 400)], [entry(3, 380)]])
    respx_mock.get("/stock/products/2/entries").mock(side_effect=lambda r: httpx.Response(200, json=next(seq)))
    respx_mock.post("/recipes/8/consume").respond(204)
    open_route = respx_mock.post("/stock/products/2/open")
    respx_mock.get("/userfields/products/2").respond(json={})

    grocy = GrocyClient("http://grocy.test/api", "k")
    res = await cook(make_services(settings, catalog, grocy), 8)
    assert not open_route.called
    assert "cannot be marked opened" in res["notices"][0]
    assert res["nutrition_total"]["missing"] == ["Honey"]
    await grocy.aclose()
