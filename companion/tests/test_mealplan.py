"""Plan -> Grocy mapping with an in-memory fake Grocy."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.mealplan import PlanRequest, WeekPlan, apply_plan, grocy_week_names, next_monday, summarize

from .conftest import make_services

WEEK = date(2026, 10, 19)  # a Monday


def plan_dict(**over):
    days = [
        {
            "date": f"2026-10-{19 + i}",
            "meals": [
                {"slot": "lunch", "recipe_key": "bolo", "servings": 2},
                {"slot": "dinner", "recipe_key": "omelette", "servings": 2},
            ],
        }
        for i in range(7)
    ]
    d = {
        "days": days,
        "recipes": [
            {
                "key": "bolo",
                "name": "Lentil bolognese",
                "servings": 2,
                "instructions": "Cook.",
                "source_url": "https://example.com/bolo",
                "uses_waste_risk": ["Spaghetti"],
                "ingredients": [
                    {"product_id": 1, "name": "spaghetti", "amount": 200, "unit": "g"},
                    {"product_id": None, "name": "red lentils", "amount": 150, "unit": "g"},
                ],
            },
            {
                "key": "omelette",
                "name": "Omelette",
                "servings": 2,
                "instructions": "Fry.",
                "est_cost_eur": 2.0,
                "ingredients": [
                    {"product_id": 2, "name": "eggs", "amount": 4, "unit": "piece"},
                    {"product_id": 3, "name": "milk", "amount": 2, "unit": "piece"},
                ],
            },
        ],
        "shopping_delta": [{"name": "red lentils", "amount": 1050, "unit": "g"}],
    }
    d.update(over)
    return d


def seed(g):
    g.seed("products", {"id": 1, "name": "Spaghetti", "qu_id_stock": 1, "qu_id_purchase": 6})
    g.seed("products", {"id": 2, "name": "Eggs", "qu_id_stock": 1, "qu_id_purchase": 5})
    g.seed("products", {"id": 3, "name": "Milk", "qu_id_stock": 3, "qu_id_purchase": 3})
    g.uf[("products", 1)] = {"kcal_100": 359, "protein_100": 12.5, "fat_100": 1.5, "carbs_100": 71}
    g.uf[("products", 2)] = {
        "kcal_100": 143,
        "protein_100": 12.6,
        "fat_100": 9.5,
        "carbs_100": 0.7,
        "net_amount_per_pack": 60,
        "net_unit": "g",
    }
    g.seed("recipes", {"id": 50, "name": "2026-42", "type": "mealplan-week"})


def test_unknown_recipe_key_rejected():
    bad = plan_dict()
    bad["days"][0]["meals"][0]["recipe_key"] = "nope"
    with pytest.raises(ValidationError):
        WeekPlan.model_validate(bad)


def test_week_helpers():
    assert next_monday(date(2026, 10, 18)) == WEEK
    assert next_monday(WEEK) == WEEK
    assert grocy_week_names(WEEK) == ["2026-42"]
    assert grocy_week_names(date(2026, 10, 22)) == ["2026-42", "2026-43"]


@pytest.mark.asyncio
async def test_apply_plan_writes_recipes_positions_and_meal_plan(settings, catalog, fake_grocy):
    seed(fake_grocy)
    svc = make_services(settings, catalog, fake_grocy)
    plan = WeekPlan.model_validate(plan_dict())
    req = PlanRequest(week_start=WEEK)
    res = await apply_plan(svc, plan, req)

    recipes = {r["name"]: r for r in await fake_grocy.objects("recipes", ["type=normal"])}
    assert set(recipes) == {"Lentil bolognese", "Omelette"}
    bolo = recipes["Lentil bolognese"]
    assert bolo["base_servings"] == 2 and "https://example.com/bolo" in bolo["description"]
    assert fake_grocy.uf[("recipes", bolo["id"])]["grocy_plus_week"] == "2026-10-19"

    pos = await fake_grocy.objects("recipes_pos")
    by = {(p["recipe_id"], p["product_id"]): p for p in pos}
    assert by[(bolo["id"], 1)]["amount"] == 200
    lentils = next(p for p in await fake_grocy.objects("products") if p["name"] == "Red lentils")
    assert by[(bolo["id"], lentils["id"])]["amount"] == 150 and res["created_products"] == ["Red lentils"]
    om = recipes["Omelette"]
    assert by[(om["id"], 2)]["amount"] == 240  # 4 eggs x 60 g, stock unit g
    # milk "piece" can't be converted to ml -> single-unit check + warning
    assert by[(om["id"], 3)]["only_check_single_unit_in_stock"] == 1
    assert any("milk" in w for w in res["warnings"])

    meals = await fake_grocy.objects("meal_plan")
    assert len(meals) == 14
    assert {m["section_id"] for m in meals} == {2, 3}
    assert ("shoppinglist", 50) in fake_grocy.calls  # week shadow recipe used for the shopping list


@pytest.mark.asyncio
async def test_rerun_replaces_only_generated_recipes(settings, catalog, fake_grocy):
    seed(fake_grocy)
    fake_grocy.seed("recipes", {"id": 9, "name": "Grandma's soup", "type": "normal"})
    svc = make_services(settings, catalog, fake_grocy)
    plan = WeekPlan.model_validate(plan_dict())
    req = PlanRequest(week_start=WEEK)
    await apply_plan(svc, plan, req)
    res = await apply_plan(svc, plan, req)
    names = sorted(r["name"] for r in await fake_grocy.objects("recipes", ["type=normal"]))
    assert names == ["Grandma's soup", "Lentil bolognese", "Omelette"]
    assert sorted(res["removed_previous"]) == ["Lentil bolognese", "Omelette"]
    assert len(await fake_grocy.objects("meal_plan")) == 14


@pytest.mark.asyncio
async def test_summary_waste_risk_cost_and_kcal(settings, catalog, fake_grocy):
    seed(fake_grocy)
    svc = make_services(settings, catalog, fake_grocy)
    plan = WeekPlan.model_validate(plan_dict())
    s = await summarize(svc, plan, PlanRequest(week_start=WEEK, dry_run=True), {1}, None)
    lunch = s["days"][0]["meals"][0]
    assert lunch["uses_waste_risk"] == ["Spaghetti"]
    assert lunch["cost_eur"] is None  # no estimate given for bolognese
    dinner = s["days"][0]["meals"][1]
    assert dinner["cost_eur"] == 2.0
    # 200 g spaghetti for 2 servings, 2 eaten, 2 people -> 359 kcal per person
    assert lunch["nutrition_per_person"]["kcal"] == 359
    assert "red lentils" in lunch["nutrition_per_person"]["missing"]
