"""
Widget registry: every dashboard widget's template (under
app/templates/widgets/) and the loader that builds its template context.

The dashboard runs every loader and merges the results, so each loader
must return exactly the variable names the widget's template and its own
/widgets/... route use. Adding a widget means adding it here and to
database.DEFAULT_LAYOUT.
"""

from collections.abc import Awaitable, Callable
from typing import NamedTuple

from app import google_calendar
from app.services import homework, shopping, tasks, weather
from app.services.meals import get_week_meal_plan


class Widget(NamedTuple):
    template: str
    load: Callable[..., Awaitable[dict]]


async def _tasks(db) -> dict:
    return {"profiles": await tasks.get_profiles_with_tasks(db)}


async def _shopping(db) -> dict:
    return {"items": await shopping.get_shopping_items(db)}


async def _meals(db) -> dict:
    return {"days": await get_week_meal_plan(db)}


async def _calendar(db) -> dict:
    return {"view": "month", "calendar_month": await google_calendar.get_month_grid(db)}


async def _weather(db) -> dict:
    return {"weather": await weather.get_weather(db)}


async def _photos(db) -> dict:
    return {}


async def _homework(db) -> dict:
    return {"homework_groups": await homework.get_homework_groups(db)}


WIDGETS: dict[str, Widget] = {
    "tasks": Widget("tasks.html", _tasks),
    "shopping": Widget("shopping.html", _shopping),
    "meals": Widget("meals.html", _meals),
    "calendar": Widget("calendar.html", _calendar),
    "weather": Widget("weather.html", _weather),
    "photos": Widget("photos_stub.html", _photos),
    "homework": Widget("homework.html", _homework),
    "practice_words": Widget("practice_words.html", homework.practice_context),
}
