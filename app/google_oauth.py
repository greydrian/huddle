"""
Google OAuth + Calendar (Section 4.2, 9.5 of the spec): single account,
standard in-browser consent flow, tokens stored locally and encrypted,
gated behind the admin PIN.

Talks to Google's REST endpoints directly via httpx rather than pulling in
google-api-python-client — the Calendar API is plain JSON over HTTPS and
this matches the project's minimal-dependency approach.

Scope is calendar.readonly (plus openid/email for the account label in
Admin) — this pass only displays events. Editing from the screen (spec 4.2)
needs the broader `calendar` write scope and its own consent round, later.
"""

import json
import os
import time
from datetime import date, datetime, timedelta, time as dtime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.database import get_setting, set_setting
from app.security import encrypt_token_json, decrypt_token_json

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
CALENDAR_LIST_ENDPOINT = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
CALENDAR_METADATA_ENDPOINT = "https://www.googleapis.com/calendar/v3/calendars/primary"
CALENDAR_EVENTS_ENDPOINT_TEMPLATE = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

CALENDAR_TIMEZONE_SETTING = "calendar_timezone"
SELECTED_CALENDARS_SETTING = "google_selected_calendars"
DEFAULT_SELECTED_CALENDARS = [{"id": "primary", "summary": "Calendar", "color": "#D6A02C"}]

SCOPES = "https://www.googleapis.com/auth/calendar.readonly openid email"
TOKEN_REFRESH_BUFFER_SECONDS = 60
MAX_BAR_SLOTS = 3  # event bars shown per week in the month grid before "+N more"


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def build_auth_url(state: str, redirect_uri: str) -> str:
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # always return a refresh_token, even on re-auth
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{httpx.QueryParams(params)}"


async def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(TOKEN_ENDPOINT, data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(TOKEN_ENDPOINT, data={
            "refresh_token": refresh_token,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        return resp.json()


async def fetch_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        return resp.json()


async def fetch_calendar_list(access_token: str) -> list[dict]:
    """Every calendar the connected account can see, for the Admin picker."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(CALENDAR_LIST_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        items = resp.json().get("items", [])
    return [
        {
            "id": item["id"],
            "summary": item.get("summaryOverride") or item.get("summary", item["id"]),
            "color": item.get("backgroundColor", "#D6A02C"),
            "primary": bool(item.get("primary")),
        }
        for item in items
    ]


async def get_selected_calendars(db) -> list[dict]:
    raw = await get_setting(db, SELECTED_CALENDARS_SETTING)
    if not raw:
        return DEFAULT_SELECTED_CALENDARS
    try:
        parsed = json.loads(raw)
        return parsed or DEFAULT_SELECTED_CALENDARS
    except (ValueError, TypeError):
        return DEFAULT_SELECTED_CALENDARS


async def set_selected_calendars(db, calendars: list[dict]):
    await set_setting(db, SELECTED_CALENDARS_SETTING, json.dumps(calendars))
    await db.commit()


async def cache_calendar_timezone(db, access_token: str):
    """Fetch the connected calendar's IANA timezone (e.g. 'Europe/London')
    and store it, so 'today'/'this week' get computed in the family's real
    timezone rather than the server's — the container runs in UTC
    regardless of where the G10 actually lives. Best-effort: a failure here
    just leaves the previous (or UTC default) setting in place."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                CALENDAR_METADATA_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            tz_name = resp.json().get("timeZone")
        if tz_name:
            await set_setting(db, CALENDAR_TIMEZONE_SETTING, tz_name)
            await db.commit()
    except httpx.HTTPError:
        pass


async def _get_calendar_timezone(db, access_token: str) -> ZoneInfo:
    tz_name = await get_setting(db, CALENDAR_TIMEZONE_SETTING)
    if not tz_name:
        # Self-heals accounts connected before this cache existed, and
        # covers the very first widget render right after a fresh connect.
        await cache_calendar_timezone(db, access_token)
        tz_name = await get_setting(db, CALENDAR_TIMEZONE_SETTING)
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


async def _load_stored_tokens(db) -> dict | None:
    cursor = await db.execute(
        "SELECT encrypted_token_json FROM auth_tokens WHERE service_name = 'google'"
    )
    row = await cursor.fetchone()
    if row is None or not row["encrypted_token_json"]:
        return None
    return decrypt_token_json(row["encrypted_token_json"])


async def store_tokens(db, tokens: dict, account_email: str | None = None):
    encrypted = encrypt_token_json(tokens)
    await db.execute(
        """INSERT INTO auth_tokens (service_name, account_email, encrypted_token_json)
           VALUES ('google', ?, ?)
           ON CONFLICT(service_name) DO UPDATE SET
             account_email = COALESCE(excluded.account_email, auth_tokens.account_email),
             encrypted_token_json = excluded.encrypted_token_json""",
        (account_email, encrypted),
    )
    await db.commit()


async def get_connected_account(db) -> str | None:
    """Email of the connected Google account, or None if not connected."""
    cursor = await db.execute(
        "SELECT account_email FROM auth_tokens WHERE service_name = 'google'"
    )
    row = await cursor.fetchone()
    return row["account_email"] if row else None


async def get_valid_access_token(db) -> str | None:
    """A usable access token, refreshing if needed. None means 'not
    connected' — callers should render the disconnected/stub state, not
    treat this as an error."""
    tokens = await _load_stored_tokens(db)
    if tokens is None:
        return None

    if time.time() < tokens.get("expires_at", 0) - TOKEN_REFRESH_BUFFER_SECONDS:
        return tokens["access_token"]

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return None

    try:
        refreshed = await refresh_access_token(refresh_token)
    except httpx.HTTPStatusError:
        # Refresh token revoked/expired server-side — treat as disconnected
        # rather than surfacing a broken widget; Admin will show "Connect"
        # again and they can re-auth.
        await db.execute("DELETE FROM auth_tokens WHERE service_name = 'google'")
        await db.commit()
        return None

    tokens["access_token"] = refreshed["access_token"]
    tokens["expires_at"] = time.time() + refreshed.get("expires_in", 3600)
    # Google doesn't re-send refresh_token on a refresh call — keep the one we have.
    await store_tokens(db, tokens)
    return tokens["access_token"]


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
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 100,
            },
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    events = [_format_event(item) for item in items]
    events.sort(key=lambda e: e["sort_key"])
    return events


async def get_month_grid(db, year: int | None = None, month: int | None = None) -> dict | None:
    """None means not connected. Otherwise a Monday-start 6-week grid for
    the given month (defaults to the current month, in the calendar's own
    timezone). Each week carries its 7 days (date/in_month/today/weekend/
    hidden_count) and a list of event "bars" — Google-style, one per event
    touching that week, clipped to the week and packed into up to
    MAX_BAR_SLOTS rows so overlapping events don't collide. Events beyond
    that cap don't get a bar; the day(s) they're on get hidden_count
    incremented instead (surfaced as "+N more" in the template)."""
    access_token = await get_valid_access_token(db)
    if not access_token:
        return None

    tz = await _get_calendar_timezone(db, access_token)
    now = datetime.now(tz)
    year = year or now.year
    month = month or now.month
    today = now.date()

    selected = await get_selected_calendars(db)

    first_of_month = date(year, month, 1)
    grid_start = first_of_month - timedelta(days=first_of_month.weekday())  # Monday on/before the 1st
    grid_end = grid_start + timedelta(days=42)

    all_events = []
    for cal in selected:
        cal_events = await fetch_events(
            access_token,
            cal["id"],
            datetime.combine(grid_start, dtime.min, tzinfo=tz),
            datetime.combine(grid_end, dtime.min, tzinfo=tz),
        )
        for event in cal_events:
            event["color"] = cal.get("color") or "#D6A02C"
        all_events.extend(cal_events)

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
    }


async def get_day_events(db, date_iso: str) -> list[dict] | None:
    """None means not connected. Otherwise every event on the given day
    across all selected calendars, all-day events first then by time."""
    access_token = await get_valid_access_token(db)
    if not access_token:
        return None

    tz = await _get_calendar_timezone(db, access_token)
    target = date.fromisoformat(date_iso)
    selected = await get_selected_calendars(db)

    day_start = datetime.combine(target, dtime.min, tzinfo=tz)
    day_end = datetime.combine(target + timedelta(days=1), dtime.min, tzinfo=tz)

    events = []
    for cal in selected:
        cal_events = await fetch_events(access_token, cal["id"], day_start, day_end)
        for event in cal_events:
            event["color"] = cal.get("color") or "#D6A02C"
        events.extend(cal_events)
    events.sort(key=lambda e: (0 if e["all_day"] else 1, e["sort_key"]))
    return events


async def revoke_and_clear(db):
    tokens = await _load_stored_tokens(db)
    if tokens and tokens.get("refresh_token"):
        try:
            async with httpx.AsyncClient() as client:
                await client.post(REVOKE_ENDPOINT, params={"token": tokens["refresh_token"]})
        except httpx.HTTPError:
            pass  # best-effort — still clear the local row either way
    await db.execute("DELETE FROM auth_tokens WHERE service_name = 'google'")
    await db.commit()
