import pytest

from app.products import link_barcode
from tests.conftest import make_services


@pytest.mark.asyncio
async def test_link_barcode_skips_known_codes(settings, catalog, fake_grocy):
    fake_grocy.seed("products", {"id": 1, "name": "Alus Guinness"})
    fake_grocy.seed("products", {"id": 2, "name": "Kefyras"})
    fake_grocy.seed("product_barcodes", {"id": 1, "product_id": 1, "barcode": "4742024130276"})
    svc = make_services(settings, catalog, fake_grocy)

    # Same product again (label photo of a scanned product): nothing to do, no Grocy UNIQUE error
    assert await link_barcode(svc, 1, "4742024130276", 3) is None
    # Code belongs to another product: left alone, with a warning
    warning = await link_barcode(svc, 2, "4742024130276", 3)
    assert warning and "Alus Guinness" in warning
    assert not [c for c in fake_grocy.calls if c[:2] == ("add", "product_barcodes")]
    # New code: added
    assert await link_barcode(svc, 2, "4770000000001", 3) is None
    assert (
        "add",
        "product_barcodes",
        {"product_id": 2, "barcode": "4770000000001", "qu_id": 3, "amount": 1},
    ) in fake_grocy.calls
