"""Idempotent bootstrap of Grocy master data (POST /setup, also run at startup)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .grocy import GrocyClient, GrocyError
from .units import GROCY_UNIT_NAMES, UnitError, normalize_unit

log = logging.getLogger(__name__)

QUANTITY_UNITS = [  # (name, plural)
    ("g", "g"),
    ("kg", "kg"),
    ("ml", "ml"),
    ("l", "l"),
    ("Piece", "Pieces"),
    ("Pack", "Packs"),
]
CONVERSIONS = [("kg", "g", 1000.0), ("l", "ml", 1000.0)]
LOCATIONS = [("Fridge", False), ("Freezer", True), ("Pantry", False)]
SHOPPING_LOCATIONS = ["Maxima", "Rimi", "Lidl", "Norfa", "IKI", "Turgus", "Kaimas"]
MEAL_SECTIONS = [("Breakfast", 1, "08:00"), ("Lunch", 2, "13:00"), ("Dinner", 3, "19:00")]

NUMBER = "number-decimal"
TEXT = "text-single-line"
PRODUCT_USERFIELDS = [
    ("kcal_100", "kcal / 100 g|ml", NUMBER),
    ("protein_100", "Protein g / 100", NUMBER),
    ("fat_100", "Fat g / 100", NUMBER),
    ("satfat_100", "Sat. fat g / 100", NUMBER),
    ("carbs_100", "Carbs g / 100", NUMBER),
    ("sugar_100", "Sugar g / 100", NUMBER),
    ("fiber_100", "Fibre g / 100", NUMBER),
    ("salt_100", "Salt g / 100", NUMBER),
    ("nutrition_source", "Nutrition source (open_food_facts|label_photo|fineli|usda|manual)", TEXT),
    ("net_amount_per_pack", "Net amount per pack", NUMBER),
    ("net_unit", "Net unit (g|ml)", TEXT),
    ("label_photo", "Nutrition label photo", "image"),
]
RECIPE_USERFIELDS = [
    ("grocy_plus_week", "Grocy+ generated for week", TEXT),
]

# Products in a product group whose name contains one of these are not treated as food
NON_FOOD_GROUP_KEYWORDS = ("non-food", "nonfood", "household", "cleaning", "hygiene", "buitis", "chemija")


@dataclass
class Catalog:
    """IDs of the Grocy master data the companion relies on."""

    qu_by_unit: dict[str, int] = field(default_factory=dict)  # canonical unit -> qu id
    unit_by_qu: dict[int, str] = field(default_factory=dict)  # qu id -> canonical unit (or raw name)
    locations: dict[str, int] = field(default_factory=dict)  # lower name -> id
    shopping_locations: dict[str, int] = field(default_factory=dict)
    meal_sections: dict[str, int] = field(default_factory=dict)

    async def load(self, grocy: GrocyClient) -> Catalog:
        self.qu_by_unit.clear()
        self.unit_by_qu.clear()
        for qu in await grocy.objects("quantity_units"):
            try:
                unit = normalize_unit(qu["name"])
            except UnitError:
                unit = qu["name"]
            self.unit_by_qu[int(qu["id"])] = unit
            self.qu_by_unit.setdefault(unit, int(qu["id"]))
        self.locations = {loc["name"].lower(): int(loc["id"]) for loc in await grocy.objects("locations")}
        self.shopping_locations = {s["name"].lower(): int(s["id"]) for s in await grocy.objects("shopping_locations")}
        self.meal_sections = {
            s["name"].lower(): int(s["id"]) for s in await grocy.objects("meal_plan_sections") if s.get("name")
        }
        return self

    def unit_of(self, qu_id: int | str | None) -> str:
        return self.unit_by_qu.get(int(qu_id or 0), "piece")

    def location_id(self, name: str | None, default: str = "pantry") -> int:
        if name and name.lower() in self.locations:
            return self.locations[name.lower()]
        return self.locations.get(default) or next(iter(self.locations.values()))

    def location_name(self, location_id: Any) -> str | None:
        for name, lid in self.locations.items():
            if str(lid) == str(location_id):
                return name.title()
        return None

    def store_id(self, name: str | None) -> int | None:
        if not name:
            return None
        return self.shopping_locations.get(name.strip().lower())


async def _ensure(
    grocy: GrocyClient, entity: str, existing: dict[str, Any], key: str, data: dict[str, Any], report: list[str]
) -> int:
    if key.lower() in existing:
        return int(existing[key.lower()]["id"])
    new_id = await grocy.add_object(entity, data)
    report.append(f"created {entity}: {key}")
    return new_id


async def run_setup(grocy: GrocyClient, catalog: Catalog) -> dict[str, Any]:
    report: list[str] = []
    warnings: list[str] = []

    # Quantity units
    qus = {q["name"].lower(): q for q in await grocy.objects("quantity_units")}
    qu_ids: dict[str, int] = {}
    for name, plural in QUANTITY_UNITS:
        qu_ids[name.lower()] = await _ensure(
            grocy, "quantity_units", qus, name, {"name": name, "name_plural": plural}, report
        )

    convs = await grocy.objects("quantity_unit_conversions")
    for frm, to, factor in CONVERSIONS:
        f_id, t_id = qu_ids[frm], qu_ids[to]
        if not any(
            int(c["from_qu_id"]) == f_id and int(c["to_qu_id"]) == t_id and not c.get("product_id") for c in convs
        ):
            await grocy.add_object(
                "quantity_unit_conversions", {"from_qu_id": f_id, "to_qu_id": t_id, "factor": factor}
            )
            report.append(f"created conversion {frm}->{to}")

    locs = {loc["name"].lower(): loc for loc in await grocy.objects("locations")}
    for name, freezer in LOCATIONS:
        await _ensure(grocy, "locations", locs, name, {"name": name, "is_freezer": int(freezer)}, report)

    stores = {s["name"].lower(): s for s in await grocy.objects("shopping_locations")}
    for name in SHOPPING_LOCATIONS:
        await _ensure(grocy, "shopping_locations", stores, name, {"name": name}, report)

    sections = {s["name"].lower(): s for s in await grocy.objects("meal_plan_sections") if s.get("name")}
    for name, sort, time_info in MEAL_SECTIONS:
        await _ensure(
            grocy,
            "meal_plan_sections",
            sections,
            name,
            {"name": name, "sort_number": sort, "time_info": time_info},
            report,
        )

    ufs = await grocy.objects("userfields")
    existing_uf = {(u["entity"], u["name"]) for u in ufs}
    for entity, fields in (("products", PRODUCT_USERFIELDS), ("recipes", RECIPE_USERFIELDS)):
        for name, caption, uf_type in fields:
            if (entity, name) not in existing_uf:
                await grocy.add_object(
                    "userfields",
                    {
                        "entity": entity,
                        "name": name,
                        "caption": caption,
                        "type": uf_type,
                        "show_as_column_in_tables": 0,
                    },
                )
                report.append(f"created userfield {entity}.{name}")

    # Opened packs of food stay "in stock"
    fixed = await fix_opened_as_out_of_stock(grocy)
    if fixed:
        report.append(f"set treat_opened_as_out_of_stock=0 on {fixed} product(s)")
    try:
        await grocy.put("/user/settings/product_presets_treat_opened_as_out_of_stock", {"value": False})
    except GrocyError as e:
        warnings.append(f"could not set user preset: {e.message}")

    try:
        cfg = await grocy.get("/system/config")
        flag = cfg.get("FEATURE_FLAG_STOCK_PRODUCT_OPENED_TRACKING")
        if flag in (False, "false", 0, "0"):
            warnings.append(
                "Grocy has opened tracking disabled: set GROCY_FEATURE_FLAG_STOCK_PRODUCT_OPENED_"
                "TRACKING=true on the grocy service"
            )
        if cfg.get("CURRENCY") != "EUR":
            warnings.append(f"Grocy currency is {cfg.get('CURRENCY')}, expected EUR (GROCY_CURRENCY=EUR)")
    except GrocyError as e:
        warnings.append(f"could not read Grocy config: {e.message}")

    await catalog.load(grocy)
    missing_units = [u for u in GROCY_UNIT_NAMES if u not in catalog.qu_by_unit]
    if missing_units:
        warnings.append(f"quantity units not mapped: {missing_units}")
    return {"changes": report, "warnings": warnings, "ok": not missing_units}


async def fix_opened_as_out_of_stock(grocy: GrocyClient, product_ids: list[int] | None = None) -> int:
    """Set treat_opened_as_out_of_stock=0 for food products (all, or the given ids)."""
    groups = {int(g["id"]): g["name"].lower() for g in await grocy.objects("product_groups")}
    non_food = {gid for gid, name in groups.items() if any(k in name for k in NON_FOOD_GROUP_KEYWORDS)}
    fixed = 0
    for p in await grocy.objects("products", ["treat_opened_as_out_of_stock=1"]):
        if product_ids is not None and int(p["id"]) not in product_ids:
            continue
        if p.get("product_group_id") and int(p["product_group_id"]) in non_food:
            continue
        await grocy.edit_object("products", int(p["id"]), {"treat_opened_as_out_of_stock": 0})
        fixed += 1
    return fixed
