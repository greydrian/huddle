"""The "next up" strip's own fragment (app/services/next_up.py), re-fetched
by static/js/fresh.js when /api/rev's "next_up" revision changes."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import next_up
from app.templating import templates

router = APIRouter()


@router.get("/next-up", response_class=HTMLResponse)
async def next_up_strip(request: Request):
    async with get_db() as db:
        return templates.TemplateResponse(request, "_next_up.html", await next_up.context(db))
