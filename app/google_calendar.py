"""
Google Calendar reads (Section 4.2 of the spec): event fetch across the
Admin-selected calendars, the month grid with Google-style bar packing, and
the single-day view.

Plain httpx against the REST API, like app/google_tasks.py. OAuth, tokens
and the calendar picker's settings live in app/google_oauth.py.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dtime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app import http_client
from app.database import CALENDAR_TIMEZONE_SETTING, family_timezone, get_setting, set_setting
from app.google_oauth import DEFAULT_EVENT_COLOR, connect, get_all_pages, get_selected_calendars

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


def _format_event(raw: dict) -> dict:
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
    }


async def fetch_events(access_token: str, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
    url = CALENDAR_EVENTS_ENDPOINT_TEMPLATE.format(calendar_id=quote(calendar_id, safe=""))
    items = await get_all_pages(url, access_token, {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 250,
    })
    events = [_format_event(item) for item in items]
    events.sort(key=lambda e: e["sort_key"])
    return events


async def _fetch_selected_events(db, access_token, tz, start: date, end: date) -> tuple[list[dict], bool]:
    """Events from every Admin-selected calendar, colour-tagged. One
    calendar failing (unshared, network blip) doesn't blank the others —
    it just flags the result as partial/offline."""
    calendars = await get_selected_calendars(db)
    time_min = datetime.combine(start, dtime.min, tzinfo=tz)
    time_max = datetime.combine(end, dtime.min, tzinfo=tz)
    # Concurrently: the request path waits on the slowest calendar, not the sum.
    results = await asyncio.gather(
        *(fetch_events(access_token, cal["id"], time_min, time_max) for cal in calendars),
        return_exceptions=True,
    )
    events: list[dict] = []
    offline = False
    for cal, result in zip(calendars, results, strict=True):
        if isinstance(result, httpx.HTTPError):
            http_client.report_failure(
                logger, OUTAGE_KEY, "Couldn't fetch calendar events; showing offline: %s",
                http_client.describe(result),
            )
            offline = True
            continue
        if isinstance(result, BaseException):
            raise result
        for event in result:
            event["color"] = cal.get("color") or DEFAULT_EVENT_COLOR
        events.extend(result)
    return events, offline


async def _load_events(db, span: Callable[[datetime], tuple[date, date]]) -> dict | None:
    """Everything the request path needs from Google, under one hard
    deadline. None = not connected; otherwise {"now", "tz", "events",
    "offline"}. `span` maps "now" in the calendar's timezone to the
    [start, end) date range to fetch."""

    async def load() -> dict | None:
        access_token, offline = await connect(db)
        if not access_token and not offline:
            return None
        tz = await _calendar_timezone(db, access_token)
        now = datetime.now(tz)
        events: list[dict] = []
        if access_token:
            start, end = span(now)
            events, fetch_offline = await _fetch_selected_events(db, access_token, tz, start, end)
            offline = offline or fetch_offline
        if not offline:
            http_client.report_success(logger, OUTAGE_KEY)
        return {"now": now, "tz": tz, "events": events, "offline": offline}

    try:
        return await asyncio.wait_for(load(), CALENDAR_DEADLINE)
    except asyncio.TimeoutError:
        # Only a connected account makes network calls, so a timeout means
        # connected-but-slow: render the offline state, never block the page.
        http_client.report_failure(
            logger, OUTAGE_KEY, "Google Calendar took over %ss; showing offline", CALENDAR_DEADLINE
        )
        tz = await family_timezone(db)
        return {"now": datetime.now(tz), "tz": tz, "events": [], "offline": True}


async def get_month_grid(db, year: int | None = None, month: int | None = None) -> dict | None:
    """None means not connected. Otherwise a Monday-start 6-week grid for
    the given month (defaults to the current month, in the calendar's own
    timezone). Each week carries its 7 days (date/in_month/today/weekend/
    hidden_count) and a list of event "bars" — Google-style, one per event
    touching that week, clipped to the week and packed into up to
    MAX_BAR_SLOTS rows so overlapping events don't collide. Events beyond
    that cap don't get a bar; the day(s) they're on get hidden_count
    incremented instead (surfaced as "+N more" in the template).

    If Google is unreachable the grid still renders (with "offline": True
    and whatever events could be fetched) — a network blip must never take
    down the whole dashboard."""
    def grid_span(now: datetime) -> tuple[date, date]:
        first = date(year or now.year, month or now.month, 1)
        grid_start = first - timedelta(days=first.weekday())  # Monday on/before the 1st
        return grid_start, grid_start + timedelta(days=42)

    loaded = await _load_events(db, grid_span)
    if loaded is None:
        return None
    now, all_events, offline = loaded["now"], loaded["events"], loaded["offline"]
    year = year or now.year
    month = month or now.month
    today = now.date()
    first_of_month = date(year, month, 1)
    grid_start, _ = grid_span(now)

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
            })

        week_events = [
            e for e in all_events
            if date.fromisoformat(e["date"]) <= week_end
            and date.fromisoformat(e["end_date"]) >= week_start
        ]
        # Earlier-starting events first; among ties, longer events first so
        # they claim a slot before a cluster of short same-day events do.
        week_events.sort(key=lambda e: (
            e["sort_key"],
            -(date.fromisoformat(e["end_date"]) - date.fromisoformat(e["date"])).days,
        ))

        slot_last_col: dict[int, int] = {}
        bars = []
        for event in week_events:
            e_start = date.fromisoformat(event["date"])
            e_end = date.fromisoformat(event["end_date"])
            col_start = max(1, (max(e_start, week_start) - week_start).days + 1)
            col_end = min(7, (min(e_end, week_end) - week_start).days + 1)

            slot = next((s for s in range(MAX_BAR_SLOTS) if slot_last_col.get(s, 0) < col_start), None)
            if slot is None:
                for i in range(col_start - 1, col_end):
                    days[i]["hidden_count"] += 1
                continue
            slot_last_col[slot] = col_end
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
    }


async def get_day_events(db, target: date) -> dict | None:
    """None means not connected. Otherwise {"events": [...], "offline": bool}
    — every event on the given day across all selected calendars, all-day
    events first then by time."""
    loaded = await _load_events(db, lambda _now: (target, target + timedelta(days=1)))
    if loaded is None:
        return None
    events = loaded["events"]
    events.sort(key=lambda e: (0 if e["all_day"] else 1, e["sort_key"]))
    return {"events": events, "offline": loaded["offline"]}
