"""
Google Calendar reads (Section 4.2 of the spec): event fetch across the
Admin-selected calendars, the month grid with Google-style bar packing, and
the week and agenda views (spec 10.5), and the single-day view.

Plain httpx against the REST API, like app/google_tasks.py. OAuth, tokens
and the calendar picker's settings live in app/google_oauth.py.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dtime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app import calendar_cache, http_client
from app.database import CALENDAR_TIMEZONE_SETTING, family_timezone, get_setting, set_setting
from app.google_oauth import (
    DEFAULT_EVENT_COLOR,
    connect,
    get_all_pages,
    get_connected_account,
    get_selected_calendars,
)
from app.services import term_dates

logger = logging.getLogger(__name__)

CALENDAR_METADATA_ENDPOINT = "https://www.googleapis.com/calendar/v3/calendars/primary"
CALENDAR_EVENTS_ENDPOINT_TEMPLATE = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

MAX_BAR_SLOTS = 3  # event bars shown per week in the month grid before "+N more"
# Hard cap on Google time per calendar render (token refresh + every page of
# every selected calendar). The dashboard awaits it inline, so a slow Google
# shows the offline state instead of stalling the page — cf. weather's
# FETCH_DEADLINE. Background sync keeps the full per-request timeouts.
CALENDAR_DEADLINE = 6.0
OUTAGE_KEY = "Google Calendar"
# School term dates (spec 10.6) drawn on the calendar as extra all-day
# "events": local only, never written to Google, and packed after Google's
# own events so they never push a real event into "+N more". Terms show
# only as a start/end marker on the day number. Bank holidays aren't drawn:
# the family's Google calendars usually show them already.
SCHOOL_BAR_KINDS = ("holiday", "half_term", "inset", "closure")
# The week view (spec 10.5): its hour grid covers at least this range and
# stretches to the week's earliest and latest events; an event shorter than
# MIN_BLOCK_MINUTES is drawn that tall so its title fits.
WEEK_HOURS = (7, 21)
WEEK_BAR_SLOTS = 3
MIN_BLOCK_MINUTES = 30
MINUTES_PER_DAY = 24 * 60
# The agenda: today, tomorrow and the rest of the week ahead.
AGENDA_DAYS = 7

# Which of Google's events a view shows (the person filter); None = all.
Keep = Callable[[dict], bool] | None


async def cache_calendar_timezone(db, access_token: str):
    """Fetch the connected calendar's IANA timezone (e.g. 'Europe/London')
    and store it, so 'today'/'this week' get computed in the family's real
    timezone rather than the server's — the container runs in UTC
    regardless of where the G10 actually lives. Best-effort: a failure here
    just leaves the previous (or UTC default) setting in place."""
    try:
        async with http_client.client() as client:
            resp = await client.get(CALENDAR_METADATA_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"})
            resp.raise_for_status()
            tz_name = resp.json().get("timeZone")
        if tz_name:
            await set_setting(db, CALENDAR_TIMEZONE_SETTING, tz_name)
            await db.commit()
    except httpx.HTTPError as exc:
        logger.warning("Couldn't fetch the calendar timezone: %s", http_client.describe(exc))


async def _calendar_timezone(db, access_token: str | None) -> ZoneInfo:
    if not await get_setting(db, CALENDAR_TIMEZONE_SETTING) and access_token:
        # Self-heals accounts connected before this cache existed, and
        # covers the very first widget render right after a fresh connect.
        await cache_calendar_timezone(db, access_token)
    return await family_timezone(db)


def _parse_google_datetime(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _popup_minutes(reminders) -> int | None:
    """The smallest popup reminder's minutes, or None if there's none."""
    minutes = [
        r["minutes"]
        for r in reminders or []
        if isinstance(r, dict)
        and r.get("method") == "popup"
        and isinstance(r.get("minutes"), int)
        and r["minutes"] >= 0
    ]
    return min(minutes) if minutes else None


def reminder_minutes(raw: dict, default_reminders: list | None) -> int | None:
    """The event's own lead time for the notification banner (spec 10.1):
    its smallest popup override, or with useDefault the calendar's
    defaultReminders (from the same events.list response). None = no popup
    reminder either way (the banner then uses its Admin default)."""
    reminders = raw.get("reminders") or {}
    if reminders.get("useDefault", True) and not reminders.get("overrides"):
        return _popup_minutes(default_reminders)
    return _popup_minutes(reminders.get("overrides"))


def _format_event(raw: dict, default_reminders: list | None = None) -> dict:
    start = raw.get("start", {})
    end = raw.get("end", {})
    all_day = "date" in start
    if all_day:
        start_dt = datetime.fromisoformat(start["date"])
        # Google's all-day end.date is EXCLUSIVE (the day *after* the last
        # day the event covers) — subtract one to get the actual last day.
        end_dt = datetime.fromisoformat(end["date"]) - timedelta(days=1) if end.get("date") else start_dt
        time_label = "All day"
    else:
        # Keep the offset Google already gives us (the calendar owner's
        # real timezone, e.g. +01:00 for Europe/London) — do NOT convert
        # to the server's local time, which is UTC inside the container
        # regardless of where the family actually lives.
        start_dt = _parse_google_datetime(start["dateTime"])
        end_dt = _parse_google_datetime(end["dateTime"]) if end.get("dateTime") else start_dt
        time_label = start_dt.strftime("%I:%M %p").lstrip("0")
    start_date = start_dt.date()
    end_date = end_dt.date()
    return {
        "title": raw.get("summary") or "(untitled)",
        "time_label": time_label,
        "all_day": all_day,
        "date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "is_multi_day": end_date != start_date,
        "sort_key": start_dt.isoformat(),
        # Where a timed event ends (Google's offset), for the week view.
        # Rows cached before it existed lack it: they're drawn an hour long.
        "end_key": None if all_day else end_dt.isoformat(),
        # For the notification banner (app/services/banners.py), which reads
        # events from calendar_cache. Rows cached before these existed lack
        # them: banners treats that as "no id" / "no reminder".
        "id": raw.get("id"),
        "reminder_minutes": reminder_minutes(raw, default_reminders),
    }


async def _school_periods(db, first: date, last: date) -> tuple[list[dict], dict[str, str]]:
    """(school "events" touching [first, last], {ISO date: "Term starts" /
    "Term ends"}) from the term dates. Shaped like _format_event's output,
    plus "school": the period's kind."""
    events, markers = [], {}
    for period in await term_dates.periods_between(db, first, last):
        if period["kind"] == "term":
            markers[period["start_date"]] = "Term starts"
            markers.setdefault(period["end_date"], "Term ends")
        elif period["kind"] in SCHOOL_BAR_KINDS:
            events.append(
                {
                    "title": period["label"],
                    "time_label": "All day",
                    "all_day": True,
                    "date": period["start_date"],
                    "end_date": period["end_date"],
                    "is_multi_day": period["end_date"] != period["start_date"],
                    "sort_key": f"{period['start_date']}T00:00:00",
                    "school": period["kind"],
                }
            )
    return events, markers


async def fetch_events(access_token: str, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
    url = CALENDAR_EVENTS_ENDPOINT_TEMPLATE.format(calendar_id=quote(calendar_id, safe=""))
    meta: dict = {}
    items = await get_all_pages(
        url,
        access_token,
        {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        },
        meta=meta,
    )
    events = [_format_event(item, meta.get("defaultReminders")) for item in items]
    events.sort(key=lambda e: e["sort_key"])
    return events


async def _fetch_selected_events(
    access_token, tz, calendars: list[dict], start: date, end: date
) -> dict[str, list[dict] | None]:
    """{calendar id: its colour-tagged events, or None if that calendar
    failed}. One calendar failing (unshared, network blip) doesn't blank
    the others."""
    time_min = datetime.combine(start, dtime.min, tzinfo=tz)
    time_max = datetime.combine(end, dtime.min, tzinfo=tz)
    # Concurrently: the request path waits on the slowest calendar, not the sum.
    results = await asyncio.gather(
        *(fetch_events(access_token, cal["id"], time_min, time_max) for cal in calendars),
        return_exceptions=True,
    )
    by_calendar: dict[str, list[dict] | None] = {}
    for cal, result in zip(calendars, results, strict=True):
        if isinstance(result, httpx.HTTPError):
            http_client.report_failure(
                logger,
                OUTAGE_KEY,
                "Couldn't fetch calendar events; showing offline: %s",
                http_client.describe(result),
            )
            by_calendar[cal["id"]] = None
            continue
        if isinstance(result, BaseException):
            raise result
        for event in result:
            event["color"] = cal.get("color") or DEFAULT_EVENT_COLOR
            event["calendar_id"] = cal["id"]  # whose events these are (the person filter)
        by_calendar[cal["id"]] = result
    return by_calendar


async def _selection(db) -> tuple[list[dict], str]:
    """The selected calendars and their calendar_cache selection key."""
    calendars = await get_selected_calendars(db)
    return calendars, calendar_cache.selection_key(await get_connected_account(db), calendars)


async def _fetch_span(db, span: Callable[[datetime], tuple[date, date]]) -> dict | None:
    """One live fetch: None = not connected; otherwise {"now", "tz",
    "range", "selection", "by_calendar"} (see _fetch_selected_events;
    every calendar is None when the token refresh itself failed)."""
    access_token, offline = await connect(db)
    if not access_token and not offline:
        return None
    tz = await _calendar_timezone(db, access_token)
    now = datetime.now(tz)
    start, end = span(now)
    calendars, selection = await _selection(db)
    if access_token:
        by_calendar = await _fetch_selected_events(access_token, tz, calendars, start, end)
    else:
        by_calendar = {cal["id"]: None for cal in calendars}
    return {"now": now, "tz": tz, "range": (start, end), "selection": selection, "by_calendar": by_calendar}


def _all_answered(loaded: dict) -> bool:
    return all(events is not None for events in loaded["by_calendar"].values())


async def _store_cache(db, loaded: dict) -> bool:
    """Save the calendars that answered. Skipped if the account or the
    selection changed while the fetch was out (the cache was cleared for
    it). True if anything was saved."""
    answered = {cal_id: events for cal_id, events in loaded["by_calendar"].items() if events is not None}
    if not answered:
        return False
    try:
        if (await _selection(db))[1] != loaded["selection"]:
            return False
        start, end = loaded["range"]
        await calendar_cache.store(db, loaded["selection"], start, end, answered)
    except sqlite3.Error as exc:  # e.g. briefly locked: the live grid still renders
        logger.warning("Couldn't save the calendar cache: %s", type(exc).__name__)
        return False
    return True


def _updated_label(fetched_at: datetime, now: datetime) -> str:
    """ "14:05" for a copy from today (family time), else "Fri 25 Sep, 14:05"
    so an old copy doesn't pass for a current one. Only the note's clock is
    converted; event times keep Google's offsets."""
    local = fetched_at.astimezone(now.tzinfo)
    if local.date() == now.date():
        return local.strftime("%H:%M")
    return f"{local:%a} {local.day} {local:%b}, {local:%H:%M}"


async def _load_events(db, span: Callable[[datetime], tuple[date, date]], cache: bool = False) -> dict | None:
    """Everything the request path needs from Google, under one hard
    deadline. None = not connected; otherwise {"now", "tz", "events",
    "offline", "updated_label"}. `span` maps "now" in the calendar's
    timezone to the [start, end) date range to fetch.

    Calendars that answered are shown live and, when `cache` is set (the
    month grid; a day view is served from its month's copy), saved to
    calendar_cache. A calendar that failed — Google down, slow, or just
    that one calendar erroring — is shown from its newest cached copy
    covering the range, and `updated_label` says when the oldest copy used
    was fetched. `offline` is set only for a failed calendar with no copy."""
    try:
        loaded = await asyncio.wait_for(_fetch_span(db, span), CALENDAR_DEADLINE)
    except asyncio.TimeoutError:
        # Only a connected account makes network calls, so a timeout means
        # connected-but-slow: render the offline state, never block the page.
        http_client.report_failure(
            logger, OUTAGE_KEY, "Google Calendar took over %ss; showing offline", CALENDAR_DEADLINE
        )
        tz = await family_timezone(db)
        now = datetime.now(tz)
        calendars, selection = await _selection(db)
        loaded = {
            "now": now,
            "tz": tz,
            "range": span(now),
            "selection": selection,
            "by_calendar": {cal["id"]: None for cal in calendars},
        }
    if loaded is None:
        return None

    if _all_answered(loaded):
        http_client.report_success(logger, OUTAGE_KEY)
    if cache:
        await _store_cache(db, loaded)

    events: list[dict] = []
    offline = False
    oldest: datetime | None = None
    for cal_id, live in loaded["by_calendar"].items():
        if live is not None:
            events.extend(live)
            continue
        start, end = loaded["range"]
        cached = await calendar_cache.load(db, loaded["selection"], start, end, cal_id)
        if cached is None:
            offline = True
            continue
        cached_events, fetched_at = cached
        events.extend(_tagged(cached_events, cal_id))
        oldest = fetched_at if oldest is None else min(oldest, fetched_at)
    loaded.update(
        events=events,
        offline=offline,
        updated_label=_updated_label(oldest, loaded["now"]) if oldest else None,
    )
    return loaded


async def cached_events(db, start: date, end: date) -> list[dict]:
    """Every selected calendar's events touching [start, end) from
    calendar_cache only — no Google call, so it answers the same online or
    off. The cache is kept fresh by the month grid and refresh_cache (every
    5 min). Calendars with no copy covering the range are skipped."""
    calendars, selection = await _selection(db)
    events: list[dict] = []
    for cal in calendars:
        cached = await calendar_cache.load(db, selection, start, end, cal["id"])
        if cached is not None:
            events.extend(_tagged(cached[0], cal["id"]))
    return events


def _tagged(events: list[dict], calendar_id: str) -> list[dict]:
    """Cached events with their calendar id (rows cached before it was
    stored lack it)."""
    for event in events:
        event.setdefault("calendar_id", calendar_id)
    return events


async def insert_event(access_token: str, calendar_id: str, event: dict) -> dict:
    """events.insert (needs the calendar.events scope). `event` is the
    Calendar API's Event resource; one with a client-chosen `id` that
    already exists comes back as HTTP 409. Raises httpx.HTTPError."""
    url = CALENDAR_EVENTS_ENDPOINT_TEMPLATE.format(calendar_id=quote(calendar_id, safe=""))
    async with http_client.client() as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {access_token}"}, json=event)
        resp.raise_for_status()
        return resp.json()


async def get_event(access_token: str, calendar_id: str, event_id: str) -> dict:
    """events.get: one event by id (a deleted one comes back with status
    "cancelled"). Raises httpx.HTTPError, or ValueError for a non-JSON body."""
    url = CALENDAR_EVENTS_ENDPOINT_TEMPLATE.format(calendar_id=quote(calendar_id, safe=""))
    async with http_client.client() as client:
        resp = await client.get(
            f"{url}/{quote(event_id, safe='')}", headers={"Authorization": f"Bearer {access_token}"}
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_cache(db) -> bool:
    """Scheduler job: re-fetch the current month's grid range, stretched to
    cover the agenda's week ahead too (no request deadline: background work
    keeps the full per-request timeouts), and save every calendar that
    answered, so the cache stays fresh even when nobody taps the wall. True
    if the cache was updated."""
    loaded = await _fetch_span(db, _refresh_span)
    if loaded is None:
        return False
    if _all_answered(loaded):
        http_client.report_success(logger, OUTAGE_KEY)
    return await _store_cache(db, loaded)


def _grid_span(now: datetime, year: int | None = None, month: int | None = None) -> tuple[date, date]:
    """The Monday-start 6-week range shown for a month (default: now's)."""
    first = date(year or now.year, month or now.month, 1)
    grid_start = first - timedelta(days=first.weekday())  # Monday on/before the 1st
    return grid_start, grid_start + timedelta(days=42)


def _refresh_span(now: datetime) -> tuple[date, date]:
    """This month's grid, and the agenda's days even when they run past it."""
    start, end = _grid_span(now)
    return start, max(end, now.date() + timedelta(days=AGENDA_DAYS))


def week_start(day: date) -> date:
    """The Monday on or before `day`."""
    return day - timedelta(days=day.weekday())


def _kept(events: list[dict], keep: Keep) -> list[dict]:
    return events if keep is None else [e for e in events if keep(e)]


def _pack_week(events: list[dict], first: date, days: list[dict], slots: int = MAX_BAR_SLOTS) -> tuple[list[dict], int]:
    """Google-style bars for the 7 days from `first` (a Monday): one per
    event touching the week, clipped to it and packed into up to `slots`
    rows so overlapping events don't collide. An event with no free row
    gets no bar; each of its days' hidden_count goes up instead ("+N
    more"). Returns (bars, rows used)."""
    last = first + timedelta(days=6)
    week_events = [
        e for e in events if date.fromisoformat(e["date"]) <= last and date.fromisoformat(e["end_date"]) >= first
    ]
    # Google's events before the school's (SCHOOL_BAR_KINDS); then
    # earlier-starting first; among ties, longer events first so they
    # claim a slot before a cluster of short same-day events do.
    week_events.sort(
        key=lambda e: (
            "school" in e,
            e["sort_key"],
            -(date.fromisoformat(e["end_date"]) - date.fromisoformat(e["date"])).days,
        )
    )

    # The columns each slot already uses. Google's events come in start
    # order, so for them "the first slot free across my columns" is the
    # same as "the first slot whose last bar ended before me"; the
    # school's, placed afterwards, can then fill any free span, even
    # one to the left of a Google bar.
    slot_cols: list[set[int]] = [set() for _ in range(slots)]
    bars: list[dict] = []
    rows = 0
    for event in week_events:
        e_start = date.fromisoformat(event["date"])
        e_end = date.fromisoformat(event["end_date"])
        col_start = max(1, (max(e_start, first) - first).days + 1)
        col_end = min(7, (min(e_end, last) - first).days + 1)
        cols = set(range(col_start, col_end + 1))

        slot = next((s for s in range(slots) if not slot_cols[s] & cols), None)
        if slot is None:
            for i in range(col_start - 1, col_end):
                days[i]["hidden_count"] += 1
            continue
        slot_cols[slot] |= cols
        bars.append({"event": event, "col_start": col_start, "col_end": col_end, "slot": slot})
        rows = max(rows, slot + 1)
    return bars, rows


def _day_cell(d: date, today: date, term_markers: dict[str, str], month: int | None = None) -> dict:
    return {
        "date": d.isoformat(),
        "day": d.day,
        "weekday": d.strftime("%a"),
        "label": d.strftime("%A, %d %B").replace(" 0", " "),
        "in_month": month is None or d.month == month,
        "is_today": d == today,
        "is_weekend": d.weekday() >= 5,
        "hidden_count": 0,
        "term_marker": term_markers.get(d.isoformat()),
    }


async def get_month_grid(db, year: int | None = None, month: int | None = None, keep: Keep = None) -> dict | None:
    """None means not connected. Otherwise a Monday-start 6-week grid for
    the given month (defaults to the current month, in the calendar's own
    timezone). Each week carries its 7 days (date/in_month/today/weekend/
    hidden_count) and a list of event "bars" (_pack_week): up to
    MAX_BAR_SLOTS rows, the rest counted per day as "+N more". `keep`
    (the person filter) picks which of Google's events are shown; the
    school's term dates always are.

    If Google is unreachable the grid renders from the saved copy
    ("updated_label" set), or failing that with "offline": True and
    whatever events could be fetched — a network blip must never take down
    the whole dashboard."""

    def grid_span(now: datetime) -> tuple[date, date]:
        return _grid_span(now, year, month)

    loaded = await _load_events(db, grid_span, cache=True)
    if loaded is None:
        return None
    now, offline = loaded["now"], loaded["offline"]
    year = year or now.year
    month = month or now.month
    today = now.date()
    first_of_month = date(year, month, 1)
    grid_start, grid_end = grid_span(now)
    school_events, term_markers = await _school_periods(db, grid_start, grid_end - timedelta(days=1))
    all_events = [*_kept(loaded["events"], keep), *school_events]

    weeks = []
    cursor = grid_start
    for _ in range(6):
        days = [_day_cell(cursor + timedelta(days=i), today, term_markers, month) for i in range(7)]
        bars, row_count = _pack_week(all_events, cursor, days)
        weeks.append({"days": days, "bars": bars, "row_count": row_count})
        cursor += timedelta(days=7)

    prev_month, prev_year = (12, year - 1) if month == 1 else (month - 1, year)
    next_month, next_year = (1, year + 1) if month == 12 else (month + 1, year)

    return {
        "year": year,
        "month": month,
        "label": first_of_month.strftime("%B %Y"),
        "weeks": weeks,
        "today": today.isoformat(),
        "is_current_month": (year, month) == (now.year, now.month),
        "prev": {"year": prev_year, "month": prev_month},
        "next": {"year": next_year, "month": next_month},
        "offline": offline,
        "updated_label": loaded["updated_label"],
    }


def _event_minutes(raw: str | None) -> tuple[date, int] | None:
    """(its date, minutes after midnight) in the event's own offset — the
    time Google gives, never converted to the container's clock."""
    try:
        moment = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None
    return (moment.date(), moment.hour * 60 + moment.minute) if moment else None


def _timed_span(event: dict) -> tuple[date, int, int] | None:
    """(day, start minute, end minute) for a timed event that fits in one
    day of the week view's time grid (one ending at midnight counts), else
    None: it's drawn in the all-day row instead."""
    if event.get("all_day") or event.get("school"):
        return None
    start = _event_minutes(event.get("sort_key"))
    if start is None:
        return None
    day, begins = start
    end = _event_minutes(event.get("end_key"))
    if end is None:
        ends = min(begins + 60, MINUTES_PER_DAY)  # cached before end_key existed
    elif end[0] == day:
        ends = end[1]
    elif end[0] == day + timedelta(days=1) and end[1] == 0:
        ends = MINUTES_PER_DAY
    else:
        return None
    return day, begins, max(ends, begins)


def _lay_out_day(items: list[tuple[int, int, dict]]) -> list[dict]:
    """Side-by-side lanes for overlapping timed events in one day column:
    each cluster of overlapping events shares the column's width between
    its lanes."""
    placed: list[dict] = []
    cluster: list[dict] = []
    lane_ends: list[int] = []
    cluster_end = -1

    def close():
        for item in cluster:
            item["lanes"] = len(lane_ends)

    for begins, ends, event in sorted(items, key=lambda i: (i[0], -i[1])):
        if cluster and begins >= cluster_end:
            close()
            cluster, lane_ends = [], []
        # A short event is drawn MIN_BLOCK_MINUTES tall: that's what it overlaps.
        drawn_end = max(ends, begins + MIN_BLOCK_MINUTES)
        lane = next((i for i, end in enumerate(lane_ends) if end <= begins), None)
        if lane is None:
            lane = len(lane_ends)
            lane_ends.append(drawn_end)
        else:
            lane_ends[lane] = drawn_end
        item = {"event": event, "begins": begins, "ends": ends, "lane": lane}
        cluster.append(item)
        placed.append(item)
        cluster_end = max(cluster_end, drawn_end)
    close()
    return placed


async def get_week(db, start: date | None = None, keep: Keep = None) -> dict | None:
    """None means not connected. Otherwise the 7 days from `start` (a
    Monday; default this week): an all-day row of bars (all-day and
    multi-day events, and the school's term dates, packed like the month
    grid) and each day's timed events positioned on an hour grid. The grid
    runs WEEK_HOURS, stretched to fit the week's earliest and latest
    events. Offline behaviour as get_month_grid (a week inside a cached
    month is served from that month's copy)."""

    def span(now: datetime) -> tuple[date, date]:
        first = start or week_start(now.date())
        return first, first + timedelta(days=7)

    loaded = await _load_events(db, span, cache=True)
    if loaded is None:
        return None
    now = loaded["now"]
    today = now.date()
    first, end = span(now)
    last = end - timedelta(days=1)
    school_events, term_markers = await _school_periods(db, first, last)
    days = [_day_cell(first + timedelta(days=i), today, term_markers) for i in range(7)]

    timed: dict[date, list[tuple[int, int, dict]]] = {}
    banner_events = list(school_events)
    for event in _kept(loaded["events"], keep):
        fits = _timed_span(event)
        if fits is None:
            banner_events.append(event)
        elif first <= fits[0] <= last:
            timed.setdefault(fits[0], []).append((fits[1], fits[2], event))
    bars, row_count = _pack_week(banner_events, first, days, WEEK_BAR_SLOTS)

    items = [item for day_items in timed.values() for item in day_items]
    first_hour = min([WEEK_HOURS[0], *(b // 60 for b, _, _ in items)])
    last_hour = max([WEEK_HOURS[1], *(-(-max(e, b + MIN_BLOCK_MINUTES) // 60) for b, e, _ in items)])
    last_hour = min(last_hour, 24)
    span_minutes = (last_hour - first_hour) * 60
    for i, day in enumerate(days):
        blocks = _lay_out_day(timed.get(first + timedelta(days=i), []))
        for block in blocks:
            begins = block["begins"] - first_hour * 60
            length = max(block["ends"] - block["begins"], MIN_BLOCK_MINUTES)
            block["top"] = round(100 * begins / span_minutes, 3)
            block["height"] = round(100 * min(length, span_minutes - begins) / span_minutes, 3)
        day["blocks"] = blocks

    return {
        "start": first.isoformat(),
        "label": _range_label(first, last),
        "days": days,
        "bars": bars,
        "row_count": row_count,
        "hours": [f"{h:02d}:00" for h in range(first_hour, last_hour)],
        "today": today.isoformat(),
        "is_current_week": first == week_start(today),
        "prev": (first - timedelta(days=7)).isoformat(),
        "next": (first + timedelta(days=7)).isoformat(),
        "offline": loaded["offline"],
        "updated_label": loaded["updated_label"],
    }


def _range_label(first: date, last: date) -> str:
    """ "28 Sep – 4 Oct 2026" (no leading zeros, cross-platform)."""
    if first.year != last.year:
        return f"{first.day} {first:%b %Y} – {last.day} {last:%b %Y}"
    return f"{first.day} {first:%b} – {last.day} {last:%b %Y}"


def _day_order(events: list[dict]) -> list[dict]:
    """All-day events first, then by time: the day view's and agenda's order."""
    return sorted(events, key=lambda e: (0 if e["all_day"] else 1, e["sort_key"]))


def _day_school(day: date, school_events: list[dict], term_markers: dict[str, str]) -> list[dict]:
    """The school's periods touching `day`, and its term start/end marker."""
    iso = day.isoformat()
    found = [e for e in school_events if e["date"] <= iso <= e["end_date"]]
    marker = term_markers.get(iso)
    if marker:
        found.append({"title": marker, "time_label": "All day", "all_day": True, "school": "term"})
    return found


def _last_day(event: dict) -> str:
    """The last day an event is on (ISO). A timed event ending at exactly
    midnight ends the day before: 22:00-00:00 isn't on the next day."""
    end = _event_minutes(event.get("end_key"))
    if not event.get("all_day") and end is not None and end[1] == 0 and event["end_date"] > event["date"]:
        return (end[0] - timedelta(days=1)).isoformat()
    return event["end_date"]


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return day.strftime("%A, %d %B").replace(" 0", " ")


async def get_agenda(db, keep: Keep = None) -> dict | None:
    """None means not connected. Otherwise the family's next AGENDA_DAYS
    days as a list: today and tomorrow always (maybe "Nothing scheduled"),
    later days only when something is on. Each day lists the school's term
    dates first, then all-day events, then by time; a multi-day event shows
    on each of its days, its time only on the first. Offline behaviour as
    get_month_grid (refresh_cache keeps these days cached)."""

    def span(now: datetime) -> tuple[date, date]:
        return now.date(), now.date() + timedelta(days=AGENDA_DAYS)

    loaded = await _load_events(db, span, cache=True)
    if loaded is None:
        return None
    today = loaded["now"].date()
    school_events, term_markers = await _school_periods(db, today, today + timedelta(days=AGENDA_DAYS - 1))
    events = _day_order(_kept(loaded["events"], keep))
    days = []
    for offset in range(AGENDA_DAYS):
        day = today + timedelta(days=offset)
        iso = day.isoformat()
        on_day = [
            e if e["date"] == iso else {**e, "time_label": "Continues"}
            for e in events
            if e["date"] <= iso <= _last_day(e)
        ]
        shown = [*_day_school(day, school_events, term_markers), *on_day]
        if shown or offset < 2:
            days.append({"date": iso, "label": _day_label(day, today), "is_today": offset == 0, "events": shown})
    return {
        "days": days,
        "today": today.isoformat(),
        "offline": loaded["offline"],
        "updated_label": loaded["updated_label"],
    }


async def get_day_events(db, target: date, keep: Keep = None) -> dict | None:
    """None means not connected. Otherwise {"events": [...], "offline": bool,
    "updated_label": "HH:MM" when served from the cache}
    — every event on the given day across all selected calendars: the
    school's term dates first (they frame the day), then all-day events,
    then by time."""
    loaded = await _load_events(db, lambda _now: (target, target + timedelta(days=1)))
    if loaded is None:
        return None
    school_events, term_markers = await _school_periods(db, target, target)
    events = [*_day_school(target, school_events, term_markers), *_day_order(_kept(loaded["events"], keep))]
    return {"events": events, "offline": loaded["offline"], "updated_label": loaded["updated_label"]}
