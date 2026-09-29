"""Notification banner (spec 10.1, app/services/banners.py): each trigger's
timing windows, quiet hours, dismissals, ordering, sounds, Admin and the
/api/rev component. Every test uses a frozen clock in the family's zone."""

import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import calendar_cache, database, google_calendar, google_oauth
from app.admin_tabs import admin_url
from app.routers import admin
from app.security import create_session_token
from app.services import banners
from app.services import tasks as task_service

TZ = ZoneInfo("Europe/London")
DAY = date(2026, 10, 5)  # a Monday, in BST (+01:00)
CAL = {"id": "family@example.com", "summary": "Family", "color": "#3D6E93"}
OTHER_CAL = {"id": "work@example.com", "summary": "Work", "color": "#C1584A"}
EVENTS_URL_PATTERN = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"
MUM, RILEY = 1, 3


def at(hour, minute=0, second=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ)


def event(summary="Swimming", start="16:30", reminders=None, event_id="ev1", day=DAY):
    begins = f"{day.isoformat()}T{start}:00+01:00"
    raw = {"id": event_id, "summary": summary, "start": {"dateTime": begins}, "end": {"dateTime": begins}}
    if reminders is not None:
        raw["reminders"] = reminders
    return raw


def popup(*minutes, method="popup"):
    return {"useDefault": False, "overrides": [{"method": method, "minutes": m} for m in minutes]}


@pytest.fixture
async def calendar(db, connected):
    """Seeds calendar_cache the way a successful month-grid fetch does:
    fill(raw_events, default_reminders=None, calendar=CAL)."""
    await google_oauth.set_selected_calendars(db, [CAL])

    async def fill(raws, defaults=None, cal=CAL):
        _, selection = await google_calendar._selection(db)
        events = [google_calendar._format_event(r, defaults) for r in raws]
        await calendar_cache.store(db, selection, date(2026, 9, 28), date(2026, 11, 9), {cal["id"]: events})

    return fill


async def texts(db, now, settings=None):
    return [b["text"] for b in await banners.active_banners(db, now, settings)]


async def save(db, **changes):
    settings = banners.default_settings()
    for key, value in changes.items():
        if key in banners.TRIGGERS:
            settings["triggers"][key].update(value)
        else:
            settings[key] = value
    await banners.save_settings(db, settings)
    return settings


def only(*kinds):
    """Settings with just these triggers on (no quiet hours in the way)."""
    settings = banners.default_settings()
    for kind in banners.TRIGGERS:
        settings["triggers"][kind]["on"] = kind in kinds
    settings["quiet_start"], settings["quiet_end"] = "23:00", "05:00"
    return settings


# --- Events starting soon ---

async def test_event_uses_its_smallest_popup_override_and_clears_at_start(db, calendar):
    await calendar([event(reminders={"useDefault": False, "overrides": [
        {"method": "popup", "minutes": 40}, {"method": "popup", "minutes": 25}, {"method": "email", "minutes": 5}]})])
    s = only("events")
    assert await texts(db, at(16, 4, 59), s) == []
    assert await texts(db, at(16, 5), s) == ["Swimming at 16:30 (in 25 min)"]
    assert await texts(db, at(16, 29, 30), s) == ["Swimming at 16:30 (in 1 min)"]
    assert await texts(db, at(16, 30), s) == []  # started: gone


async def test_event_with_use_default_takes_the_calendars_default_reminders(db, calendar):
    await calendar([event(reminders={"useDefault": True})],
                   defaults=[{"method": "email", "minutes": 60}, {"method": "popup", "minutes": 10}])
    s = only("events")
    assert await texts(db, at(16, 19), s) == []
    assert await texts(db, at(16, 20), s) == ["Swimming at 16:30 (in 10 min)"]


@pytest.mark.parametrize("reminders", [
    {"useDefault": False},                       # reminders switched off on the event
    popup(5, method="email"),                    # only an email reminder
    {"useDefault": True},                        # defaults, but the calendar has no popup default
    None,                                        # no reminders field at all
])
async def test_event_without_a_popup_reminder_uses_the_admin_default(db, calendar, reminders):
    await calendar([event(reminders=reminders)], defaults=[{"method": "email", "minutes": 10}])
    s = only("events")
    assert await texts(db, at(15, 59), s) == []
    assert await texts(db, at(16, 0), s) == ["Swimming at 16:30 (in 30 min)"]
    s["lead_minutes"] = 45
    assert await texts(db, at(15, 45), s) == ["Swimming at 16:30 (in 45 min)"]


async def test_event_lead_time_is_capped_at_two_hours(db, calendar):
    await calendar([event(reminders=popup(24 * 60))])  # a day-before reminder
    s = only("events")
    assert await texts(db, at(14, 29, 59), s) == []
    assert await texts(db, at(14, 30), s) == ["Swimming at 16:30 (in 2 h)"]
    assert await texts(db, at(15, 10), s) == ["Swimming at 16:30 (in 1 h 20 min)"]


async def test_all_day_events_and_other_calendars_are_left_out(db, calendar):
    all_day = {"id": "ad", "summary": "Half term", "start": {"date": "2026-10-05"}, "end": {"date": "2026-10-06"}}
    await calendar([all_day, event(reminders=popup(30))])
    await calendar([event("Work meeting", reminders=popup(30), event_id="w1")], cal=OTHER_CAL)  # not selected
    assert await texts(db, at(16, 10), only("events")) == ["Swimming at 16:30 (in 20 min)"]


async def test_event_person_comes_from_a_name_prefix(db, calendar):
    await calendar([
        event("Riley: Dentist", start="16:30", reminders=popup(30), event_id="a"),
        event("Note: bring kit", start="16:40", reminders=popup(30), event_id="b"),
    ])
    found = await banners.active_banners(db, at(16, 15), only("events"))
    assert [(b["text"], b["person"]) for b in found] == [
        ("Dentist at 16:30 (in 15 min)", {"name": "Riley", "colour": "#D6A02C",
                                          "avatar": {"kind": "initial", "emoji": None, "url": None}}),
        ("Note: bring kit at 16:40 (in 25 min)", None),  # "Note" isn't a family member: Everyone
    ]
    assert found[0]["key"] == "event:a:2026-10-05T16:30:00+01:00"


async def test_event_just_after_midnight_shows_the_evening_before(db, calendar):
    await calendar([event("Night ferry", start="00:30", reminders=popup(60), day=DAY + timedelta(days=1))])
    assert await texts(db, at(23, 45), only("events") | {"quiet_start": "01:00", "quiet_end": "05:00"}) == [
        "Night ferry at 00:30 (in 45 min)"]


async def test_event_window_across_the_clocks_going_back(db, calendar):
    """25 Oct 2026: 02:00 BST becomes 01:00 GMT. At 01:50 BST (00:50 UTC)
    an event at the second 01:20 (GMT, 01:20 UTC) is 30 real minutes away,
    though its wall-clock time looks earlier."""
    ferry = {"id": "ferry", "summary": "Late ferry", "start": {"dateTime": "2026-10-25T01:20:00+00:00"},
             "end": {"dateTime": "2026-10-25T02:00:00+00:00"}, "reminders": popup(45)}
    await calendar([ferry])
    s = only("events") | {"quiet_start": "12:00", "quiet_end": "13:00"}
    bst = datetime(2026, 10, 25, 1, 50, tzinfo=TZ)            # fold=0: the first 01:50, BST
    assert bst.utcoffset() == timedelta(hours=1)
    assert await texts(db, bst, s) == ["Late ferry at 01:20 (in 30 min)"]
    assert await texts(db, datetime(2026, 10, 25, 0, 34, tzinfo=TZ), s) == []  # 76 min before: too early
    gmt = datetime(2026, 10, 25, 1, 19, tzinfo=TZ, fold=1)     # the second 01:19, GMT
    assert await texts(db, gmt, s) == ["Late ferry at 01:20 (in 1 min)"]
    assert await texts(db, datetime(2026, 10, 25, 1, 20, tzinfo=TZ, fold=1), s) == []  # started


async def test_recurring_instances_and_a_rescheduled_event_get_new_keys(db, calendar, client, frozen):
    # singleEvents=true: each instance of a series has its own id and start.
    await calendar([
        event("Swimming", reminders=popup(30), event_id="swim_20261005T153000Z"),
        event("Swimming", reminders=popup(30), event_id="swim_20261006T153000Z", day=DAY + timedelta(days=1)),
    ])
    s = only("events")
    [today] = await banners.active_banners(db, at(16, 10), s)
    await client.post("/api/banners/dismiss", data={"key": today["key"]})
    assert await texts(db, at(16, 10), s) == []
    tomorrow = at(16, 10, day=DAY + timedelta(days=1))
    assert await texts(db, tomorrow, s) == ["Swimming at 16:30 (in 20 min)"]  # next week's lesson still shows

    # The same event moved an hour later: a new occurrence, so it shows again.
    await calendar([event("Swimming", start="17:30", reminders=popup(30), event_id="swim_20261005T153000Z")])
    assert await texts(db, at(17, 10), s) == ["Swimming at 17:30 (in 20 min)"]


async def test_a_hostile_event_title_is_escaped(db, calendar, client, frozen):
    title = """<img src=x onerror="alert(1)">'</button><script>alert(2)</script>"""
    raw = event(title, reminders=popup(30))
    del raw["id"]  # so the title also stands in for the key (hx-vals, data-key)
    await calendar([raw])
    frozen(at(16, 10))
    html = (await client.get("/banners")).text
    assert "&lt;img src=x" in html
    assert "<img" not in html and "<script" not in html and "</button><script" not in html
    key = re.search(r'data-key="([^"]+)"', html).group(1)
    assert banners.KEY_PATTERN.fullmatch(key)


async def test_reminders_come_through_from_googles_response(db, connected, google):
    """events.list's defaultReminders reach the cache, and the banner then
    works from the cache alone: no Google call, even with Google down."""
    await google_oauth.set_selected_calendars(db, [CAL])
    route = google.get(url__regex=EVENTS_URL_PATTERN)
    route.respond(200, json={"defaultReminders": [{"method": "popup", "minutes": 15}],
                             "items": [event(reminders={"useDefault": True}), event("Piano", "17:00", popup(50), "p")]})
    grid = await google_calendar.get_month_grid(db, 2026, 10)
    assert grid["offline"] is False

    route.respond(503)  # Google down now
    calls = route.call_count
    assert await texts(db, at(16, 15), only("events")) == ["Swimming at 16:30 (in 15 min)",
                                                           "Piano at 17:00 (in 45 min)"]
    assert route.call_count == calls


# --- Homework ---

async def _homework(db, title, due, profile=RILEY, done=0, archived=0, subject="Maths"):
    await db.execute(
        "INSERT INTO homework (profile_id, subject, title, due_date, done, archived) VALUES (?, ?, ?, ?, ?, ?)",
        (profile, subject, title, due.isoformat(), done, archived),
    )
    await db.commit()


async def test_homework_due_tomorrow_shows_from_four_the_day_before(db):
    await _homework(db, "Fractions", DAY + timedelta(days=1))
    s = only("homework")
    assert await texts(db, at(15, 59), s) == []
    found = await banners.active_banners(db, at(16, 0), s)
    assert [b["text"] for b in found] == ["Homework due tomorrow: Fractions (Maths)"]
    assert found[0]["person"]["name"] == "Riley"


async def test_homework_due_today_shows_from_seven_until_done(db):
    await _homework(db, "Spellings", DAY, subject="")
    s = only("homework") | {"quiet_start": "23:00", "quiet_end": "05:00"}
    assert await texts(db, at(6, 59), s) == []
    assert await texts(db, at(7, 0), s) == ["Homework due today: Spellings"]
    assert await texts(db, at(22, 59), s) == ["Homework due today: Spellings"]
    await db.execute("UPDATE homework SET done = 1, done_at = ?", (at(17).isoformat(),))
    await db.commit()
    assert await texts(db, at(17, 1), s) == []


async def test_overdue_archived_and_later_homework_make_no_banner(db):
    await _homework(db, "Old", DAY - timedelta(days=1))
    await _homework(db, "Archived", DAY, archived=1)
    await _homework(db, "Next week", DAY + timedelta(days=7))
    assert await texts(db, at(17), only("homework")) == []


# --- Today's school events ---

async def _approved_event(db, payload, status="approved", profile=None, external_id=None):
    source = await db.execute("INSERT INTO import_sources (kind, source_ref, status) VALUES ('gmail', ?, 'extracted')",
                              (f"m{time.monotonic_ns()}",))
    await db.execute(
        "INSERT INTO import_candidates (source_id, kind, profile_id, payload_json, status, created_table, external_id) "
        "VALUES (?, 'event', ?, ?, ?, 'google_calendar', ?)",
        (source.lastrowid, profile, json.dumps(payload), status, external_id),
    )
    await db.commit()


async def test_school_events_show_from_seven_until_six_on_the_day(db):
    await _approved_event(db, {"title": "Non-uniform day", "date": DAY.isoformat(), "all_day": True})
    await _approved_event(db, {"title": "Sports day", "date": DAY.isoformat(), "all_day": False,
                               "start_time": "14:00"}, profile=RILEY)
    await _approved_event(db, {"title": "Still pending", "date": DAY.isoformat(), "all_day": True}, status="pending")
    await _approved_event(db, {"title": "Tomorrow", "date": (DAY + timedelta(days=1)).isoformat(), "all_day": True})
    s = only("school") | {"quiet_start": "23:00", "quiet_end": "05:00"}
    assert await texts(db, at(6, 59), s) == []
    found = await banners.active_banners(db, at(7, 0), s)
    assert [(b["text"], b["person"] and b["person"]["name"]) for b in found] == [
        ("Today: Non-uniform day", None), ("Today: Sports day at 14:00", "Riley")]
    assert len(await texts(db, at(17, 59), s)) == 2
    assert await texts(db, at(18, 0), s) == []


async def test_a_school_event_also_in_google_shows_once(db, calendar):
    """Approving it wrote it to Google, so it's in the calendar cache too:
    only the school trigger shows it."""
    await _approved_event(db, {"title": "Sports day", "date": DAY.isoformat(), "all_day": False,
                               "start_time": "14:00"}, external_id="hs123")
    await calendar([event("Sports day", "14:00", popup(60), "hs123"), event("Piano", "14:10", popup(60), "p")])
    assert await texts(db, at(13, 30)) == ["Piano at 14:10 (in 40 min)", "Today: Sports day at 14:00"]


async def test_inset_days_and_closures_from_the_term_dates(db):
    await db.execute("INSERT INTO school_periods (kind, start_date, end_date, label, source) "
                     "VALUES ('inset', ?, ?, '', 'manual')", (DAY.isoformat(), DAY.isoformat()))
    await db.execute("INSERT INTO school_periods (kind, start_date, end_date, label, source) "
                     "VALUES ('half_term', ?, ?, 'Half term', 'manual')", (DAY.isoformat(), DAY.isoformat()))
    await db.commit()
    assert await texts(db, at(8), only("school")) == ["Today: INSET day (no school)"]


# --- Chores left late ---

async def test_chores_show_per_person_once_the_evening_group_starts(db):
    await db.executemany("INSERT INTO tasks (profile_id, title, is_completed) VALUES (?, ?, ?)",
                         [(MUM, "Bins", 0), (MUM, "Dishes", 0), (MUM, "Done", 1), (RILEY, "Bag", 0), (3, "Teeth", 1)])
    await db.commit()
    s = only("chores")
    assert await texts(db, at(17, 59), s) == []
    found = await banners.active_banners(db, at(18, 0), s)
    assert [(b["text"], b["person"]["name"]) for b in found] == [
        ("2 chores still to do", "Mum"), ("1 chore still to do", "Riley")]

    await task_service.set_group_boundaries(db, "12:00", "17:00")
    assert len(await texts(db, at(17, 0), s)) == 2
    await db.execute("UPDATE tasks SET is_completed = 1")
    await db.commit()
    assert await texts(db, at(18), s) == []


# --- Quiet hours ---

@pytest.mark.parametrize(("hour", "minute", "quiet"), [
    (20, 59, False), (21, 0, True), (23, 59, True), (0, 0, True), (6, 59, True), (7, 0, False), (12, 0, False),
])
def test_quiet_hours_across_midnight(hour, minute, quiet):
    assert banners.in_quiet_hours(at(hour, minute).time(), "21:00", "07:00") is quiet


def test_quiet_hours_within_one_day():
    assert banners.in_quiet_hours(at(13, 30).time(), "13:00", "14:00")
    assert not banners.in_quiet_hours(at(14, 0).time(), "13:00", "14:00")
    assert not banners.in_quiet_hours(at(12, 59).time(), "13:00", "14:00")


async def test_nothing_shows_in_quiet_hours(db):
    await db.execute("INSERT INTO tasks (profile_id, title) VALUES (1, 'Bins')")
    await db.commit()
    s = banners.default_settings()  # 21:00-07:00
    assert await texts(db, at(20, 59), s) == ["1 chore still to do"]
    assert await texts(db, at(21, 0), s) == []


# --- Dismissal, ordering, sound ---

@pytest.fixture
def frozen(monkeypatch):
    """Freeze the banner clock for routes: frozen(datetime)."""
    def freeze(now):
        monkeypatch.setattr(banners, "_clock", lambda tz: now.astimezone(tz))
    return freeze


async def _two_chores(db):
    await db.executemany("INSERT INTO tasks (profile_id, title) VALUES (?, ?)", [(MUM, "Bins"), (RILEY, "Bag")])
    await db.commit()


async def test_tapping_a_banner_dismisses_that_occurrence_across_reloads(db, client, frozen):
    await _two_chores(db)
    frozen(at(19))
    key = f"chores:{MUM}:{DAY.isoformat()}"
    assert key in (await client.get("/banners")).text

    resp = await client.post("/api/banners/dismiss", data={"key": key})
    assert resp.status_code == 200
    assert key not in resp.text and "chores:3:" in resp.text  # the other one stays
    assert key not in (await client.get("/banners")).text      # stored server-side
    assert key not in (await client.get("/")).text
    # The next day's occurrence is a new key, so it shows again.
    await db.execute("UPDATE tasks SET is_completed = 0")
    await db.commit()
    assert f"chores:{MUM}:{(DAY + timedelta(days=1)).isoformat()}" in [
        b["key"] for b in await banners.active_banners(db, at(19, day=DAY + timedelta(days=1)))]


@pytest.mark.parametrize("key", ["", "nope", "chores:1:" + "x" * 300, "<script>:x"])
async def test_unknown_dismiss_keys_are_ignored(db, client, key):
    resp = await client.post("/api/banners/dismiss", data={"key": key})
    assert resp.status_code == 200 and 'id="banner-bar"' in resp.text
    assert await banners.dismissed_keys(db) == set()


async def test_old_dismissals_are_pruned(db):
    t0 = datetime(2026, 10, 5, 12, tzinfo=TZ)
    assert await banners.dismiss(db, "chores:1:2026-10-05", t0)
    assert await banners.dismiss(db, "chores:1:2026-10-06", t0 + timedelta(days=1))
    assert await banners.dismissed_keys(db) == {"chores:1:2026-10-05", "chores:1:2026-10-06"}
    await banners.dismiss(db, "chores:1:2026-10-08", t0 + timedelta(days=2, seconds=1))
    assert await banners.dismissed_keys(db) == {"chores:1:2026-10-06", "chores:1:2026-10-08"}


async def test_concurrent_dismissals_are_all_kept(db):
    """Quick taps are separate requests on separate connections: none is lost."""
    async def tap(n):
        async with database.get_db() as conn:
            assert await banners.dismiss(conn, f"term:{n}:2026-10-05")

    await asyncio.gather(*(tap(n) for n in range(12)))
    assert await banners.dismissed_keys(db) == {f"term:{n}:2026-10-05" for n in range(12)}


@pytest.mark.parametrize(("event_id", "title"), [
    ("x" * 300, "Swimming"),             # an id far longer than a key may be
    ("id with spaces/and:colons", "Swimming"),
    (None, "Swimming lessons: with a very long title " * 8),  # no id: the title stands in
])
async def test_every_event_key_fits_and_can_be_dismissed(db, calendar, client, frozen, event_id, title):
    raw = event(title, reminders=popup(30))
    if event_id is None:
        del raw["id"]
    else:
        raw["id"] = event_id
    await calendar([raw])
    [found] = await banners.active_banners(db, at(16, 10), only("events"))
    assert len(found["key"]) <= banners.MAX_KEY and banners.KEY_PATTERN.fullmatch(found["key"])
    assert found["key"].endswith(":2026-10-05T16:30:00+01:00")
    assert (await client.post("/api/banners/dismiss", data={"key": found["key"]})).status_code == 200
    assert await banners.active_banners(db, at(16, 10), only("events")) == []


async def test_dismissals_are_capped(db, monkeypatch):
    monkeypatch.setattr(banners, "MAX_DISMISSED", 3)
    t0 = datetime(2026, 10, 5, 12, tzinfo=TZ)
    for n in range(5):
        await banners.dismiss(db, f"term:{n}:2026-10-05", t0 + timedelta(minutes=n))
    assert await banners.dismissed_keys(db) == {"term:2:2026-10-05", "term:3:2026-10-05", "term:4:2026-10-05"}


async def test_more_than_two_banners_order_and_plus_n_more(db, calendar, client, frozen):
    await task_service.set_group_boundaries(db, "12:00", "16:00")
    await _two_chores(db)
    await _homework(db, "Fractions", DAY + timedelta(days=1))
    await _approved_event(db, {"title": "Non-uniform day", "date": DAY.isoformat(), "all_day": True})
    await calendar([event("Piano", "16:50", popup(60), "p"), event("Swimming", "16:30", popup(60), "s")])
    now = at(16, 10)
    assert await texts(db, now) == [
        "Swimming at 16:30 (in 20 min)", "Piano at 16:50 (in 40 min)",  # events first, soonest first
        "Today: Non-uniform day", "Homework due tomorrow: Fractions (Maths)",
        "1 chore still to do", "1 chore still to do",  # same time: family order, Mum then Riley
    ]

    frozen(now)
    html = (await client.get("/banners")).text
    buttons = re.findall(r'<button type="button" class="banner [^"]*"[^>]*>', html)
    assert len(buttons) == 6
    assert all("hidden" not in b for b in buttons[:2])
    assert all("data-panel hidden" in b and "banner-extra" in b for b in buttons[2:])
    assert "+4 more" in html


async def test_sound_flag_follows_its_trigger(db, client, frozen):
    await _two_chores(db)
    await _homework(db, "Fractions", DAY + timedelta(days=1))
    await save(db, chores={"sound": True})
    found = await banners.active_banners(db, at(19))
    assert {b["kind"]: b["sound"] for b in found} == {"homework": False, "chores": True}
    frozen(at(19))
    html = (await client.get("/banners")).text
    assert html.count("data-sound") == 2  # the two chore banners
    assert re.search(r'data-key="homework:[^"]+"\s+aria-label', html)


async def test_switched_off_triggers_show_nothing(db):
    await _two_chores(db)
    await save(db, chores={"on": False})
    assert await texts(db, at(19)) == []


async def test_unreadable_settings_fall_back_to_the_defaults(db):
    from app.database import set_setting
    for junk in ("{not json", "[1, 2]", json.dumps({"lead_minutes": 999, "quiet_start": "7pm", "triggers": "x"})):
        await set_setting(db, banners.SETTINGS_KEY, junk)
        await db.commit()
        assert await banners.get_settings(db) == banners.default_settings()


# --- The dashboard and /api/rev ---

async def test_dashboard_renders_the_bar_between_top_bar_and_grid(db, client, frozen):
    await _two_chores(db)
    frozen(at(19))
    html = (await client.get("/")).text
    assert html.index('class="topbar"') < html.index('id="banner-bar"') < html.index('id="dashboard-scroll"')
    assert "1 chore still to do" in html and "Everyone" not in html
    frozen(at(22))  # quiet hours: the bar is there, empty
    html = (await client.get("/")).text
    assert 'class="banner-bar is-empty"' in html and "chore still to do" not in html


async def test_api_rev_changes_when_a_banner_appears(db, client, frozen, calendar):
    await calendar([event(reminders=popup(25))])
    frozen(at(16, 4))
    before = (await client.get("/api/rev")).json()["widgets"]
    assert (await client.get("/api/rev")).json()["widgets"] == before  # stable
    frozen(at(16, 5))
    after = (await client.get("/api/rev")).json()["widgets"]
    assert [k for k in before if before[k] != after[k]] == ["banners"]


async def test_everyone_pill_for_a_banner_with_no_person(db, client, frozen):
    await db.execute("INSERT INTO school_periods (kind, start_date, end_date, label, source) "
                     "VALUES ('closure', ?, ?, 'Snow day', 'manual')", (DAY.isoformat(), DAY.isoformat()))
    await db.commit()
    frozen(at(8))
    html = (await client.get("/banners")).text
    assert "Today: Snow day (no school)" in html and "person-pill-everyone" in html


# --- Admin ---

@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


FORM = {"on_events": "1", "on_homework": "1", "sound_homework": "1", "lead_minutes": "45",
        "quiet_start": "22:00", "quiet_end": "06:30"}


async def test_admin_saves_the_banner_settings(db, admin_client):
    resp = await admin_client.post("/admin/banners", data=FORM)
    assert resp.status_code == 303 and resp.headers["location"] == admin_url("banners")
    settings = await banners.get_settings(db)
    assert settings == {
        "triggers": {"events": {"on": True, "sound": False}, "homework": {"on": True, "sound": True},
                     "school": {"on": False, "sound": False}, "chores": {"on": False, "sound": False}},
        "lead_minutes": 45, "quiet_start": "22:00", "quiet_end": "06:30",
    }
    html = (await admin_client.get(admin_url("banners").split("#")[0])).text
    assert 'id="banners"' in html and 'name="sound_homework" value="1" checked' in html
    assert 'value="45"' in html and 'value="06:30"' in html


@pytest.mark.parametrize(("changes", "code"), [
    ({"lead_minutes": "4"}, "banner-lead"),
    ({"lead_minutes": "121"}, "banner-lead"),
    ({"lead_minutes": "ten"}, "banner-lead"),
    ({"lead_minutes": ""}, "banner-lead"),
    ({"quiet_start": "25:00"}, "banner-quiet"),
    ({"quiet_end": "7am"}, "banner-quiet"),
    ({"quiet_start": "07:00", "quiet_end": "07:00"}, "banner-quiet"),
])
async def test_admin_rejects_bad_times_and_lead(db, admin_client, changes, code):
    resp = await admin_client.post("/admin/banners", data=FORM | changes)
    assert resp.status_code == 303 and resp.headers["location"] == admin_url("banners", error=code)
    assert await banners.get_settings(db) == banners.default_settings()  # nothing saved
    html = (await admin_client.get(resp.headers["location"])).text
    assert admin.ADMIN_ERRORS[code][1].replace("'", "&#39;") in html


async def test_admin_lead_range_edges_are_allowed(db, admin_client):
    for lead in ("5", "120"):
        await admin_client.post("/admin/banners", data=FORM | {"lead_minutes": lead})
        assert (await banners.get_settings(db))["lead_minutes"] == int(lead)


async def test_admin_banner_settings_need_the_pin(db, client):
    resp = await client.post("/admin/banners", data=FORM)
    assert resp.status_code == 303 and "/admin/login" in resp.headers["location"]
    assert await banners.get_settings(db) == banners.default_settings()
