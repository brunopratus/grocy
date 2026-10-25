"""Weekly meal plan: adapt the household's plan to current stock with Claude and write it to Grocy."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .claude import compact_json, image_block, pdf_block
from .deps import Services
from .grocy import GrocyError
from .nutrition import Totals
from .products import NewProduct, create_product, net_of
from .reports import ProductInfo
from .units import UnitError, convert, fmt_amount

log = logging.getLogger(__name__)

WEEK_USERFIELD = "grocy_plus_week"
RISK_DAYS = 5
SLOTS = ("breakfast", "lunch", "dinner")


# --- Claude output schema -------------------------------------------------------


class PlanIngredient(BaseModel):
    product_id: int | None = Field(
        None, description="ID of an existing Grocy product from the catalog when it matches, else null"
    )
    name: str = Field(description="Ingredient name (English, generic, e.g. 'spaghetti', 'red lentils')")
    amount: float = Field(gt=0, description="Amount for the recipe's servings")
    unit: Literal["g", "ml", "piece"]


class PlanRecipe(BaseModel):
    key: str = Field(description="Short unique id, e.g. 'lentil-bolognese'")
    name: str
    servings: int = Field(gt=0, description="Servings the ingredient amounts are for")
    source_url: str | None = None
    instructions: str = Field(description="Short method, max ~8 lines")
    ingredients: list[PlanIngredient]
    uses_waste_risk: list[str] = Field(
        default_factory=list, description="Names of opened / soon-due stock items this recipe uses up"
    )
    est_cost_eur: float | None = Field(
        None, description="Estimated total cost for `servings` in EUR (Lithuanian supermarket prices)"
    )


class PlanMeal(BaseModel):
    slot: Literal["breakfast", "lunch", "dinner"]
    recipe_key: str
    servings: int = Field(gt=0)


class PlanDay(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    meals: list[PlanMeal]


class ShoppingItem(BaseModel):
    name: str
    amount: float
    unit: Literal["g", "ml", "piece"]
    product_id: int | None = None


class WeekPlan(BaseModel):
    days: list[PlanDay]
    recipes: list[PlanRecipe]
    shopping_delta: list[ShoppingItem] = Field(
        default_factory=list, description="What must be bought beyond current stock for the whole week"
    )
    prep_schedule: list[str] = Field(default_factory=list, description="Batch-prep steps, e.g. 'Sun: cook rice'")
    notes: str | None = None

    @model_validator(mode="after")
    def _check_keys(self) -> WeekPlan:
        keys = {r.key for r in self.recipes}
        if len(keys) != len(self.recipes):
            raise ValueError("recipe keys must be unique")
        missing = {m.recipe_key for d in self.days for m in d.meals} - keys
        if missing:
            raise ValueError(f"meals reference unknown recipe keys: {sorted(missing)}")
        for d in self.days:
            date.fromisoformat(d.date)
        return self


class PlanRequest(BaseModel):
    week_start: date
    people: int = 2
    kcal_target: int | None = Field(None, ge=1000, le=5000, description="Per person per day")
    budget_eur: float | None = Field(None, gt=0, description="Food budget for the week")
    dry_run: bool = False
    extra_preferences: str | None = None
    keep_recipes: bool = Field(True, description="Keep the received plan's meals as they are; only check stock")


SYSTEM = """You are the meal planner of a 2-person household in Kaunas, Lithuania (EUR, stores: Maxima, Rimi,
Lidl, Norfa, IKI and the local market). You adapt the weekly plan the household received to what is already
at home, to prevent food waste.

Rules, in priority order:
1. Use opened and soon-due items (marked risk=true) first, in the earliest days.
2. Use what is in stock before buying anything new.
3. Keep the received plan's structure (breakfast/lunch/dinner each day) and keep its recipes where sensible;
   swap recipes only when that clearly uses up stock or the plan's recipe needs many purchases.
4. Hit the kcal target per person per day when given; respect the budget when given.
5. Give every ingredient in g, ml or piece, scaled to the recipe's servings. Map ingredients to existing
   catalog products by product_id whenever one matches (same food, any brand); otherwise product_id null.
6. Cook-once-eat-twice is fine: several meals may share one recipe_key (then the recipe's servings must
   cover all of those meals' servings combined, and each meal's servings is what is eaten at that meal).
7. Keep source links from the received plan in source_url.
Dates must be exactly the 7 days of the requested week."""


# --- helpers ----------------------------------------------------------------------


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7)


def grocy_week_names(start: date) -> list[str]:
    """Names of Grocy's 'mealplan-week' shadow recipes covering these 7 days (strftime %Y-%W)."""
    names: list[str] = []
    for i in range(7):
        n = (start + timedelta(days=i)).strftime("%Y-%W").lstrip("0")
        if n not in names:
            names.append(n)
    return names


async def inventory_snapshot(svc: Services) -> dict[str, Any]:
    """Current stock with amounts, due dates and opened flags, plus the product catalog."""
    today = date.today()
    stock = await svc.grocy.stock()
    volatile = await svc.grocy.volatile(due_soon_days=RISK_DAYS)
    risk_ids = {
        int(p["product_id"])
        for key in ("due_products", "overdue_products", "expired_products")
        for p in volatile.get(key, [])
    }
    items = []
    for s in stock:
        pid = int(s["product_id"])
        unit = svc.catalog.unit_of(s["product"].get("qu_id_stock"))
        opened = float(s.get("amount_opened") or 0)
        due = s.get("best_before_date")
        days_left = (date.fromisoformat(due) - today).days if due and not due.startswith("2999") else None
        risk = pid in risk_ids or opened > 0 or (days_left is not None and days_left <= RISK_DAYS)
        if risk:
            risk_ids.add(pid)
        items.append(
            {
                "product_id": pid,
                "name": s["product"]["name"],
                "stock": fmt_amount(float(s["amount"]), unit),
                "opened": fmt_amount(opened, unit) if opened else None,
                "due": due if days_left is not None else None,
                "days_left": days_left,
                "risk": risk,
            }
        )
    items.sort(key=lambda x: (not x["risk"], x["days_left"] if x["days_left"] is not None else 9999))
    catalog = [
        {"id": int(p["id"]), "name": p["name"], "unit": svc.catalog.unit_of(p.get("qu_id_stock"))}
        for p in await svc.products()
    ]
    return {"stock": items, "catalog": catalog, "risk_ids": sorted(risk_ids)}


async def generate_plan(
    svc: Services,
    req: PlanRequest,
    inv: dict[str, Any],
    upload: bytes | None,
    upload_type: str | None,
    text: str | None,
) -> WeekPlan:
    days = [(req.week_start + timedelta(days=i)).isoformat() for i in range(7)]
    content: list[dict[str, Any]] = []
    if upload:
        if (upload_type or "").endswith("pdf"):
            content.append(pdf_block(upload))
        elif (upload_type or "").startswith("image/"):
            content.append(image_block(upload, upload_type))
        else:
            content.append({"type": "text", "text": "Received weekly plan:\n" + upload.decode("utf-8", "replace")})
    if text:
        content.append({"type": "text", "text": "Received weekly plan:\n" + text})
    prefs = {
        "week_dates": days,
        "people": req.people,
        "kcal_target_per_person_per_day": req.kcal_target,
        "budget_eur_week": req.budget_eur,
        "extra_preferences": req.extra_preferences,
    }
    keep = (
        "KEEP THE RECEIVED PLAN AS IT IS: same meals on the same days and slots, same recipes and amounts. "
        "Do not swap or add recipes; rules 1-4 do not apply. Only map ingredients to catalog products and "
        "give what is needed, so it can be checked against the stock.\n\n"
        if req.keep_recipes
        else ""
    )
    content.append(
        {
            "type": "text",
            "text": (
                f"{keep}Preferences: {compact_json(prefs)}\n\n"
                f"Current stock (risk=true means opened or due within {RISK_DAYS} days):\n"
                f"{compact_json(inv['stock'])}\n\n"
                f"Product catalog for product_id mapping:\n{compact_json(inv['catalog'])}\n\n"
                "Produce the adapted plan for these 7 days."
            ),
        }
    )
    return await svc.claude.structured(
        task="mealplan",
        schema=WeekPlan,
        content=content,
        system=SYSTEM,
        effort="medium",
        max_tokens=32000,
        tool_description="Submit the adapted weekly meal plan.",
    )


# --- writing to Grocy ---------------------------------------------------------------


async def _delete_generated(svc: Services, week: str, start: date) -> list[str]:
    """Remove recipes this tool generated earlier for the same week (and their meal plan entries)."""
    removed: list[str] = []
    end = start + timedelta(days=6)
    for r in await svc.grocy.objects("recipes", ["type=normal"]):
        rid = int(r["id"])
        uf = await svc.grocy.userfields("recipes", rid)
        if uf.get(WEEK_USERFIELD) != week:
            continue
        for e in await svc.grocy.objects("meal_plan", [f"recipe_id={rid}"]):
            if start.isoformat() <= e["day"] <= end.isoformat():
                await svc.grocy.delete_object("meal_plan", int(e["id"]))
        for pos in await svc.grocy.objects("recipes_pos", [f"recipe_id={rid}"]):
            await svc.grocy.delete_object("recipes_pos", int(pos["id"]))
        await svc.grocy.delete_object("recipes", rid)
        removed.append(r["name"])
    return removed


async def _resolve_product(
    svc: Services, ing: PlanIngredient, products: dict[int, dict[str, Any]], by_name: dict[str, int], created: list[str]
) -> int:
    if ing.product_id and ing.product_id in products:
        return ing.product_id
    pid = by_name.get(ing.name.strip().lower())
    if pid:
        return pid
    prod = await create_product(
        svc,
        NewProduct(
            name=ing.name.strip().capitalize(),
            stock_unit=ing.unit,
            purchase_unit=ing.unit,
            location="Pantry",
            description="Created by Grocy+ meal planner",
        ),
    )
    pid = int(prod["id"])
    products[pid] = prod
    by_name[prod["name"].lower()] = pid
    created.append(prod["name"])
    return pid


async def add_recipe(
    svc: Services,
    r: PlanRecipe,
    description: str,
    products: dict[int, dict[str, Any]],
    by_name: dict[str, int],
    created: list[str],
    warnings: list[str],
) -> int:
    """Create a Grocy recipe with its ingredients (creating missing products); returns the recipe id."""
    rid = await svc.grocy.add_object(
        "recipes",
        {"name": r.name, "description": description, "base_servings": r.servings, "desired_servings": r.servings},
    )
    for ing in r.ingredients:
        pid = await _resolve_product(svc, ing, products, by_name, created)
        prod = products[pid]
        stock_unit = svc.catalog.unit_of(prod.get("qu_id_stock"))
        pos: dict[str, Any] = {
            "recipe_id": rid,
            "product_id": pid,
            "qu_id": int(prod["qu_id_stock"]),
            "note": f"{ing.amount:g} {ing.unit} {ing.name}",
        }
        try:
            uf = await svc.grocy.userfields("products", pid)
            net, net_unit = net_of(uf)
            pos["amount"] = round(
                convert(ing.amount, ing.unit, stock_unit, net_amount_per_pack=net, net_unit=net_unit), 3
            )
        except UnitError:
            pos["amount"] = 1
            pos["only_check_single_unit_in_stock"] = 1
            warnings.append(
                f"{r.name}: '{ing.name}' {ing.amount:g} {ing.unit} can't be converted to "
                f"{stock_unit}; only checking that some is in stock"
            )
        await svc.grocy.add_object("recipes_pos", pos)
    return rid


async def apply_plan(svc: Services, plan: WeekPlan, req: PlanRequest) -> dict[str, Any]:
    week = req.week_start.isoformat()
    warnings: list[str] = []
    removed = await _delete_generated(svc, week, req.week_start)

    products = {int(p["id"]): p for p in await svc.products()}
    by_name = {p["name"].strip().lower(): pid for pid, p in products.items()}
    created_products: list[str] = []
    recipe_ids: dict[str, int] = {}

    for r in plan.recipes:
        desc = (
            r.instructions
            + (f"\n\nSource: {r.source_url}" if r.source_url else "")
            + f"\n\nGenerated by Grocy+ for the week of {week}."
        )
        rid = await add_recipe(svc, r, desc, products, by_name, created_products, warnings)
        await svc.grocy.set_userfields("recipes", rid, {WEEK_USERFIELD: week})
        recipe_ids[r.key] = rid

    for d in plan.days:
        for m in d.meals:
            await svc.grocy.add_object(
                "meal_plan",
                {
                    "day": d.date,
                    "type": "recipe",
                    "recipe_id": recipe_ids[m.recipe_key],
                    "recipe_servings": m.servings,
                    "section_id": svc.catalog.meal_sections.get(m.slot, -1),
                },
            )

    # Shopping list: Grocy's per-week shadow recipe aggregates all meal plan entries of that week
    before = {int(i["id"]): i for i in await svc.grocy.objects("shopping_list")}
    week_names = grocy_week_names(req.week_start)
    shadow = [
        int(w["id"]) for w in await svc.grocy.objects("recipes", ["type=mealplan-week"]) if w["name"] in week_names
    ]
    try:
        if shadow:
            for shadow_id in shadow:
                await svc.grocy.add_missing_to_shoppinglist(shadow_id)
        else:
            for rid in recipe_ids.values():
                await svc.grocy.add_missing_to_shoppinglist(rid)
    except GrocyError as e:
        warnings.append(f"Shopping list not updated: {e.message}")
    after = await svc.grocy.objects("shopping_list")
    delta = []
    for item in after:
        old = before.get(int(item["id"]))
        added = float(item.get("amount") or 0) - (float(old.get("amount") or 0) if old else 0)
        if added > 1e-6 and item.get("product_id"):
            p = products.get(int(item["product_id"]))
            unit = svc.catalog.unit_of(p.get("qu_id_stock")) if p else ""
            delta.append(
                {
                    "product_id": int(item["product_id"]),
                    "name": p["name"] if p else "?",
                    "amount": round(added, 2),
                    "unit": unit,
                }
            )
    svc.db.set_kv(f"plan_recipes:{week}", json.dumps(recipe_ids))
    return {
        "recipe_ids": recipe_ids,
        "removed_previous": removed,
        "created_products": created_products,
        "shopping_list_added": delta,
        "warnings": warnings,
    }


# --- summary ----------------------------------------------------------------------------


async def summarize(
    svc: Services, plan: WeekPlan, req: PlanRequest, risk_ids: set[int], recipe_ids: dict[str, int] | None
) -> dict[str, Any]:
    info = ProductInfo(svc)
    products = {int(p["id"]): p for p in await svc.products()}
    people = max(req.people, 1)
    recipes = {r.key: r for r in plan.recipes}

    per_serving: dict[str, Totals] = {}
    cost_per_serving: dict[str, float | None] = {}
    for key, r in recipes.items():
        total = Totals()
        for ing in r.ingredients:
            if ing.product_id and ing.product_id in products:
                stock_unit = await info.stock_unit(ing.product_id)
                uf = await info.userfields(ing.product_id)
                net, net_unit = net_of(uf)
                try:
                    amount = convert(ing.amount, ing.unit, stock_unit, net_amount_per_pack=net, net_unit=net_unit)
                    total = total.add(await info.nutrients(ing.product_id, amount))
                    continue
                except UnitError:
                    pass
            total = total.add(Totals(missing=[ing.name]))
        per_serving[key] = total.divided(r.servings)
        cost_per_serving[key] = (r.est_cost_eur / r.servings) if r.est_cost_eur is not None else None
        if recipe_ids and key in recipe_ids:
            try:
                ful = await svc.grocy.recipe_fulfillment(recipe_ids[key])
                cost_per_serving[key] = float(ful.get("costs_per_serving") or 0)
            except GrocyError:
                pass

    days_out: list[dict[str, Any]] = []
    week_cost = 0.0
    for d in plan.days:
        day_total, day_cost = Totals(), 0.0
        meals = []
        for m in d.meals:
            r = recipes[m.recipe_key]
            nut = per_serving[m.recipe_key].times(m.servings)
            cps = cost_per_serving[m.recipe_key]
            cost = cps * m.servings if cps is not None else None
            day_total = day_total.add(nut)
            day_cost += cost or 0.0
            ing_ids = {i.product_id for i in r.ingredients if i.product_id}
            uses = sorted(
                {products[i]["name"] for i in ing_ids if i in risk_ids and i in products} | set(r.uses_waste_risk)
            )
            meals.append(
                {
                    "slot": m.slot,
                    "recipe": r.name,
                    "servings": m.servings,
                    "uses_waste_risk": uses,
                    "cost_eur": round(cost, 2) if cost is not None else None,
                    "nutrition_per_person": nut.divided(people).rounded(),
                }
            )
        week_cost += day_cost
        days_out.append(
            {
                "date": d.date,
                "meals": meals,
                "cost_eur": round(day_cost, 2),
                "nutrition_per_person": day_total.divided(people).rounded(),
            }
        )
    return {
        "week_start": req.week_start.isoformat(),
        "days": days_out,
        "week_cost_eur": round(week_cost, 2),
        "shopping_delta": [s.model_dump() for s in plan.shopping_delta],
        "prep_schedule": plan.prep_schedule,
        "notes": plan.notes,
    }


async def run(
    svc: Services, req: PlanRequest, upload: bytes | None, upload_type: str | None, text: str | None
) -> dict[str, Any]:
    if not upload and not text:
        raise ValueError("Upload the weekly plan (PDF/text/image) or send it as text")
    inv = await inventory_snapshot(svc)
    plan = await generate_plan(svc, req, inv, upload, upload_type, text)
    written: dict[str, Any] | None = None
    if not req.dry_run:
        written = await apply_plan(svc, plan, req)
    summary = await summarize(svc, plan, req, set(inv["risk_ids"]), written["recipe_ids"] if written else None)
    if written:
        summary["shopping_delta_grocy"] = written["shopping_list_added"]
        summary["grocy"] = {k: v for k, v in written.items() if k != "shopping_list_added"}
    svc.db.save_plan(req.week_start.isoformat(), req.dry_run, plan.model_dump(), summary)
    svc.db.add_event(
        "plan",
        f"Meal plan for week of {req.week_start} {'previewed' if req.dry_run else 'written'}",
        data={"recipes": len(plan.recipes), "week_cost_eur": summary["week_cost_eur"]},
    )
    return {"dry_run": req.dry_run, "summary": summary, "plan": plan.model_dump()}
