import pytest

from app import receipts
from tests.conftest import QU, make_services

# What Claude returns for the Maxima receipt of 2026-10-23 (bag, can deposits and discount lines skipped)
PARSED = {
    "store": "Maxima",
    "date": "2026-10-23",
    "total_eur": 20.23,
    "lines": [
        {
            "text": "Alus ANARKIST NEW ENGLAND IPA",
            "name_en": "IPA beer",
            "quantity": 2,
            "unit": "piece",
            "line_total_eur": 4.70,
            "is_food": True,
            "product_id": None,
        },
        {
            "text": "Tamsusis alus GUINNESS DRAUGHT",
            "name_en": "Stout beer",
            "quantity": 1,
            "unit": "piece",
            "line_total_eur": 1.89,
            "is_food": True,
            "product_id": 1,
        },
        {
            "text": "Sausas kačių ėdalas ONE JUNIOR su vištiena",
            "name_en": "Dry cat food",
            "quantity": 2,
            "unit": "pack",
            "line_total_eur": 9.50,
            "is_food": False,
            "product_id": None,
        },
        {
            "text": "Aštrūs keptų ryžių krekeriai AJI",
            "name_en": "Rice crackers",
            "quantity": 1,
            "unit": "pack",
            "line_total_eur": 3.59,
            "is_food": True,
            "product_id": 999,
        },
    ],
}


def _seed(grocy):
    grocy.seed(
        "products", {"id": 1, "name": "Alus Guinness", "qu_id_stock": QU["piece"], "qu_id_purchase": QU["piece"]}
    )


@pytest.mark.asyncio
async def test_preview_matches_only_known_products(settings, catalog, fake_grocy, tmp_path):
    _seed(fake_grocy)
    svc = make_services(settings, catalog, fake_grocy, [PARSED])
    r = await receipts.import_photo(svc, b"jpg", "image/jpeg", False, str(tmp_path))
    assert r["store"] == "Maxima" and r["total_eur"] == 20.23 and not r["committed"]
    assert [line["product_id"] for line in r["lines"]] == [None, 1, None, None]  # 999 is not in Grocy
    assert (tmp_path / "receipts" / r["original"]).read_bytes() == b"jpg"
    assert not [c for c in fake_grocy.calls if c[0] == "add_stock"]
    prompt = svc.claude._client.requests[0]["system"]  # type: ignore[attr-defined]
    assert "depozitinė" in str(prompt) and "AČIŪ" in str(prompt)


@pytest.mark.asyncio
async def test_commit_confirmed_lines(settings, catalog, fake_grocy):
    _seed(fake_grocy)
    svc = make_services(settings, catalog, fake_grocy)
    body = receipts.ReceiptCommitIn(
        store="Maxima", lines=[receipts.CommitLine(product_id=1, quantity=1, unit="piece", line_total_eur=1.89)]
    )
    r = await receipts.commit_lines(svc, body)
    assert r["added"] == 1, r
    assert [c for c in fake_grocy.calls if c[0] == "add_stock"]
