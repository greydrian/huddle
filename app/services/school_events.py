"""
Approving a school inbox *event* (services/imports.py) adds it to the
Google calendar picked in Admin → School email, via events.insert. Needs
the calendar.events scope; without it Admin asks for a reconnect.

Claim, then act, like imports.approve_candidate: the candidate is moved
pending -> 'approving' by a conditional update (committed) before Google is
called, so a double submit can't create two events. A Calendar failure puts
it back to pending with a clear error; success stores the new event's id.
The event id is chosen here, deterministically per candidate, so even a
crash between the insert and the final update can't lead to a second
event: the retry gets HTTP 409 (already exists), which counts as done. The
calendar is fixed at the first claim too (claim_calendar_id), so changing
the School events calendar in between can't put it in two calendars; it's
only forgotten when Google definitely refused the insert.
"""

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta
from datetime import time as dtime

import httpx

from app import calendar_cache, google_accounts, google_calendar, google_oauth, http_client
from app.database import family_timezone, get_setting, set_setting
from app.services import calendar_prefs, homework
from app.services.extraction import MAX_EVENT_NOTES
from app.services.imports import CandidateError, _pending_candidate

logger = logging.getLogger(__name__)

CALENDAR_SETTING = calendar_prefs.SCHOOL_EVENTS_KEY
CREATED_TABLE = "google_calendar"
NOTE = "Added from the school inbox on the family display."
DEFAULT_LENGTH = timedelta(hours=1)
_TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


async def get_target_calendar(db) -> dict | None:
    """{"account_id", "id", "summary"} of the calendar school events go to, or None."""
    try:
        target = json.loads(await get_setting(db, CALENDAR_SETTING) or "null")
    except ValueError:
        return None
    return calendar_prefs.one_calendar(target)


async def set_target_calendar(db, calendar: dict | None):
    await set_setting(db, CALENDAR_SETTING, calendar_prefs.one_calendar_value(calendar))
    await db.commit()


async def _can_write(db, target: dict) -> bool:
    """The target's account does Writing events and granted calendar.events
    (stage 1: one account does it)."""
    writer = await google_accounts.job_account(db, "write_events")
    return (
        writer is not None
        and google_accounts.job_ready(writer, "write_events")
        and writer["id"] == target["account_id"]
    )


def _time(value: str | None, field: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    match = _TIME.fullmatch(value)
    if not match:
        raise homework.ValidationError(f"{field} isn't a valid time — use HH:MM.")
    return f"{int(match[1]):02d}:{match[2]}"


def event_fields(form: dict) -> dict:
    """The (possibly edited) approve form as an event payload. A blank start
    time means all day. Raises homework.ValidationError."""
    title = homework.clean_text(form.get("title"), "Title", homework.MAX_TITLE, required=True)
    day = homework.parse_optional_date(form.get("date"), "Date")
    if not day:
        raise homework.ValidationError("Date can't be blank.")
    start = _time(form.get("start_time"), "Start time")
    end = _time(form.get("end_time"), "End time") if start else None
    if start and end and end <= start:
        raise homework.ValidationError("The end time must be after the start time.")
    notes = homework.clean_text(form.get("notes"), "Notes", MAX_EVENT_NOTES)
    return {"title": title, "date": day, "start_time": start, "end_time": end, "all_day": start is None, "notes": notes}


def event_id(candidate: dict) -> str:
    """A stable Calendar event id for this candidate (base32hex: 0-9a-v)."""
    seed = f"{candidate['id']}:{candidate['source_id']}:{candidate['created_at']}"
    return "hs" + hashlib.sha256(seed.encode()).hexdigest()[:30]


def event_resource(candidate: dict, fields: dict, tz) -> dict:
    notes = "\n\n".join(part for part in (fields["notes"], NOTE) if part)
    day = date.fromisoformat(fields["date"])
    if fields["all_day"]:
        start = {"date": day.isoformat()}
        end = {"date": (day + timedelta(days=1)).isoformat()}  # Google's end date is exclusive
    else:
        hours, minutes = fields["start_time"].split(":")
        begins = datetime.combine(day, dtime(int(hours), int(minutes)), tzinfo=tz)
        if fields["end_time"]:
            hours, minutes = fields["end_time"].split(":")
            ends = datetime.combine(day, dtime(int(hours), int(minutes)), tzinfo=tz)
        else:
            ends = begins + DEFAULT_LENGTH
        start = {"dateTime": begins.isoformat(), "timeZone": tz.key}
        end = {"dateTime": ends.isoformat(), "timeZone": tz.key}
    return {"id": event_id(candidate), "summary": fields["title"], "description": notes, "start": start, "end": end}


def _failure_code(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return "import-calendar-scope"
        if status == 404:
            return "import-calendar-missing"
        return "import-calendar-failed"
    return "import-calendar-offline"


async def _release(db, candidate_id: int, forget_calendar: bool = False):
    """Back to pending after a failed insert (only if still claimed by us).
    The claimed calendar is kept unless Google definitely didn't create the
    event (a timeout or 5xx might have)."""
    await db.execute(
        """UPDATE import_candidates SET status = 'pending', updated_at = datetime('now'),
               claim_calendar_id = CASE WHEN ? THEN NULL ELSE claim_calendar_id END
           WHERE id = ? AND status = 'approving'""",
        (int(forget_calendar), candidate_id),
    )
    await db.commit()


def _definitely_not_created(exc: BaseException) -> bool:
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and 400 <= exc.response.status_code < 500
        and exc.response.status_code not in (408, 409, 429)
    )


async def approve_event(db, candidate_id: int, form: dict) -> str:
    """Adds the event to the chosen calendar. Returns the Calendar event id.
    Raises CandidateError (codes in admin.ADMIN_ERRORS) or
    homework.ValidationError; on any failure the candidate stays pending."""
    candidate = await _pending_candidate(db, candidate_id)
    if candidate["kind"] != "event":
        raise CandidateError("import-missing")
    target = await get_target_calendar(db)
    if target is None:
        raise CandidateError("import-event")
    if not await _can_write(db, target):
        raise CandidateError("import-calendar-scope")
    fields = event_fields(form)
    access_token, offline = await google_oauth.connect(db, target["account_id"])
    if offline:
        raise CandidateError("import-calendar-offline")
    if not access_token:  # disconnected since (e.g. a revoked grant)
        raise CandidateError("import-calendar-scope")
    tz = await family_timezone(db)

    # The claim is the lock: of two approvals racing, only one moves it out
    # of pending. Committed before Google is called, so the write lock isn't
    # held across the network.
    claimed = await db.execute(
        """UPDATE import_candidates SET status = 'approving', updated_at = datetime('now'),
               claim_calendar_id = COALESCE(claim_calendar_id, ?)
           WHERE id = ? AND status = 'pending'""",
        (target["id"], candidate_id),
    )
    await db.commit()
    if claimed.rowcount != 1:
        raise CandidateError("import-missing")
    calendar_id = (
        await (
            await db.execute("SELECT claim_calendar_id FROM import_candidates WHERE id = ?", (candidate_id,))
        ).fetchone()
    )["claim_calendar_id"]

    resource = event_resource(candidate, fields, tz)
    try:
        try:
            created = await google_calendar.insert_event(access_token, calendar_id, resource)
            created_id = created.get("id") or resource["id"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:  # 409: this very event already exists
                raise
            created_id = resource["id"]
    except httpx.HTTPError as exc:
        code = _failure_code(exc)
        http_client.report_failure(
            logger, "Google Calendar (add event)", "Couldn't add a school event: %s", http_client.describe(exc)
        )
        await _release(db, candidate_id, forget_calendar=_definitely_not_created(exc))
        raise CandidateError(code) from None
    except BaseException:
        await _release(db, candidate_id)
        raise
    http_client.report_success(logger, "Google Calendar (add event)")

    payload = {**json.loads(candidate["payload_json"]), **fields}
    await db.execute(
        """UPDATE import_candidates SET status = 'approved', payload_json = ?, created_table = ?,
               external_id = ?, updated_at = datetime('now') WHERE id = ? AND status = 'approving'""",
        (json.dumps(payload), CREATED_TABLE, created_id, candidate_id),
    )
    # The wall's saved copy of that account's calendars predates the new event: drop it and fetch again.
    await calendar_cache.clear_account(db, target["account_id"])
    await db.commit()
    try:
        await google_calendar.refresh_cache(db)
    except Exception as exc:  # best-effort; the widget refetches on its own poll
        logger.warning("Couldn't refresh the calendar after adding an event: %s", type(exc).__name__)
    return created_id
