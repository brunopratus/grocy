"""Phase 4: receipt import.

Built now: ``POST /receipts/photo`` — Claude parses a receipt photo, lines are matched to
Grocy products, and (with ``commit=true``) matched food lines are added to stock with
price and store. The original photo is kept under ``/data/receipts``.

TODO (phase 4, later):
  * IMAP mailbox for Maxima / Rimi e-receipts (``ImapReceiptSource``): poll, parse the
    HTML/PDF attachment with the same ``ReceiptParse`` schema, store the original.
  * Optional Lidl Plus connector (unofficial API, fragile) behind ``ReceiptSource``.
  * Remember line-text -> product mappings so repeat purchases match without Claude.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .claude import compact_json, image_block
from .deps import Services
from .products import StockAddIn, add_stock

log = logging.getLogger(__name__)


class ReceiptLine(BaseModel):
    text: str = Field(description="Line text as printed")
    name_en: str = Field(description="Generic English product name")
    quantity: float = Field(gt=0, description="Quantity bought (weight or count)")
    unit: Literal["g", "kg", "ml", "l", "piece", "pack"]
    line_total_eur: float = Field(description="Amount paid for this line after line discounts")
    is_food: bool = True
    product_id: int | None = Field(None, description="Matching catalog product id, only when clearly the same")


class ReceiptParse(BaseModel):
    store: str | None = Field(None, description="Maxima, Rimi, Lidl, Norfa, IKI or other")
    date: str | None = Field(None, description="Purchase date YYYY-MM-DD")
    total_eur: float | None = None
    lines: list[ReceiptLine]


class CommitLine(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)
    unit: str
    line_total_eur: float | None = Field(None, ge=0)


class ReceiptCommitIn(BaseModel):
    store: str | None = None
    lines: list[CommitLine]


class ReceiptSource(Protocol):
    """Interface for automatic receipt sources (IMAP e-receipts, Lidl Plus...). Not implemented yet."""

    async def fetch_new(self) -> list[tuple[bytes, str]]:
        """Return new receipt originals as (bytes, media_type)."""
        ...


SYSTEM = """You read Lithuanian supermarket receipts (Maxima, Rimi, Lidl, Norfa, IKI). Lines are usually
Lithuanian and abbreviated. Weighed items show a weight like '0,734 kg x 2,49'; repeated items show
'2,35 X 2 vnt.' under the name (quantity 2). Discount lines ('Nuolaida', 'AČIŪ nuolaida prekei: …')
belong to the item they name or the item just above: subtract them from that line, never list them.
Skip deposits ('užstatas', 'Skardinė (depozitinė)', 'Depozitas/įmoka'), shopping bags ('maišas'),
totals, VAT, loyalty card and payment lines. Pet food, cleaning and other non-food: is_food=false.
Map lines to catalog products by product_id only when it is clearly the same product."""


async def import_photo(
    svc: Services, image: bytes, media_type: str | None, commit: bool, data_dir: str
) -> dict[str, Any]:
    folder = Path(data_dir) / "receipts"
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / f"{datetime.now():%Y%m%d-%H%M%S}.jpg"
    original.write_bytes(image)

    catalog = [{"id": int(p["id"]), "name": p["name"]} for p in await svc.products()]
    parsed = await svc.claude.structured(
        task="receipt_photo",
        schema=ReceiptParse,
        system=SYSTEM,
        effort="medium",
        max_tokens=8000,
        content=[
            image_block(image, media_type),
            {"type": "text", "text": f"Product catalog: {compact_json(catalog)}\nParse this receipt."},
        ],
    )
    known_ids = {c["id"] for c in catalog}
    results: list[dict[str, Any]] = []
    for line in parsed.lines:
        row: dict[str, Any] = line.model_dump()
        if line.product_id not in known_ids:
            row["product_id"] = None
        if commit and line.is_food and row["product_id"]:
            row.update(await _add_line(svc, CommitLine(**row), parsed.store))
        results.append(row)
    try:
        purchase_date = date.fromisoformat(parsed.date) if parsed.date else None
    except ValueError:
        purchase_date = None
    return {
        "store": parsed.store,
        "date": purchase_date.isoformat() if purchase_date else None,
        "total_eur": parsed.total_eur,
        "lines": results,
        "committed": commit,
        "original": original.name,
    }


async def _add_line(svc: Services, line: CommitLine, store: str | None) -> dict[str, str]:
    try:
        res = await add_stock(
            svc,
            StockAddIn(
                product_id=line.product_id,
                amount=line.quantity,
                unit=line.unit,
                price_total=line.line_total_eur,
                store=store,
            ),
        )
        return {"added": res["message"]}
    except Exception as e:  # noqa: BLE001 - report per line
        return {"error": str(e)}


async def commit_lines(svc: Services, body: ReceiptCommitIn) -> dict[str, Any]:
    """Add the lines the user confirmed after a preview (no second Claude call)."""
    results = [{"product_id": line.product_id, **await _add_line(svc, line, body.store)} for line in body.lines]
    added = sum(1 for r in results if "added" in r)
    svc.db.add_event("receipt", f"Receipt from {body.store or 'unknown store'}: {added} line(s) added to stock")
    return {"added": added, "lines": results}
