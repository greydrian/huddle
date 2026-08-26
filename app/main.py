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

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.routers import dashboard, tasks, shopping, meals, layout, admin

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Family Display", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(tasks.router)
app.include_router(shopping.router)
app.include_router(meals.router)
app.include_router(layout.router)
app.include_router(admin.router)
