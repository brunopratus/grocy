from dataclasses import replace

import pytest

from app import names, products, receipts
from tests.conftest import QU, make_services


@pytest.mark.asyncio
async def test_products_renamed_in_english_and_found_by_original_name(settings, catalog, fake_grocy):
    fake_grocy.seed(
        "products", {"id": 1, "name": "Kiaušiniai", "qu_id_stock": QU["piece"], "qu_id_purchase": QU["piece"]}
    )
    fake_grocy.seed("products", {"id": 2, "name": "Eggs", "qu_id_stock": QU["piece"], "qu_id_purchase": QU["piece"]})
    fake_grocy.seed("products", {"id": 3, "name": "Rice", "qu_id_stock": QU["g"], "qu_id_purchase": QU["g"]})
    reply = {"names": [{"id": 1, "english": "Eggs"}, {"id": 2, "english": "Eggs"}, {"id": 3, "english": "Rice"}]}
    svc = make_services(replace(settings, english_names=True), catalog, fake_grocy, [reply])
    r = await names.rename_to_english(svc)
    assert r["renamed"] == [{"id": 1, "from": "Kiaušiniai", "to": "Eggs (2)"}]  # "Eggs" was taken
    assert fake_grocy.tables["products"][1]["name"] == "Eggs (2)"
    assert (await products.find_product_by_name(svc, "kiaušiniai"))["id"] == 1
    assert await names.rename_to_english(svc) == {"renamed": []}  # done once, no new Claude call
    catalog_rows = [{"id": 1, "name": "Eggs (2)", "was": "Kiaušiniai"}]
    assert receipts.similar_product(catalog_rows, "KIAUSINIAI L 10vnt") == 1


def test_open_list_is_kept_on_the_server(settings, catalog, fake_grocy):
    from fastapi.testclient import TestClient

    from app.main import create_app

    svc = make_services(settings, catalog, fake_grocy)
    auth = {"Authorization": "Bearer secret"}
    with TestClient(create_app(svc, start_background=False)) as client:
        assert client.get("/lists/current", headers=auth).json() is None
        stock_take = {"mode": "home", "lines": [{"text": "Kiaušiniai", "quantity": 10}]}
        assert client.put("/lists/current", headers=auth, json=stock_take).json() == {"saved": True}
        assert client.get("/lists/current", headers=auth).json() == stock_take
        client.put("/lists/current", headers=auth, json=None)
        assert client.get("/lists/current", headers=auth).json() is None
