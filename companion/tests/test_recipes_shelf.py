import json

import pytest

from app import products, recipes, stock
from tests.conftest import QU, make_services

PAGE = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@graph":[
{"@type":"WebPage","name":"x"},{"@type":"Recipe","name":"Cepelinai","recipeYield":"4",
"recipeIngredient":["1 kg bulvių","300 g kiaulienos faršo"]}]}</script></head>
<body><nav>menu</nav><p>Long story about grandma…</p></body></html>"""


def test_page_text_prefers_jsonld_recipe():
    text = recipes.page_text(PAGE)
    assert text.startswith("schema.org Recipe:")
    assert json.loads(text.split(": ", 1)[1])["name"] == "Cepelinai"


def test_page_text_falls_back_to_visible_text():
    text = recipes.page_text("<html><style>p{}</style><body><h1>Blynai</h1><p>2 kiaušiniai</p></body></html>")
    assert text == "Blynai 2 kiaušiniai"


@pytest.mark.asyncio
async def test_import_recipe_creates_grocy_recipe(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 1, "name": "Bulvės", "qu_id_stock": QU["g"], "qu_id_purchase": QU["g"]})
    parsed = {
        "name": "Cepelinai",
        "servings": 4,
        "instructions": "Grate, fill, boil.",
        "ingredients": [
            {"product_id": 1, "name": "potatoes", "amount": 1000, "unit": "g"},
            {"product_id": None, "name": "minced pork", "amount": 300, "unit": "g"},
        ],
    }
    svc = make_services(settings, catalog, fake_grocy, [parsed])
    r = await recipes.import_recipe(svc, text="Cepelinai: 1 kg bulvių, 300 g faršo")
    assert r["recipe"]["name"] == "Cepelinai" and r["servings"] == 4
    assert r["created_products"] == ["Minced pork"]
    pos = list(fake_grocy.tables["recipes_pos"].values())
    assert [(p["product_id"], p["amount"]) for p in pos][0] == (1, 1000)


@pytest.mark.asyncio
async def test_import_needs_some_input(settings, catalog, fake_grocy):
    svc = make_services(settings, catalog, fake_grocy)
    with pytest.raises(recipes.RecipeError):
        await recipes.import_recipe(svc)


@pytest.mark.asyncio
async def test_shelf_life_asks_then_saves(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 3, "name": "Vištienos filė", "default_best_before_days": 0})
    advice = {
        "is_food": True,
        "storage_location": "Fridge",
        "shelf_life_unopened_days": 2,
        "shelf_life_after_opening_days": 1,
        "location_after_opening": "Fridge",
    }
    svc = make_services(
        settings,
        catalog,
        fake_grocy,
        [
            {**advice, "question": "Fresh or frozen?", "options": ["Fresh", "Frozen"]},
            {**advice, "storage_location": "Freezer", "shelf_life_unopened_days": 180},
        ],
    )
    assert products.needs_shelf_life(fake_grocy.tables["products"][3])
    q = await products.shelf_life(svc, 3, products.ShelfLifeIn())
    assert q == {"status": "question", "question": "Fresh or frozen?", "options": ["Fresh", "Frozen"]}
    assert "default_best_before_days" not in [k for c in fake_grocy.calls if c[0] == "edit" for k in c[3]]
    saved = await products.shelf_life(svc, 3, products.ShelfLifeIn(answer="Frozen"))
    assert saved["status"] == "saved"
    row = fake_grocy.tables["products"][3]
    assert row["default_best_before_days"] == 180 and row["location_id"] == catalog.locations["freezer"]
    assert saved["product"]["location"] == "Freezer"
    assert not products.needs_shelf_life(row)


@pytest.mark.asyncio
async def test_inventory_lists_stock(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 5, "name": "Kefyras", "qu_id_stock": QU["piece"], "location_id": 1})
    fake_grocy.product_stock[5] = 2
    svc = make_services(settings, catalog, fake_grocy)
    items = await stock.inventory(svc)
    assert items == [
        {
            "product_id": 5,
            "name": "Kefyras",
            "was": None,
            "amount": 2.0,
            "unit": "piece",
            "kind": "count",
            "container_g": None,
            "amount_text": "2 pieces",
            "opened_text": None,
            "all_opened": False,
            "location": "Fridge",
            "due": None,
            "days_left": None,
            "value_eur": 0.0,
        }
    ]
