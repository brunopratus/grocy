"""Recipes from anywhere: a link, pasted text or a photo -> Grocy recipe -> stock check, nutrition, cost.

Claude reads the recipe and maps its ingredients to the household's products; the recipe is saved in
Grocy (so it can be cooked, planned and reused), then checked against the stock.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .claude import compact_json, image_block
from .deps import Services
from .grocy import GrocyError
from .mealplan import PlanIngredient, PlanRecipe, add_recipe, inventory_snapshot
from .products import CaptureError
from .reports import recipe_nutrition

log = logging.getLogger(__name__)

PAGE_LIMIT = 20000  # characters of page text sent to Claude
PAGE_USER_AGENT = "Mozilla/5.0 (compatible; GrocyPlus-Companion/0.1; household recipe import)"


class RecipeImport(BaseModel):
    name: str = Field(description="Recipe name (keep the original language)")
    servings: int = Field(gt=0, description="Servings the ingredient amounts are for")
    instructions: str = Field(description="Short method, max ~10 lines")
    ingredients: list[PlanIngredient]


class RecipeError(CaptureError):
    """Shown to the user as a plain message."""


SYSTEM = """You turn a recipe (web page, pasted text or photo; any language, often Lithuanian or English)
into a structured recipe for a household in Kaunas, Lithuania. Give every ingredient in g, ml or piece,
for the recipe's servings (convert cups, spoons and similar to g or ml). Map an ingredient to a catalog
product by product_id only when it is the same food (any brand); prefer products that are in stock.
Skip water, and salt or pepper 'to taste'. Do not invent ingredients."""


def _jsonld_recipe(page: str) -> Any | None:
    """Most recipe sites embed a schema.org Recipe as JSON-LD: compact, reliable input for Claude."""
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, flags=re.S | re.I):
        try:
            data = json.loads(html.unescape(block.strip()))
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                kind = node.get("@type")
                if kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind):
                    return node
                stack.extend(v for k, v in node.items() if k == "@graph")
    return None


def page_text(page: str) -> str:
    recipe = _jsonld_recipe(page)
    if recipe is not None:
        return "schema.org Recipe: " + json.dumps(recipe, ensure_ascii=False)[:PAGE_LIMIT]
    page = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    return re.sub(r"\s+", " ", text).strip()[:PAGE_LIMIT]


async def fetch_page(http: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await http.get(url, headers={"User-Agent": PAGE_USER_AGENT, "Accept": "text/html"}, timeout=20.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise RecipeError(f"Couldn't open the link ({e}). Paste the recipe text or a photo instead.") from e
    return page_text(resp.text)


async def import_recipe(
    svc: Services,
    *,
    url: str | None = None,
    text: str | None = None,
    image: tuple[bytes, str | None] | None = None,
    servings: int | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if url:
        content.append({"type": "text", "text": f"Recipe web page {url}:\n{await fetch_page(svc.http, url)}"})
    if text:
        content.append({"type": "text", "text": f"Recipe text:\n{text[:PAGE_LIMIT]}"})
    if image:
        content.append(image_block(*image))
    if not content:
        raise RecipeError("Give a link, the recipe text or a photo")
    snap = await inventory_snapshot(svc)
    content.append(
        {
            "type": "text",
            "text": f"Catalog: {compact_json(snap['catalog'])}\nIn stock: {compact_json(snap['stock'])}\n"
            "Read this recipe.",
        }
    )
    parsed = await svc.claude.structured(
        task="recipe_import", schema=RecipeImport, system=SYSTEM, effort="medium", max_tokens=6000, content=content
    )
    products = {int(p["id"]): p for p in await svc.products()}
    by_name = {p["name"].strip().lower(): pid for pid, p in products.items()}
    created: list[str] = []
    warnings: list[str] = []
    recipe = PlanRecipe(key="import", source_url=url, **parsed.model_dump())
    description = parsed.instructions + (f"\n\nSource: {url}" if url else "") + "\n\nImported by Grocy+."
    rid = await add_recipe(svc, recipe, description, products, by_name, created, warnings)
    svc.db.add_event("recipe", f"Imported recipe '{parsed.name}'", data={"recipe_id": rid, "url": url})
    result = await check(svc, rid, servings)
    result["created_products"] = created
    result["warnings"] = warnings + result["warnings"]
    return result


async def check(svc: Services, recipe_id: int, servings: int | None = None) -> dict[str, Any]:
    """Which ingredients are in stock, nutrition (total and per serving) and cost."""
    if servings:
        await svc.grocy.edit_object("recipes", recipe_id, {"desired_servings": servings})
    base = await recipe_nutrition(svc, recipe_id)
    positions = await svc.grocy.objects("recipes_pos_resolved", [f"recipe_id={recipe_id}"])
    by_product = {int(p["product_id"]): p for p in positions}
    ingredients = []
    for ing in base["ingredients"]:
        pos = by_product.get(ing["product_id"], {})
        ok = bool(int(pos.get("need_fulfilled") or 0))
        ingredients.append(
            {
                **ing,
                "in_stock": round(float(pos.get("stock_amount") or 0), 2),
                "missing": 0 if ok else round(float(pos.get("missing_amount") or 0), 2),
                "ok": ok,
            }
        )
    missing_nutrition = base["total"].get("missing", [])
    return {
        "recipe": base["recipe"],
        "servings": base["servings"],
        "ingredients": ingredients,
        "all_in_stock": all(i["ok"] for i in ingredients),
        "total": base["total"],
        "per_serving": base["per_serving"],
        "cost_total_eur": base["cost_total_eur"],
        "cost_per_serving_eur": base["cost_per_serving_eur"],
        "missing_nutrition": missing_nutrition,
        "warnings": [],
    }


async def missing_to_shopping_list(svc: Services, recipe_id: int) -> dict[str, Any]:
    try:
        await svc.grocy.add_missing_to_shoppinglist(recipe_id)
    except GrocyError as e:
        raise RecipeError(f"Shopping list not updated: {e.message}") from e
    return {"message": "Missing ingredients added to the shopping list in Grocy"}
