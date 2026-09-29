"""Adding an event from the wall (spec 10.5, services/calendar_add.py): only
into the Family calendar, "Name: Title" for one person, one event per
double tap, a rate limit, validation, and the scope / setup gates."""

import asyncio
import json
import re
import time
from datetime import timedelta

import httpx
import pytest

from app import database, google_oauth
from app.services import calendar_add, calendar_prefs

EVENTS_URL_PATTERN = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"
FAMILY_INSERT = "https://www.googleapis.com/calendar/v3/calendars/family%40group.calendar.google.com/events"
CALENDARS = [
    {"id": "family@group.calendar.google.com", "summary": "Family", "color": "#123456"},
    {"id": "mum@example.com", "summary": "Mum", "color": "#654321"},
]
KEY = "tap-0123456789abcdef"


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    calendar_add.reset_rate_limit()
    yield
    calendar_add.reset_rate_limit()


@pytest.fixture
async def with_scope(db):
    await google_oauth.store_tokens(db, {
        "access_token": "tok", "refresh_token": "r", "expires_at": time.time() + 3600,
        "scope": f"{google_oauth.CALENDAR_READ_SCOPE} {google_oauth.CALENDAR_EVENTS_SCOPE}",
    }, "family@example.com")


@pytest.fixture
async def family(db, with_scope):
    await google_oauth.set_selected_calendars(db, CALENDARS)
    await calendar_prefs.set_family_calendar(db, {"id": CALENDARS[0]["id"], "summary": "Family"})


@pytest.fixture
def gcal(google):
    """Every events.list answers empty; events.insert on the Family calendar
    echoes the event back. Any other POST is unmocked and fails the test."""
    google.get(url__regex=EVENTS_URL_PATTERN).respond(200, json={"items": []})
    insert = google.post(FAMILY_INSERT, name="insert").mock(
        side_effect=lambda request: httpx.Response(200, json=json.loads(request.content))
    )
    return insert


async def _today(db):
    return await database.family_today(db)


async def _riley(db) -> int:
    return (await (await db.execute("SELECT id FROM profiles WHERE name = 'Riley'")).fetchone())["id"]


async def test_adds_to_the_family_calendar_only(db, family, gcal, google):
    today = await _today(db)
    added = await calendar_add.add_family_event(db, title="Bins out", day=today.isoformat(), request_key=KEY)

    assert gcal.call_count == 1
    assert all(call.request.method == "GET" or call.request.url == FAMILY_INSERT for call in google.calls)
    body = json.loads(gcal.calls.last.request.content)
    assert body["summary"] == "Bins out"
    assert body["start"] == {"date": today.isoformat()}
    assert body["end"] == {"date": (today + timedelta(days=1)).isoformat()}  # exclusive
    assert body["id"] == calendar_add.event_id(KEY, CALENDARS[0]["id"])
    assert re.fullmatch(r"[0-9a-v]{5,1024}", body["id"])
    assert added == {"id": body["id"], "summary": "Bins out", "date": today.isoformat(), "calendar": "Family",
                     "duplicate": False}


async def test_one_person_is_saved_as_name_colon_title(db, family, gcal):
    today = await _today(db)
    await calendar_add.add_family_event(db, title="Dentist", day=today.isoformat(), start_time="16:30",
                                        person_id=await _riley(db), request_key=KEY)

    body = json.loads(gcal.calls.last.request.content)
    assert body["summary"] == "Riley: Dentist"
    assert body["start"]["dateTime"].startswith(f"{today.isoformat()}T16:30:00")
    assert body["start"]["timeZone"] == "Europe/London"
    assert body["end"]["dateTime"].startswith(f"{today.isoformat()}T17:30:00")  # an hour by default
    # Already prefixed: not doubled.
    assert calendar_add.summary_for("Riley: Swim", {"id": 1, "name": "Riley"}) == "Riley: Swim"
    assert calendar_add.summary_for("Swim", {"id": 1, "name": "Riley Jones"}) == "Riley: Swim"


async def test_explicit_end_time(db, family, gcal):
    today = await _today(db)
    await calendar_add.add_family_event(db, title="Party", day=today.isoformat(), start_time="14:00",
                                        end_time="16:00", request_key=KEY)
    body = json.loads(gcal.calls.last.request.content)
    assert body["end"]["dateTime"].startswith(f"{today.isoformat()}T16:00:00")


async def test_double_tap_creates_one_event(db, family, gcal):
    today = (await _today(db)).isoformat()
    first = await calendar_add.add_family_event(db, title="Bins out", day=today, request_key=KEY)
    again = await calendar_add.add_family_event(db, title="Bins out", day=today, request_key=KEY)

    assert gcal.call_count == 1
    assert again["duplicate"] is True and again["id"] == first["id"]


async def test_two_taps_at_once_create_one_event(db, family, gcal):
    today = (await _today(db)).isoformat()
    release = asyncio.Event()

    async def slow(request):
        await release.wait()
        return httpx.Response(200, json=json.loads(request.content))
    gcal.mock(side_effect=slow)

    async with database.get_db() as other:
        first = asyncio.create_task(calendar_add.add_family_event(db, title="X", day=today, request_key=KEY))
        await asyncio.sleep(0.05)
        with pytest.raises(calendar_add.AddEventError) as exc:
            await calendar_add.add_family_event(other, title="X", day=today, request_key=KEY)
        assert exc.value.code == "busy"
        release.set()
        await first
    assert gcal.call_count == 1


async def test_a_lost_response_retried_is_not_a_second_event(db, family, gcal):
    """Google made it, but the answer never came: the same form again gets 409."""
    today = (await _today(db)).isoformat()
    gcal.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=today, request_key=KEY)
    assert exc.value.code == "offline"

    gcal.mock(side_effect=None, return_value=httpx.Response(409, json={"error": {"code": 409}}))
    added = await calendar_add.add_family_event(db, title="X", day=today, request_key=KEY)
    assert added["id"] == calendar_add.event_id(KEY, CALENDARS[0]["id"]) and not added["duplicate"]


@pytest.mark.parametrize(("status", "code"), [(403, "scope"), (404, "missing"), (400, "failed"), (503, "offline")])
async def test_google_refusals_release_the_claim(db, family, gcal, status, code):
    today = (await _today(db)).isoformat()
    gcal.mock(side_effect=None, return_value=httpx.Response(status))
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=today, request_key=KEY)
    assert exc.value.code == code
    claims = json.loads(await database.get_setting(db, calendar_add.CLAIMS_KEY))
    assert KEY not in claims  # the same form can be sent again


async def test_rate_limit(db, family, gcal, monkeypatch):
    monkeypatch.setattr(calendar_add, "RATE_LIMIT", 3)
    today = (await _today(db)).isoformat()
    for n in range(3):
        await calendar_add.add_family_event(db, title=f"E{n}", day=today, request_key=f"{KEY}-{n}")
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="E4", day=today, request_key=f"{KEY}-4")
    assert exc.value.code == "rate"
    assert gcal.call_count == 3
    assert calendar_add.RATE_WINDOW == 3600


def test_the_default_rate_limit_is_twenty_an_hour():
    assert (calendar_add.RATE_LIMIT, calendar_add.RATE_WINDOW) == (20, 3600)


async def test_a_duplicate_tap_does_not_use_up_the_rate_limit(db, family, gcal, monkeypatch):
    monkeypatch.setattr(calendar_add, "RATE_LIMIT", 1)
    today = (await _today(db)).isoformat()
    await calendar_add.add_family_event(db, title="E", day=today, request_key=KEY)
    assert (await calendar_add.add_family_event(db, title="E", day=today, request_key=KEY))["duplicate"]


@pytest.mark.parametrize(("fields", "code"), [
    ({"title": "  "}, "title"),
    ({"title": "x" * 101}, "title"),
    ({"day": "yesterday"}, "day"),
    ({"day": "PAST"}, "day"),
    ({"day": "FAR"}, "day"),
    ({"start_time": "25:00"}, "time"),
    ({"start_time": "10:00", "end_time": "09:30"}, "end"),
    ({"person_id": "999"}, "person"),
    ({"person_id": "abc"}, "person"),
    ({"request_key": "short"}, "request"),
])
async def test_validation(db, family, gcal, fields, code):
    today = await _today(db)
    args = {"title": "Dentist", "day": today.isoformat(), "request_key": KEY, **fields}
    if args["day"] == "PAST":
        args["day"] = (today - timedelta(days=1)).isoformat()
    elif args["day"] == "FAR":
        args["day"] = (today + timedelta(days=calendar_add.MAX_DAYS_AHEAD + 1)).isoformat()
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, **args)
    assert exc.value.code == code
    assert gcal.call_count == 0


async def test_missing_scope_is_refused_before_google(db, gcal, connected):
    await google_oauth.set_selected_calendars(db, CALENDARS)
    await calendar_prefs.set_family_calendar(db, {"id": CALENDARS[0]["id"], "summary": "Family"})
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=(await _today(db)).isoformat(), request_key=KEY)
    assert exc.value.code == "scope" and "reconnect" in exc.value.message.lower()
    assert gcal.call_count == 0


async def test_no_family_calendar_is_refused(db, gcal, with_scope):
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=(await _today(db)).isoformat(), request_key=KEY)
    assert exc.value.code == "no-calendar"
    assert gcal.call_count == 0


async def test_adding_refreshes_the_cache(db, family, gcal, google):
    today = await _today(db)
    await calendar_add.add_family_event(db, title="X", day=today.isoformat(), request_key=KEY)
    rows = (await (await db.execute("SELECT COUNT(*) FROM calendar_cache")).fetchone())[0]
    assert rows == len(CALENDARS)  # refetched straight away


# --- The widget ---------------------------------------------------------------------------

def _form(today, **extra):
    return {"title": "Dentist", "day": today.isoformat(), "start_time": "", "end_time": "", "person_id": "",
            "request_key": KEY, "view": "week", **extra}


async def test_plus_shows_only_when_adding_is_set_up(client, db, gcal, with_scope):
    await google_oauth.set_selected_calendars(db, CALENDARS)
    assert "cal-add-toggle" not in (await client.get("/widgets/calendar")).text  # no Family calendar

    await calendar_prefs.set_family_calendar(db, {"id": CALENDARS[0]["id"], "summary": "Family"})
    html = (await client.get("/widgets/calendar")).text
    assert "cal-add-toggle" in html and "Add to Family" in html
    # Outside the drag handle, like every other control.
    header = html.split('class="widget-heading drag-handle"')[1].split("</div>")[0]
    assert "cal-add-toggle" not in header and "cal-view-btn" not in header


async def test_plus_is_hidden_without_the_scope(client, db, gcal, connected):
    await google_oauth.set_selected_calendars(db, CALENDARS)
    await calendar_prefs.set_family_calendar(db, {"id": CALENDARS[0]["id"], "summary": "Family"})
    assert "cal-add-toggle" not in (await client.get("/widgets/calendar")).text


async def test_widget_add_renders_a_note_and_keeps_the_view(client, db, family, gcal):
    today = await _today(db)
    riley = await _riley(db)
    resp = await client.post("/widgets/calendar/events", data=_form(today, person_id=str(riley)))

    assert resp.status_code == 200
    assert "Added “Riley: Dentist” to Family." in resp.text
    assert 'data-view="week"' in resp.text
    assert 'id="cal-add-form"' in resp.text and re.search(r'id="cal-add-form"[^>]*\shidden', resp.text)
    assert KEY not in resp.text  # a new form, a new key


async def test_widget_add_error_keeps_what_was_typed(client, db, family, gcal):
    today = await _today(db)
    resp = await client.post("/widgets/calendar/events", data=_form(today, start_time="10:00", end_time="09:00"))

    assert resp.status_code == 200
    assert calendar_add.ERRORS["end"] in resp.text
    assert 'value="Dentist"' in resp.text and f'value="{KEY}"' in resp.text
    assert not re.search(r'id="cal-add-form"[^>]*\shidden', resp.text)
    assert gcal.call_count == 0


async def test_widget_add_is_refused_from_another_origin(client, db, family, gcal):
    today = await _today(db)
    resp = await client.post("/widgets/calendar/events", data=_form(today),
                             headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert gcal.call_count == 0


async def test_form_uses_the_on_screen_keyboard(client, db, family, gcal, monkeypatch):
    monkeypatch.setattr(database, "onscreen_keyboard_enabled", True)
    html = (await client.get("/widgets/calendar")).text
    assert re.search(r'<input type="text" name="title" class="cal-add-title"[^>]*data-osk="text"', html)
