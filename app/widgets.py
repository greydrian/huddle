"""
Widget registry: every dashboard widget's template (under
app/templates/widgets/) and the loader that builds its template context.

The dashboard runs the loader of every widget it shows today (hidden and
school-days-only-on-a-day-off widgets are skipped, app/services/layout.py)
and merges the results, so each loader must return exactly the variable
names the widget's template and its own /widgets/... route use. Adding a
widget means adding it here and to database.DEFAULT_LAYOUT. (The Photos
placeholder was removed in migration 5, spec 10.2.)
"""

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from app import google_calendar
from app.services import homework, shopping, tasks, weather
from app.services.meals import get_week_meal_plan


class Widget(NamedTuple):
    template: str
    load: Callable[..., Awaitable[dict]]
    label: str  # its name in Admin → Display → Widgets


async def _shopping(db) -> dict:
    return {"items": await shopping.get_shopping_items(db)}


async def _meals(db) -> dict:
    return {"days": await get_week_meal_plan(db)}


async def _calendar(db) -> dict:
    return {"view": "month", "calendar_month": await google_calendar.get_month_grid(db)}


async def _weather(db) -> dict:
    return {"weather": await weather.get_weather(db)}


async def _homework(db) -> dict:
    return {"homework_groups": await homework.get_homework_groups(db)}


WIDGETS: dict[str, Widget] = {
    "calendar": Widget("calendar.html", _calendar, "Calendar"),
    "tasks": Widget("tasks.html", tasks.widget_context, "Today's tasks"),
    "shopping": Widget("shopping.html", _shopping, "Shopping list"),
    "meals": Widget("meals.html", _meals, "Meal plan"),
    "weather": Widget("weather.html", _weather, "Weather"),
    "homework": Widget("homework.html", _homework, "Homework"),
    "practice_words": Widget("practice_words.html", homework.practice_context, "Practice words"),
}
