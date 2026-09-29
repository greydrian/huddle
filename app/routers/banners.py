"""
Notification banner (spec 10.1, app/services/banners.py): the bar's own
fragment, re-fetched by static/js/fresh.js when /api/rev's "banners"
revision changes, and the PIN-free tap-to-dismiss (anyone at the wall).
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import banners
from app.templating import templates

router = APIRouter()


async def _render(request: Request, db) -> HTMLResponse:
    return templates.TemplateResponse(request, "_banners.html", await banners.context(db))


@router.get("/banners", response_class=HTMLResponse)
async def banner_bar(request: Request):
    async with get_db() as db:
        return await _render(request, db)


@router.post("/api/banners/dismiss", response_class=HTMLResponse)
async def dismiss_banner(request: Request, key: str = Form("")):
    """Hides that occurrence (an unknown key is ignored) and re-renders the bar."""
    async with get_db() as db:
        await banners.dismiss(db, key)
        return await _render(request, db)
