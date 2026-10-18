"""HTTP API."""

from __future__ import annotations

import hmac
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel

from . import alerts, mealplan, products, receipts, reports, stock
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
async def health() -> dict[str, Any]:
    return {"ok": True}


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
    }


# --- product capture ----------------------------------------------------------------


@router.post("/products/barcode/{code}")
async def barcode(code: str, request: Request) -> dict[str, Any]:
    return await products.capture_barcode(svc_of(request), code)


@router.post("/products/{product_id}/label-photo")
async def label_photo(
    product_id: str, request: Request, images: list[UploadFile] = File(...), barcode: str | None = Form(None)
) -> dict[str, Any]:
    pid: int | None
    if product_id == "new":
        pid = None
    elif product_id.isdigit():
        pid = int(product_id)
    else:
        raise HTTPException(400, "product_id must be a number or 'new'")
    files = [await _read(f) for f in images]
    return await products.label_photo(svc_of(request), pid, files, barcode)


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


@router.post("/stock/{product_id}/weigh")
async def weigh(product_id: int, body: stock.WeighIn, request: Request) -> dict[str, Any]:
    return await stock.weigh(svc_of(request), product_id, body)


@router.get("/events")
async def events(request: Request, limit: int = Query(100, le=1000), kind: str | None = None) -> list[Any]:
    return svc_of(request).db.list_events(limit, kind)


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
async def usage(request: Request, since: str | None = None) -> dict[str, Any]:
    return svc_of(request).db.claude_usage(since)


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


@router.get("/bank/matches")
async def bank_matches() -> dict[str, Any]:
    raise HTTPException(501, "Phase 5 (bank link) is not implemented yet")
