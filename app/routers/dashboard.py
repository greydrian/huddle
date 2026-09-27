"""
Home screen (Section 4.1): the Gridstack dashboard that hosts every widget.
Real widgets (tasks, shopping, meals) render live data; calendar, weather,
photos, and homework are stubbed pending their respective integrations.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import google_oauth
from app.database import family_today, get_db
from app.routers.meals import get_week_meal_plan
from app.routers.shopping import get_shopping_items
from app.routers.tasks import get_profiles_with_tasks
from app.templating import templates

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
        layout = [dict(row) for row in layout_rows]

        profiles = await get_profiles_with_tasks(db)
        shopping_items = await get_shopping_items(db)
        meal_days = await get_week_meal_plan(db)
        calendar_month = await google_oauth.get_month_grid(db)
        today = await family_today(db)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "layout": layout,
            "profiles": profiles,
            "shopping_items": shopping_items,
            "meal_days": meal_days,
            "view": "month",
            "calendar_month": calendar_month,
            "today": today.isoformat(),
        },
    )
