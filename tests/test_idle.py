"""The idle screen's server side (spec 10.2, app/idle.py): the Admin
settings, /api/idle (family clock, next event from calendar_cache, weather
from its cache, this week's photos) and the dashboard wiring. The browser
behaviour is in tests/e2e/test_idle.py."""

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import calendar_cache, google_calendar, google_oauth, google_photos, idle
from app.admin_tabs import admin_url
from app.routers import admin
from app.security import create_session_token
from app.services import weather

TZ = ZoneInfo("Europe/London")
DAY = date(2026, 10, 5)
CAL = {"id": "family@example.com", "summary": "Family", "color": "#3D6E93"}
LOCATION = {"name": "Reading", "country": "United Kingdom", "latitude": 51.45, "longitude": -0.97}


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


def at(hour, minute=0):
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=TZ)


def timed(summary, start, event_id):
    begins = f"{DAY.isoformat()}T{start}:00+01:00"
    return {"id": event_id, "summary": summary, "start": {"dateTime": begins}, "end": {"dateTime": begins}}


@pytest.fixture
async def events(db, connected):
    await google_oauth.set_selected_calendars(db, [CAL])
    _, selection = await google_calendar._selection(db)
    raws = [
        {"id": "a", "summary": "Inset day", "start": {"date": DAY.isoformat()}, "end": {"date": "2026-10-06"}},
        timed("Breakfast club", "07:30", "b"),
        timed("Swimming", "16:30", "c"),
        timed("Parents' evening", "18:00", "d"),
    ]
    formatted = [google_calendar._format_event(r) for r in raws]
    await calendar_cache.store(db, selection, date(2026, 9, 28), date(2026, 11, 9), {CAL["id"]: formatted})


# --- Settings ---

async def test_defaults(db):
    assert await idle.get_settings(db) == {
        "mode": "slideshow", "night_mode": "dim", "delay_minutes": 5, "night_start": "", "night_end": "",
        "dim_percent": 8, "interval_seconds": 20,
    }


async def test_admin_saves_the_settings(admin_client, db):
    form = {"mode": "dim", "night_mode": "dashboard", "delay_minutes": "10", "interval_seconds": "30",
            "dim_percent": "5", "night_start": "21:30", "night_end": "6:45"}
    resp = await admin_client.post("/admin/idle", data=form)
    assert resp.headers["location"] == admin_url("idle")
    assert await idle.get_settings(db) == {
        "mode": "dim", "night_mode": "dashboard", "delay_minutes": 10, "interval_seconds": 30,
        "dim_percent": 5, "night_start": "21:30", "night_end": "06:45",
    }
    html = (await admin_client.get("/admin?tab=display")).text
    assert 'name="night_start" value="21:30"' in html
    assert 'name="mode" value="dim" checked' in html and 'name="night_mode" value="dashboard" checked' in html
    assert "Fully PLUS is needed for real dimming" in html and "screen-off timer" in html


@pytest.mark.parametrize(("change", "code"), [
    ({"mode": "party"}, "idle-mode"),
    ({"night_mode": ""}, "idle-mode"),
    ({"delay_minutes": "0"}, "idle-numbers"),
    ({"delay_minutes": "5.5"}, "idle-numbers"),
    ({"interval_seconds": "1"}, "idle-numbers"),
    ({"dim_percent": "80"}, "idle-numbers"),
    ({"night_start": "21:00"}, "idle-night"),
    ({"night_start": "21:00", "night_end": "21:00"}, "idle-night"),
    ({"night_start": "25:00", "night_end": "07:00"}, "idle-night"),
])
async def test_bad_settings_change_nothing(admin_client, db, change, code):
    form = {"mode": "slideshow", "night_mode": "dim", "delay_minutes": "5", "interval_seconds": "20",
            "dim_percent": "8", **change}
    resp = await admin_client.post("/admin/idle", data=form)
    assert resp.headers["location"] == admin_url("idle", error=code)
    assert await idle.get_settings(db) == idle.DEFAULTS
    html = (await admin_client.get(resp.headers["location"].split("#")[0])).text
    assert admin.ADMIN_ERRORS[code][1].split("'")[0] in html


async def test_unreadable_saved_settings_fall_back_to_defaults(db):
    await db.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)",
                     (idle.SETTINGS_KEY, json.dumps({"mode": "x", "delay_minutes": 999, "night_start": "21:00"})))
    await db.commit()
    assert await idle.get_settings(db) == idle.DEFAULTS


# --- What the idle screen shows ---

async def test_next_event_is_the_next_timed_one_today(db, events):
    assert await idle.next_event(db, at(9)) == {"title": "Swimming", "time": "16:30"}
    assert (await idle.next_event(db, at(17)))["title"] == "Parents' evening"
    assert await idle.next_event(db, at(18, 30)) is None  # nothing left today (the all-day one never counts)


async def test_no_calendar_means_no_next_event(db):
    assert await idle.next_event(db, at(9)) is None


async def test_weather_comes_from_the_cache_only(db, google):
    assert await weather.cached_now(db) is None  # no location
    await weather.set_location(db, LOCATION)
    assert await weather.cached_now(db) is None  # no forecast yet: never fetched from here
    now = datetime.now(timezone.utc)
    forecast = {"utc_offset_seconds": 0, "current": {"temperature": 13.6, "weather_code": 61},
                "daily": [{"date": now.date().isoformat(), "weather_code": 61, "max": 15.0, "min": 8.0}]}
    await db.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (weather.CACHE_SETTING, json.dumps({
        "fetched_at": (now - timedelta(hours=3)).isoformat(), "latitude": LOCATION["latitude"],
        "longitude": LOCATION["longitude"], "forecast": forecast})))
    await db.commit()
    assert await weather.cached_now(db) == {"temperature": 14, "category": "rain", "condition": "Rain"}
    assert not google.calls


async def test_context_has_the_family_clock_and_this_weeks_photos(db, events):
    saved = [google_photos._save_image(google_photos_jpeg(i)) for i in range(3)]
    await google_photos.replace_set(db, saved)
    ids = [r["id"] for r in await google_photos.list_photos(db)]
    ctx = await idle.context(db, now=at(9, 5).astimezone(timezone.utc))
    assert ctx["now"] == "2026-10-05T09:05:00"  # family wall-clock time, not UTC
    assert ctx["next_event"]["title"] == "Swimming"
    assert ctx["photos"] == [f"/photos/{i}" for i in google_photos.weekly_order(ids, DAY)]
    assert ctx["mode"] == "slideshow"


def google_photos_jpeg(i):
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (40, 30), (i * 40, 90, 90)).save(out, "JPEG")
    return out.getvalue()


async def test_api_idle_is_pin_free_and_json(client):
    resp = await client.get("/api/idle")
    assert resp.status_code == 200
    body = resp.json()
    assert {"mode", "night_mode", "delay_minutes", "now", "next_event", "weather", "photos"} <= body.keys()


async def test_the_dashboard_carries_the_idle_screen(client):
    html = (await client.get("/")).text
    assert 'id="idle-screen"' in html and "/static/js/idle.js" in html
    config = html.split('data-config=\'')[1].split("'")[0]
    assert json.loads(config.replace("&#34;", '"').replace("&amp;", "&"))["mode"] == "slideshow"
