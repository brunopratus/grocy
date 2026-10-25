import pytest

from app import receipts
from app.stock import UseIn, WeighIn, use, weigh
from app.units import convert_food, density
from tests.conftest import QU, make_services


def test_weight_to_volume_by_food():
    assert density("Pienas 2,5%") == 1.03 and density("Grietinėlė 30%") == 1.0 and density("Saulėgrąžų aliejus") == 0.92
    assert convert_food(620, "g", "ml", name="Pienas") == 602  # 620 g of milk is 602 ml
    assert convert_food(500, "ml", "g", name="Medus") == 710
    assert convert_food(1030, "g", "l", name="Milk") == 1.0


@pytest.mark.asyncio
async def test_stock_take_weighed_milk_in_its_carton(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 4, "name": "Pienas", "qu_id_stock": QU["ml"], "qu_id_purchase": QU["ml"]})
    svc = make_services(settings, catalog, fake_grocy)
    line = receipts.CommitLine(product_id=4, quantity=650, unit="g", container_g=30, opened=True)
    r = await receipts.commit_lines(svc, receipts.ReceiptCommitIn(lines=[line]))
    assert r["lines"][0]["added"].startswith("Added 602 ml Pienas"), r
    assert svc.db.get_kv("container_g:4") == "30.0"  # the carton is remembered for weighing later


@pytest.mark.asyncio
async def test_weigh_milk_uses_remembered_carton(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 4, "name": "Pienas", "qu_id_stock": QU["ml"]})
    fake_grocy.product_stock[4] = 602
    svc = make_services(settings, catalog, fake_grocy)
    svc.db.set_kv("container_g:4", "30")
    res = await weigh(svc, 4, WeighIn(grams_measured=442, includes_container=True))
    assert res["amount"] == 400 and ("inventory", 4, 400) in fake_grocy.calls


@pytest.mark.asyncio
async def test_use_a_dash_opens_the_pack_first(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 4, "name": "Pienas", "qu_id_stock": QU["ml"]})
    fake_grocy.uf[("products", 4)] = {"net_amount_per_pack": 1000, "net_unit": "ml"}
    fake_grocy.seed("stock", {"id": 1, "stock_id": "a", "product_id": 4, "amount": 2000, "open": 0})
    svc = make_services(settings, catalog, fake_grocy)
    r = await use(svc, 4, UseIn(amount=20, unit="ml"))
    assert ("open", 4, 1000, "a") in fake_grocy.calls
    assert ("consume", 4, 20, "a") in fake_grocy.calls  # taken from the opened pack
    assert r["message"] == "Used 20 ml Pienas · 1.98 l left"
    r = await use(svc, 4, UseIn(all=True))
    assert r["message"] == "Finished Pienas"


@pytest.mark.asyncio
async def test_weigh_open_bottle_keeps_the_unopened_one(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 4, "name": "Pienas", "qu_id_stock": QU["ml"]})
    fake_grocy.seed("stock", {"id": 1, "stock_id": "open", "product_id": 4, "amount": 600, "open": 1})
    fake_grocy.seed("stock", {"id": 2, "stock_id": "new", "product_id": 4, "amount": 1000, "open": 0})
    fake_grocy.product_stock[4] = 1600
    svc = make_services(settings, catalog, fake_grocy)
    res = await weigh(svc, 4, WeighIn(grams_measured=442, includes_container=True, container_g=30))
    assert res["amount"] == 1400 and ("consume", 4, 200, "open") in fake_grocy.calls
    assert "Pienas: weighed 400 ml" in res["message"]


@pytest.mark.asyncio
async def test_edit_rename_and_remove_from_the_stock_list(settings, catalog, fake_grocy):
    from app.stock import EntryEdit, RenameIn, edit_entry, entries, rename

    fake_grocy.seed("products", {"id": 4, "name": "Pienas", "qu_id_stock": QU["ml"]})
    fake_grocy.seed("products", {"id": 5, "name": "Eggs", "qu_id_stock": QU["piece"]})
    row = {"id": 7, "stock_id": "s7", "product_id": 4, "amount": 1000, "open": 0, "location_id": 1}
    fake_grocy.seed("stock", {**row, "best_before_date": "2026-11-01", "purchased_date": "2026-10-25", "price": 1.2})
    svc = make_services(settings, catalog, fake_grocy)
    e = await entries(svc, 4)
    assert e["entries"][0] == {
        "id": 7,
        "amount": 1000.0,
        "amount_text": "1 l",
        "best_before": "2026-11-01",
        "opened": False,
        "location": "Fridge",
    }
    r = await edit_entry(svc, 4, 7, EntryEdit(amount=600, best_before=None, location="Pantry", opened=True))
    body = next(c for c in fake_grocy.calls if c[0] == "edit_entry")[2]
    assert body["amount"] == 600 and body["open"] == 1 and body["location_id"] == 3
    assert body["best_before_date"] == "2999-12-31" and body["purchased_date"] == "2026-10-25" and body["price"] == 1.2
    assert r["message"] == "Saved Pienas: 600 ml"
    await edit_entry(svc, 4, 7, EntryEdit(amount=0))
    assert ("consume", 4, 1000.0, "s7") in fake_grocy.calls
    with pytest.raises(Exception, match="already called"):
        await rename(svc, 4, RenameIn(name="eggs"))
    assert (await rename(svc, 4, RenameIn(name="Milk 2.5%")))["name"] == "Milk 2.5%"
    assert svc.db.get_kv("original_name:4") == "Pienas"
