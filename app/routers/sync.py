"""
Sync health (see app/sync_status.py): the dashboard's top-bar status dot,
which re-fetches itself every minute, and Admin's "Sync now" button. The
Sync panel itself is part of the Admin page (admin/_sync_panel.html).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import sync_status, task_sync
from app.auth import require_admin
from app.database import get_db
from app.templating import templates

router = APIRouter()


@router.get("/sync-status", response_class=HTMLResponse)
async def sync_dot(request: Request):
    async with get_db() as db:
        sync = await sync_status.summary(db)
    return templates.TemplateResponse(request, "_sync_dot.html", {"sync": sync})


@router.post("/admin/sync", dependencies=[Depends(require_admin)])
async def sync_now():
    """Runs one cycle now. If the scheduler's cycle is mid-run this doesn't
    start a second one (run_sync's lock) — Admin says so instead."""
    async with get_db() as db:
        ran = await task_sync.run_sync(db)
    return RedirectResponse(url=f"/admin?sync={'done' if ran else 'busy'}#sync", status_code=303)
