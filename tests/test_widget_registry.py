import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import database
from app.services import weather
from app.widgets import WIDGETS

WIDGET_TEMPLATES = Path(database.__file__).parent / "templates" / "widgets"

# widget route -> a string only the seeded data can put there. If a registry
# loader returns a context key its template doesn't read, the widget renders
# empty on the dashboard and its marker goes missing.
MARKERS = {
    "/widgets/tasks": "Feed the axolotl",
    "/widgets/shopping": "Quince paste",
    "/widgets/meals": "Moussaka night",
    "/widgets/homework": "Volcano diorama",
    "/widgets/practice-words": "xylophonic",
    "/widgets/weather": "Zanzibarville",
}


def test_every_default_layout_widget_is_registered():
    missing = [widget_id for widget_id, *_ in database.DEFAULT_LAYOUT if widget_id not in WIDGETS]
    assert missing == []


def test_every_registered_template_exists():
    missing = [w.template for w in WIDGETS.values() if not (WIDGET_TEMPLATES / w.template).is_file()]
    assert missing == []


@pytest.fixture
async def seeded(db):
    today = (await database.family_today(db)).isoformat()
    await db.execute("INSERT INTO tasks (profile_id, title) VALUES (1, ?)", (MARKERS["/widgets/tasks"],))
    await db.execute("INSERT INTO shopping_items (title) VALUES (?)", (MARKERS["/widgets/shopping"],))
    await db.execute(
        "INSERT INTO meal_plans (date, meal_description) VALUES (?, ?)", (today, MARKERS["/widgets/meals"])
    )
    await db.execute("INSERT INTO homework (profile_id, title) VALUES (1, ?)", (MARKERS["/widgets/homework"],))
    await db.execute(
        "INSERT INTO practice_word_lists (profile_id, title, words) VALUES (1, 'Spellings', ?)",
        (MARKERS["/widgets/practice-words"],),
    )
    # A fresh cache, so the weather widget renders without a network call.
    location = {"name": MARKERS["/widgets/weather"], "country": "", "latitude": 1.0, "longitude": 2.0}
    await weather.set_location(db, location)
    await database.set_setting(db, weather.CACHE_SETTING, json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "latitude": 1.0,
        "longitude": 2.0,
        "forecast": {
            "utc_offset_seconds": 0,
            "current": {"temperature": 12.0, "weather_code": 3},
            "daily": [{"date": today, "weather_code": 3, "max": 14.0, "min": 6.0}],
        },
    }))
    await db.commit()


@pytest.mark.parametrize("route", MARKERS)
async def test_widget_data_reaches_dashboard_and_own_route(seeded, client, route):
    marker = MARKERS[route]
    assert marker in (await client.get(route)).text
    assert marker in (await client.get("/")).text


async def test_dashboard_renders_every_registered_widget(client):
    html = (await client.get("/")).text
    for widget_id, *_ in database.DEFAULT_LAYOUT:
        assert f'gs-id="{widget_id}"' in html
    assert 'id="widget-tasks"' in html and 'id="widget-calendar"' in html
