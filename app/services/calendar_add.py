"""
Adding an event from the wall (spec 10.5): the calendar widget's PIN-free
"+", and later the assistant's quick add (docs/assistant-spec.md A2), both
through add_family_event().

- It only ever goes into the **one Family calendar** chosen in Admin
  (services/calendar_prefs), never a personal or work calendar. Editing and
  deleting stay in Google Calendar.
- An event for one person is saved as **"Name: Title"** ("Alanna:
  Dentist"), which the person filter and the banner pick up
  (services/people).
- Needs the calendar.events scope; without it the caller shows a reconnect
  hint.

Claim, then act, like services/school_events: every add carries a
`request_key` (the form's one-off token; A2 its confirm card's), claimed in
app_settings under SQLite's write lock before Google is called, so a double
tap can't insert twice. The Calendar event id is derived from that key, so
even a retry after a lost response gets HTTP 409 (already exists), which
counts as done. A failed insert releases the claim, so the same form can be
sent again. At most RATE_LIMIT adds an hour for the whole wall.
"""

import asyncio
import hashlib
import json
import logging
import re
import time as _time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime

import httpx

from app import calendar_cache, google_calendar, google_oauth, http_client
from app.database import family_timezone, family_today, get_setting, set_setting
from app.services import calendar_prefs, people

logger = logging.getLogger(__name__)

OUTAGE_KEY = "Google Calendar (add event)"
CLAIMS_KEY = "calendar_add_claims"
CLAIM_KEEP = timedelta(days=2)
MAX_CLAIMS = 200
NOTE = "Added on the family display."
DEFAULT_LENGTH = timedelta(hours=1)
MAX_TITLE = 100
MAX_DAYS_AHEAD = 400
RATE_LIMIT = 20          # adds per RATE_WINDOW, for the whole wall
RATE_WINDOW = 3600       # seconds
_recent_adds: deque[float] = deque()  # monotonic times of recent adds (one process)

REQUEST_KEY = re.compile(r"[A-Za-z0-9_-]{8,64}")
_TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

ERRORS = {
    "title": f"Give the event a name (up to {MAX_TITLE} characters).",
    "day": "Pick a day from today onwards.",
    "time": "Pick the times again: use times like 16:30.",
    "end": "The end time must be after the start time.",
    "person": "That family member no longer exists. Pick someone else, or Everyone.",
    "request": "That form is out of date. Close it and try again.",
    "no-calendar": "Adding events isn't set up: choose the Family calendar in Admin → Google & Sync.",
    "scope": "Google needs reconnecting in Admin (Disconnect, then Connect) before events can be added.",
    "offline": "Couldn't reach Google Calendar, so the event wasn't added. Try again in a minute.",
    "missing": "The Family calendar can't be found any more. Choose it again in Admin.",
    "failed": "Google Calendar didn't accept the event, so it wasn't added.",
    "rate": "That's a lot of new events in one go. Try again in a little while.",
    "busy": "That event is already being added. One moment…",
}


class AddEventError(ValueError):
    """`code` is an ERRORS key; `message` is what to show."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.message = ERRORS[code]


def reset_rate_limit() -> None:
    _recent_adds.clear()


def _rate_limited(now: float) -> bool:
    while _recent_adds and now - _recent_adds[0] >= RATE_WINDOW:
        _recent_adds.popleft()
    return len(_recent_adds) >= RATE_LIMIT


def _give_back(slot: float) -> None:
    try:
        _recent_adds.remove(slot)
    except ValueError:
        pass  # already aged out of the window


# --- Validation ---------------------------------------------------------------------------

def _clean_title(value: str | None) -> str:
    title = " ".join(_CONTROL.sub(" ", value or "").split())
    if not title or len(title) > MAX_TITLE:
        raise AddEventError("title")
    return title


def _clean_time(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    match = _TIME.fullmatch(value)
    if not match:
        raise AddEventError("time")
    return f"{int(match[1]):02d}:{match[2]}"


def _clean_day(value: str | date | None, today: date) -> date:
    try:
        day = value if isinstance(value, date) else date.fromisoformat((value or "").strip())
    except ValueError:
        raise AddEventError("day") from None
    if not today <= day <= today + timedelta(days=MAX_DAYS_AHEAD):
        raise AddEventError("day")
    return day


async def _person(db, person_id) -> dict | None:
    if person_id in (None, "", "everyone"):
        return None
    try:
        pid = int(person_id)
    except (TypeError, ValueError):
        raise AddEventError("person") from None
    row = await (await db.execute("SELECT id, name FROM profiles WHERE id = ?", (pid,))).fetchone()
    if row is None:
        raise AddEventError("person")
    return dict(row)


def summary_for(title: str, person: dict | None) -> str:
    """"Alanna: Dentist" for one person (unless the title already says so),
    else the title as typed."""
    if person is None:
        return title
    named, _ = people.split_person(title, [person])
    return title if named is not None else f"{people.first_name(person['name'])}: {title}"


def event_id(request_key: str, calendar_id: str) -> str:
    """A stable Calendar event id for this add (base32hex: 0-9a-v)."""
    return "hf" + hashlib.sha256(f"{request_key}:{calendar_id}".encode()).hexdigest()[:30]


def event_resource(eid: str, summary: str, day: date, start: str | None, end: str | None, tz) -> dict:
    if start is None:
        # Google's all-day end date is exclusive.
        when = {"start": {"date": day.isoformat()}, "end": {"date": (day + timedelta(days=1)).isoformat()}}
    else:
        begins = datetime.combine(day, dtime.fromisoformat(start), tzinfo=tz)
        ends = datetime.combine(day, dtime.fromisoformat(end), tzinfo=tz) if end else begins + DEFAULT_LENGTH
        when = {"start": {"dateTime": begins.isoformat(), "timeZone": tz.key},
                "end": {"dateTime": ends.isoformat(), "timeZone": tz.key}}
    return {"id": eid, "summary": summary, "description": NOTE, **when}


# --- Claims -------------------------------------------------------------------------------

def _read_claims(raw: str | None) -> dict[str, dict]:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)} \
        if isinstance(value, dict) else {}


def _prune(claims: dict[str, dict], now: datetime) -> dict[str, dict]:
    cutoff = now - CLAIM_KEEP
    kept = {}
    for key, claim in claims.items():
        try:
            at = datetime.fromisoformat(claim.get("at", ""))
        except (TypeError, ValueError):
            continue
        if at.tzinfo is not None and at >= cutoff:
            kept[key] = claim
    return dict(sorted(kept.items(), key=lambda kv: kv[1]["at"], reverse=True)[:MAX_CLAIMS])


async def _update_claims(db, change: Callable[[dict[str, dict]], dict | None]) -> dict | None:
    """Read-modify-write the claims under SQLite's write lock (two taps are
    two requests: only one can claim). `change(claims)` edits in place and
    returns the result passed back."""
    if db.in_transaction:
        await db.commit()
    await db.execute("BEGIN IMMEDIATE")
    try:
        claims = _read_claims(await get_setting(db, CLAIMS_KEY))
        result = change(claims)
        await set_setting(db, CLAIMS_KEY, json.dumps(_prune(claims, datetime.now(UTC))))
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return result


async def _claim(db, key: str) -> dict | None:
    """Claims `key`. None if it's ours now; else the existing claim."""
    def change(claims):
        existing = claims.get(key)
        if existing is not None:
            return existing
        claims[key] = {"state": "adding", "at": datetime.now(UTC).isoformat()}
        return None
    return await _update_claims(db, change)


async def _finish(db, key: str, done: dict | None) -> None:
    """Marks the claim done (with what was added), or releases it."""
    def change(claims):
        if done is None:
            claims.pop(key, None)
        else:
            claims[key] = {"state": "done", "at": datetime.now(UTC).isoformat(), **done}
        return None
    await _update_claims(db, change)


def _failure_code(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return "scope"
        if status == 404:
            return "missing"
        if status == 429 or status >= 500:
            return "offline"
        return "failed"
    return "offline"


# --- The add ------------------------------------------------------------------------------

async def add_family_event(
    db,
    *,
    title: str,
    day: str | date,
    start_time: str | None = None,
    end_time: str | None = None,
    person_id: int | str | None = None,
    request_key: str,
) -> dict:
    """Adds one event to the Family calendar. Returns {"id", "summary",
    "date", "calendar", "duplicate"} ("duplicate": this request_key was
    already added, nothing new was created). Raises AddEventError."""
    if not isinstance(request_key, str) or not REQUEST_KEY.fullmatch(request_key):
        raise AddEventError("request")
    clean_title = _clean_title(title)
    the_day = _clean_day(day, await family_today(db))
    start = _clean_time(start_time)
    end = _clean_time(end_time) if start else None
    if start and end and end <= start:
        raise AddEventError("end")
    person = await _person(db, person_id)
    summary = summary_for(clean_title, person)
    if len(summary) > MAX_TITLE + 40:
        raise AddEventError("title")

    family = await calendar_prefs.get_family_calendar(db)
    if family is None:
        raise AddEventError("no-calendar")
    if not await google_oauth.has_scope(db, google_oauth.CALENDAR_EVENTS_SCOPE):
        raise AddEventError("scope")

    existing = await _claim(db, request_key)
    if existing is not None:  # a repeat of an add: never a second event, never counted
        if existing.get("state") == "done":
            return {"id": existing.get("id"), "summary": existing.get("summary", summary),
                    "date": existing.get("date", the_day.isoformat()),
                    "calendar": existing.get("calendar", family["summary"]), "duplicate": True}
        raise AddEventError("busy")
    # Check and reserve the rate slot with no await in between, so
    # concurrent adds can't all pass the check before any of them counts.
    slot = _time.monotonic()
    if _rate_limited(slot):
        await _finish(db, request_key, None)
        raise AddEventError("rate")
    _recent_adds.append(slot)
    try:
        created = await _insert(db, request_key, family, summary, the_day, start, end)
    except BaseException:
        _give_back(slot)
        await _finish(db, request_key, None)  # the same form can be sent again
        raise
    await _finish(db, request_key, created)
    await _refresh(db)
    return {**created, "duplicate": False}


async def _insert(db, key: str, family: dict, summary: str, day: date, start, end) -> dict:
    access_token, offline = await google_oauth.connect(db)
    if offline:
        raise AddEventError("offline")
    if not access_token:  # disconnected since (e.g. a revoked grant)
        raise AddEventError("scope")
    tz = await family_timezone(db)
    resource = event_resource(event_id(key, family["id"]), summary, day, start, end, tz)
    try:
        try:
            created = await google_calendar.insert_event(access_token, family["id"], resource)
            created_id = created.get("id") or resource["id"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:  # 409: this very event already exists
                raise
            created_id = resource["id"]
    except httpx.HTTPError as exc:
        http_client.report_failure(logger, OUTAGE_KEY, "Couldn't add an event from the wall: %s",
                                   http_client.describe(exc))
        raise AddEventError(_failure_code(exc)) from None
    http_client.report_success(logger, OUTAGE_KEY)
    return {"id": created_id, "summary": summary, "date": day.isoformat(), "calendar": family["summary"]}


async def _refresh(db) -> None:
    """The wall's saved copy predates the new event: drop it and fetch again
    (best effort, and never longer than the calendar's own deadline; the
    widget's next render fetches anyway)."""
    await calendar_cache.clear(db)
    await db.commit()
    try:
        await asyncio.wait_for(google_calendar.refresh_cache(db), google_calendar.CALENDAR_DEADLINE)
    except Exception as exc:
        logger.warning("Couldn't refresh the calendar after adding an event: %s", type(exc).__name__)
