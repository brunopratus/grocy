"""HTTP API."""

from __future__ import annotations

import hmac
import json
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel

from . import alerts, bugs, mealplan, names, products, receipts, recipes, reports, stock, usage
from .deps import Services
from .setup import run_setup

router = APIRouter()

COOKIE = "companion_token"
MAX_UPLOAD = 25 * 1024 * 1024


def svc_of(request: Request) -> Services:
    return request.app.state.services


async def _read(upload: UploadFile) -> tuple[bytes, str | None]:
    data = await upload.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (max 25 MB)")
    if not data:
        raise HTTPException(400, f"Empty upload: {upload.filename}")
    return data, upload.content_type


# --- auth ----------------------------------------------------------------------


class LoginIn(BaseModel):
    token: str


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    expected = svc_of(request).settings.companion_token
    if not expected or not hmac.compare_digest(body.token.encode(), expected.encode()):
        raise HTTPException(401, "Wrong token")
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, body.token, max_age=365 * 24 * 3600, httponly=True, samesite="strict", secure=secure)
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return {"ok": True, "version": svc_of(request).settings.version}


# --- setup / lookups --------------------------------------------------------------


@router.post("/setup")
async def setup(request: Request) -> dict[str, Any]:
    svc = svc_of(request)
    return await run_setup(svc.grocy, svc.catalog)


@router.get("/lookups")
async def lookups(request: Request) -> dict[str, Any]:
    """Products, recipes, stores and locations for the phone UI's pickers."""
    svc = svc_of(request)
    prods = [
        {"id": int(p["id"]), "name": p["name"], "unit": svc.catalog.unit_of(p.get("qu_id_stock"))}
        for p in await svc.products()
    ]
    recipes = [
        {"id": int(r["id"]), "name": r["name"], "servings": r.get("desired_servings")}
        for r in await svc.grocy.objects("recipes", ["type=normal"])
    ]
    return {
        "products": sorted(prods, key=lambda p: p["name"].lower()),
        "recipes": sorted(recipes, key=lambda r: r["name"].lower()),
        "stores": sorted(svc.catalog.shopping_locations, key=str.lower),
        "locations": sorted(svc.catalog.locations, key=str.lower),
        "people": svc.settings.people,
        "version": svc.settings.version,
        "testing": svc.settings.testing,
        "home_stock_done": svc.db.get_kv("home_stock_done") == "1",
    }


# --- product capture ----------------------------------------------------------------


@router.post("/products/barcode/{code}")
async def barcode(code: str, request: Request) -> dict[str, Any]:
    return await products.capture_barcode(svc_of(request), code)


@router.post("/products/{product_id}/label-photo")
async def label_photo(
    product_id: str,
    request: Request,
    images: list[UploadFile] = File(...),
    barcode: str | None = Form(None),
    net_amount: float | None = Form(None, gt=0),
    net_unit: str | None = Form(None),
) -> dict[str, Any]:
    pid: int | None
    if product_id == "new":
        pid = None
    elif product_id.isdigit():
        pid = int(product_id)
    else:
        raise HTTPException(400, "product_id must be a number or 'new'")
    files = [await _read(f) for f in images]
    return await products.label_photo(svc_of(request), pid, files, barcode, net_amount, net_unit)


@router.post("/products/{product_id}/shelf-life")
async def shelf_life(product_id: int, request: Request, body: products.ShelfLifeIn | None = None) -> dict[str, Any]:
    return await products.shelf_life(svc_of(request), product_id, body or products.ShelfLifeIn())


@router.get("/products/{product_id}/card")
async def product_card(product_id: int, request: Request) -> dict[str, Any]:
    return await receipts.product_card(svc_of(request), product_id)


@router.post("/products/by-name")
async def product_by_name(body: products.ByNameIn, request: Request) -> dict[str, Any]:
    return await products.product_by_name(svc_of(request), body)


@router.post("/products/produce")
async def produce(body: products.ProduceIn, request: Request) -> dict[str, Any]:
    return await products.add_produce(svc_of(request), body)


@router.post("/stock/add")
async def stock_add(body: products.StockAddIn, request: Request) -> dict[str, Any]:
    return await products.add_stock(svc_of(request), body)


@router.post("/ocr/due-date")
async def due_date(request: Request, image: UploadFile = File(...)) -> dict[str, Any]:
    data, mt = await _read(image)
    return (await products.read_due_date(svc_of(request), data, mt)).model_dump()


# --- cooking and partial use ---------------------------------------------------------


@router.post("/recipes/{recipe_id}/cook")
async def cook(recipe_id: int, request: Request, body: stock.CookIn | None = None) -> dict[str, Any]:
    return await stock.cook(svc_of(request), recipe_id, body.servings if body else None)


@router.post("/recipes/import")
async def recipe_import(
    request: Request,
    url: str | None = Form(None),
    text: str | None = Form(None),
    servings: int | None = Form(None),
    file: UploadFile | None = File(None),
) -> dict[str, Any]:
    image = await _read(file) if file and file.filename else None
    return await recipes.import_recipe(
        svc_of(request),
        url=(url or "").strip() or None,
        text=(text or "").strip() or None,
        image=image,
        servings=servings,
    )


@router.get("/recipes/{recipe_id}/check")
async def recipe_check(
    recipe_id: int, request: Request, servings: int | None = Query(None, ge=1, le=50)
) -> dict[str, Any]:
    return await recipes.check(svc_of(request), recipe_id, servings)


@router.post("/recipes/{recipe_id}/shopping-list")
async def recipe_shopping_list(recipe_id: int, request: Request) -> dict[str, Any]:
    return await recipes.missing_to_shopping_list(svc_of(request), recipe_id)


@router.post("/stock/{product_id}/weigh")
async def weigh(product_id: int, body: stock.WeighIn, request: Request) -> dict[str, Any]:
    return await stock.weigh(svc_of(request), product_id, body)


@router.get("/events")
async def events(request: Request, limit: int = Query(100, le=1000), kind: str | None = None) -> list[Any]:
    return svc_of(request).db.list_events(limit, kind)


@router.get("/stock/all")
async def stock_all(request: Request) -> list[dict[str, Any]]:
    return await stock.inventory(svc_of(request))


@router.post("/stock/{product_id}/open")
async def open_product(product_id: int, request: Request) -> dict[str, Any]:
    return await stock.open_product(svc_of(request), product_id)


@router.post("/stock/{product_id}/use")
async def use_product(product_id: int, body: stock.UseIn, request: Request) -> dict[str, Any]:
    return await stock.use(svc_of(request), product_id, body)


@router.get("/stock/{product_id}/entries")
async def stock_entries(product_id: int, request: Request) -> dict[str, Any]:
    return await stock.entries(svc_of(request), product_id)


@router.put("/stock/{product_id}/entries/{entry_id}")
async def edit_stock_entry(product_id: int, entry_id: int, body: stock.EntryEdit, request: Request) -> dict[str, Any]:
    return await stock.edit_entry(svc_of(request), product_id, entry_id, body)


@router.post("/products/{product_id}/name")
async def rename_product(product_id: int, body: stock.RenameIn, request: Request) -> dict[str, Any]:
    return await stock.rename(svc_of(request), product_id, body)


@router.get("/stock/open-due")
async def open_due(request: Request, days: int | None = Query(None, ge=0, le=60)) -> list[dict[str, Any]]:
    return await alerts.open_and_due(svc_of(request), days)


# --- nutrition and cost reporting ------------------------------------------------------


@router.get("/recipes/{recipe_id}/nutrition")
async def recipe_nutrition(recipe_id: int, request: Request) -> dict[str, Any]:
    return reports._public(await reports.recipe_nutrition(svc_of(request), recipe_id))


@router.get("/reports/week")
async def week_report(request: Request, start: date | None = None) -> dict[str, Any]:
    if start is None:
        today = date.today()
        start = date.fromordinal(today.toordinal() - today.weekday())
    return await reports.week_report(svc_of(request), start)


@router.get("/nutrition/search")
async def nutrition_search(request: Request, q: str, fi: str | None = None) -> list[dict[str, Any]]:
    """Generic-food candidates (Fineli, USDA) per 100 g for a query; `fi` adds a Finnish query for Fineli."""
    found = await products.food_candidates(svc_of(request), q, fi)
    return [{**c.model_dump(), "label": c.label} for c in found]


@router.get("/usage")
async def usage_raw(request: Request, since: str | None = None) -> dict[str, Any]:
    return svc_of(request).db.claude_usage(since)


@router.get("/usage/summary")
async def usage_summary(request: Request) -> dict[str, Any]:
    return usage.summary(svc_of(request))


@router.post("/usage/credit")
async def usage_credit(body: usage.CreditIn, request: Request) -> dict[str, Any]:
    return usage.set_credit(svc_of(request), body)


# --- meal plan -----------------------------------------------------------------------


@router.post("/mealplan/generate")
async def mealplan_generate(
    request: Request,
    file: UploadFile | None = File(None),
    text: str | None = Form(None),
    week_start: date | None = Form(None),
    people: int | None = Form(None),
    kcal_target: int | None = Form(None),
    budget_eur: float | None = Form(None),
    dry_run: bool = Form(False),
    preferences: str | None = Form(None),
) -> dict[str, Any]:
    svc = svc_of(request)
    data, mt = (await _read(file)) if file is not None and file.filename else (None, None)
    req = mealplan.PlanRequest(
        week_start=week_start or mealplan.next_monday(),
        people=people or svc.settings.people,
        kcal_target=kcal_target,
        budget_eur=budget_eur,
        dry_run=dry_run,
        extra_preferences=preferences,
    )
    return await mealplan.run(svc, req, data, mt, text)


# --- alerts ----------------------------------------------------------------------------


@router.get("/alerts/preview")
async def alerts_preview(request: Request) -> dict[str, Any]:
    return await alerts.preview(svc_of(request))


@router.post("/alerts/send")
async def alerts_send(request: Request, force: bool = False) -> dict[str, Any]:
    return await alerts.send(svc_of(request), force)


# --- phase 4 / 5 ---------------------------------------------------------------------------


@router.post("/receipts/photo")
async def receipt_photo(request: Request, image: UploadFile = File(...), commit: bool = Form(False)) -> dict[str, Any]:
    svc = svc_of(request)
    data, mt = await _read(image)
    return await receipts.import_photo(svc, data, mt, commit, str(Path(svc.settings.db_path).parent))


@router.get("/receipts/inbox")
async def receipt_inbox(request: Request) -> dict[str, Any]:
    return await receipts.inbox(svc_of(request))


class HomeStockIn(BaseModel):
    done: bool = True


@router.post("/setup/home-stock")
async def home_stock(body: HomeStockIn, request: Request) -> dict[str, Any]:
    """The one-time 'add what's already at home' step: done (or skipped) hides it from the home screen."""
    svc_of(request).db.set_kv("home_stock_done", "1" if body.done else "0")
    return {"home_stock_done": body.done}


# The open checklist / stock-take, kept on the server too: the phone's copy can be cleared by the browser
LIST_KEY = "current_list"
LIST_MAX = 1_000_000


@router.get("/lists/current")
async def current_list(request: Request) -> Any:
    raw = svc_of(request).db.get_kv(LIST_KEY)
    return json.loads(raw) if raw else None


@router.put("/lists/current")
async def save_current_list(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > LIST_MAX:
        raise HTTPException(413, "List too large")
    data = json.loads(body or b"null")
    svc_of(request).db.set_kv(LIST_KEY, json.dumps(data, ensure_ascii=False) if data else "")
    return {"saved": bool(data)}


@router.post("/products/english-names")
async def english_names(request: Request) -> dict[str, Any]:
    """Name products in English (the ones not done yet)."""
    return await names.rename_to_english(svc_of(request))


@router.post("/receipts/commit")
async def receipt_commit(request: Request, body: receipts.ReceiptCommitIn) -> dict[str, Any]:
    return await receipts.commit_lines(svc_of(request), body)


# --- bug reports --------------------------------------------------------------------------


@router.post("/bugs")
async def bug_report(
    request: Request,
    description: str = Form(..., min_length=3, max_length=5000),
    screen: str | None = Form(None),
    context: str = Form("{}"),
    screenshot: UploadFile | None = File(None),
) -> dict[str, Any]:
    try:
        ctx = json.loads(context)
    except ValueError:
        ctx = {"raw": context[:5000]}
    image = (await _read(screenshot))[0] if screenshot and screenshot.filename else None
    return await bugs.report(svc_of(request), description, screen, ctx if isinstance(ctx, dict) else {}, image)


@router.get("/bugs")
async def bug_list(request: Request, status: str | None = None) -> list[dict[str, Any]]:
    return svc_of(request).db.list_bugs(status)


@router.get("/bank/matches")
async def bank_matches() -> dict[str, Any]:
    raise HTTPException(501, "Phase 5 (bank link) is not implemented yet")
