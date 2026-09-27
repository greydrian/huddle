"""
Home screen (Section 4.1): the Gridstack dashboard that hosts every widget.
Which widgets exist, their templates and their data all come from the
registry in app/widgets.py.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.appearance import current_mode
from app.database import family_today, get_db
from app.templating import templates
from app.widgets import WIDGETS

router = APIRouter()


@router.get("/health")
async def health():
    """Used by Docker's HEALTHCHECK — deliberately doesn't touch the DB, so it
    still reports honestly even if SQLite is briefly locked mid-write."""
    return {"status": "ok"}


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
