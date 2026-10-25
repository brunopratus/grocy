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

import contextlib
import difflib
import logging
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .claude import compact_json, image_block, pdf_block
from .deps import Services
from .products import StockAddIn, add_stock, net_of, set_pack_size, summary
from .units import UnitError, convert, fmt_amount, normalize_unit

log = logging.getLogger(__name__)


class ReceiptLine(BaseModel):
    text: str = Field(description="Line text as printed")
    name_en: str = Field(description="Generic English product name")
    quantity: float = Field(gt=0, description="Quantity bought (weight or count)")
    unit: Literal["g", "kg", "ml", "l", "piece", "pack"]
    line_total_eur: float = Field(description="Amount paid for this line after line discounts")
    is_food: bool = True
    product_id: int | None = Field(None, description="Matching catalog product id, only when clearly the same")
    suggested_product_id: int | None = Field(
        None,
        description="When product_id is null: a catalog product that is probably the same food under another "
        "name, brand or size (e.g. 'Viščių kiaušiniai L' -> 'Kiaušiniai'); the household confirms it",
    )


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
    pack_size: float | None = Field(None, gt=0, description="Net amount of one pack/piece, confirmed by the user")
    pack_unit: str | None = Field(None, description="g or ml")
    opened: bool = Field(False, description="Already opened (home stock-take): one pack/piece, or a weighed amount")
    best_before: date | None = None
    location: str | None = Field(None, description="Fridge, Freezer or Pantry; default: the product's")


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
Map lines to catalog products by product_id only when it is clearly the same product; when it only
probably is (other wording, brand or size), give it as suggested_product_id instead."""


async def import_photo(
    svc: Services, image: bytes, media_type: str | None, commit: bool, data_dir: str
) -> dict[str, Any]:
    folder = Path(data_dir) / "receipts"
    folder.mkdir(parents=True, exist_ok=True)
    is_pdf = media_type == "application/pdf" or image[:5] == b"%PDF-"
    original = folder / f"{datetime.now():%Y%m%d-%H%M%S}.{'pdf' if is_pdf else 'jpg'}"
    original.write_bytes(image)

    catalog = [{"id": int(p["id"]), "name": p["name"]} for p in await svc.products()]
    parsed = await svc.claude.structured(
        task="receipt_photo",
        schema=ReceiptParse,
        system=SYSTEM,
        effort="medium",
        max_tokens=8000,
        content=[
            pdf_block(image) if is_pdf else image_block(image, media_type),
            {"type": "text", "text": f"Product catalog: {compact_json(catalog)}\nParse this receipt."},
        ],
    )
    known_ids = {c["id"] for c in catalog}
    results: list[dict[str, Any]] = []
    for line in parsed.lines:
        row: dict[str, Any] = line.model_dump()
        if line.product_id not in known_ids:
            row["product_id"] = None
        row["product"] = await product_card(svc, row["product_id"]) if row["product_id"] else None
        suggestion = None
        if not row["product_id"]:
            suggestion = line.suggested_product_id if line.suggested_product_id in known_ids else None
            suggestion = suggestion or similar_product(catalog, line.text, line.name_en)
        row["suggestion"] = await product_card(svc, suggestion) if suggestion else None
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


def _norm(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return [w for w in re.findall(r"[a-z]{3,}", plain)]


def similar_product(catalog: list[dict[str, Any]], *names: str) -> int | None:
    """Fallback suggestion when Claude gave none: the catalog product sharing the most similar word."""
    words = [w for n in names if n for w in _norm(n)]
    best, best_score = None, 0.0
    for product in catalog:
        for pw in _norm(product["name"]):
            for w in words:
                score = difflib.SequenceMatcher(None, w, pw).ratio()
                if score > best_score:
                    best, best_score = product["id"], score
    return best if best_score >= 0.8 else None


async def inbox(svc: Services) -> dict[str, Any]:
    """Receipts waiting to be checked, from automatic sources (e-mail in a later phase; none yet).

    "Finish shopping" in the app shows these first and falls back to a photo of the paper receipt."""
    return {"receipts": [], "sources": []}


async def product_card(svc: Services, product_id: int) -> dict[str, Any]:
    """What the receipt checklist shows per product: stock unit, pack size and nutrition."""
    product = await svc.grocy.object("products", product_id)
    return summary(product, svc, await svc.grocy.userfields("products", product_id))


async def _add_line(svc: Services, line: CommitLine, store: str | None) -> dict[str, str]:
    try:
        if line.pack_size:
            # Remember the pack size the user confirmed ("1 pack of butter = 500 g") for next time
            try:
                pack_unit = normalize_unit(line.pack_unit or "g")
            except UnitError:
                pack_unit = "g"
            await set_pack_size(svc, await svc.grocy.object("products", line.product_id), line.pack_size, pack_unit)
        res = await add_stock(
            svc,
            StockAddIn(
                product_id=line.product_id,
                amount=line.quantity,
                unit=line.unit,
                price_total=line.line_total_eur,
                store=store,
                best_before=line.best_before,
                location=line.location,
            ),
        )
        message = res["message"]
        if line.opened:
            message += await _open_added(svc, line, res)
        return {"added": message}
    except Exception as e:  # noqa: BLE001 - report per line
        return {"error": str(e)}


async def _open_added(svc: Services, line: CommitLine, res: dict[str, Any]) -> str:
    """Mark what was just added as opened: one pack/piece of a counted line, the whole of a weighed one."""
    product = await svc.grocy.object("products", line.product_id)
    added, stock_unit = float(res["added"]), res["unit"]
    amount = added
    try:
        counted = normalize_unit(line.unit) in ("piece", "pack")
    except UnitError:
        counted = False
        amount = 1.0  # one of the product's own Grocy units ("Bottle")
    if counted:
        net, net_unit = net_of(await svc.grocy.userfields("products", line.product_id))
        with contextlib.suppress(UnitError):
            amount = convert(1, line.unit, stock_unit, net_amount_per_pack=net, net_unit=net_unit)
    amount = min(amount, added)
    await svc.grocy.open_stock(line.product_id, round(amount, 4))
    svc.db.add_event(
        "opened",
        f"Opened {fmt_amount(amount, stock_unit)} {product['name']} (stock-take)",
        product_id=line.product_id,
        product_name=product["name"],
    )
    return f", {fmt_amount(amount, stock_unit)} of it opened"


async def commit_lines(svc: Services, body: ReceiptCommitIn) -> dict[str, Any]:
    """Add the lines the user confirmed after a preview (no second Claude call)."""
    results = [{"product_id": line.product_id, **await _add_line(svc, line, body.store)} for line in body.lines]
    added = sum(1 for r in results if "added" in r)
    svc.db.add_event("receipt", f"Receipt from {body.store or 'unknown store'}: {added} line(s) added to stock")
    return {"added": added, "lines": results}
