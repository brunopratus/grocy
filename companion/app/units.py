"""Unit normalisation and conversion between purchase/recipe units and Grocy stock units.

Food products use g, ml or piece as the stock unit so that partial use
("500 g of a 1 kg pack") is representable. Packs/pieces convert to mass or
volume only when the product's net amount per pack/piece is known.
"""

from __future__ import annotations

import re
from typing import Literal

Dimension = Literal["mass", "volume", "count"]

# canonical unit -> (dimension, factor to the dimension's base unit g / ml / piece)
CANONICAL: dict[str, tuple[Dimension, float]] = {
    "mg": ("mass", 0.001),
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "ml": ("volume", 1.0),
    "cl": ("volume", 10.0),
    "dl": ("volume", 100.0),
    "l": ("volume", 1000.0),
    "piece": ("count", 1.0),
    "pack": ("count", 1.0),
}

# Grocy quantity unit names created by /setup, per canonical unit
GROCY_UNIT_NAMES: dict[str, str] = {
    "g": "g",
    "kg": "kg",
    "ml": "ml",
    "l": "l",
    "piece": "Piece",
    "pack": "Pack",
}

_ALIASES: dict[str, str] = {
    # mass
    "milligram": "mg",
    "milligrams": "mg",
    "gram": "g",
    "grams": "g",
    "gr": "g",
    "grm": "g",
    "gramm": "g",
    "gramai": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "kgs": "kg",
    # volume
    "millilitre": "ml",
    "milliliter": "ml",
    "millilitres": "ml",
    "milliliters": "ml",
    "mls": "ml",
    "centilitre": "cl",
    "centiliter": "cl",
    "decilitre": "dl",
    "deciliter": "dl",
    "litre": "l",
    "liter": "l",
    "litres": "l",
    "liters": "l",
    "ltr": "l",
    "lt": "l",
    "litras": "l",
    # count
    "pc": "piece",
    "pcs": "piece",
    "pieces": "piece",
    "unit": "piece",
    "units": "piece",
    "x": "piece",
    "vnt": "piece",
    "vnt.": "piece",
    "szt": "piece",
    "szt.": "piece",
    "tk": "piece",
    "gab": "piece",
    "ea": "piece",
    "each": "piece",
    "item": "piece",
    "items": "piece",
    "packs": "pack",
    "package": "pack",
    "packet": "pack",
    "pak": "pack",
    "pakuotė": "pack",
    "op": "pack",
}


class UnitError(ValueError):
    pass


def normalize_unit(unit: str) -> str:
    """Map a free-form unit ("Gram", "kg", "vnt", "Piece") to a canonical unit."""
    u = unit.strip().lower()
    if u in CANONICAL:
        return u
    if u in _ALIASES:
        return _ALIASES[u]
    u2 = u.rstrip(".")
    if u2 in CANONICAL:
        return u2
    if u2 in _ALIASES:
        return _ALIASES[u2]
    raise UnitError(f"Unknown unit '{unit}'")


def dimension(unit: str) -> Dimension:
    return CANONICAL[normalize_unit(unit)][0]


def convert(
    amount: float,
    from_unit: str,
    to_unit: str,
    *,
    net_amount_per_pack: float | None = None,
    net_unit: str | None = None,
) -> float:
    """Convert ``amount`` from ``from_unit`` to ``to_unit``.

    ``net_amount_per_pack``/``net_unit`` describe one pack or piece of the product
    (e.g. 500 g) and allow count <-> mass/volume conversions.
    """
    f = normalize_unit(from_unit)
    t = normalize_unit(to_unit)
    if f == t:
        return float(amount)
    fd, ff = CANONICAL[f]
    td, tf = CANONICAL[t]
    if fd == td and fd != "count":
        return float(amount) * ff / tf
    if fd == "count" and td == "count":
        # piece <-> pack: treated as the same thing (one sellable item)
        return float(amount)

    if not net_amount_per_pack or not net_unit:
        raise UnitError(f"Cannot convert {from_unit} to {to_unit} without the net amount per pack")
    n = normalize_unit(net_unit)
    nd, nf = CANONICAL[n]
    if nd == "count":
        raise UnitError("Net amount per pack must be a mass or volume")
    if fd == "count":
        if td != nd:
            raise UnitError(f"Cannot convert {from_unit} to {to_unit}: pack size is a {nd}")
        return float(amount) * net_amount_per_pack * nf / tf
    if td == "count":
        if fd != nd:
            raise UnitError(f"Cannot convert {from_unit} to {to_unit}: pack size is a {nd}")
        return float(amount) * ff / (net_amount_per_pack * nf)
    raise UnitError(f"Cannot convert {from_unit} ({fd}) to {to_unit} ({td})")


def to_base(amount: float, unit: str) -> tuple[float, str]:
    """Express a mass/volume amount in g or ml (counts are returned unchanged)."""
    u = normalize_unit(unit)
    d, factor = CANONICAL[u]
    if d == "mass":
        return amount * factor, "g"
    if d == "volume":
        return amount * factor, "ml"
    return amount, u


_QTY_RE = re.compile(
    r"(?:(?P<count>\d+)\s*[x×*]\s*)?(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>[a-zA-Zė.]+)",
)


def parse_quantity(text: str | None) -> tuple[float, str] | None:
    """Parse labels like '500 g', '1,5 l', '4 x 125 g', '6 vnt' into (amount, canonical unit).

    Multi-packs are multiplied out ('4 x 125 g' -> 500 g). Returns None if nothing usable.
    """
    if not text:
        return None
    for m in _QTY_RE.finditer(text):
        try:
            unit = normalize_unit(m.group("unit"))
        except UnitError:
            continue
        amount = float(m.group("amount").replace(",", "."))
        if m.group("count"):
            amount *= int(m.group("count"))
        if unit == "pack":
            continue
        base_amount, base_unit = to_base(amount, unit)
        return round(base_amount, 3), base_unit
    return None


def fmt_amount(amount: float, unit: str) -> str:
    """Human friendly amount: 1500 g -> '1.5 kg', 500 g -> '500 g'."""
    try:
        u = normalize_unit(unit) if unit else ""
    except UnitError:  # a unit made in Grocy by hand ("Bottle", "Jar"): show it as it is
        return f"{round(amount, 1):g} {unit}"
    if u == "g" and amount >= 1000:
        return f"{amount / 1000:g} kg"
    if u == "ml" and amount >= 1000:
        return f"{amount / 1000:g} l"
    if u in ("piece", "pack"):
        return f"{amount:g} {u if amount == 1 else u + 's'}"
    return f"{round(amount, 1):g} {u}"


# Grams per ml of foods that are stocked by volume but easiest to weigh (matched on the product name,
# Lithuanian or English; first match wins, so the more specific names come first). Anything else: 1.0.
DENSITIES: list[tuple[tuple[str, ...], float]] = [
    (("grietinėl", "cream"), 1.0),
    (("grietin", "sour cream", "jogurt", "yogurt", "yoghurt"), 1.04),
    (("aliej", "oil"), 0.92),
    (("medus", "honey"), 1.42),
    (("sirup", "syrup"), 1.33),
    (("pien", "milk", "kefyr", "kefir", "pasuk"), 1.03),
    (("sult", "juice", "nektar"), 1.05),
    (("padaž", "sauce", "kečup", "ketchup"), 1.1),
]


def density(name: str) -> float:
    n = name.lower()
    return next((d for words, d in DENSITIES if any(w in n for w in words)), 1.0)


def convert_food(
    amount: float,
    from_unit: str,
    to_unit: str,
    *,
    name: str = "",
    net_amount_per_pack: float | None = None,
    net_unit: str | None = None,
) -> float:
    """Like convert(), plus weight <-> volume using the food's density (e.g. weighed milk -> ml)."""
    try:
        return convert(amount, from_unit, to_unit, net_amount_per_pack=net_amount_per_pack, net_unit=net_unit)
    except UnitError:
        fd, ff = CANONICAL[normalize_unit(from_unit)]
        td, tf = CANONICAL[normalize_unit(to_unit)]
        if {fd, td} != {"mass", "volume"}:
            raise
        base = float(amount) * ff  # g or ml
        return round(base / density(name) if fd == "mass" else base * density(name)) / tf  # whole g / ml
