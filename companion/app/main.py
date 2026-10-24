"""FastAPI app: auth, error mapping, startup bootstrap, daily alert scheduler, phone UI."""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import alerts
from .bugs import install_log_buffer
from .claude import ClaudeClient, ClaudeError
from .config import Settings, get_settings
from .db import Database
from .deps import Services
from .grocy import GrocyClient, GrocyError
from .products import CaptureError
from .routes import COOKIE, router
from .setup import Catalog, run_setup
from .units import UnitError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
install_log_buffer()
log = logging.getLogger("companion")

STATIC = Path(__file__).resolve().parent.parent / "static"
PUBLIC_PATHS = {"/", "/login", "/health", "/manifest.webmanifest", "/sw.js", "/favicon.ico"}


async def _bootstrap(svc: Services) -> None:
    """Run /setup at startup; Grocy may still be booting, so retry for a while."""
    for attempt in range(20):
        try:
            report = await run_setup(svc.grocy, svc.catalog)
            log.info("setup done: %s", report)
            return
        except (GrocyError, httpx.HTTPError) as e:
            log.warning("setup attempt %d failed: %s", attempt + 1, e)
        except Exception:
            log.exception("setup attempt %d failed unexpectedly", attempt + 1)
        await asyncio.sleep(min(5 * (attempt + 1), 60))
    log.error("setup gave up; call POST /setup once Grocy is reachable")


def build_services(settings: Settings) -> Services:
    db = Database(settings.db_path)
    return Services(
        settings=settings,
        grocy=GrocyClient(settings.grocy_api, settings.grocy_api_key),
        claude=ClaudeClient(settings, db),
        db=db,
        catalog=Catalog(),
        http=httpx.AsyncClient(timeout=30.0, follow_redirects=True),
    )


def create_app(services: Services | None = None, *, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = services or build_services(get_settings())
        app.state.services = svc
        scheduler: AsyncIOScheduler | None = None
        task: asyncio.Task[None] | None = None
        if start_background:
            if not svc.settings.companion_token:
                log.error("COMPANION_TOKEN is not set: every API call will be refused")
            task = asyncio.create_task(_bootstrap(svc))
            scheduler = AsyncIOScheduler(timezone=svc.settings.tz)
            scheduler.add_job(
                alerts.send,
                CronTrigger(hour=svc.settings.alert_hour, minute=0, timezone=svc.settings.tz),
                args=[svc],
                id="daily_alert",
                misfire_grace_time=3600,
                coalesce=True,
            )
            scheduler.start()
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await svc.grocy.aclose()
            await svc.http.aclose()

    app = FastAPI(title="Grocy+ companion", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def auth(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        expected = request.app.state.services.settings.companion_token
        if not expected:
            return JSONResponse({"detail": "COMPANION_TOKEN is not configured"}, status_code=503)
        auth_header = request.headers.get("authorization", "")
        supplied = (
            request.headers.get("x-companion-token")
            or (auth_header[7:] if auth_header.lower().startswith("bearer ") else None)
            or request.cookies.get(COOKIE)
            or ""
        )
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(GrocyError)
    async def grocy_error(_: Request, exc: GrocyError) -> JSONResponse:
        status = 404 if exc.status == 404 else 502
        return JSONResponse({"detail": f"Grocy: {exc.message}", "grocy_status": exc.status}, status_code=status)

    @app.exception_handler(ClaudeError)
    async def claude_error(_: Request, exc: ClaudeError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(CaptureError)
    async def capture_error(_: Request, exc: CaptureError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(UnitError)
    async def unit_error(_: Request, exc: UnitError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"detail": f"Internal error: {type(exc).__name__}: {exc}"}, status_code=500)

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        return FileResponse(STATIC / "sw.js", media_type="text/javascript")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
