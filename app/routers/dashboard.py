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

from app import idle, scheduler, sync_status
from app.appearance import current_mode
from app.database import get_db
from app.freshness import BANNERS, today_info, widget_revisions
from app.services import banners, layout
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
        # Only the widgets shown today, where they're shown (spec 10.3):
        # hidden ones and "school days only" ones on a day off are left out,
        # and so are their loaders.
        shown = await layout.shown_layout(db)
        # Widget templates are included into dashboard.html and inherit its
        # context, so each loader's names must match its template's.
        contexts = {row["widget_id"]: await WIDGETS[row["widget_id"]].load(db) for row in shown}
        context = {name: value for widget_context in contexts.values() for name, value in widget_context.items()}
        today = await today_info(db)
        banner_context = await banners.context(db)
        widget_revs = widget_revisions({**contexts, BANNERS: banner_context})
        layout_generation = await layout.generation(db, list(contexts))
        appearance = await current_mode(db)
        idle_screen = await idle.context(db)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **context,
            **banner_context,
            "layout": shown,
            "layout_generation": layout_generation,
            "widget_templates": {widget_id: widget.template for widget_id, widget in WIDGETS.items()},
            "today": today["date"],
            "today_next_change": today["next_change_in"],
            "widget_revs": widget_revs,
            "appearance": appearance,
            "idle": idle_screen,
        },
    )
