"""
Home screen (Section 4.1): the Gridstack dashboard that hosts every widget.
Real widgets (tasks, shopping, meals, calendar, weather, homework, practice
words) render live data; photos is stubbed pending its integration.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import google_oauth
from app.database import family_today, get_db
from app.routers.homework import get_handwriting_style, get_homework_groups, get_practice_lists
from app.routers.meals import get_week_meal_plan
from app.routers.shopping import get_shopping_items
from app.routers.tasks import get_profiles_with_tasks
from app.routers.weather import get_weather
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
        weather = await get_weather(db)
        today = await family_today(db)
        homework_groups = await get_homework_groups(db)
        practice_lists = await get_practice_lists(db)
        handwriting_style = await get_handwriting_style(db)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "layout": layout,
            "profiles": profiles,
            "items": shopping_items,  # names must match widgets/shopping.html
            "days": meal_days,  # names must match widgets/meals.html
            "view": "month",
            "calendar_month": calendar_month,
            "weather": weather,
            "today": today.isoformat(),
            "homework_groups": homework_groups,  # names must match widgets/homework.html
            "practice_lists": practice_lists,  # and widgets/practice_words.html
            "handwriting_style": handwriting_style,
        },
    )
