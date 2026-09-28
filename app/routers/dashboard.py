"""
Home screen (Section 4.1): the Gridstack dashboard that hosts every widget.
Which widgets exist, their templates and their data all come from the
registry in app/widgets.py.
"""

import asyncio
import logging
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import scheduler, sync_status
from app.appearance import current_mode
from app.database import family_today, get_db
from app.templating import templates
from app.widgets import WIDGETS

# Under the HEALTHCHECK's own 3s timeout, so a hung DB answers 503 rather
# than timing out the probe.
HEALTH_DB_TIMEOUT_SECONDS = 2

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health():
    """Used by Docker's HEALTHCHECK (any 2xx passes), so it answers 200
    whenever the app and its database work — a Google outage or broken sync
    must never make the container "unhealthy"; that's reported in the body.
    Only a database that can't be read at all gives 503. WAL readers never
    wait on a writer, so a busy SQLite doesn't trip it."""
    body = {"status": "ok", "db": "ok", "scheduler": "running" if scheduler.scheduler.running else "stopped"}
    try:
        async with asyncio.timeout(HEALTH_DB_TIMEOUT_SECONDS), get_db() as db:
            await (await db.execute("SELECT 1")).fetchone()
            try:
                body["sync"] = sync_status.health_summary(await sync_status.summary(db))
            except Exception as exc:  # detail only: never fail the healthcheck over it
                logger.warning("/health: sync status unavailable: %s", type(exc).__name__)
                body["sync"] = {"state": "unknown"}
    except (sqlite3.Error, TimeoutError, OSError) as exc:
        logger.warning("/health: database check failed: %s", type(exc).__name__)
        return JSONResponse({**body, "status": "error", "db": "error"}, status_code=503)
    return body


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    async with get_db() as db:
        layout_rows = await (await db.execute(
            "SELECT * FROM layout_state WHERE is_visible = 1"
        )).fetchall()
        # Widget templates are included into dashboard.html and inherit its
        # context, so each loader's names must match its template's.
        context = {}
        for widget in WIDGETS.values():
            context.update(await widget.load(db))
        today = await family_today(db)
        appearance = await current_mode(db)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **context,
            "layout": [dict(row) for row in layout_rows],
            "widget_templates": {widget_id: widget.template for widget_id, widget in WIDGETS.items()},
            "today": today.isoformat(),
            "appearance": appearance,
        },
    )
