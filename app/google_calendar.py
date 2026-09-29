"""
Google Calendar reads (Section 4.2 of the spec): event fetch across the
Admin-selected calendars, the month grid with Google-style bar packing, and
the single-day view.

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


async def cache_calendar_timezone(db, access_token: str):
    """Fetch the connected calendar's IANA timezone (e.g. 'Europe/London')
    and store it, so 'today'/'this week' get computed in the family's real
    timezone rather than the server's — the container runs in UTC
    regardless of where the G10 actually lives. Best-effort: a failure here
    just leaves the previous (or UTC default) setting in place."""
    try:
        async with http_client.client() as client:
            resp = await client.get(
                CALENDAR_METADATA_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
            )
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
        r["minutes"] for r in reminders or []
        if isinstance(r, dict) and r.get("method") == "popup" and isinstance(r.get("minutes"), int)
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
        end_dt = (
            datetime.fromisoformat(end["date"]) - timedelta(days=1)
            if end.get("date") else start_dt
        )
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
            events.append({
                "title": period["label"],
                "time_label": "All day",
                "all_day": True,
                "date": period["start_date"],
                "end_date": period["end_date"],
                "is_multi_day": period["end_date"] != period["start_date"],
                "sort_key": f"{period['start_date']}T00:00:00",
                "school": period["kind"],
            })
    return events, markers


async def fetch_events(access_token: str, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
    url = CALENDAR_EVENTS_ENDPOINT_TEMPLATE.format(calendar_id=quote(calendar_id, safe=""))
    meta: dict = {}
    items = await get_all_pages(url, access_token, {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 250,
    }, meta=meta)
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
                logger, OUTAGE_KEY, "Couldn't fetch calendar events; showing offline: %s",
                http_client.describe(result),
            )
            by_calendar[cal["id"]] = None
            continue
        if isinstance(result, BaseException):
            raise result
        for event in result:
            event["color"] = cal.get("color") or DEFAULT_EVENT_COLOR
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
    """"14:05" for a copy from today (family time), else "Fri 25 Sep, 14:05"
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
        loaded = {"now": now, "tz": tz, "range": span(now), "selection": selection,
                  "by_calendar": {cal["id"]: None for cal in calendars}}
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
        events.extend(cached_events)
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
            events.extend(cached[0])
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


async def refresh_cache(db) -> bool:
    """Scheduler job: re-fetch the current month's grid range (no request
    deadline: background work keeps the full per-request timeouts) and
    save every calendar that answered, so the cache stays fresh even when
    nobody taps the wall. True if the cache was updated."""
    loaded = await _fetch_span(db, _grid_span)
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


async def get_month_grid(db, year: int | None = None, month: int | None = None) -> dict | None:
    """None means not connected. Otherwise a Monday-start 6-week grid for
    the given month (defaults to the current month, in the calendar's own
    timezone). Each week carries its 7 days (date/in_month/today/weekend/
    hidden_count) and a list of event "bars" — Google-style, one per event
    touching that week, clipped to the week and packed into up to
    MAX_BAR_SLOTS rows so overlapping events don't collide. Events beyond
    that cap don't get a bar; the day(s) they're on get hidden_count
    incremented instead (surfaced as "+N more" in the template).

    If Google is unreachable the grid renders from the saved copy
    ("updated_label" set), or failing that with "offline": True and
    whatever events could be fetched — a network blip must never take down
    the whole dashboard."""
    def grid_span(now: datetime) -> tuple[date, date]:
        return _grid_span(now, year, month)

    loaded = await _load_events(db, grid_span, cache=True)
    if loaded is None:
        return None
    now, all_events, offline = loaded["now"], loaded["events"], loaded["offline"]
    year = year or now.year
    month = month or now.month
    today = now.date()
    first_of_month = date(year, month, 1)
    grid_start, grid_end = grid_span(now)
    school_events, term_markers = await _school_periods(db, grid_start, grid_end - timedelta(days=1))
    all_events = [*all_events, *school_events]

    weeks = []
    cursor = grid_start
    for _ in range(6):
        week_start = cursor
        week_end = cursor + timedelta(days=6)

        days = []
        for i in range(7):
            d = cursor + timedelta(days=i)
            days.append({
                "date": d.isoformat(),
                "day": d.day,
                "in_month": d.month == month,
                "is_today": d == today,
                "is_weekend": d.weekday() >= 5,
                "hidden_count": 0,
                "term_marker": term_markers.get(d.isoformat()),
            })

        week_events = [
            e for e in all_events
            if date.fromisoformat(e["date"]) <= week_end
            and date.fromisoformat(e["end_date"]) >= week_start
        ]
        # Google's events before the school's (SCHOOL_BAR_KINDS); then
        # earlier-starting first; among ties, longer events first so they
        # claim a slot before a cluster of short same-day events do.
        week_events.sort(key=lambda e: (
            "school" in e,
            e["sort_key"],
            -(date.fromisoformat(e["end_date"]) - date.fromisoformat(e["date"])).days,
        ))

        # The columns each slot already uses. Google's events come in start
        # order, so for them "the first slot free across my columns" is the
        # same as "the first slot whose last bar ended before me"; the
        # school's, placed afterwards, can then fill any free span, even
        # one to the left of a Google bar.
        slot_cols: list[set[int]] = [set() for _ in range(MAX_BAR_SLOTS)]
        bars = []
        for event in week_events:
            e_start = date.fromisoformat(event["date"])
            e_end = date.fromisoformat(event["end_date"])
            col_start = max(1, (max(e_start, week_start) - week_start).days + 1)
            col_end = min(7, (min(e_end, week_end) - week_start).days + 1)
            cols = set(range(col_start, col_end + 1))

            slot = next((s for s in range(MAX_BAR_SLOTS) if not slot_cols[s] & cols), None)
            if slot is None:
                for i in range(col_start - 1, col_end):
                    days[i]["hidden_count"] += 1
                continue
            slot_cols[slot] |= cols
            bars.append({"event": event, "col_start": col_start, "col_end": col_end, "slot": slot})

        row_count = max((bar["slot"] for bar in bars), default=-1) + 1
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


async def get_day_events(db, target: date) -> dict | None:
    """None means not connected. Otherwise {"events": [...], "offline": bool,
    "updated_label": "HH:MM" when served from the cache}
    — every event on the given day across all selected calendars, all-day
    events first then by time."""
    loaded = await _load_events(db, lambda _now: (target, target + timedelta(days=1)))
    if loaded is None:
        return None
    school_events, term_markers = await _school_periods(db, target, target)
    events = loaded["events"]
    events.sort(key=lambda e: (0 if e["all_day"] else 1, e["sort_key"]))
    marker = term_markers.get(target.isoformat())
    if marker:
        school_events.append({"title": marker, "time_label": "All day", "all_day": True, "school": "term"})
    events = [*school_events, *events]  # the school's first: they frame the day
    return {"events": events, "offline": loaded["offline"], "updated_label": loaded["updated_label"]}
