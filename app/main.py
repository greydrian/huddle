"""
Family Display — FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

This runs headless on the GMKtec G10 (Docker host); the display is a
wall-mounted Samsung Galaxy Tab A9+ running Fully Kiosk Browser, pointed
at the G10's LAN address over Wi-Fi (see README's kiosk setup section).
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # local dev convenience — reads .env if present, before any
                # GOOGLE_CLIENT_ID/SECRET env reads happen at import time below

# One-time app log setup so module loggers (Google offline, sync skips,
# token revocation) show in `docker compose logs`. uvicorn configures only
# its own loggers, so without this our WARNINGs would be the bare lastResort.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO: noise once a minute, and the token
# revoke URL carries the refresh token in its query string — keep it off.
logging.getLogger("httpx").setLevel(logging.WARNING)
# The Anthropic SDK's client (school inbox) is httpx2; same rule.
logging.getLogger("httpx2").setLevel(logging.WARNING)
# aiosqlite's DEBUG log prints every SQL parameter (school documents included).
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
# APScheduler logs "Running job…"/"executed successfully" every minute at INFO.
logging.getLogger("apscheduler").setLevel(logging.WARNING)

SENSITIVE_QUERY_PATHS = ("/admin/google/callback",)


class RedactOAuthQuery(logging.Filter):
    """uvicorn's access log prints the full path, which for the OAuth
    callback includes the one-time ?code=…&state=…. Drop the query string
    for those paths. uvicorn.access args: (client, method, path, http_version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path, sep, _query = args[2].partition("?")
            if sep and path in SENSITIVE_QUERY_PATHS:
                record.args = (*args[:2], path, *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(RedactOAuthQuery())

from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import appearance, freshness, scheduler
from app.database import get_db, init_db
from app.routers import (
    admin,
    banners,
    calendar,
    dashboard,
    homework,
    layout,
    meals,
    pen_test,
    shopping,
    sync,
    tasks,
    weather,
)
from app.services import imports
from app.upload_guard import UploadGuard

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with get_db() as db:
        # Documents being read when the app stopped are gone from memory.
        await imports.fail_interrupted(db)
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Family Display", lifespan=lifespan)
# Admin uploads: session + size checked before the multipart body is read.
app.add_middleware(UploadGuard)


@app.middleware("http")
async def reject_cross_site_writes(request: Request, call_next):
    """The kiosk routes (shopping, tasks, meals, layout) are deliberately
    PIN-free, so any web page open on a LAN device could otherwise POST to
    them. Browsers always send Origin on cross-site POSTs; refuse any write
    whose Origin isn't this app. Requests with no Origin (curl, tests) pass."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        # "null" (sandboxed iframe / opaque origin) has no netloc, so it's refused too.
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-site request refused", status_code=403)
    return await call_next(request)


class RevalidatedStaticFiles(StaticFiles):
    """Static URLs aren't versioned, so without a Cache-Control header browsers
    (and Fully Kiosk) heuristically reuse a stale style.css/keyboard.js for
    days after a deploy. no-cache makes them revalidate every load; unchanged
    files still come back as a cheap 304 via the ETag."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatedStaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(appearance.router)
app.include_router(freshness.router)
app.include_router(tasks.router)
app.include_router(shopping.router)
app.include_router(meals.router)
app.include_router(layout.router)
app.include_router(admin.router)
app.include_router(calendar.router)
app.include_router(weather.router)
app.include_router(homework.router)
app.include_router(sync.router)
app.include_router(banners.router)
app.include_router(pen_test.router)
