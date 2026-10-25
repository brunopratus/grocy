from dataclasses import replace

import pytest

from app import products, receipts
from tests.conftest import QU, make_services

ADVICE = {
    "english_name": "buckwheat groats",
    "usda_query": "buckwheat groats",
    "fineli_query_fi": "tattari",
    "storage_location": "Pantry",
    "shelf_life_days": 365,
    "shelf_life_after_opening_days": 180,
    "typical_piece_weight_g": None,
}


@pytest.mark.asyncio
async def test_by_name_existing_suggestion_and_new(settings, catalog, fake_grocy):
    fake_grocy.seed(
        "products", {"id": 1, "name": "Kiaušiniai", "qu_id_stock": QU["piece"], "qu_id_purchase": QU["piece"]}
    )
    svc = make_services(replace(settings, generic_food_sources=()), catalog, fake_grocy, [ADVICE])
    r = await products.product_by_name(svc, products.ByNameIn(name="kiaušiniai"))
    assert r["status"] == "existing" and r["product"]["id"] == 1
    r = await products.product_by_name(svc, products.ByNameIn(name="kiausiniai L"))
    assert r["status"] == "suggestion" and r["suggestion"]["name"] == "Kiaušiniai"
    r = await products.product_by_name(svc, products.ByNameIn(name="grikiai"))
    assert r["status"] == "created" and r["product"]["name"] == "Grikiai" and r["product"]["stock_unit"] == "g"


@pytest.mark.asyncio
async def test_home_stock_line_opened_with_date_and_place(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 4, "name": "Pienas", "qu_id_stock": QU["ml"], "qu_id_purchase": QU["pack"]})
    fake_grocy.uf[("products", 4)] = {"net_amount_per_pack": 1000, "net_unit": "ml"}
    svc = make_services(settings, catalog, fake_grocy)
    body = receipts.ReceiptCommitIn(
        lines=[
            receipts.CommitLine(
                product_id=4, quantity=2, unit="pack", opened=True, best_before="2026-10-30", location="Fridge"
            )
        ]
    )
    r = await receipts.commit_lines(svc, body)
    assert r["added"] == 1 and r["lines"][0]["added"] == "Added 2 l Pienas, 1 l of it opened"
    add = next(c for c in fake_grocy.calls if c[0] == "add_stock")
    assert add[2] == 2000 and add[3]["best_before_date"] == "2026-10-30" and add[3]["location_id"] == 1
    assert ("open", 4, 1000, None) in fake_grocy.calls
    assert add[3].get("price") is None  # stock-take: no price


@pytest.mark.asyncio
async def test_own_grocy_unit_is_added_as_is(settings, catalog, fake_grocy):
    catalog.unit_by_qu[7] = "Bottle"  # a unit made by hand in Grocy, unknown to the companion
    fake_grocy.seed("products", {"id": 3, "name": "Kefyras", "qu_id_stock": 7, "qu_id_purchase": 7})
    svc = make_services(settings, catalog, fake_grocy)
    body = receipts.ReceiptCommitIn(lines=[receipts.CommitLine(product_id=3, quantity=2, unit="Bottle", opened=True)])
    r = await receipts.commit_lines(svc, body)
    assert r["added"] == 1, r
    assert next(c for c in fake_grocy.calls if c[0] == "add_stock")[2] == 2
    assert ("open", 3, 1, None) in fake_grocy.calls


@pytest.mark.asyncio
async def test_weight_of_a_product_kept_in_pieces_sets_its_pack_size(settings, catalog, fake_grocy):
    fake_grocy.seed(
        "products", {"id": 9, "name": "Oat flakes", "qu_id_stock": QU["piece"], "qu_id_purchase": QU["piece"]}
    )
    svc = make_services(settings, catalog, fake_grocy)
    body = receipts.ReceiptCommitIn(lines=[receipts.CommitLine(product_id=9, quantity=1.5, unit="kg")])
    r = await receipts.commit_lines(svc, body)
    assert r["added"] == 1, r
    assert fake_grocy.uf[("products", 9)] == {"net_amount_per_pack": 1500, "net_unit": "g"}
    assert next(c for c in fake_grocy.calls if c[0] == "add_stock")[2] == 1  # one pack of 1.5 kg
