"""Calendar v1.2 (spec 10.5): the week and agenda views, the Admin default
view and the idle return, the person filter, and Admin's calendar options.
Adding events is in test_calendar_add.py."""

import json
import re
import time
from datetime import date, timedelta

import httpx
import pytest

from app import calendar_cache, database, google_calendar, google_oauth
from app.auth import create_session_token
from app.routers import admin
from app.services import calendar_prefs, calendar_view, people, term_dates

EVENTS_URL_PATTERN = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"
# Monday 10 - Sunday 16 August 2026.
WEEK = date(2026, 8, 10)
TWO_CALENDARS = [
    {"id": "family", "summary": "Family", "color": "#123456"},
    {"id": "riley-cal", "summary": "Riley's clubs", "color": "#654321"},
]


def timed(title, day, start, end, offset="+01:00", event_id=None):
    event = {"summary": title, "start": {"dateTime": f"{day}T{start}:00{offset}"},
             "end": {"dateTime": f"{day}T{end}:00{offset}"}}
    if event_id:
        event["id"] = event_id
    return event


def all_day(title, first, last_exclusive):
    return {"summary": title, "start": {"date": first}, "end": {"date": last_exclusive}}


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


@pytest.fixture
def events(google, connected):
    route = google.get(url__regex=EVENTS_URL_PATTERN)

    def serve(*items):
        route.respond(200, json={"items": list(items)})
    serve.route = route
    return serve


def per_calendar(by_id: dict):
    """A side effect answering each calendar's events.list with its own items."""
    def answer(request):
        cal_id = request.url.path.split("/calendars/")[1].split("/")[0]
        return httpx.Response(200, json={"items": by_id.get(cal_id.replace("%40", "@"), [])})
    return answer


async def _profile_ids(db) -> dict[str, int]:
    rows = await (await db.execute("SELECT id, name FROM profiles")).fetchall()
    return {r["name"]: r["id"] for r in rows}


def _root(html: str) -> str:
    return re.search(r'<div class="widget-card" id="widget-calendar"[^>]*>', html, re.S).group(0)


# --- Week view ----------------------------------------------------------------------------

async def test_week_places_timed_events_on_the_hour_grid(db, events):
    events(
        timed("Swimming", "2026-08-11", "16:00", "17:30"),
        timed("Piano", "2026-08-11", "17:00", "17:30"),   # overlaps Swimming: side by side
        timed("Breakfast club", "2026-08-12", "07:30", "08:00"),
    )

    week = await google_calendar.get_week(db, WEEK)

    assert week["label"] == "10 Aug – 16 Aug 2026"
    assert week["hours"][0] == "07:00" and week["hours"][-1] == "20:00"  # 07:00-21:00
    tue = {b["event"]["title"]: b for b in week["days"][1]["blocks"]}
    minutes = 14 * 60
    assert tue["Swimming"]["top"] == pytest.approx(100 * 9 * 60 / minutes, abs=0.01)
    assert tue["Swimming"]["height"] == pytest.approx(100 * 90 / minutes, abs=0.01)
    assert (tue["Swimming"]["lane"], tue["Piano"]["lane"]) == (0, 1)
    assert tue["Swimming"]["lanes"] == tue["Piano"]["lanes"] == 2
    wed = week["days"][2]["blocks"]
    assert [b["event"]["title"] for b in wed] == ["Breakfast club"] and wed[0]["lanes"] == 1


async def test_week_hour_range_stretches_to_fit_its_events(db, events):
    events(timed("Early run", "2026-08-10", "05:30", "06:15"), timed("Late film", "2026-08-15", "21:30", "23:00"))

    week = await google_calendar.get_week(db, WEEK)

    assert week["hours"][0] == "05:00" and week["hours"][-1] == "22:00"


async def test_week_keeps_googles_own_offset(db, events):
    # 09:00 in New York is drawn at 09:00, never converted to the server's UTC.
    events(timed("Call", "2026-08-10", "09:00", "10:00", offset="-04:00"))

    block = (await google_calendar.get_week(db, WEEK))["days"][0]["blocks"][0]

    assert block["begins"] == 9 * 60 and block["event"]["time_label"] == "9:00 AM"


async def test_week_all_day_row_has_all_day_multi_day_and_term_bars(db, events):
    await term_dates.insert_period(db, term_dates.clean_period("inset", "2026-08-14", "2026-08-14", "INSET day"),
                                   "manual")
    await db.commit()
    events(
        all_day("Camping", "2026-08-08", "2026-08-12"),  # Sat 8 - Tue 11
        {"summary": "Overnight", "start": {"dateTime": "2026-08-12T20:00:00+01:00"},
         "end": {"dateTime": "2026-08-13T09:00:00+01:00"}},  # timed, but over two days
        {"summary": "Late show", "start": {"dateTime": "2026-08-13T22:00:00+01:00"},
         "end": {"dateTime": "2026-08-14T00:00:00+01:00"}},  # ends at midnight: one day
    )

    week = await google_calendar.get_week(db, WEEK)

    bars = {b["event"]["title"]: (b["col_start"], b["col_end"]) for b in week["bars"]}
    assert bars == {"Camping": (1, 2), "Overnight": (3, 4), "INSET day": (5, 5)}
    late = week["days"][3]["blocks"][0]
    assert late["event"]["title"] == "Late show" and late["ends"] == 24 * 60
    assert week["hours"][-1] == "23:00"


async def test_week_widget_renders_and_links_each_day(client, events):
    events(timed("Swimming", "2026-08-11", "16:00", "17:00"))

    html = (await client.get("/widgets/calendar?view=week&start=2026-08-12")).text  # any day: its week

    assert 'data-view="week"' in html and "10 Aug – 16 Aug 2026" in html
    assert "Swimming" in html and "cal-wk-event" in html
    assert 'hx-get="/widgets/calendar/day/2026-08-11?back=week"' in html
    assert 'hx-get="/widgets/calendar?view=week&start=2026-08-03"' in html  # previous week


async def test_week_offline_is_served_from_its_months_cache(client, db, events):
    events(timed("Swimming", "2026-08-11", "16:00", "17:00"))
    await google_calendar.get_month_grid(db, 2026, 8)  # caches August's grid
    events.route.side_effect = httpx.ConnectError("offline")

    html = (await client.get("/widgets/calendar?view=week&start=2026-08-10")).text

    assert "Swimming" in html and "Last updated" in html and "reach Google" not in html


async def test_week_offline_without_cache_says_so(client, events):
    events.route.side_effect = httpx.ConnectError("offline")
    html = (await client.get("/widgets/calendar?view=week&start=2026-08-10")).text
    assert "reach Google" in html


async def test_week_bad_start_is_a_400(client):
    assert (await client.get("/widgets/calendar?view=week&start=nope")).status_code == 400
    assert (await client.get("/widgets/calendar?view=nope")).status_code == 422


# --- Agenda -------------------------------------------------------------------------------

async def test_agenda_lists_today_tomorrow_and_busy_days_ahead(db, events):
    today = await database.family_today(db)
    day = [(today + timedelta(days=n)).isoformat() for n in range(8)]
    events(
        timed("Dentist", day[0], "09:00", "10:00"),
        all_day("Grandma visits", day[3], day[5]),   # two days: shown on both
        timed("Beyond the week", day[7], "09:00", "10:00"),
    )

    agenda = await google_calendar.get_agenda(db)

    labels = [d["label"] for d in agenda["days"]]
    assert labels[:2] == ["Today", "Tomorrow"] and len(labels) == 4
    assert [e["title"] for e in agenda["days"][0]["events"]] == ["Dentist"]
    assert agenda["days"][1]["events"] == []
    assert [e["time_label"] for e in agenda["days"][2]["events"]] == ["All day"]
    assert [e["time_label"] for e in agenda["days"][3]["events"]] == ["Continues"]


async def test_agenda_event_ending_at_midnight_is_not_on_the_next_day(db, events):
    today = await database.family_today(db)
    tomorrow = today + timedelta(days=1)
    events(
        {"summary": "Late film", "start": {"dateTime": f"{today.isoformat()}T22:00:00+01:00"},
         "end": {"dateTime": f"{tomorrow.isoformat()}T00:00:00+01:00"}},
        {"summary": "Night shift", "start": {"dateTime": f"{today.isoformat()}T22:00:00+01:00"},
         "end": {"dateTime": f"{tomorrow.isoformat()}T06:00:00+01:00"}},
    )

    agenda = await google_calendar.get_agenda(db)

    assert [e["title"] for e in agenda["days"][0]["events"]] == ["Late film", "Night shift"]
    assert [(e["title"], e["time_label"]) for e in agenda["days"][1]["events"]] == [("Night shift", "Continues")]


async def test_agenda_widget_and_offline_from_the_refreshed_cache(client, db, events):
    today = await database.family_today(db)
    events(timed("Dentist", today.isoformat(), "09:00", "10:00"))
    assert await google_calendar.refresh_cache(db) is True  # covers the agenda's week too
    events.route.side_effect = httpx.ConnectError("offline")

    html = (await client.get("/widgets/calendar?view=agenda")).text

    assert 'data-view="agenda"' in html and "Today" in html and "Tomorrow" in html
    assert "Dentist" in html and "Last updated" in html and "reach Google" not in html


# --- Default view and the idle return ----------------------------------------------------

async def test_default_view_is_the_admin_choice(client, db, events):
    events()
    assert calendar_prefs.DEFAULT_VIEW == "month"
    assert 'data-view="month"' in (await client.get("/widgets/calendar")).text

    await calendar_prefs.set_default_view(db, "agenda")
    assert 'data-view="agenda"' in (await client.get("/widgets/calendar")).text
    dashboard = (await client.get("/")).text
    assert 'data-view="agenda"' in dashboard
    # An older month link still opens the month.
    assert 'data-view="month"' in (await client.get("/widgets/calendar?year=2026&month=8")).text


@pytest.mark.parametrize("url", [
    "/widgets/calendar?view=week&start=2026-08-10&person=1",
    "/widgets/calendar?year=2026&month=8",
    "/widgets/calendar/day/2026-08-11?back=agenda",
    "/widgets/calendar?view=agenda",
])
async def test_every_view_polls_back_to_the_default(client, events, url):
    events()
    root = _root((await client.get(url)).text)
    # Each re-render restarts the timer, so this fires after that long untouched;
    # /widgets/calendar is the default view for today, unfiltered.
    assert 'hx-get="/widgets/calendar"' in root
    assert f'hx-trigger="every {calendar_view.IDLE_SECONDS}s [window.huddleCalendarIdle' in root


async def test_not_connected_shows_the_stub_without_polling(client, google):
    html = (await client.get("/widgets/calendar?view=week")).text
    assert "Not connected yet" in html and "hx-trigger" not in _root(html)
    assert "cal-toolbar" not in html


def test_idle_script_is_on_the_dashboard_only_once():
    from pathlib import Path
    dashboard = (Path(__file__).parents[1] / "app/templates/dashboard.html").read_text(encoding="utf-8")
    assert dashboard.count("/static/js/calendar.js") == 1


# --- Person filter ------------------------------------------------------------------------

async def test_owner_is_the_linked_calendar_first_then_the_title(db):
    ids = await _profile_ids(db)
    profiles = [{"id": i, "name": n} for n, i in ids.items()]
    links = {"riley-cal": ids["Riley"], "family": calendar_prefs.EVERYONE}

    def owners(title, cal):
        return calendar_view.event_owners({"title": title, "calendar_id": cal}, links, profiles)

    assert owners("Swimming with Jamie", "riley-cal") == {ids["Riley"]}  # the calendar wins
    assert owners("Jamie: Dentist", "family") == {ids["Jamie"]}
    assert owners("Pick up Riley and Jamie", "family") == {ids["Riley"], ids["Jamie"]}
    assert owners("Riley's party", "unlinked-cal") == {ids["Riley"]}
    assert owners("Bins out", "family") is None  # everyone
    assert owners("Rileyball", "family") is None  # whole words only


async def test_person_filter_shows_theirs_and_everyones(client, db, events):
    await google_oauth.set_selected_calendars(db, TWO_CALENDARS)
    ids = await _profile_ids(db)
    await calendar_prefs.set_people_links(db, {"riley-cal": ids["Riley"], "family": calendar_prefs.EVERYONE})
    events.route.side_effect = per_calendar({
        "family": [timed("Jamie: Dentist", "2026-08-11", "09:00", "10:00"),
                   timed("Bins out", "2026-08-11", "19:00", "19:15"),
                   timed("Riley's party", "2026-08-12", "14:00", "16:00")],
        "riley-cal": [timed("Swimming", "2026-08-13", "16:00", "17:00")],
    })

    riley = (await client.get(f"/widgets/calendar?view=week&start=2026-08-10&person={ids['Riley']}")).text
    jamie = (await client.get(f"/widgets/calendar?view=week&start=2026-08-10&person={ids['Jamie']}")).text
    everyone = (await client.get("/widgets/calendar?view=week&start=2026-08-10")).text

    assert "Swimming" in riley and "Riley&#39;s party" in riley and "Bins out" in riley
    assert "Dentist" not in riley
    assert "Dentist" in jamie and "Bins out" in jamie and "Swimming" not in jamie
    assert all(t in everyone for t in ("Swimming", "Dentist", "Bins out"))
    # The pressed pill toggles back to everyone; the others switch person.
    assert re.search(r'aria-pressed="true"\s+hx-get="/widgets/calendar\?view=week&amp;start=2026-08-10"', riley) \
        or re.search(r'aria-pressed="true"\s+hx-get="/widgets/calendar\?view=week&start=2026-08-10"', riley)
    assert f"person={ids['Jamie']}" in riley


async def test_person_filter_keeps_term_dates_and_works_in_month_and_day(client, db, events):
    await term_dates.insert_period(db, term_dates.clean_period("inset", "2026-08-12", "2026-08-12", "INSET day"),
                                   "manual")
    await db.commit()
    ids = await _profile_ids(db)
    events(timed("Jamie: Dentist", "2026-08-12", "09:00", "10:00"))

    month = (await client.get(f"/widgets/calendar?year=2026&month=8&person={ids['Riley']}")).text
    day = (await client.get(f"/widgets/calendar/day/2026-08-12?person={ids['Riley']}")).text

    assert "INSET day" in month and "Dentist" not in month
    assert "INSET day" in day and "Dentist" not in day
    assert f'hx-get="/widgets/calendar/day/2026-08-12?person={ids["Riley"]}"' in month


async def test_unknown_person_shows_everyone(client, events):
    events(timed("Bins out", "2026-08-11", "19:00", "19:15"))
    html = (await client.get("/widgets/calendar?view=week&start=2026-08-10&person=999")).text
    assert "Bins out" in html and 'aria-pressed="true"' not in html.split("cal-people")[1]


async def test_cached_events_learn_their_calendar(db):
    await calendar_cache.store(db, (await google_calendar._selection(db))[1], date(2026, 8, 1), date(2026, 9, 1),
                               {"primary": [{"title": "Old row", "date": "2026-08-03", "end_date": "2026-08-03"}]})
    events = await google_calendar.cached_events(db, date(2026, 8, 3), date(2026, 8, 4))
    assert events[0]["calendar_id"] == "primary"


# --- The shared name helper (banners use it too) -----------------------------------------

def test_people_helper():
    alanna, riley = {"id": 1, "name": "Alanna Smith"}, {"id": 2, "name": "Riley"}
    profiles = [alanna, riley]
    assert people.first_name("Alanna Smith") == "Alanna" and people.first_name("  ") == ""
    assert people.split_person("Alanna: Dentist", profiles) == (alanna, "Dentist")
    assert people.split_person("alanna smith:  Dentist ", profiles) == (alanna, "Dentist")
    assert people.split_person("Note: Riley", profiles) == (None, "Note: Riley")
    assert people.split_person("Riley:", profiles) == (None, "Riley:")
    assert people.named_people("Riley: Take Alanna", profiles) == [riley]  # the prefix decides
    assert people.named_people("Alanna and Riley", profiles) == [alanna, riley]
    assert people.named_people("Rileys", profiles) == []


def test_names_match_as_written():
    """A name that's also a word counts only when capitalised as in Admin."""
    may, will, riley = {"id": 1, "name": "May"}, {"id": 2, "name": "Will"}, {"id": 3, "name": "Riley"}
    hyphen, lower = {"id": 4, "name": "Riley-Smith"}, {"id": 5, "name": "jo"}
    profiles = [may, will, riley, hyphen, lower]
    assert people.named_people("May half term", profiles) == [may]  # capitalised: accepted
    assert people.named_people("will you pick up the parcel", profiles) == []
    assert people.named_people("Tea with Will", profiles) == [will]
    assert people.named_people("WILL: swim", profiles) == [will]  # the prefix ignores case
    assert people.named_people("Riley's swim", profiles) == [riley]  # possessive
    assert people.named_people("Riley-Smith's party", profiles) == [hyphen]  # not Riley
    assert people.named_people("Party at the Riley-Smiths", profiles) == []
    assert people.named_people("Jo's party", profiles) == [lower]  # lower case in Admin: capitalised too
    assert people.named_people("riley swim", profiles) == []


def test_banners_use_the_shared_helper():
    from app.services import banners
    assert banners.people is people and not hasattr(banners, "_split_person")


# --- Admin --------------------------------------------------------------------------------

@pytest.fixture
def calendar_list(google):
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": [
        {"id": "family", "summary": "Family", "accessRole": "owner", "backgroundColor": "#123456"},
        {"id": "riley-cal", "summary": "Riley's clubs", "accessRole": "reader"},
        {"id": "work", "summary": "Work", "accessRole": "owner"},
    ]})
    google.get(url__startswith="https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(200, json={"items": []})
    return google


@pytest.fixture
async def connected_with_events(db):
    await google_oauth.store_tokens(db, {
        "access_token": "tok", "refresh_token": "r", "expires_at": time.time() + 3600,
        "scope": f"{google_oauth.CALENDAR_READ_SCOPE} {google_oauth.CALENDAR_EVENTS_SCOPE}",
    }, "family@example.com")


async def test_admin_picks_the_family_calendar_from_shown_writable_ones(admin_client, db, calendar_list,
                                                                        connected_with_events):
    await google_oauth.set_selected_calendars(db, TWO_CALENDARS)

    ok = await admin_client.post("/admin/google/family-calendar", data={"calendar_id": "family"})
    assert ok.headers["location"] == "/admin?tab=google#calendar-options"
    assert await calendar_prefs.get_family_calendar(db) == {"id": "family", "summary": "Family"}

    for refused in ("riley-cal", "work", "nope"):  # read-only / not shown / unknown
        resp = await admin_client.post("/admin/google/family-calendar", data={"calendar_id": refused})
        assert resp.headers["location"] == "/admin?tab=google&error=calendar-family#calendar-options"
    assert (await calendar_prefs.get_family_calendar(db))["id"] == "family"

    page = (await admin_client.get("/admin?tab=google")).text
    assert 'id="calendar-options"' in page and '<option value="family" selected>' in page
    assert '<option value="work"' not in page.split('id="calendar-options"')[1].split("</form>")[1]

    await admin_client.post("/admin/google/family-calendar", data={"calendar_id": ""})
    assert await calendar_prefs.get_family_calendar(db) is None
    assert "the wall has no" in (await admin_client.get("/admin?tab=google")).text


async def test_family_calendar_no_longer_shown_stops_counting(db):
    await google_oauth.set_selected_calendars(db, TWO_CALENDARS)
    await calendar_prefs.set_family_calendar(db, {"id": "family", "summary": "Family"})
    await google_oauth.set_selected_calendars(db, TWO_CALENDARS[1:])
    assert await calendar_prefs.get_family_calendar(db) is None
    assert await calendar_prefs.get_family_setting(db) == {"id": "family", "summary": "Family"}


async def test_admin_saves_the_default_view(admin_client, db):
    resp = await admin_client.post("/admin/google/calendar-view", data={"view": "week"})
    assert resp.headers["location"] == "/admin?tab=google#calendar-options"
    assert await calendar_prefs.get_default_view(db) == "week"
    bad = await admin_client.post("/admin/google/calendar-view", data={"view": "year"})
    assert "error=calendar-view" in bad.headers["location"]
    assert await calendar_prefs.get_default_view(db) == "week"


async def test_admin_links_calendars_to_people(admin_client, db, calendar_list, connected_with_events):
    await google_oauth.set_selected_calendars(db, TWO_CALENDARS)
    ids = await _profile_ids(db)

    await admin_client.post("/admin/google/calendar-people", data={
        "calendar_1": "family", "owner_1": "everyone",
        "calendar_2": "riley-cal", "owner_2": str(ids["Riley"]),
        "calendar_3": "work", "owner_3": str(ids["Mum"]),  # not shown on the wall: dropped
        "calendar_4": "family", "owner_4": "999",
    })

    assert await calendar_prefs.get_people_links(db) == {"family": "everyone", "riley-cal": ids["Riley"]}
    page = (await admin_client.get("/admin?tab=google")).text
    assert f'<option value="{ids["Riley"]}" selected>Riley</option>' in page


async def test_admin_hints_at_a_reconnect_without_the_events_scope(admin_client, db, calendar_list, connected):
    page = (await admin_client.get("/admin?tab=google")).text
    assert "Reconnect to allow adding events" in page


async def test_prefs_ignore_junk(db):
    await database.set_setting(db, calendar_prefs.PEOPLE_KEY, json.dumps({"a": 3, "b": "everyone", "c": "x",
                                                                          "d": True}))
    await database.set_setting(db, calendar_prefs.DEFAULT_VIEW_KEY, "not json")
    await database.set_setting(db, calendar_prefs.FAMILY_KEY, json.dumps(["x"]))
    await db.commit()
    assert await calendar_prefs.get_people_links(db) == {"a": 3, "b": "everyone"}
    assert await calendar_prefs.get_default_view(db) == "month"
    assert await calendar_prefs.get_family_setting(db) is None


@pytest.mark.parametrize(("path", "data"), [
    ("/admin/google/family-calendar", {"calendar_id": "family"}),
    ("/admin/google/calendar-view", {"view": "week"}),
    ("/admin/google/calendar-people", {"calendar_1": "family", "owner_1": "1"}),
])
async def test_calendar_options_require_admin(client, db, google, path, data):
    resp = await client.post(path, data=data)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"
    assert not google.calls
    assert await calendar_prefs.get_default_view(db) == "month"
    assert await calendar_prefs.get_family_setting(db) is None and await calendar_prefs.get_people_links(db) == {}
