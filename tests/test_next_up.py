"""The "next up" strip (app/services/next_up.py): each person's next timed
event today from calendar_cache only, whose it is by the person filter's
rules, the Admin switch, and the /api/rev component."""

import json
from datetime import date, datetime

import pytest

from app import calendar_cache, database, google_calendar, google_oauth
from app.routers import admin
from app.security import create_session_token
from app.services import calendar_prefs, next_up
from tests.test_banners import CAL, OTHER_CAL, TZ, at, event

DAY = date(2026, 10, 5)
MUM, DAD, RILEY, JAMIE = 1, 2, 3, 4


def timed(summary, start, event_id, end=None, day=DAY):
    raw = event(summary, start, event_id=event_id, day=day)
    if end:
        raw["end"] = {"dateTime": f"{day.isoformat()}T{end}:00+01:00"}
    return raw


def all_day(summary, event_id, day=DAY):
    return {"id": event_id, "summary": summary, "start": {"date": day.isoformat()}, "end": {"date": day.isoformat()}}


@pytest.fixture
async def calendars(db, connected):
    """fill({calendar id: [raw events]}) caches them as a month-grid fetch does."""
    await google_oauth.set_selected_calendars(db, [CAL, OTHER_CAL])

    async def fill(by_calendar: dict[str, list]):
        _, selection = await google_calendar._selection(db)
        events = {
            cal_id: [google_calendar._format_event(r, None) for r in raws] for cal_id, raws in by_calendar.items()
        }
        await calendar_cache.store(db, selection, date(2026, 9, 28), date(2026, 11, 9), events)

    return fill


def summary(items):
    return [(i["person"]["name"] if i["person"] else "Everyone", i["title"], i["time"], i["in"]) for i in items]


async def test_each_persons_next_event_today_in_family_order(db, calendars):
    await calendars(
        {
            CAL["id"]: [
                timed("Riley: Swimming", "16:30", "a"),
                timed("Riley: Tea at Nan's", "18:00", "b"),  # later: Riley's next is swimming
                timed("Mum: Dentist", "11:15", "c"),
                timed("Bins out", "19:00", "d"),  # no name: Everyone's
                timed("Mum: Yoga", "08:00", "e"),  # already started: not "next"
                all_day("Riley: Non-uniform day", "f"),  # all day: the banner's job
                timed("Riley: Football", "10:00", "g", day=date(2026, 10, 6)),  # tomorrow
            ]
        }
    )
    items = await next_up.items(db, at(9, 0))
    assert summary(items) == [
        ("Mum", "Dentist", "11:15", "in 2 h 15 min"),
        ("Riley", "Swimming", "16:30", "in 7 h 30 min"),
        ("Everyone", "Bins out", "19:00", "in 10 h"),
    ]


async def test_a_calendar_linked_to_a_person_is_theirs_without_a_name_prefix(db, calendars):
    await calendar_prefs.set_people_links(db, {OTHER_CAL["id"]: JAMIE, CAL["id"]: calendar_prefs.EVERYONE})
    await calendars(
        {OTHER_CAL["id"]: [timed("Nursery pickup", "15:00", "a")], CAL["id"]: [timed("Film night", "19:30", "b")]}
    )
    items = await next_up.items(db, at(12, 0))
    assert summary(items) == [
        ("Jamie", "Nursery pickup", "15:00", "in 3 h"),
        ("Everyone", "Film night", "19:30", "in 7 h 30 min"),
    ]


async def test_one_event_naming_two_people_is_next_for_both(db, calendars):
    await calendars({CAL["id"]: [timed("Riley and Jamie: Swimming", "16:30", "a")]})
    names = [i["person"]["name"] for i in await next_up.items(db, at(9))]
    assert names == ["Riley", "Jamie"]


async def test_minutes_round_up_and_nothing_left_means_empty(db, calendars):
    await calendars({CAL["id"]: [timed("Riley: Swimming", "16:30", "a")]})
    assert summary(await next_up.items(db, at(16, 29, 30))) == [("Riley", "Swimming", "16:30", "in 1 min")]
    assert await next_up.items(db, at(16, 30)) == []  # it has started
    assert await next_up.items(db, at(23, 59)) == []


async def test_no_google_or_no_cache_is_simply_empty(db):
    assert await next_up.items(db, at(9)) == []


async def test_the_admin_switch_turns_it_off(db, client, calendars):
    await calendars({CAL["id"]: [timed("Riley: Swimming", "16:30", "a")]})
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    assert (await next_up.context(db, at(9)))["next_up"]

    resp = await client.post("/admin/next-up", data={})  # unticked
    assert resp.status_code == 303 and resp.headers["location"].endswith("#banners")
    assert await database.get_setting(db, next_up.SETTING) == "0"
    assert await next_up.context(db, at(9)) == {"next_up": [], "next_up_enabled": False}

    await client.post("/admin/next-up", data={"enabled": "true"})
    assert await next_up.is_enabled(db)
    assert 'Show a "Next up" line' in (await client.get("/admin?tab=display")).text


async def test_strip_renders_on_the_dashboard_and_its_own_route(db, client, calendars, monkeypatch):
    monkeypatch.setattr(next_up, "_clock", lambda tz: at(9))
    await calendars({CAL["id"]: [timed("Riley: Swimming", "16:30", "a")]})
    for path in ("/", "/next-up"):
        html = (await client.get(path)).text
        assert 'id="next-up" class="next-up"' in html, path
        assert 'data-rev-key="next_up"' in html and "data-busy-any" in html, path
        assert "Swimming" in html and "16:30 · in 7 h 30 min" in html, path

    monkeypatch.setattr(next_up, "_clock", lambda tz: at(17))
    assert 'class="next-up is-empty"' in (await client.get("/next-up")).text


async def test_rev_moves_as_the_minutes_count_down(db, client, calendars, monkeypatch):
    await calendars({CAL["id"]: [timed("Riley: Swimming", "16:30", "a")]})
    revs = []
    for minute in (0, 0, 1):
        monkeypatch.setattr(next_up, "_clock", lambda tz, m=minute: at(9, m))
        revs.append((await client.get("/api/rev")).json()["widgets"]["next_up"])
    assert revs[0] == revs[1] != revs[2]


async def test_times_keep_googles_offset(db, calendars):
    """An event Google gives in another zone keeps its own clock time, like
    the banners (never re-converted through the container's clock)."""
    raw = {"id": "a", "summary": "Riley: Call Grandad", "start": {"dateTime": "2026-10-05T19:00:00+02:00"}}
    raw["end"] = raw["start"]
    await calendars({CAL["id"]: [raw]})
    assert summary(await next_up.items(db, at(9))) == [("Riley", "Call Grandad", "19:00", "in 9 h")]


def test_in_words():
    assert [next_up.in_words(m) for m in (1, 59, 60, 61, 125)] == [
        "in 1 min",
        "in 59 min",
        "in 1 h",
        "in 1 h 1 min",
        "in 2 h 5 min",
    ]


async def test_setting_default_is_on(db):
    assert await next_up.is_enabled(db)
    assert json.loads(json.dumps(await next_up.context(db, datetime(2026, 10, 5, 9, tzinfo=TZ))))["next_up_enabled"]
