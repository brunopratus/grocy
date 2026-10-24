import pytest

from app import products, stock
from tests.conftest import QU, make_services

LABEL = {
    "product_name": "Coca-Cola",
    "name_en": "Cola",
    "brand": "Coca-Cola",
    "language": "lt",
    "net_quantity": None,
    "net_unit": None,
    "nutrition_basis": "per_100",
    "portion_size": None,
    "nutrition": {"kcal": 42, "protein": 0, "fat": 0, "carbs": 10.6, "sugar": 10.6, "salt": 0},
    "storage_location": "Pantry",
    "shelf_life_unopened_days": 180,
    "shelf_life_after_opening_days": 3,
    "is_food": True,
}


def _conversion(grocy):
    return [r for r in grocy.tables.get("quantity_unit_conversions", {}).values() if r["product_id"] == 9]


@pytest.mark.asyncio
async def test_pack_conversion_updates_the_one_grocy_made(settings, catalog, fake_grocy):
    # Grocy adds a 1:1 pack -> ml conversion itself when purchase and stock units differ
    fake_grocy.seed("products", {"id": 9, "name": "Cola", "qu_id_stock": QU["ml"], "qu_id_purchase": QU["pack"]})
    fake_grocy.seed(
        "quantity_unit_conversions",
        {"id": 1, "product_id": 9, "from_qu_id": QU["pack"], "to_qu_id": QU["ml"], "factor": 1},
    )
    svc = make_services(settings, catalog, fake_grocy)
    await products.set_pack_size(svc, fake_grocy.tables["products"][9], 2, "l")
    assert [c["factor"] for c in _conversion(fake_grocy)] == [2000]
    assert fake_grocy.uf[("products", 9)] == {"net_amount_per_pack": 2, "net_unit": "l"}


@pytest.mark.asyncio
async def test_label_uses_typed_pack_size_when_not_printed(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 9, "name": "Cola", "qu_id_stock": QU["ml"], "qu_id_purchase": QU["pack"]})
    svc = make_services(settings, catalog, fake_grocy, [LABEL])
    r = await products.label_photo(svc, 9, [(b"jpg", "image/jpeg")], None, 2000, "ml")
    assert r["pack_size"] == {"amount": 2000, "unit": "ml"}
    assert [c["factor"] for c in _conversion(fake_grocy)] == [2000]
    assert r["product"]["nutrition_per_100"]["kcal"] == 42


@pytest.mark.asyncio
async def test_label_without_pack_size_says_so(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 9, "name": "Cola", "qu_id_stock": QU["ml"], "qu_id_purchase": QU["ml"]})
    svc = make_services(settings, catalog, fake_grocy, [LABEL])
    r = await products.label_photo(svc, 9, [(b"jpg", "image/jpeg")])
    assert r["pack_size"] is None


@pytest.mark.asyncio
async def test_open_one_pack_of_a_gram_product(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 4, "name": "Varškė", "qu_id_stock": QU["g"], "qu_id_purchase": QU["g"]})
    fake_grocy.uf[("products", 4)] = {"net_amount_per_pack": 180, "net_unit": "g"}
    fake_grocy.seed(
        "stock", {"id": 1, "product_id": 4, "stock_id": "a", "amount": 360, "open": 0, "best_before_date": "2026-11-01"}
    )
    svc = make_services(settings, catalog, fake_grocy)
    r = await stock.open_product(svc, 4)
    assert ("open", 4, 180, "a") in fake_grocy.calls
    assert r == {"message": "Opened 180 g Varškė, use by 2026-10-27", "due": "2026-10-27"}


@pytest.mark.asyncio
async def test_open_needs_unopened_stock(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 4, "name": "Varškė", "qu_id_stock": QU["piece"]})
    svc = make_services(settings, catalog, fake_grocy)
    with pytest.raises(products.CaptureError):
        await stock.open_product(svc, 4)
