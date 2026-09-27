"""
Family Display — FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

This runs headless on the GMKtec G10 (Docker host); the display is a
wall-mounted Samsung Galaxy Tab A9+ running Fully Kiosk Browser, pointed
at the G10's LAN address over Wi-Fi (see README's kiosk setup section).
"""

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # local dev convenience — reads .env if present, before any
                # GOOGLE_CLIENT_ID/SECRET env reads happen at import time below

from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import appearance, scheduler
from app.database import init_db
from app.routers import admin, calendar, dashboard, homework, layout, meals, shopping, tasks, weather

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Family Display", lifespan=lifespan)


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
app.include_router(tasks.router)
app.include_router(shopping.router)
app.include_router(meals.router)
app.include_router(layout.router)
app.include_router(admin.router)
app.include_router(calendar.router)
app.include_router(weather.router)
app.include_router(homework.router)
