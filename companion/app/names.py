"""English product names: the household reads English, not Lithuanian (or the language on the pack).

Products are named in English in Grocy ("Kiaušiniai" -> "Eggs"). The original name is kept in the
companion DB (kv "original_name:<id>") so receipts in Lithuanian and typed Lithuanian names still match.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .claude import compact_json
from .deps import Services

log = logging.getLogger(__name__)

PREFIX = "original_name:"
BATCH = 60

SYSTEM = """You name grocery products in plain English for a household in Lithuania that doesn't read
Lithuanian well. For each product give the short English name a shopper would use: 'Kiaušiniai' -> 'Eggs',
'Grietinė 30%' -> 'Sour cream 30%', 'CVETLICNI MED' -> 'Flower honey', 'Rugių duona' -> 'Rye bread'.
Keep a brand in parentheses as it is, e.g. 'Dark soy sauce (Jade Bridge)'. Keep fat %, flavour and variety.
If a name is already English, return it unchanged. Never invent a brand."""


class EnglishName(BaseModel):
    id: int
    english: str = Field(description="Short English product name")


class EnglishNames(BaseModel):
    names: list[EnglishName]


def originals(svc: Services) -> dict[int, str]:
    return {int(k[len(PREFIX) :]): v for k, v in svc.db.kv_prefix(PREFIX).items()}


async def english_names(svc: Services, items: list[tuple[int, str]]) -> dict[int, str]:
    """One cheap Claude call per batch; names it can't translate stay as they are."""
    out: dict[int, str] = {}
    for start in range(0, len(items), BATCH):
        chunk = items[start : start + BATCH]
        try:
            parsed = await svc.claude.structured(
                task="english_names",
                schema=EnglishNames,
                cheap=True,
                effort="low",
                max_tokens=4000,
                system=SYSTEM,
                content=[
                    {"type": "text", "text": f"Products: {compact_json([{'id': i, 'name': n} for i, n in chunk])}"}
                ],
            )
        except Exception as e:  # noqa: BLE001 - the original name is still usable
            log.warning("English names failed: %s", e)
            continue
        known = dict(chunk)
        out.update({n.id: n.english.strip()[:120] for n in parsed.names if n.id in known and n.english.strip()})
    return out


async def rename_to_english(svc: Services, product_ids: list[int] | None = None) -> dict[str, Any]:
    """Name products in English (all not done yet, or the given ones); keeps the original for matching."""
    done = originals(svc)
    products = {int(p["id"]): p for p in await svc.products()}
    todo = [(pid, p["name"]) for pid, p in products.items() if pid not in done]
    if product_ids is not None:
        todo = [(pid, name) for pid, name in todo if pid in set(product_ids)]
    if not todo:
        return {"renamed": []}
    english = await english_names(svc, todo)
    taken = {p["name"].strip().lower() for p in products.values()}
    renamed = []
    for pid, name in todo:
        new = english.get(pid)
        if new is None:
            continue  # Claude failed: try again next time
        svc.db.set_kv(f"{PREFIX}{pid}", name)
        if new.strip().lower() == name.strip().lower():
            continue
        candidate, i = new, 2
        while candidate.lower() in taken:
            candidate, i = f"{new} ({i})", i + 1
        await svc.grocy.edit_object("products", pid, {"name": candidate})
        taken.add(candidate.lower())
        renamed.append({"id": pid, "from": name, "to": candidate})
        log.info("renamed product %s: %s -> %s", pid, name, candidate)
    if renamed:
        svc.db.add_event("renamed", f"Named {len(renamed)} product(s) in English", data={"renamed": renamed})
    return {"renamed": renamed}
