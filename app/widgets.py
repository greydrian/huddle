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

from app.services import calendar_view, homework, meals, shopping, tasks, weather


class Widget(NamedTuple):
    template: str
    load: Callable[..., Awaitable[dict]]
    label: str  # its name in Admin → Display → Widgets


async def _shopping(db) -> dict:
    return await shopping.widget_context(db)


async def _meals(db) -> dict:
    return await meals.widget_context(db)


async def _calendar(db) -> dict:
    return await calendar_view.widget_context(db)


async def _weather(db) -> dict:
    return {"weather": await weather.get_weather(db)}


WIDGETS: dict[str, Widget] = {
    "calendar": Widget("calendar.html", _calendar, "Calendar"),
    "tasks": Widget("tasks.html", tasks.widget_context, "Today's tasks"),
    "shopping": Widget("shopping.html", _shopping, "Shopping list"),
    "meals": Widget("meals.html", _meals, "Meal plan"),
    "weather": Widget("weather.html", _weather, "Weather"),
    "homework": Widget("homework.html", homework.homework_context, "Homework"),
    "practice_words": Widget("practice_words.html", homework.practice_context, "Practice words"),
}
