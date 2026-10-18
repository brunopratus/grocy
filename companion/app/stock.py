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
from .units import UnitError, convert, fmt_amount, normalize_unit

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
    container_g: float | None = Field(None, ge=0, description="Container weight when tare handling is off")


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
    if dim_unit in ("ml", "l", "cl", "dl"):
        notes.append("Volume product: assuming 1 g ≈ 1 ml")

    tare_enabled = bool(int(prod.get("enable_tare_weight_handling") or 0))
    tare = float(prod.get("tare_weight") or 0)
    includes = body.includes_container if body.includes_container is not None else tare_enabled
    before = float(details.get("stock_amount") or 0)

    if tare_enabled:
        gross_g = body.grams_measured if includes else body.grams_measured + tare
        net_g = gross_g - tare
        new_for_grocy = convert(gross_g, "g", "kg") if dim_unit == "kg" else gross_g  # Grocy expects gross
    else:
        if includes:
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
    base_unit = "g" if dim_unit in ("g", "kg", "mg") else "ml"
    new_amount = convert(net_g, "g" if base_unit == "g" else "ml", dim_unit)
    if new_for_grocy is None:
        new_for_grocy = new_amount

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
    res = await svc.grocy.inventory(product_id, round(new_for_grocy, 3))
    tx = res[0]["transaction_id"] if isinstance(res, list) and res else None
    verb = "used/lost" if diff < 0 else "more than recorded"
    msg = (
        f"{prod['name']}: weighed {fmt_amount(new_amount, unit)} "
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
        "transaction_id": tx,
        "message": msg,
        "notes": notes,
    }
