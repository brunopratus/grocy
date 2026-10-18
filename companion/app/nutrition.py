"""Nutrition data: per-100 g/ml model, Open Food Facts, USDA FoodData Central, and the math."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .units import UnitError, convert, normalize_unit, parse_quantity

log = logging.getLogger(__name__)

NUTRIENT_FIELDS = ("kcal", "protein", "fat", "satfat", "carbs", "sugar", "fiber", "salt")
# Grocy product userfield name for each nutrient (all per 100 g/ml)
USERFIELD = {n: f"{n}_100" for n in NUTRIENT_FIELDS}


class Per100(BaseModel):
    """Nutrition per 100 g or 100 ml."""

    kcal: float | None = Field(None, description="Energy in kcal per 100 g/ml")
    protein: float | None = Field(None, description="Protein g per 100 g/ml")
    fat: float | None = Field(None, description="Fat g per 100 g/ml")
    satfat: float | None = Field(None, description="Saturated fat g per 100 g/ml")
    carbs: float | None = Field(None, description="Carbohydrates g per 100 g/ml")
    sugar: float | None = Field(None, description="Sugars g per 100 g/ml")
    fiber: float | None = Field(None, description="Fibre g per 100 g/ml")
    salt: float | None = Field(None, description="Salt g per 100 g/ml")

    def is_complete(self) -> bool:
        """Enough data to be useful: energy plus the three macros."""
        return all(v is not None for v in (self.kcal, self.protein, self.fat, self.carbs))

    def to_userfields(self) -> dict[str, Any]:
        return {USERFIELD[n]: round(v, 2) for n in NUTRIENT_FIELDS if (v := getattr(self, n)) is not None}

    @classmethod
    def from_userfields(cls, uf: dict[str, Any]) -> Per100:
        vals: dict[str, float | None] = {}
        for n in NUTRIENT_FIELDS:
            raw = uf.get(USERFIELD[n])
            try:
                vals[n] = float(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                vals[n] = None
        return cls(**vals)

    def scaled(self, factor: float) -> Per100:
        return Per100(**{n: (v * factor if (v := getattr(self, n)) is not None else None) for n in NUTRIENT_FIELDS})


class Totals(BaseModel):
    """Absolute nutrient amounts (kcal, grams) for a quantity of food."""

    kcal: float = 0.0
    protein: float = 0.0
    fat: float = 0.0
    satfat: float = 0.0
    carbs: float = 0.0
    sugar: float = 0.0
    fiber: float = 0.0
    salt: float = 0.0
    missing: list[str] = Field(default_factory=list, description="Ingredients without nutrition data")

    def add(self, other: Totals) -> Totals:
        data = {n: getattr(self, n) + getattr(other, n) for n in NUTRIENT_FIELDS}
        return Totals(**data, missing=sorted(set(self.missing) | set(other.missing)))

    def times(self, factor: float) -> Totals:
        return Totals(**{n: getattr(self, n) * factor for n in NUTRIENT_FIELDS}, missing=list(self.missing))

    def divided(self, by: float) -> Totals:
        by = by or 1
        return Totals(**{n: getattr(self, n) / by for n in NUTRIENT_FIELDS}, missing=list(self.missing))

    def rounded(self) -> dict[str, Any]:
        out: dict[str, Any] = {n: round(float(getattr(self, n)), 1) for n in NUTRIENT_FIELDS}
        out["kcal"] = round(self.kcal)
        if self.missing:
            out["missing"] = self.missing
        return out


def amount_in_base(
    amount: float, stock_unit: str, net_amount_per_pack: float | None, net_unit: str | None
) -> float | None:
    """Stock amount -> grams or ml (None if the unit can't be expressed as mass/volume)."""
    try:
        u = normalize_unit(stock_unit)
    except UnitError:
        return None
    if u in ("g", "kg", "mg"):
        return convert(amount, u, "g")
    if u in ("ml", "l", "cl", "dl"):
        return convert(amount, u, "ml")
    if net_amount_per_pack and net_unit:
        try:
            base = "g" if normalize_unit(net_unit) in ("g", "kg", "mg") else "ml"
            return convert(amount, u, base, net_amount_per_pack=net_amount_per_pack, net_unit=net_unit)
        except UnitError:
            return None
    return None


def nutrients_for(
    name: str,
    amount: float,
    stock_unit: str,
    per100: Per100,
    net_amount_per_pack: float | None = None,
    net_unit: str | None = None,
) -> Totals:
    """Nutrients in ``amount`` (stock units) of a product with ``per100`` data."""
    base = amount_in_base(amount, stock_unit, net_amount_per_pack, net_unit)
    if base is None or per100.kcal is None:
        return Totals(missing=[name])
    factor = base / 100.0
    return Totals(**{n: (getattr(per100, n) or 0.0) * factor for n in NUTRIENT_FIELDS})


def calories_per_stock_unit(
    per100: Per100, stock_unit: str, net_amount_per_pack: float | None = None, net_unit: str | None = None
) -> float | None:
    """Value for Grocy's native ``calories`` field (kcal per 1 stock unit)."""
    if per100.kcal is None:
        return None
    base = amount_in_base(1.0, stock_unit, net_amount_per_pack, net_unit)
    return round(per100.kcal * base / 100.0, 4) if base is not None else None


# --- Open Food Facts ---------------------------------------------------------

OFF_FIELDS = (
    "code,product_name,product_name_en,generic_name,brands,quantity,product_quantity,"
    "product_quantity_unit,image_url,image_front_url,nutriments,categories_tags"
)


class OffProduct(BaseModel):
    code: str
    name: str
    brand: str | None = None
    net_amount: float | None = None
    net_unit: str | None = None
    image_url: str | None = None
    nutrition: Per100 = Field(default_factory=Per100)


def parse_off_nutriments(n: dict[str, Any]) -> Per100:
    def g(*keys: str) -> float | None:
        for k in keys:
            v = n.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    kcal = g("energy-kcal_100g")
    if kcal is None and (kj := g("energy-kj_100g", "energy_100g")) is not None:
        kcal = kj / 4.184
    salt = g("salt_100g")
    if salt is None and (sodium := g("sodium_100g")) is not None:
        salt = sodium * 2.5
    return Per100(
        kcal=kcal,
        protein=g("proteins_100g"),
        fat=g("fat_100g"),
        satfat=g("saturated-fat_100g"),
        carbs=g("carbohydrates_100g"),
        sugar=g("sugars_100g"),
        fiber=g("fiber_100g"),
        salt=salt,
    )


def parse_off_product(code: str, p: dict[str, Any]) -> OffProduct:
    name = (p.get("product_name") or p.get("product_name_en") or p.get("generic_name") or "").strip()
    brand = (p.get("brands") or "").split(",")[0].strip() or None
    net_amount: float | None = None
    net_unit: str | None = None
    try:
        if p.get("product_quantity") and p.get("product_quantity_unit"):
            q = parse_quantity(f"{p['product_quantity']} {p['product_quantity_unit']}")
        else:
            q = parse_quantity(p.get("quantity"))
    except (TypeError, ValueError):
        q = None
    if q:
        net_amount, net_unit = q
    return OffProduct(
        code=code,
        name=name or f"Product {code}",
        brand=brand,
        net_amount=net_amount,
        net_unit=net_unit,
        image_url=p.get("image_front_url") or p.get("image_url"),
        nutrition=parse_off_nutriments(p.get("nutriments") or {}),
    )


async def off_lookup(http: httpx.AsyncClient, code: str, user_agent: str) -> OffProduct | None:
    url = f"https://world.openfoodfacts.org/api/v2/product/{code}"
    try:
        resp = await http.get(url, params={"fields": OFF_FIELDS}, headers={"User-Agent": user_agent}, timeout=20.0)
    except httpx.HTTPError as e:
        log.warning("Open Food Facts lookup failed: %s", e)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        log.warning("Open Food Facts returned %s", resp.status_code)
        return None
    data = resp.json()
    if data.get("status") != 1 or not data.get("product"):
        return None
    return parse_off_product(code, data["product"])


# --- USDA FoodData Central ---------------------------------------------------

# nutrientNumber -> our field. 208 = Energy kcal; Foundation foods often only have 957/958 (Atwater).
USDA_NUMBERS = {
    "203": "protein",
    "204": "fat",
    "606": "satfat",
    "205": "carbs",
    "269": "sugar",
    "291": "fiber",
}
USDA_ENERGY = ("208", "958", "957")
USDA_SODIUM = "307"


class UsdaFood(BaseModel):
    fdc_id: int
    description: str
    data_type: str
    nutrition: Per100


def parse_usda_food(food: dict[str, Any]) -> UsdaFood:
    by_number: dict[str, float] = {}
    for fn in food.get("foodNutrients", []):
        num = str(fn.get("nutrientNumber") or fn.get("number") or "")
        val = fn.get("value", fn.get("amount"))
        unit = (fn.get("unitName") or "").upper()
        if not num or val is None:
            continue
        if num in USDA_ENERGY and unit == "KJ":
            continue
        by_number.setdefault(num, float(val))
    vals: dict[str, float | None] = {f: by_number.get(n) for n, f in USDA_NUMBERS.items()}
    vals["kcal"] = next((by_number[n] for n in USDA_ENERGY if n in by_number), None)
    vals["salt"] = by_number[USDA_SODIUM] * 2.5 / 1000 if USDA_SODIUM in by_number else None
    return UsdaFood(
        fdc_id=int(food.get("fdcId", 0)),
        description=food.get("description", ""),
        data_type=food.get("dataType", ""),
        nutrition=Per100(**vals),
    )


async def usda_search(http: httpx.AsyncClient, query: str, api_key: str, page_size: int = 8) -> list[UsdaFood]:
    """Search FDC, preferring Foundation and SR Legacy data (per 100 g)."""
    url = "https://api.nal.usda.gov/fdc/v1/foods/search"
    results: list[UsdaFood] = []
    for data_types in ("Foundation,SR Legacy", "Survey (FNDDS)"):
        try:
            resp = await http.get(
                url,
                params={"api_key": api_key, "query": query, "dataType": data_types, "pageSize": page_size},
                timeout=20.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("USDA search failed: %s", e)
            return results
        results = [parse_usda_food(f) for f in resp.json().get("foods", [])]
        results = [r for r in results if r.nutrition.kcal is not None]
        if results:
            return results
    return results
