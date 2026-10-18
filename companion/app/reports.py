"""Recipe nutrition and the weekly cost/nutrition/waste report."""

from __future__ import annotations

import contextlib
from datetime import date, timedelta
from typing import Any

from .deps import Services
from .grocy import GrocyError
from .nutrition import Per100, Totals, nutrients_for
from .products import net_of
from .units import UnitError, convert


class ProductInfo:
    """Per-request cache of product rows, nutrition userfields and units."""

    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self._products: dict[int, dict[str, Any]] | None = None
        self._uf: dict[int, dict[str, Any]] = {}

    async def product(self, pid: int) -> dict[str, Any]:
        if self._products is None:
            self._products = {int(p["id"]): p for p in await self.svc.products()}
        return self._products[pid]

    async def userfields(self, pid: int) -> dict[str, Any]:
        if pid not in self._uf:
            self._uf[pid] = await self.svc.grocy.userfields("products", pid)
        return self._uf[pid]

    async def stock_unit(self, pid: int) -> str:
        return self.svc.catalog.unit_of((await self.product(pid)).get("qu_id_stock"))

    async def nutrients(self, pid: int, amount_stock: float) -> Totals:
        p = await self.product(pid)
        uf = await self.userfields(pid)
        net, net_unit = net_of(uf)
        return nutrients_for(
            p["name"], amount_stock, await self.stock_unit(pid), Per100.from_userfields(uf), net, net_unit
        )


async def recipe_nutrition(svc: Services, recipe_id: int, info: ProductInfo | None = None) -> dict[str, Any]:
    info = info or ProductInfo(svc)
    recipe = await svc.grocy.object("recipes", recipe_id)
    servings = int(recipe.get("desired_servings") or recipe.get("base_servings") or 1)
    positions = await svc.grocy.objects("recipes_pos_resolved", [f"recipe_id={recipe_id}"])
    total = Totals()
    ingredients = []
    for pos in positions:
        pid = int(pos["product_id"])
        amount = float(pos.get("recipe_amount") or 0)
        n = await info.nutrients(pid, amount)
        total = total.add(n)
        ingredients.append(
            {
                "product_id": pid,
                "name": (await info.product(pid))["name"],
                "amount": round(amount, 2),
                "unit": await info.stock_unit(pid),
                "kcal": round(n.kcal),
            }
        )
    try:
        ful = await svc.grocy.recipe_fulfillment(recipe_id)
    except GrocyError:
        ful = {}
    cost = float(ful.get("costs") or 0)
    return {
        "recipe": {"id": recipe_id, "name": recipe["name"]},
        "servings": servings,
        "ingredients": ingredients,
        "total": total.rounded(),
        "per_serving": total.divided(servings).rounded(),
        "cost_total_eur": round(cost, 2),
        "cost_per_serving_eur": round(float(ful.get("costs_per_serving") or cost / servings), 2),
        "grocy_calories_total": ful.get("calories"),
        "need_fulfilled": bool(int(ful.get("need_fulfilled") or 0)) if ful else None,
        "_per_serving_totals": total.divided(servings),
        "_cost_per_serving": float(ful.get("costs_per_serving") or (cost / servings if servings else 0)),
    }


def _public(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if not k.startswith("_")}


async def week_report(svc: Services, start: date) -> dict[str, Any]:
    end = start + timedelta(days=6)
    people = max(svc.settings.people, 1)
    info = ProductInfo(svc)
    entries = await svc.grocy.objects("meal_plan", [f"day>={start.isoformat()}", f"day<={end.isoformat()}"])
    sections = {v: k for k, v in svc.catalog.meal_sections.items()}

    recipe_cache: dict[int, dict[str, Any]] = {}
    days: dict[str, dict[str, Any]] = {
        (start + timedelta(days=i)).isoformat(): {"meals": [], "total": Totals(), "cost": 0.0} for i in range(7)
    }
    for e in sorted(entries, key=lambda x: (x["day"], int(x.get("section_id") or 0))):
        day = days.get(e["day"])
        if day is None:
            continue
        meal: dict[str, Any] = {"section": sections.get(int(e.get("section_id") or -1), "").title() or None}
        if e.get("type") == "recipe" and e.get("recipe_id"):
            rid = int(e["recipe_id"])
            if rid not in recipe_cache:
                recipe_cache[rid] = await recipe_nutrition(svc, rid, info)
            rn = recipe_cache[rid]
            n_serv = float(e.get("recipe_servings") or 1)
            nut = rn["_per_serving_totals"].times(n_serv)
            cost = rn["_cost_per_serving"] * n_serv
            meal.update({"type": "recipe", "recipe_id": rid, "name": rn["recipe"]["name"], "servings": n_serv})
        elif e.get("type") == "product" and e.get("product_id"):
            pid = int(e["product_id"])
            amount = float(e.get("product_amount") or 0)
            unit = await info.stock_unit(pid)
            qu_unit = svc.catalog.unit_of(e.get("product_qu_id")) if e.get("product_qu_id") else unit
            uf = await info.userfields(pid)
            net, net_unit = net_of(uf)
            with contextlib.suppress(UnitError):
                amount = convert(amount, qu_unit, unit, net_amount_per_pack=net, net_unit=net_unit)
            nut = await info.nutrients(pid, amount)
            details = await svc.grocy.product_details(pid)
            cost = amount * float(details.get("last_price") or 0)
            meal.update(
                {
                    "type": "product",
                    "product_id": pid,
                    "name": (await info.product(pid))["name"],
                    "amount": amount,
                    "unit": unit,
                }
            )
        else:
            meal.update({"type": e.get("type"), "name": e.get("note") or ""})
            nut, cost = Totals(), 0.0
        meal["nutrition"] = nut.rounded()
        meal["cost_eur"] = round(cost, 2)
        day["meals"].append(meal)
        day["total"] = day["total"].add(nut)
        day["cost"] += cost

    out_days = []
    week_total, week_cost = Totals(), 0.0
    for d, v in days.items():
        week_total = week_total.add(v["total"])
        week_cost += v["cost"]
        out_days.append(
            {
                "date": d,
                "meals": v["meals"],
                "nutrition_total": v["total"].rounded(),
                "nutrition_per_person": v["total"].divided(people).rounded(),
                "cost_eur": round(v["cost"], 2),
                "cost_per_person_eur": round(v["cost"] / people, 2),
            }
        )

    stock = await _stock_movements(svc, info, start, end)
    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "people": people,
        "days": out_days,
        "week": {
            "cost_eur": round(week_cost, 2),
            "cost_per_person_per_day_eur": round(week_cost / people / 7, 2),
            "nutrition_per_person_per_day": week_total.divided(people * 7).rounded(),
        },
        **stock,
        "recipes": [_public(r) for r in recipe_cache.values()],
    }


async def _stock_movements(svc: Services, info: ProductInfo, start: date, end: date) -> dict[str, Any]:
    logs = await svc.grocy.objects(
        "stock_log",
        [
            "undone=0",
            f"row_created_timestamp>={start.isoformat()} 00:00:00",
            f"row_created_timestamp<={end.isoformat()} 23:59:59",
        ],
    )
    consumed: dict[int, dict[str, Any]] = {}
    wasted: dict[int, dict[str, Any]] = {}
    corrections: dict[int, dict[str, Any]] = {}
    for row in logs:
        amount = float(row.get("amount") or 0)
        if amount >= 0:
            continue
        ttype = row.get("transaction_type")
        if ttype == "consume":
            bucket = wasted if int(row.get("spoiled") or 0) else consumed
        elif ttype == "inventory-correction":
            bucket = corrections
        else:
            continue
        pid = int(row["product_id"])
        try:
            name = (await info.product(pid))["name"]
            unit = await info.stock_unit(pid)
        except KeyError:
            name, unit = f"#{pid}", ""
        b = bucket.setdefault(pid, {"product_id": pid, "name": name, "amount": 0.0, "unit": unit, "value_eur": 0.0})
        b["amount"] += -amount
        b["value_eur"] += -amount * float(row.get("price") or 0)

    def fmt(bucket: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        rows = sorted(bucket.values(), key=lambda r: -r["value_eur"])
        for r in rows:
            r["amount"] = round(r["amount"], 2)
            r["value_eur"] = round(r["value_eur"], 2)
        return rows

    c, w, k = fmt(consumed), fmt(wasted), fmt(corrections)
    return {
        "stock_consumed": c,
        "stock_wasted": w,
        "stock_corrections": k,
        "consumed_value_eur": round(sum(r["value_eur"] for r in c), 2),
        "wasted_value_eur": round(sum(r["value_eur"] for r in w), 2),
    }
