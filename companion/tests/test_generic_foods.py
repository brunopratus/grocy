"""Fineli + USDA generic food lookup for market produce."""

import httpx
import pytest
import respx

from app.nutrition import FINELI_URL, fineli_search, parse_fineli_food
from app.products import ProduceAdvice, generic_nutrition

from .conftest import make_services

FINELI_CARROT = {
    "id": 11060,
    "name": {"fi": "Porkkana, kuorittu", "sv": "Morot, skalade", "en": "Carrot, peeled"},
    "energy": 140.5,  # kJ
    "energyKcal": 33.6,
    "fat": 0.2,
    "protein": 0.6,
    "carbohydrate": 6.5,
    "fiber": 2.7,
    "sugar": 4.9,
    "saturatedFat": 0.04,
    "salt": 95.0,  # mg
}
USDA_CARROTS = {
    "fdcId": 2258586,
    "description": "Carrots, raw",
    "dataType": "Foundation",
    "foodNutrients": [
        {"nutrientNumber": "958", "unitName": "KCAL", "value": 48},
        {"nutrientNumber": "203", "unitName": "G", "value": 0.94},
        {"nutrientNumber": "204", "unitName": "G", "value": 0.35},
        {"nutrientNumber": "205", "unitName": "G", "value": 10.3},
    ],
}
ADVICE = ProduceAdvice(
    english_name="carrots, raw",
    usda_query="carrots raw",
    fineli_query_fi="porkkana",
    storage_location="Fridge",
    shelf_life_days=21,
    shelf_life_after_opening_days=4,
    typical_piece_weight_g=70,
)


def test_parse_fineli_food():
    c = parse_fineli_food(FINELI_CARROT)
    assert (c.source, c.ref, c.description, c.detail) == ("fineli", "11060", "Carrot, peeled", "Porkkana, kuorittu")
    assert c.nutrition.kcal == 33.6 and c.nutrition.carbs == 6.5
    assert c.nutrition.salt == pytest.approx(0.095)  # mg -> g
    assert c.label.startswith("Fineli (THL), CC BY 4.0 #11060")


def test_parse_fineli_kj_only_and_flat_name():
    c = parse_fineli_food({"id": 1, "name": "Rye bread", "energy": 1000, "protein": 8})
    assert c.description == "Rye bread"
    assert c.nutrition.kcal == pytest.approx(239.0, abs=0.1)


@pytest.mark.asyncio
@respx.mock
async def test_fineli_search_merges_queries():
    route = respx.get(FINELI_URL).mock(
        side_effect=[
            httpx.Response(200, json=[FINELI_CARROT]),
            httpx.Response(200, json=[FINELI_CARROT, {**FINELI_CARROT, "id": 11061, "name": {"en": "Carrot, boiled"}}]),
        ]
    )
    async with httpx.AsyncClient() as http:
        found = await fineli_search(http, ["porkkana", "carrot"])
    assert [c.ref for c in found] == ["11060", "11061"]
    assert route.calls[0].request.url.params["q"] == "porkkana"


@pytest.mark.asyncio
@respx.mock
async def test_pick_across_sources(settings, catalog, fake_grocy):
    respx.get(FINELI_URL).respond(json=[FINELI_CARROT])
    respx.get("https://api.nal.usda.gov/fdc/v1/foods/search").respond(json={"foods": [USDA_CARROTS]})
    svc = make_services(settings, catalog, fake_grocy, [{"index": 0, "reason": "Fineli raw carrot"}])
    match = await generic_nutrition(svc, ADVICE)
    assert match is not None and match.source == "fineli" and match.nutrition.kcal == 33.6
    prompt = svc.claude._client.requests[0]["messages"][0]["content"][0]["text"]
    assert '"source":"fineli"' in prompt and '"source":"usda"' in prompt
    await svc.http.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_fineli_down_falls_back_to_usda(settings, catalog, fake_grocy):
    respx.get(FINELI_URL).respond(503)
    respx.get("https://api.nal.usda.gov/fdc/v1/foods/search").respond(json={"foods": [USDA_CARROTS]})
    svc = make_services(settings, catalog, fake_grocy, [{"index": 0, "reason": "only USDA"}])
    match = await generic_nutrition(svc, ADVICE)
    assert match is not None and match.source == "usda" and match.label == "USDA FDC 2258586: Carrots, raw"
    await svc.http.aclose()


@pytest.mark.asyncio
async def test_sources_can_be_limited(settings, catalog, fake_grocy):
    from dataclasses import replace

    svc = make_services(replace(settings, generic_food_sources=()), catalog, fake_grocy)
    assert await generic_nutrition(svc, ADVICE) is None
    await svc.http.aclose()
