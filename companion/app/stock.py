"""Cooking and partial use: consume a recipe, then open partly used packs so due dates recalculate."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from .deps import Services
from .grocy import GrocyError
from .nutrition import Per100, Totals, nutrients_for
from .products import CaptureError, net_of
from .units import UnitError, convert, convert_food, density, dimension, fmt_amount, normalize_unit

log = logging.getLogger(__name__)

EPS = 1e-6
NEVER = "2999-12-31"


@dataclass
class Usage:
    product_id: int
    entry_id: int
    amount: float  # consumed, stock units
    price: float | None  # per stock unit


@dataclass
class OpenAction:
    product_id: int
    entry_id: int
    stock_id: str
    remaining: float


@dataclass
class CookPlan:
    usages: list[Usage] = field(default_factory=list)
    opens: list[OpenAction] = field(default_factory=list)
    already_open: list[OpenAction] = field(default_factory=list)


def plan_opens(before: dict[int, list[dict[str, Any]]], after: dict[int, list[dict[str, Any]]]) -> CookPlan:
    """Compare stock entries before/after consuming a recipe.

    Every entry that shrank is a usage. If it still has stock left and was not open,
    it must be opened (Grocy's recipe consume doesn't do that).
    """
    plan = CookPlan()
    for pid, entries in before.items():
        post_by_id = {int(e["id"]): e for e in after.get(pid, [])}
        for e in entries:
            eid = int(e["id"])
            pre_amount = float(e["amount"])
            post = post_by_id.get(eid)
            post_amount = float(post["amount"]) if post else 0.0
            consumed = pre_amount - post_amount
            if consumed <= EPS:
                continue
            price = float(e["price"]) if e.get("price") not in (None, "") else None
            plan.usages.append(Usage(pid, eid, consumed, price))
            if post is not None and post_amount > EPS:
                action = OpenAction(pid, eid, str(post["stock_id"]), post_amount)
                if int(e.get("open") or 0) == 0 and int(post.get("open") or 0) == 0:
                    plan.opens.append(action)
                else:
                    plan.already_open.append(action)
    return plan


def _due_text(d: str | None) -> str:
    return "no due date" if not d or d.startswith("2999") else f"use by {d}"


class CookIn(BaseModel):
    servings: int | None = Field(None, gt=0)


async def _entries(svc: Services, product_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
    return {pid: await svc.grocy.stock_entries(pid) for pid in product_ids}


async def cook(svc: Services, recipe_id: int, servings: int | None = None) -> dict[str, Any]:
    recipe = await svc.grocy.object("recipes", recipe_id)
    if servings:
        await svc.grocy.edit_object("recipes", recipe_id, {"desired_servings": servings})
        recipe["desired_servings"] = servings
    servings_cooked = int(recipe.get("desired_servings") or recipe.get("base_servings") or 1)

    positions = await svc.grocy.objects("recipes_pos_resolved", [f"recipe_id={recipe_id}"])
    product_ids = {int(p["product_id"]) for p in positions}
    if not product_ids:
        raise CaptureError("Recipe has no ingredients")
    products = {int(p["id"]): p for p in await svc.products()}

    before = await _entries(svc, product_ids)
    await svc.grocy.consume_recipe(recipe_id)
    after = await _entries(svc, product_ids)
    plan = plan_opens(before, after)

    today = date.today().isoformat()
    notices: list[str] = []
    opened: list[dict[str, Any]] = []
    for act in plan.opens:
        prod = products[act.product_id]
        unit = svc.catalog.unit_of(prod.get("qu_id_stock"))
        left = fmt_amount(act.remaining, unit)
        if int(prod.get("enable_tare_weight_handling") or 0) or int(prod.get("disable_open") or 0):
            msg = f"{prod['name']}: partly used for '{recipe['name']}', {left} left (cannot be marked opened)"
            notices.append(msg)
            svc.db.add_event(
                "used",
                msg,
                product_id=act.product_id,
                product_name=prod["name"],
                recipe_id=recipe_id,
                amount=act.remaining,
                unit=unit,
            )
            continue
        try:
            await svc.grocy.open_stock(act.product_id, act.remaining, act.stock_id)
        except GrocyError as e:
            msg = f"{prod['name']}: {left} left, could not mark opened ({e.message})"
            notices.append(msg)
            continue
        entry = next((e for e in await svc.grocy.stock_entries(act.product_id) if int(e["id"]) == act.entry_id), None)
        due = entry.get("best_before_date") if entry else None
        msg = f"{prod['name']}: opened {today} for '{recipe['name']}', {left} left, {_due_text(due)}"
        notices.append(msg)
        opened.append(
            {
                "product_id": act.product_id,
                "name": prod["name"],
                "remaining": act.remaining,
                "unit": unit,
                "best_before_date": due,
            }
        )
        svc.db.add_event(
            "opened",
            msg,
            product_id=act.product_id,
            product_name=prod["name"],
            recipe_id=recipe_id,
            amount=act.remaining,
            unit=unit,
            data={"best_before_date": due, "stock_id": act.stock_id},
        )
    for act in plan.already_open:
        prod = products[act.product_id]
        unit = svc.catalog.unit_of(prod.get("qu_id_stock"))
        entry = next((e for e in after.get(act.product_id, []) if int(e["id"]) == act.entry_id), None)
        due = entry.get("best_before_date") if entry else None
        notices.append(f"{prod['name']}: already open, {fmt_amount(act.remaining, unit)} left, {_due_text(due)}")

    # Cost and nutrition of what was actually consumed
    consumed: dict[int, float] = {}
    cost = 0.0
    cost_known = True
    for u in plan.usages:
        consumed[u.product_id] = consumed.get(u.product_id, 0.0) + u.amount
        if u.price is None:
            cost_known = False
        else:
            cost += u.amount * u.price
    totals = Totals()
    consumed_list = []
    for pid, amount in consumed.items():
        prod = products[pid]
        unit = svc.catalog.unit_of(prod.get("qu_id_stock"))
        uf = await svc.grocy.userfields("products", pid)
        net, net_unit = net_of(uf)
        n = nutrients_for(prod["name"], amount, unit, Per100.from_userfields(uf), net, net_unit)
        totals = totals.add(n)
        consumed_list.append(
            {"product_id": pid, "name": prod["name"], "amount": round(amount, 3), "unit": unit, "kcal": round(n.kcal)}
        )
        svc.db.add_event(
            "used",
            f"Used {fmt_amount(amount, unit)} {prod['name']} for '{recipe['name']}'",
            product_id=pid,
            product_name=prod["name"],
            recipe_id=recipe_id,
            amount=amount,
            unit=unit,
        )

    result = {
        "recipe": {"id": recipe_id, "name": recipe["name"]},
        "servings": servings_cooked,
        "notices": notices,
        "opened": opened,
        "consumed": consumed_list,
        "cost_total_eur": round(cost, 2),
        "cost_per_serving_eur": round(cost / servings_cooked, 2),
        "cost_complete": cost_known,
        "nutrition_total": totals.rounded(),
        "nutrition_per_serving": totals.divided(servings_cooked).rounded(),
    }
    svc.db.add_event(
        "cooked",
        f"Cooked '{recipe['name']}' ({servings_cooked} servings)",
        recipe_id=recipe_id,
        data={k: result[k] for k in ("cost_total_eur", "nutrition_per_serving", "notices")},
    )
    return result


# --- weighing ---------------------------------------------------------------------


class WeighIn(BaseModel):
    grams_measured: float = Field(ge=0)
    includes_container: bool | None = Field(
        None, description="Weight includes the jar/container. Default: true if tare handling is enabled"
    )
    container_g: float | None = Field(
        None, ge=0, description="Container weight when tare handling is off (remembered for the product)"
    )


async def weigh(svc: Services, product_id: int, body: WeighIn) -> dict[str, Any]:
    details = await svc.grocy.product_details(product_id)
    prod = details["product"]
    unit = svc.catalog.unit_of(prod.get("qu_id_stock"))
    notes: list[str] = []
    try:
        dim_unit = normalize_unit(unit)
    except UnitError as e:
        raise CaptureError(f"Stock unit '{unit}' cannot be weighed") from e
    if dim_unit in ("piece", "pack"):
        raise CaptureError(f"{prod['name']} is counted in {unit}s; weighing needs a g or ml stock unit")
    d = density(prod["name"])
    if dim_unit in ("ml", "l", "cl", "dl"):
        notes.append(f"Weight to volume: 1 g ≈ {round(1 / d, 2)} ml for {prod['name']}")
    container_key = f"container_g:{product_id}"

    tare_enabled = bool(int(prod.get("enable_tare_weight_handling") or 0))
    tare = float(prod.get("tare_weight") or 0)
    includes = body.includes_container if body.includes_container is not None else tare_enabled
    before = float(details.get("stock_amount") or 0)

    if tare_enabled:
        gross_g = body.grams_measured if includes else body.grams_measured + tare
        net_g = gross_g - tare
        new_for_grocy = convert_food(gross_g, "g", dim_unit, name=prod["name"])  # Grocy expects gross
    else:
        if includes:
            if body.container_g is not None:
                svc.db.set_kv(container_key, str(body.container_g))  # weighed once, remembered
            else:
                saved = svc.db.get_kv(container_key)
                body.container_g = float(saved) if saved else None
            if body.container_g is None:
                raise CaptureError(
                    "Weight includes the container: give container_g or enable tare weight handling on the product"
                )
            net_g = body.grams_measured - body.container_g
        else:
            net_g = body.grams_measured
        new_for_grocy = None
    if net_g < -EPS:
        raise CaptureError("Measured weight is below the container weight")
    net_g = max(net_g, 0.0)
    new_amount = _tidy(convert_food(net_g, "g", dim_unit, name=prod["name"]), dim_unit)
    if new_for_grocy is None:
        new_for_grocy = new_amount

    # The open bottle was weighed: unopened packs on the shelf are still there, and what was used
    # comes out of the open one
    entries = await svc.grocy.stock_entries(product_id)
    opened = [e for e in entries if int(e.get("open") or 0)]
    sealed = sum(float(e["amount"]) for e in entries if not int(e.get("open") or 0))
    weighed = new_amount
    if opened and sealed:
        new_amount += sealed
        new_for_grocy += sealed
        notes.append(f"The open one has {fmt_amount(weighed, unit)}, plus {fmt_amount(sealed, unit)} unopened")

    diff = new_amount - before
    if abs(diff) < 0.01:
        return {
            "product": {"id": product_id, "name": prod["name"]},
            "changed": False,
            "amount": before,
            "unit": unit,
            "message": f"{prod['name']}: unchanged at {fmt_amount(before, unit)}",
            "notes": notes,
        }
    open_entry = min(opened, key=lambda e: e.get("best_before_date") or "9999") if opened else None
    if open_entry and not tare_enabled and diff < 0 and -diff <= float(open_entry["amount"]) + EPS:
        res = await svc.grocy.consume_stock(product_id, round(-diff, 4), stock_entry_id=open_entry["stock_id"])
    else:
        res = await svc.grocy.inventory(product_id, round(new_for_grocy, 3))
    tx = res[0]["transaction_id"] if isinstance(res, list) and res else None
    verb = "used/lost" if diff < 0 else "more than recorded"
    msg = (
        f"{prod['name']}: weighed {fmt_amount(weighed, unit)} "
        f"(was {fmt_amount(before, unit)}, {fmt_amount(abs(diff), unit)} {verb})"
    )
    svc.db.add_event(
        "weighed",
        msg,
        product_id=product_id,
        product_name=prod["name"],
        amount=new_amount,
        unit=unit,
        data={"before": before, "diff": diff, "transaction_id": tx},
    )
    return {
        "product": {"id": product_id, "name": prod["name"]},
        "changed": True,
        "before": before,
        "amount": new_amount,
        "difference": diff,
        "unit": unit,
        "container_g": body.container_g,
        "transaction_id": tx,
        "message": msg,
        "notes": notes,
    }


def _tidy(amount: float, unit: str) -> float:
    """Whole g / ml (a scale isn't more precise than that); 3 decimals for kg / l."""
    return round(amount, 3) if unit in ("kg", "l", "dl", "cl") else round(amount)


def _kind(unit: str) -> str:
    try:
        return dimension(unit)
    except UnitError:
        return "count"  # the product's own Grocy unit ("Bottle")


class UseIn(BaseModel):
    amount: float | None = Field(None, gt=0, description="How much was used, in `unit`")
    unit: str | None = Field(None, description="g, ml, piece, ... Default: the stock unit")
    all: bool = Field(False, description="Finished it: use up everything in stock")


async def use(svc: Services, product_id: int, body: UseIn) -> dict[str, Any]:
    """Quick use from the stock list ('a dash of milk'): consume from the opened pack, opening one if needed."""
    product = await svc.grocy.object("products", product_id)
    name, unit = product["name"], svc.catalog.unit_of(product.get("qu_id_stock"))
    entries = await svc.grocy.stock_entries(product_id)
    have = sum(float(e["amount"]) for e in entries)
    if have <= EPS:
        raise CaptureError(f"{name}: nothing in stock")
    if body.all:
        amount = have
    else:
        if body.amount is None:
            raise CaptureError("How much was used?")
        net, net_unit = net_of(await svc.grocy.userfields("products", product_id))
        try:
            amount = convert_food(
                body.amount, body.unit or unit, unit, name=name, net_amount_per_pack=net, net_unit=net_unit
            )
        except UnitError as e:
            raise CaptureError(str(e)) from e
        amount = min(amount, have)
        if _kind(unit) != "count" and not any(int(e.get("open") or 0) for e in entries) and amount < have - EPS:
            await open_product(svc, product_id)  # poured from a new pack: that pack is now open
            entries = await svc.grocy.stock_entries(product_id)
    opened = [e for e in entries if int(e.get("open") or 0)]
    entry = min(opened, key=lambda e: e.get("best_before_date") or "9999") if opened else None
    entry_id = entry["stock_id"] if entry and amount <= float(entry["amount"]) + EPS else None
    await svc.grocy.consume_stock(product_id, round(amount, 4), stock_entry_id=entry_id)
    left = max(have - amount, 0.0)
    message = (
        f"Finished {name}" if left <= EPS else f"Used {fmt_amount(amount, unit)} {name} · {fmt_amount(left, unit)} left"
    )
    svc.db.add_event("used", message, product_id=product_id, product_name=name, amount=amount, unit=unit)
    return {"message": message, "left": left, "unit": unit}


async def inventory(svc: Services) -> list[dict[str, Any]]:
    """Everything in stock, for the phone's inventory list and quick search."""
    today = date.today()
    was = svc.db.kv_prefix("original_name:")
    items = []
    for s in await svc.grocy.stock():
        product = s["product"]
        unit = svc.catalog.unit_of(product.get("qu_id_stock"))
        amount = float(s.get("amount") or 0)
        opened = float(s.get("amount_opened") or 0)
        due = s.get("best_before_date")
        days_left = (date.fromisoformat(due) - today).days if due and not due.startswith("2999") else None
        items.append(
            {
                "product_id": int(s["product_id"]),
                "name": product["name"],
                "was": was.get(f"original_name:{int(s['product_id'])}"),  # searchable by its old name too
                "amount": amount,
                "unit": unit,
                "kind": _kind(unit),
                "container_g": float(c) if (c := svc.db.get_kv(f"container_g:{int(s['product_id'])}")) else None,
                "amount_text": fmt_amount(amount, unit),
                "opened_text": fmt_amount(opened, unit) if opened else None,
                "location": svc.catalog.location_name(product.get("location_id")),
                "due": due if days_left is not None else None,
                "days_left": days_left,
                "value_eur": round(float(s.get("value") or 0), 2),
            }
        )
    return sorted(items, key=lambda i: i["name"].lower())


async def open_product(svc: Services, product_id: int) -> dict[str, Any]:
    """Mark one pack/piece as opened (Grocy then shortens its due date to the 'after opening' days)."""
    product = await svc.grocy.object("products", product_id)
    unit = svc.catalog.unit_of(product.get("qu_id_stock"))
    entries = [e for e in await svc.grocy.stock_entries(product_id) if not int(e.get("open") or 0)]
    if not entries:
        raise CaptureError(f"{product['name']}: nothing unopened in stock")
    entry = min(entries, key=lambda e: e.get("best_before_date") or "9999")
    available = float(entry["amount"])
    net, net_unit = net_of(await svc.grocy.userfields("products", product_id))
    try:
        dim = normalize_unit(unit)
    except UnitError:
        dim = unit
    if dim in ("piece", "pack"):
        amount = 1.0
    elif net and net_unit:
        amount = convert(net, net_unit, unit)  # one pack, e.g. 500 g of a 1.5 kg stock
    else:
        amount = available  # pack size unknown: the whole stock entry is one pack
    amount = min(amount, available)
    await svc.grocy.open_stock(product_id, amount, entry["stock_id"])
    after = [e for e in await svc.grocy.stock_entries(product_id) if int(e.get("open") or 0)]
    due = min((e.get("best_before_date") or "" for e in after), default="") or None
    message = f"Opened {fmt_amount(amount, unit)} {product['name']}, {_due_text(due)}"
    svc.db.add_event("opened", message, product_id=product_id, product_name=product["name"])
    return {"message": message, "due": due}


# --- editing what's in stock (tap an item in the stock list) ----------------------------------


async def entries(svc: Services, product_id: int) -> dict[str, Any]:
    """The product and each pack/entry in stock: amount, best before, place, opened."""
    product = await svc.grocy.object("products", product_id)
    unit = svc.catalog.unit_of(product.get("qu_id_stock"))
    rows = []
    for e in sorted(await svc.grocy.stock_entries(product_id), key=lambda e: e.get("best_before_date") or ""):
        due = e.get("best_before_date")
        rows.append(
            {
                "id": int(e["id"]),
                "amount": float(e["amount"]),
                "amount_text": fmt_amount(float(e["amount"]), unit),
                "best_before": due if due and not due.startswith("2999") else None,
                "opened": bool(int(e.get("open") or 0)),
                "location": svc.catalog.location_name(e.get("location_id")),
            }
        )
    return {"product": {"id": product_id, "name": product["name"], "unit": unit}, "entries": rows}


class EntryEdit(BaseModel):
    amount: float = Field(ge=0, description="New amount in the stock unit; 0 removes the entry")
    best_before: date | None = Field(None, description="Empty: no date")
    location: str | None = Field(None, description="Fridge, Freezer or Pantry")
    opened: bool = False


async def edit_entry(svc: Services, product_id: int, entry_id: int, body: EntryEdit) -> dict[str, Any]:
    product = await svc.grocy.object("products", product_id)
    unit = svc.catalog.unit_of(product.get("qu_id_stock"))
    entry = next((e for e in await svc.grocy.stock_entries(product_id) if int(e["id"]) == entry_id), None)
    if entry is None:
        raise CaptureError("That item isn't in stock any more: reload the list")
    if body.amount <= EPS:
        await svc.grocy.consume_stock(product_id, float(entry["amount"]), stock_entry_id=entry["stock_id"])
        message = f"Removed {fmt_amount(float(entry['amount']), unit)} {product['name']} from stock"
    else:
        await svc.grocy.edit_stock_entry(
            entry_id,
            {
                "amount": body.amount,
                "best_before_date": body.best_before.isoformat() if body.best_before else NEVER,
                "location_id": svc.catalog.location_id(body.location) if body.location else entry.get("location_id"),
                "shopping_location_id": entry.get("shopping_location_id"),
                "price": entry.get("price"),
                "open": 1 if body.opened else 0,
                "purchased_date": entry.get("purchased_date"),
                "note": entry.get("note"),
            },
        )
        message = f"Saved {product['name']}: {fmt_amount(body.amount, unit)}"
    svc.db.add_event("edited", message, product_id=product_id, product_name=product["name"])
    return {"message": message, **await entries(svc, product_id)}


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


async def rename(svc: Services, product_id: int, body: RenameIn) -> dict[str, Any]:
    product = await svc.grocy.object("products", product_id)
    new = body.name.strip()
    others = {p["name"].strip().lower() for p in await svc.products() if int(p["id"]) != product_id}
    if new.lower() in others:
        raise CaptureError(f"Another product is already called '{new}'")
    key = f"original_name:{product_id}"
    if not svc.db.get_kv(key):
        svc.db.set_kv(key, product["name"])  # the old name still matches receipts
    await svc.grocy.edit_object("products", product_id, {"name": new})
    svc.db.add_event("renamed", f"Renamed '{product['name']}' to '{new}'", product_id=product_id, product_name=new)
    return {"message": f"Renamed to {new}", "name": new}
