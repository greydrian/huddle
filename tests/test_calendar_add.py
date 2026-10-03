"""Adding an event from the wall (spec 10.5, services/calendar_add.py): only
into the Family calendar, "Name: Title" for one person, one event per
double tap, a rate limit, validation, and the scope / setup gates."""

import asyncio
import json
import re
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from markupsafe import escape

from app import database, google_accounts, google_oauth
from app.services import calendar_add, calendar_prefs

EVENTS_URL_PATTERN = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"
FAMILY_INSERT = "https://www.googleapis.com/calendar/v3/calendars/family%40group.calendar.google.com/events"
CALENDARS = [
    {"account_id": 1, "id": "family@group.calendar.google.com", "summary": "Family", "color": "#123456"},
    {"account_id": 1, "id": "mum@example.com", "summary": "Mum", "color": "#654321"},
]
KEY = "tap-0123456789abcdef"


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    calendar_add.reset_rate_limit()
    yield
    calendar_add.reset_rate_limit()


@pytest.fixture
async def with_scope(db):
    await google_oauth.store_tokens(
        db,
        {
            "access_token": "tok",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
            "scope": f"{google_oauth.CALENDAR_READ_SCOPE} {google_oauth.CALENDAR_EVENTS_SCOPE}",
        },
        "family@example.com",
    )


@pytest.fixture
async def family(db, with_scope):
    await google_oauth.set_selected_calendars(db, CALENDARS)
    await calendar_prefs.set_family_calendar(db, {"account_id": 1, "id": CALENDARS[0]["id"], "summary": "Family"})


class FakeFamilyCalendar:
    """events.insert / events.get on the Family calendar, like Google: an id
    that's taken gets 409. `lose_answer`: the next insert is made, but its
    answer never arrives (a timeout)."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.lose_answer = False

    def insert(self, request):
        body = json.loads(request.content)
        if body["id"] in self.events:
            return httpx.Response(409, json={"error": {"code": 409, "errors": [{"reason": "duplicate"}]}})
        self.events[body["id"]] = body
        if self.lose_answer:
            self.lose_answer = False
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json=body)

    def get(self, request):
        event = self.events.get(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=event) if event else httpx.Response(404)


@pytest.fixture
def gcal(google):
    """Every events.list answers empty; the Family calendar takes inserts
    (gcal.fake holds them). Any other POST is unmocked and fails the test."""
    fake = FakeFamilyCalendar()
    google.get(url__regex=re.escape(FAMILY_INSERT) + r"/[^/?]+$", name="get").mock(side_effect=fake.get)
    google.get(url__regex=EVENTS_URL_PATTERN, name="list").respond(200, json={"items": []})
    insert = google.post(FAMILY_INSERT, name="insert").mock(side_effect=fake.insert)
    insert.fake = fake
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
    assert added == {
        "id": body["id"],
        "summary": "Bins out",
        "date": today.isoformat(),
        "calendar": "Family",
        "duplicate": False,
    }


async def test_one_person_is_saved_as_name_colon_title(db, family, gcal):
    today = await _today(db)
    await calendar_add.add_family_event(
        db, title="Dentist", day=today.isoformat(), start_time="16:30", person_id=await _riley(db), request_key=KEY
    )

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
    await calendar_add.add_family_event(
        db, title="Party", day=today.isoformat(), start_time="14:00", end_time="16:00", request_key=KEY
    )
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


async def test_a_lost_response_retried_is_not_a_second_event(db, family, gcal, google):
    """Google made it, but the answer never came: the same form again gets
    409, reads the event back, and it matches: added, once."""
    today = (await _today(db)).isoformat()
    gcal.fake.lose_answer = True
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=today, request_key=KEY)
    assert exc.value.code == "offline"

    added = await calendar_add.add_family_event(db, title="X", day=today, request_key=KEY)
    assert added["id"] == calendar_add.event_id(KEY, CALENDARS[0]["id"]) and not added["duplicate"]
    assert len(gcal.fake.events) == 1 and google.routes["get"].call_count == 1


async def test_a_changed_form_under_a_used_key_is_its_own_event(db, family, gcal):
    """Lost answer, then the form is edited but sent under the same key: the
    taken id holds a different event, so it's added once under a new id, and
    sending that again finds it rather than adding a third."""
    today = (await _today(db)).isoformat()
    gcal.fake.lose_answer = True
    with pytest.raises(calendar_add.AddEventError):
        await calendar_add.add_family_event(db, title="Swim", day=today, request_key=KEY)

    added = await calendar_add.add_family_event(db, title="Swimming", day=today, request_key=KEY)
    assert added["id"] != calendar_add.event_id(KEY, CALENDARS[0]["id"])
    assert sorted(e["summary"] for e in gcal.fake.events.values()) == ["Swim", "Swimming"]

    await database.set_setting(db, calendar_add.CLAIMS_KEY, "{}")  # as if that answer was lost too
    await db.commit()
    again = await calendar_add.add_family_event(db, title="Swimming", day=today, request_key=KEY)
    assert again["id"] == added["id"] and len(gcal.fake.events) == 2


async def test_a_taken_id_at_another_time_is_not_reported_as_added(db, family, gcal):
    """Same title and day, but a different start: a different event."""
    today = (await _today(db)).isoformat()
    gcal.fake.lose_answer = True
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="Swim", day=today, start_time="10:00", request_key=KEY)
    assert "may already have been added" in exc.value.message  # a timeout: it might be in Google

    added = await calendar_add.add_family_event(db, title="Swim", day=today, start_time="16:30", request_key=KEY)
    assert added["id"] != calendar_add.event_id(KEY, CALENDARS[0]["id"])
    starts = sorted(e["start"]["dateTime"][11:16] for e in gcal.fake.events.values())
    assert starts == ["10:00", "16:30"]


def test_same_start_compares_instants_and_all_day_dates():
    ours = {"dateTime": "2026-10-01T16:30:00+01:00"}
    assert calendar_add._same_start({"dateTime": "2026-10-01T15:30:00Z"}, ours)
    assert not calendar_add._same_start({"dateTime": "2026-10-01T16:00:00+01:00"}, ours)
    assert not calendar_add._same_start({"date": "2026-10-01"}, ours)
    assert calendar_add._same_start({"date": "2026-10-01"}, {"date": "2026-10-01"})
    assert not calendar_add._same_start({"dateTime": "2026-10-01T00:00:00+01:00"}, {"date": "2026-10-01"})


async def _set_claim(db, state, minutes_ago):
    at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    await database.set_setting(db, calendar_add.CLAIMS_KEY, json.dumps({KEY: {"state": state, "at": at}}))
    await db.commit()


async def test_a_stuck_adding_claim_goes_stale_and_the_409_settles_it(db, family, gcal):
    """A request that died after Google made the event (its outcome never
    recorded): 'busy' at first, then after STALE_CLAIM a resubmit re-claims,
    and the read-back finds the event: added once."""
    today = (await _today(db)).isoformat()
    eid = calendar_add.event_id(KEY, CALENDARS[0]["id"])
    gcal.fake.events[eid] = {"id": eid, "summary": "X", "start": {"date": today}}

    await _set_claim(db, "adding", 1)
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=today, request_key=KEY)
    assert exc.value.code == "busy"

    await _set_claim(db, "adding", 6)
    added = await calendar_add.add_family_event(db, title="X", day=today, request_key=KEY)
    assert added["id"] == eid and len(gcal.fake.events) == 1
    assert calendar_add.STALE_CLAIM == timedelta(minutes=5)


async def test_a_done_claim_never_goes_stale(db, family, gcal):
    await _set_claim(db, "done", 60)
    added = await calendar_add.add_family_event(db, title="X", day=(await _today(db)).isoformat(), request_key=KEY)
    assert added["duplicate"] and gcal.call_count == 0


@pytest.mark.parametrize("step", ["get_family_calendar", "has_scope", "family_today", "connect", "family_timezone"])
async def test_a_database_error_before_the_claim_is_a_message_not_a_500(db, family, gcal, client, monkeypatch, step):
    """A briefly locked database during the add (only the add's own call
    fails; the widget's re-render afterwards reads fine)."""
    target = {
        "get_family_calendar": calendar_prefs,
        "has_scope": google_oauth,
        "connect": google_oauth,
        "family_today": calendar_add,
        "family_timezone": calendar_add,
    }[step]
    real = getattr(target, step)
    failed = []

    async def locked_once(*args, **kwargs):
        if not failed:
            failed.append(1)
            raise sqlite3.OperationalError("database is locked")
        return await real(*args, **kwargs)

    today = await _today(db)
    monkeypatch.setattr(target, step, locked_once)

    resp = await client.post("/widgets/calendar/events", data=_form(today))

    assert failed and resp.status_code == 200
    assert str(escape(calendar_add.ERRORS["storage"])) in resp.text
    assert gcal.call_count == 0
    assert KEY not in json.loads(await database.get_setting(db, calendar_add.CLAIMS_KEY) or "{}")


async def test_a_database_error_finding_the_person_is_a_message(db, family, gcal, monkeypatch):
    async def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(calendar_add, "_person", locked)
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(
            db, title="X", day=(await _today(db)).isoformat(), person_id="1", request_key=KEY
        )
    assert exc.value.code == "storage"


async def test_an_event_deleted_in_google_is_not_mistaken_for_this_one(db, family, gcal):
    today = (await _today(db)).isoformat()
    gcal.fake.events[calendar_add.event_id(KEY, CALENDARS[0]["id"])] = {
        "summary": "X",
        "start": {"date": today},
        "status": "cancelled",
    }
    added = await calendar_add.add_family_event(db, title="X", day=today, request_key=KEY)
    assert gcal.fake.events[added["id"]].get("status") != "cancelled"


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {}, "scope"),
        (403, {"error": {"errors": [{"reason": "forbidden"}]}}, "scope"),
        (403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}, "google-busy"),
        (403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}, "google-busy"),
        (429, {}, "google-busy"),
        (404, {}, "missing"),
        (400, {}, "failed"),
        (503, {}, "offline"),
    ],
)
async def test_google_refusals_release_the_claim(db, family, gcal, status, body, code):
    today = (await _today(db)).isoformat()
    gcal.mock(side_effect=None, return_value=httpx.Response(status, json=body))
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=today, request_key=KEY)
    assert exc.value.code == code
    if code == "google-busy":
        assert "reconnect" not in exc.value.message.lower() and "shortly" in exc.value.message
    claims = json.loads(await database.get_setting(db, calendar_add.CLAIMS_KEY))
    assert KEY not in claims  # the same form can be sent again


async def test_unreadable_google_answer_is_a_failure_not_a_500(db, family, gcal, client):
    today = await _today(db)
    gcal.mock(side_effect=None, return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=today.isoformat(), request_key=KEY)
    assert exc.value.code == "failed"
    assert KEY not in json.loads(await database.get_setting(db, calendar_add.CLAIMS_KEY))

    resp = await client.post("/widgets/calendar/events", data=_form(today, request_key=KEY + "b"))
    assert resp.status_code == 200 and str(escape(calendar_add.ERRORS["failed"])) in resp.text


async def test_a_database_error_claiming_is_a_message_not_a_500(db, family, gcal, client, monkeypatch):
    async def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(calendar_add, "_update_claims", locked)
    today = await _today(db)

    resp = await client.post("/widgets/calendar/events", data=_form(today))

    assert resp.status_code == 200 and str(escape(calendar_add.ERRORS["storage"])) in resp.text
    assert gcal.call_count == 0


async def test_a_database_error_recording_the_add_still_reports_it(db, family, gcal, monkeypatch):
    real = calendar_add._update_claims
    calls = []

    async def fail_second(db_, change):
        calls.append(1)
        if len(calls) > 1:
            raise sqlite3.OperationalError("database is locked")
        return await real(db_, change)

    monkeypatch.setattr(calendar_add, "_update_claims", fail_second)

    added = await calendar_add.add_family_event(db, title="X", day=(await _today(db)).isoformat(), request_key=KEY)
    assert added["summary"] == "X" and len(gcal.fake.events) == 1


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


@pytest.mark.parametrize(
    ("fields", "code"),
    [
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
    ],
)
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
    # Writing events ticked, but Google never allowed calendar.events ("Needs a permission").
    await google_accounts.update(db, 1, None, ["calendars", "tasks", "write_events"])
    await google_oauth.set_selected_calendars(db, CALENDARS)
    await calendar_prefs.set_family_calendar(db, {"account_id": 1, "id": CALENDARS[0]["id"], "summary": "Family"})
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=(await _today(db)).isoformat(), request_key=KEY)
    assert exc.value.code == "scope" and "reconnect" in exc.value.message.lower()
    assert gcal.call_count == 0


async def test_no_family_calendar_is_refused(db, gcal, with_scope):
    with pytest.raises(calendar_add.AddEventError) as exc:
        await calendar_add.add_family_event(db, title="X", day=(await _today(db)).isoformat(), request_key=KEY)
    assert exc.value.code == "no-calendar"
    assert gcal.call_count == 0


async def _cache_rows(db):
    return [
        tuple(r)
        for r in await (
            await db.execute("SELECT range_start, range_end, calendar_id FROM calendar_cache ORDER BY 1, 3")
        ).fetchall()
    ]


async def test_adding_refreshes_the_cache_in_the_background(db, family, gcal, monkeypatch):
    """An upsert, never a clear (another month's copy stays for offline use),
    and the add doesn't wait for it."""
    from app import calendar_cache, google_calendar

    _, selection = await google_calendar._selection(db)
    other_month = (date(2020, 1, 1), date(2020, 2, 1))
    await calendar_cache.store(db, 1, selection[1], *other_month, {CALENDARS[0]["id"]: []})
    release = asyncio.Event()
    real = google_calendar.refresh_cache

    async def slow_refresh(db_):
        await release.wait()
        return await real(db_)

    monkeypatch.setattr(google_calendar, "refresh_cache", slow_refresh)

    today = await _today(db)
    await asyncio.wait_for(calendar_add.add_family_event(db, title="X", day=today.isoformat(), request_key=KEY), 2)
    assert len(calendar_add._refreshes) == 1  # still running: the add didn't wait
    release.set()
    await calendar_add.wait_for_refreshes()

    rows = await _cache_rows(db)
    assert ("2020-01-01", "2020-02-01", CALENDARS[0]["id"]) in rows  # not cleared
    assert len(rows) == 1 + len(CALENDARS)  # this month refetched for every calendar


async def test_the_widget_shows_the_new_event_straight_away(client, db, family, gcal, google):
    """The re-render after an add fetches live, so the event is there even
    before the background refresh has run."""
    today = await _today(db)
    # events.list now answers with whatever was inserted.
    google.routes["list"].mock(
        side_effect=lambda request: httpx.Response(200, json={"items": list(gcal.fake.events.values())})
    )
    resp = await client.post("/widgets/calendar/events", data=_form(today, view="agenda"))
    assert "Added “Dentist” to Family." in resp.text
    assert resp.text.count("Dentist") >= 2  # the note and the agenda row


# --- The widget ---------------------------------------------------------------------------


def _form(today, **extra):
    return {
        "title": "Dentist",
        "day": today.isoformat(),
        "start_time": "",
        "end_time": "",
        "person_id": "",
        "request_key": KEY,
        "view": "week",
        **extra,
    }


async def test_plus_shows_only_when_adding_is_set_up(client, db, gcal, with_scope):
    await google_oauth.set_selected_calendars(db, CALENDARS)
    assert "cal-add-toggle" not in (await client.get("/widgets/calendar")).text  # no Family calendar

    await calendar_prefs.set_family_calendar(db, {"account_id": 1, "id": CALENDARS[0]["id"], "summary": "Family"})
    html = (await client.get("/widgets/calendar")).text
    assert "cal-add-toggle" in html and "Add to Family" in html
    # Outside the drag handle, like every other control.
    header = html.split('class="widget-heading drag-handle"')[1].split("</div>")[0]
    assert "cal-add-toggle" not in header and "cal-view-btn" not in header


async def test_plus_is_hidden_without_the_scope(client, db, gcal, connected):
    await google_oauth.set_selected_calendars(db, CALENDARS)
    await calendar_prefs.set_family_calendar(db, {"account_id": 1, "id": CALENDARS[0]["id"], "summary": "Family"})
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
    resp = await client.post("/widgets/calendar/events", data=_form(today), headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert gcal.call_count == 0


async def test_form_uses_the_on_screen_keyboard(client, db, family, gcal, monkeypatch):
    monkeypatch.setattr(database, "onscreen_keyboard_enabled", True)
    html = (await client.get("/widgets/calendar")).text
    assert re.search(r'<input type="text" name="title" class="cal-add-title"[^>]*data-osk="text"', html)
