"""Product capture: barcode, nutrition label photo, market produce, add stock, due-date OCR."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Awaitable
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .claude import compact_json, image_block
from .deps import Services
from .grocy import GrocyError, file_userfield_value
from .nutrition import (
    FoodCandidate,
    OffUnavailable,
    Per100,
    calories_per_stock_unit,
    fineli_search,
    off_lookup,
    usda_candidate,
    usda_search,
)
from .units import UnitError, convert, fmt_amount, normalize_unit

log = logging.getLogger(__name__)

Location = Literal["Fridge", "Freezer", "Pantry"]


class CaptureError(ValueError):
    pass


# --- models -------------------------------------------------------------------


class NewProduct(BaseModel):
    name: str
    description: str | None = None
    stock_unit: str = "g"
    purchase_unit: str | None = None  # default: "pack" when the pack size is known, else the stock unit
    location: str | None = "Pantry"
    best_before_days: int = 0
    after_open_days: int = 0
    net_amount_per_pack: float | None = None
    net_unit: str | None = None
    nutrition: Per100 | None = None
    nutrition_source: str | None = None
    barcode: str | None = None
    store: str | None = None
    food: bool = True


class StorageAdvice(BaseModel):
    is_food: bool = Field(description="False for non-food items such as detergent")
    storage_location: Location = Field(description="Where it is kept before opening")
    shelf_life_unopened_days: int = Field(ge=0, le=3650, description="Typical shelf life unopened, days")
    shelf_life_after_opening_days: int = Field(
        ge=0, le=3650, description="Use within N days after opening (0 if opening does not matter)"
    )
    location_after_opening: Location = Field(description="Where it is kept after opening")


class LabelExtraction(BaseModel):
    product_name: str = Field(description="Product name as printed, original language")
    name_en: str = Field(description="Short English product name, e.g. 'Spaghetti' or 'Greek yogurt 2%'")
    brand: str | None = None
    language: str | None = Field(None, description="Main label language code, e.g. lt, lv, et, pl, en")
    net_quantity: float | None = Field(None, description="Net quantity of the whole pack, number only")
    net_unit: Literal["g", "ml", "piece"] | None = Field(None, description="Unit of net_quantity")
    nutrition_basis: Literal["per_100", "per_portion_only", "none"] = Field(
        description="per_100 if a per 100 g/ml column exists; per_portion_only if only per portion; "
        "none if no nutrition table is visible"
    )
    portion_size: float | None = Field(None, description="Portion size in g or ml when per_portion_only")
    nutrition: Per100 = Field(description="Values exactly as printed for the chosen basis")
    is_food: bool = True
    storage_location: Location = "Pantry"
    shelf_life_unopened_days: int | None = Field(None, ge=0, le=3650)
    shelf_life_after_opening_days: int = Field(
        ge=0, le=3650, description="Printed 'after opening use within' value, else a typical value"
    )
    notes: str | None = None


class ProduceAdvice(BaseModel):
    english_name: str = Field(description="Generic English food term, e.g. 'carrots, raw'")
    usda_query: str = Field(description="Best search query for USDA FoodData Central (English)")
    fineli_query_fi: str = Field(description="Generic Finnish food name for the Fineli database, e.g. 'porkkana'")
    storage_location: Location
    shelf_life_days: int = Field(ge=0, le=3650, description="Typical shelf life where stored, days")
    shelf_life_after_opening_days: int = Field(ge=0, le=3650, description="Shelf life once cut/opened, days")
    typical_piece_weight_g: float | None = Field(None, description="Average weight of one piece in grams")


class FoodPick(BaseModel):
    index: int | None = Field(description="Index of the best matching food, or null if none fits")
    reason: str


class DueDate(BaseModel):
    date: str | None = Field(description="Printed date as YYYY-MM-DD, null if unreadable")
    kind: Literal["best_before", "use_by", "unknown"] = "unknown"
    raw_text: str | None = None
    confidence: Literal["high", "medium", "low"] = "low"

    @field_validator("date")
    @classmethod
    def _iso(cls, v: str | None) -> str | None:
        if v is None:
            return v
        date.fromisoformat(v)
        return v


# --- helpers ------------------------------------------------------------------


def summary(product: dict[str, Any], svc: Services, uf: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "id": int(product["id"]),
        "name": product["name"],
        "stock_unit": svc.catalog.unit_of(product.get("qu_id_stock")),
        "default_best_before_days": product.get("default_best_before_days"),
        "default_best_before_days_after_open": product.get("default_best_before_days_after_open"),
        "location": svc.catalog.location_name(product.get("location_id")),
    }
    if uf is not None:
        out["nutrition_per_100"] = Per100.from_userfields(uf).model_dump(exclude_none=True)
        out["nutrition_source"] = uf.get("nutrition_source") or None
        out["net_amount_per_pack"] = uf.get("net_amount_per_pack") or None
        out["net_unit"] = uf.get("net_unit") or None
    return out


async def find_product_by_name(svc: Services, name: str) -> dict[str, Any] | None:
    target = name.strip().lower()
    for p in await svc.products():
        if p["name"].strip().lower() == target:
            return p
    return None


async def unique_name(svc: Services, name: str) -> str:
    names = {p["name"].lower() for p in await svc.products()}
    if name.lower() not in names:
        return name
    i = 2
    while f"{name} ({i})".lower() in names:
        i += 1
    return f"{name} ({i})"


def net_of(uf: dict[str, Any]) -> tuple[float | None, str | None]:
    try:
        net = float(uf.get("net_amount_per_pack") or 0) or None
    except (TypeError, ValueError):
        net = None
    return net, (uf.get("net_unit") or None)


async def link_barcode(svc: Services, product_id: int, barcode: str, qu_id: int) -> str | None:
    """Attach ``barcode`` to the product unless Grocy already has it. Returns a warning, if any."""
    found = await svc.grocy.product_by_barcode(barcode)
    if found:
        owner = found["product"]
        if int(owner["id"]) == product_id:
            return None
        return f"Barcode {barcode} already belongs to '{owner['name']}' in Grocy; not added again."
    await svc.grocy.add_object(
        "product_barcodes", {"product_id": product_id, "barcode": barcode, "qu_id": qu_id, "amount": 1}
    )
    return None


async def create_product(svc: Services, spec: NewProduct) -> dict[str, Any]:
    cat = svc.catalog
    stock_unit = normalize_unit(spec.stock_unit)
    stock_qu = cat.qu_by_unit[stock_unit]
    purchase_unit = spec.purchase_unit or (
        "pack" if spec.net_amount_per_pack and stock_unit in ("g", "ml") else stock_unit
    )
    purchase_qu = cat.qu_by_unit[normalize_unit(purchase_unit)]
    calories = None
    if spec.nutrition:
        calories = calories_per_stock_unit(spec.nutrition, stock_unit, spec.net_amount_per_pack, spec.net_unit)
    name = await unique_name(svc, spec.name.strip()[:120])
    data: dict[str, Any] = {
        "name": name,
        "description": spec.description or "",
        "location_id": cat.location_id(spec.location),
        "qu_id_stock": stock_qu,
        "qu_id_purchase": purchase_qu,
        "qu_id_consume": stock_qu,
        "qu_id_price": purchase_qu,
        "default_best_before_days": spec.best_before_days,
        "default_best_before_days_after_open": spec.after_open_days,
        "treat_opened_as_out_of_stock": 0 if spec.food else 1,
    }
    if calories is not None:
        data["calories"] = calories
    if (store_id := cat.store_id(spec.store)) is not None:
        data["shopping_location_id"] = store_id
    pid = await svc.grocy.add_object("products", data)

    if purchase_qu != stock_qu and spec.net_amount_per_pack and spec.net_unit:
        factor = convert(spec.net_amount_per_pack, spec.net_unit, stock_unit)
        await set_pack_conversion(svc, pid, purchase_qu, stock_qu, factor)

    uf: dict[str, Any] = {}
    if spec.nutrition:
        uf.update(spec.nutrition.to_userfields())
    if spec.nutrition_source:
        uf["nutrition_source"] = spec.nutrition_source
    if spec.net_amount_per_pack:
        uf["net_amount_per_pack"] = spec.net_amount_per_pack
        uf["net_unit"] = spec.net_unit or ""
    if uf:
        await svc.grocy.set_userfields("products", pid, uf)
    if spec.barcode and (warning := await link_barcode(svc, pid, spec.barcode, purchase_qu)):
        log.warning(warning)
    svc.db.add_event(
        "created",
        f"Created product '{name}'",
        product_id=pid,
        product_name=name,
        data={"source": spec.nutrition_source, "barcode": spec.barcode},
    )
    return await svc.grocy.object("products", pid)


async def set_pack_conversion(svc: Services, product_id: int, from_qu: int, to_qu: int, factor: float) -> None:
    """1 pack = factor stock units. Grocy creates a 1:1 conversion by itself when a product's purchase and
    stock units differ, so update that one instead of adding a second (which Grocy refuses)."""
    existing = await svc.grocy.objects(
        "quantity_unit_conversions", [f"product_id={product_id}", f"from_qu_id={from_qu}", f"to_qu_id={to_qu}"]
    )
    if existing:
        await svc.grocy.edit_object("quantity_unit_conversions", int(existing[0]["id"]), {"factor": factor})
    else:
        await svc.grocy.add_object(
            "quantity_unit_conversions",
            {"from_qu_id": from_qu, "to_qu_id": to_qu, "factor": factor, "product_id": product_id},
        )


async def set_pack_size(svc: Services, product: dict[str, Any], amount: float, unit: str) -> None:
    """Remember the net amount of one pack/piece and keep Grocy's pack conversion in line with it."""
    pid = int(product["id"])
    await svc.grocy.set_userfields("products", pid, {"net_amount_per_pack": amount, "net_unit": unit})
    purchase_qu, stock_qu = int(product["qu_id_purchase"]), int(product["qu_id_stock"])
    stock_unit = svc.catalog.unit_of(stock_qu)
    if purchase_qu != stock_qu:
        # Stock kept in pieces: no conversion; the pack size is then only used for nutrition
        with contextlib.suppress(UnitError):
            await set_pack_conversion(svc, pid, purchase_qu, stock_qu, convert(amount, unit, stock_unit))


async def update_nutrition(
    svc: Services,
    product: dict[str, Any],
    per100: Per100,
    source: str,
    net_amount: float | None = None,
    net_unit: str | None = None,
) -> None:
    pid = int(product["id"])
    uf: dict[str, Any] = {**per100.to_userfields(), "nutrition_source": source}
    if net_amount:
        uf["net_amount_per_pack"] = net_amount
        uf["net_unit"] = net_unit or ""
    await svc.grocy.set_userfields("products", pid, uf)
    stock_unit = svc.catalog.unit_of(product.get("qu_id_stock"))
    cal = calories_per_stock_unit(per100, stock_unit, net_amount, net_unit)
    if cal is not None:
        await svc.grocy.edit_object("products", pid, {"calories": cal})


async def storage_advice(svc: Services, name: str, context: str | None = None) -> StorageAdvice | None:
    try:
        return await svc.claude.structured(
            task="storage_advice",
            schema=StorageAdvice,
            cheap=True,
            effort="low",
            max_tokens=1500,
            system="You are a food safety assistant for a household in Lithuania. Give typical, conservative "
            "home storage values.",
            content=[{"type": "text", "text": f"Product: {name}\nContext: {context or '-'}"}],
        )
    except Exception as e:  # noqa: BLE001 - advice is optional
        log.warning("storage advice failed for %s: %s", name, e)
        return None


class ShelfLifeAdvice(StorageAdvice):
    question: str | None = Field(
        None,
        description="Only when the answer changes the shelf life a lot and can't be told from the name "
        "(fresh or frozen? raw or cooked? opened already?): one short question for the household",
    )
    options: list[str] = Field(default_factory=list, description="2-4 short answers to the question")


class ShelfLifeIn(BaseModel):
    answer: str | None = Field(None, description="The household's answer to Claude's question")


SHELF_LIFE_SYSTEM = (
    "You are a food safety assistant for a household in Kaunas, Lithuania. From the product name (often "
    "Lithuanian) give typical, conservative home storage values. If one fact you can't infer changes the "
    "answer a lot, ask it as `question` with short `options` and still give your best values for the most "
    "likely case; otherwise leave question empty. Non-food items: is_food=false, shelf life 0 if irrelevant."
)


def needs_shelf_life(product: dict[str, Any]) -> bool:
    """Products made by hand in Grocy often have no default due days (0 = Grocy dates them today)."""
    return int(product.get("default_best_before_days") or 0) == 0


async def shelf_life(svc: Services, product_id: int, body: ShelfLifeIn) -> dict[str, Any]:
    """Claude suggests shelf life and storage for a product; asks one question first when needed."""
    product = await svc.grocy.object("products", product_id)
    text = f"Product: {product['name']}"
    if product.get("description"):
        text += f"\nDescription: {product['description']}"
    if body.answer:
        text += f"\nThe household answered your question: {body.answer}. Do not ask again."
    advice = await svc.claude.structured(
        task="shelf_life",
        schema=ShelfLifeAdvice,
        cheap=True,
        effort="low",
        max_tokens=1500,
        system=SHELF_LIFE_SYSTEM,
        content=[{"type": "text", "text": text}],
    )
    if advice.question and not body.answer:
        return {"status": "question", "question": advice.question, "options": advice.options[:4]}
    changes: dict[str, Any] = {
        # -1 = never overdue in Grocy (non-food, or no meaningful date)
        "default_best_before_days": advice.shelf_life_unopened_days or -1,
        "default_best_before_days_after_open": advice.shelf_life_after_opening_days,
        "location_id": svc.catalog.location_id(advice.storage_location),
    }
    await svc.grocy.edit_object("products", product_id, changes)
    svc.db.add_event(
        "shelf_life",
        f"{product['name']}: keeps {advice.shelf_life_unopened_days} d in {advice.storage_location}, "
        f"{advice.shelf_life_after_opening_days} d after opening",
        product_id=product_id,
        product_name=product["name"],
        data={**advice.model_dump(), "answer": body.answer},
    )
    product = await svc.grocy.object("products", product_id)
    return {"status": "saved", "product": summary(product, svc, await svc.grocy.userfields("products", product_id))}


async def _upload_picture(svc: Services, product_id: int, data: bytes, ext: str = "jpg") -> None:
    fname = f"gp{product_id}_{secrets.token_hex(4)}.{ext}"
    await svc.grocy.upload_file("productpictures", fname, data)
    await svc.grocy.edit_object("products", product_id, {"picture_file_name": fname})


# --- barcode ------------------------------------------------------------------


async def capture_barcode(svc: Services, code: str) -> dict[str, Any]:
    code = code.strip()
    found = await svc.grocy.product_by_barcode(code)
    if found:
        product = found["product"]
        uf = await svc.grocy.userfields("products", int(product["id"]))
        complete = Per100.from_userfields(uf).is_complete()
        return {
            "status": "existing",
            "product": summary(product, svc, uf),
            "stock_amount": found.get("stock_amount"),
            "needs_label_photo": not complete,
            "message": "Known product." + ("" if complete else " Photo of the nutrition table needed."),
        }

    try:
        off = await off_lookup(svc.http, code, svc.settings.off_user_agent)
    except OffUnavailable as e:
        return {
            "status": "lookup_failed",
            "barcode": code,
            "needs_label_photo": True,
            "message": f"Open Food Facts is unreachable right now ({e}). Try again later, or take a photo of "
            f"the nutrition table: POST /products/new/label-photo with barcode={code}.",
        }
    if off is None:
        return {
            "status": "not_found",
            "barcode": code,
            "needs_label_photo": True,
            "message": "Unknown barcode. Photo of the nutrition table (and front/net weight) needed: "
            f"POST /products/new/label-photo with barcode={code}.",
        }

    name = f"{off.name} ({off.brand})" if off.brand and off.brand.lower() not in off.name.lower() else off.name
    advice = await storage_advice(svc, name, "packaged product from Open Food Facts")
    stock_unit = off.net_unit if off.net_unit in ("g", "ml") else "piece"
    product = await create_product(
        svc,
        NewProduct(
            name=name,
            description=f"Barcode {code}"
            + (f", {fmt_amount(off.net_amount, off.net_unit)}" if off.net_amount and off.net_unit else ""),
            stock_unit=stock_unit,
            location=advice.storage_location if advice else "Pantry",
            best_before_days=advice.shelf_life_unopened_days if advice else 0,
            after_open_days=advice.shelf_life_after_opening_days if advice else 0,
            net_amount_per_pack=off.net_amount,
            net_unit=off.net_unit,
            nutrition=off.nutrition,
            nutrition_source="open_food_facts" if off.nutrition.kcal is not None else None,
            barcode=code,
            food=advice.is_food if advice else True,
        ),
    )
    if off.image_url:
        try:
            img = await svc.http.get(off.image_url, timeout=20.0, headers={"User-Agent": svc.settings.off_user_agent})
            if img.status_code == 200 and img.content:
                await _upload_picture(svc, int(product["id"]), img.content)
        except Exception as e:  # noqa: BLE001 - picture is optional
            log.info("could not fetch OFF image: %s", e)
    uf = await svc.grocy.userfields("products", int(product["id"]))
    complete = off.nutrition.is_complete()
    return {
        "status": "created",
        "product": summary(product, svc, uf),
        "needs_label_photo": not complete,
        "message": f"Created '{product['name']}' from Open Food Facts."
        + ("" if complete else " Photo of the nutrition table needed."),
    }


# --- label photo ----------------------------------------------------------------

LABEL_SYSTEM = """You read food packaging photos for a household in Lithuania.
Labels can be in Lithuanian, Latvian, Estonian, Polish, English or other languages.
Lithuanian hints: "Maistinė vertė 100 g" = nutrition per 100 g, "energinė vertė" = energy,
"riebalai" = fat, "iš kurių sočiosios riebalų rūgštys" = of which saturates, "angliavandeniai" = carbs,
"iš kurių cukrūs" = sugars, "skaidulinės medžiagos" = fibre, "baltymai" = protein, "druska" = salt,
"grynasis kiekis" = net quantity, "atidarius suvartoti per X d." = use within X days after opening.
Latvian: "uzturvērtība", "tauki", "ogļhidrāti", "olbaltumvielas", "sāls". Estonian: "toitumisalane teave",
"rasvad", "süsivesikud", "valgud", "sool". Polish: "wartość odżywcza", "tłuszcz", "węglowodany", "białko", "sól".
Energy: if only kJ is printed, convert to kcal (kJ / 4.184). Copy numbers exactly; use a dot as decimal
separator. If a per 100 g/ml column exists, use it (basis per_100). If only per portion values exist,
use basis per_portion_only and give the portion size in g/ml. Do not invent nutrition values."""


async def label_photo(
    svc: Services,
    product_id: int | None,
    images: list[tuple[bytes, str | None]],
    barcode: str | None = None,
    net_amount: float | None = None,
    net_unit: str | None = None,
) -> dict[str, Any]:
    """Read a nutrition label photo. The pack size comes from the photo when printed on it; otherwise
    from ``net_amount``/``net_unit`` typed by the user (they win when both are given)."""
    if not 1 <= len(images) <= 2:
        raise CaptureError("Send 1 or 2 images: the nutrition table and optionally the front/net weight")
    typed_amount, typed_unit = net_amount, (net_unit if net_unit in ("g", "ml") else None)
    product: dict[str, Any] | None = None
    if product_id:
        product = await svc.grocy.object("products", product_id)

    content: list[dict[str, Any]] = [image_block(data, mt) for data, mt in images]
    content.append(
        {
            "type": "text",
            "text": "Extract the product data from these label photos."
            + (f" Known product name: {product['name']}." if product else ""),
        }
    )
    ext = await svc.claude.structured(
        task="label_photo",
        schema=LabelExtraction,
        content=content,
        system=LABEL_SYSTEM,
        effort="medium",
        max_tokens=4000,
    )

    per100 = ext.nutrition
    warnings: list[str] = []
    if ext.nutrition_basis == "per_portion_only":
        if ext.portion_size:
            per100 = ext.nutrition.scaled(100.0 / ext.portion_size)
            warnings.append(f"Only per-portion values printed; scaled from {ext.portion_size:g} g/ml portion.")
        else:
            warnings.append("Only per-portion values printed and portion size unknown; nutrition not stored.")
            per100 = Per100()
    elif ext.nutrition_basis == "none":
        warnings.append("No nutrition table found in the photo.")

    net_unit = ext.net_unit if ext.net_unit in ("g", "ml") else None
    net_amount = ext.net_quantity if net_unit else None
    if typed_amount and typed_unit:
        net_amount, net_unit = typed_amount, typed_unit

    if product is None:
        name = ext.name_en if not ext.brand else f"{ext.name_en} ({ext.brand})"
        product = await create_product(
            svc,
            NewProduct(
                name=name,
                description=ext.product_name + (f" — barcode {barcode}" if barcode else ""),
                stock_unit=net_unit or "piece",
                location=ext.storage_location,
                best_before_days=ext.shelf_life_unopened_days or 0,
                after_open_days=ext.shelf_life_after_opening_days,
                net_amount_per_pack=net_amount,
                net_unit=net_unit,
                nutrition=per100 if per100.kcal is not None else None,
                nutrition_source="label_photo" if per100.kcal is not None else None,
                barcode=barcode,
                food=ext.is_food,
            ),
        )
        status = "created"
    else:
        status = "updated"
        if per100.kcal is not None:
            await update_nutrition(svc, product, per100, "label_photo", net_amount, net_unit)
        if net_amount and net_unit:
            await set_pack_size(svc, product, net_amount, net_unit)
        changes: dict[str, Any] = {}
        if not int(product.get("default_best_before_days_after_open") or 0):
            changes["default_best_before_days_after_open"] = ext.shelf_life_after_opening_days
        if not int(product.get("default_best_before_days") or 0) and ext.shelf_life_unopened_days:
            changes["default_best_before_days"] = ext.shelf_life_unopened_days
        if changes:
            await svc.grocy.edit_object("products", int(product["id"]), changes)
        if barcode and (
            warning := await link_barcode(svc, int(product["id"]), barcode, int(product["qu_id_purchase"]))
        ):
            warnings.append(warning)

    pid = int(product["id"])
    # Store the nutrition table photo in the label_photo userfield, the front photo as product picture
    try:
        data, _mt = images[0]
        stored = f"label_{pid}_{secrets.token_hex(4)}.jpg"
        await svc.grocy.upload_file("userfiles", stored, data)
        await svc.grocy.set_userfields("products", pid, {"label_photo": file_userfield_value(stored, stored)})
        if len(images) == 2 and not product.get("picture_file_name"):
            await _upload_picture(svc, pid, images[1][0])
    except GrocyError as e:
        warnings.append(f"Photo not saved in Grocy: {e.message}")

    product = await svc.grocy.object("products", pid)
    uf = await svc.grocy.userfields("products", pid)
    svc.db.add_event(
        "label",
        f"Label photo read for '{product['name']}'",
        product_id=pid,
        product_name=product["name"],
        # Kept for the future Lithuanian food database (phase 6): label text, barcode, per-100 values
        data={**ext.model_dump(), "barcode": barcode, "nutrition_per_100": per100.model_dump()},
    )
    return {
        "status": status,
        "product": summary(product, svc, uf),
        "extracted": ext.model_dump(),
        "suggested": {
            "storage_location": ext.storage_location,
            "shelf_life_after_opening_days": ext.shelf_life_after_opening_days,
            "shelf_life_unopened_days": ext.shelf_life_unopened_days,
        },
        "pack_size": {"amount": net_amount, "unit": net_unit} if net_amount and net_unit else None,
        "warnings": warnings,
    }


# --- market produce ---------------------------------------------------------------


class ProduceIn(BaseModel):
    name: str
    weight_g: float | None = Field(None, gt=0)
    count: float | None = Field(None, gt=0)
    price_eur: float | None = Field(None, ge=0)
    store: str | None = "Turgus"
    best_before: date | None = None


async def _produce_advice(svc: Services, name: str) -> ProduceAdvice:
    return await svc.claude.structured(
        task="produce_translate",
        schema=ProduceAdvice,
        cheap=True,
        effort="low",
        max_tokens=1500,
        system="You help a household in Kaunas, Lithuania record fresh market produce (no barcode). "
        "The name may be Lithuanian, English or Portuguese. Give the generic English food term as "
        "USDA FoodData Central would name it (raw unless stated), the generic Finnish name for the Finnish "
        "food composition database Fineli, and typical home storage values.",
        content=[{"type": "text", "text": f"Produce: {name}"}],
    )


async def food_candidates(svc: Services, english: str, finnish: str | None = None) -> list[FoodCandidate]:
    """Generic-food candidates from the configured sources (GENERIC_FOOD_SOURCES), searched in parallel."""
    jobs: list[Awaitable[list[Any]]] = []
    for source in svc.settings.generic_food_sources:
        if source == "fineli":
            jobs.append(
                fineli_search(svc.http, [q for q in (finnish, english) if q], user_agent=svc.settings.off_user_agent)
            )
        elif source == "usda":
            jobs.append(usda_search(svc.http, english, svc.settings.usda_fdc_api_key))
    results = await asyncio.gather(*jobs)
    out: list[FoodCandidate] = []
    for res in results:
        out.extend(r if isinstance(r, FoodCandidate) else usda_candidate(r) for r in res)
    return out


async def generic_nutrition(svc: Services, advice: ProduceAdvice) -> FoodCandidate | None:
    """Best per-100 g match for a generic food across Fineli and USDA (Claude, cheap model, picks)."""
    foods = await food_candidates(svc, advice.usda_query, advice.fineli_query_fi)
    if not foods:
        return None
    candidates = [
        {
            "index": i,
            "source": f.source,
            "description": f.description,
            "detail": f.detail,
            "kcal_100g": round(f.nutrition.kcal or 0),
        }
        for i, f in enumerate(foods)
    ]
    pick = await svc.claude.structured(
        task="food_pick",
        schema=FoodPick,
        cheap=True,
        effort="low",
        max_tokens=1000,
        system="Pick the database food that best matches the produce as bought (raw, whole, generic). "
        "Prefer Fineli (Nordic data, closest to Lithuanian produce) over USDA when both match equally well.",
        content=[{"type": "text", "text": f"Produce: {advice.english_name}\nCandidates: {compact_json(candidates)}"}],
    )
    if pick.index is None or not 0 <= pick.index < len(foods):
        return None
    return foods[pick.index]


async def add_produce(svc: Services, body: ProduceIn) -> dict[str, Any]:
    if not body.weight_g and not body.count:
        raise CaptureError("Give weight_g or count")
    product = await find_product_by_name(svc, body.name)
    created = False
    advice: ProduceAdvice | None = None
    if product is None:
        advice = await _produce_advice(svc, body.name)
        match = await generic_nutrition(svc, advice)
        product = await create_product(
            svc,
            NewProduct(
                name=body.name.strip().capitalize(),
                description=f"{advice.english_name}" + (f" — {match.label}" if match else ""),
                stock_unit="g",
                purchase_unit="g",
                location=advice.storage_location,
                best_before_days=advice.shelf_life_days,
                after_open_days=advice.shelf_life_after_opening_days,
                net_amount_per_pack=advice.typical_piece_weight_g,
                net_unit="g" if advice.typical_piece_weight_g else None,
                nutrition=match.nutrition if match else None,
                nutrition_source=match.source if match else None,
                store=body.store,
            ),
        )
        created = True

    pid = int(product["id"])
    uf = await svc.grocy.userfields("products", pid)
    grams = body.weight_g
    if not grams and body.count:
        net, net_unit = net_of(uf)
        if not net:
            advice = advice or await _produce_advice(svc, body.name)
            net = advice.typical_piece_weight_g
            if net:
                await svc.grocy.set_userfields("products", pid, {"net_amount_per_pack": net, "net_unit": "g"})
        if not net:
            raise CaptureError("Unknown piece weight; please give weight_g")
        grams = body.count * net
    assert grams is not None
    stock_unit = svc.catalog.unit_of(product.get("qu_id_stock"))
    amount = convert(grams, "g", stock_unit) if stock_unit in ("g", "kg") else grams
    result = await _book_purchase(svc, product, amount, body.price_eur, body.store, body.best_before, None)
    return {"status": "created" if created else "existing", "product": summary(product, svc, uf), **result}


# --- add stock with price and store ---------------------------------------------


class StockAddIn(BaseModel):
    product_id: int | None = None
    barcode: str | None = None
    amount: float = Field(gt=0)
    unit: str | None = Field(None, description="g, kg, ml, l, piece, pack; default = stock unit")
    price_total: float | None = Field(None, ge=0, description="Total price paid in EUR")
    store: str | None = None
    best_before: date | None = None
    location: str | None = None


async def _book_purchase(
    svc: Services,
    product: dict[str, Any],
    amount_stock: float,
    price_total: float | None,
    store: str | None,
    best_before: date | None,
    location: str | None,
) -> dict[str, Any]:
    pid = int(product["id"])
    stock_unit = svc.catalog.unit_of(product.get("qu_id_stock"))
    store_id = svc.catalog.store_id(store)
    unit_price = round(price_total / amount_stock, 6) if price_total is not None and amount_stock else None
    res = await svc.grocy.add_stock(
        pid,
        round(amount_stock, 4),
        price=unit_price,
        shopping_location_id=store_id,
        best_before_date=best_before.isoformat() if best_before else None,
        location_id=svc.catalog.location_id(location) if location else None,
    )
    tx = res[0]["transaction_id"] if isinstance(res, list) and res else None
    msg = (
        f"Added {fmt_amount(amount_stock, stock_unit)} {product['name']}"
        + (f" for €{price_total:.2f}" if price_total is not None else "")
        + (f" at {store}" if store else "")
    )
    svc.db.add_event(
        "added",
        msg,
        product_id=pid,
        product_name=product["name"],
        amount=amount_stock,
        unit=stock_unit,
        data={"price_total": price_total, "store": store, "transaction_id": tx},
    )
    out: dict[str, Any] = {"added": amount_stock, "unit": stock_unit, "transaction_id": tx, "message": msg}
    if store and store_id is None:
        out["warning"] = f"Unknown store '{store}', recorded without shopping location"
    return out


async def add_stock(svc: Services, body: StockAddIn) -> dict[str, Any]:
    if body.product_id:
        product = await svc.grocy.object("products", body.product_id)
    elif body.barcode:
        found = await svc.grocy.product_by_barcode(body.barcode)
        if not found:
            raise CaptureError("Unknown barcode; scan it first via /products/barcode/{code}")
        product = found["product"]
    else:
        raise CaptureError("Give product_id or barcode")
    stock_unit = svc.catalog.unit_of(product.get("qu_id_stock"))
    uf = await svc.grocy.userfields("products", int(product["id"]))
    net, net_unit = net_of(uf)
    unit = body.unit or stock_unit
    try:
        amount = convert(body.amount, unit, stock_unit, net_amount_per_pack=net, net_unit=net_unit)
    except UnitError as e:
        raise CaptureError(str(e)) from e
    return await _book_purchase(svc, product, amount, body.price_total, body.store, body.best_before, body.location)


# --- due date OCR -------------------------------------------------------------------


async def read_due_date(svc: Services, image: bytes, media_type: str | None) -> DueDate:
    today = date.today().isoformat()
    return await svc.claude.structured(
        task="due_date",
        schema=DueDate,
        effort="low",
        max_tokens=1500,
        system="Read the printed best-before / use-by date on food packaging. Lithuanian: 'Geriausias iki' = "
        "best before, 'Tinka vartoti iki' = use by. Dates are usually DD.MM.YYYY or DD.MM.YY in Europe. "
        f"Today is {today}; if the year is missing assume the next occurrence.",
        content=[image_block(image, media_type), {"type": "text", "text": "What is the date?"}],
    )
