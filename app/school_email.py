"""
School email import: once a day (by default) read the school's emails from
the connected Gmail account and feed each one into the school inbox
(services/imports.ingest), where Claude finds the spellings, homework and
events in it and a parent approves them in Admin. Nothing reaches the wall
unapproved.

Which emails: From an Admin-editable sender allowlist ("office@school",
"*@school-domain"), minus an exclusion list, received after a stored
checkpoint (the first run looks back BACKFILL_DAYS). The SENDCo address is
excluded always, whatever the lists say: those are private conversations,
and are never fetched or sent anywhere. Defence in depth, the Gmail search
leaves them out, then each message's From/To/Cc/Reply-To are fetched as
metadata only and re-checked before its body is fetched.

Only the school emails are sent to the Anthropic API; that's what the
family consented to.

Scheduling (scheduler.py checks `run_if_due` every CHECK_SECONDS and once
at startup, like the nightly backup): daily at a family-local time (default
18:00), twice daily (that time and 12 hours before it), weekly on Friday at
that time, or off. The due-check is done in UTC from the family timezone,
so DST changes neither skip nor double a check. A failed check is retried
after RETRY_AFTER_FAILURE. "Check now" in Admin and the scheduler share one
lock, so two checks never run at once.

The checkpoint only moves after a clean run: a Gmail outage or a transient
Claude failure leaves it, and the next run re-lists the same window.
Messages already in the inbox are skipped before anything is fetched, so an
overlap costs one list call.

Logging is codes and counts only: never subjects, bodies, attachments,
addresses of message senders, or tokens.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime

import httpx

from app import google_gmail, google_oauth, http_client, sync_status
from app.database import family_timezone, get_db, get_setting, set_setting
from app.services import extraction, imports

logger = logging.getLogger(__name__)

OUTAGE_KEY = "Gmail (school email)"

SENDERS_SETTING = "school_email_senders"
EXCLUSIONS_SETTING = "school_email_exclusions"
SCHEDULE_SETTING = "school_email_schedule"
CHECKPOINT_SETTING = "school_email_checkpoint"   # epoch seconds
STATUS_SETTING = "school_email_status"

DEFAULT_SENDERS = ("office@greshamprimary.school", "*@gresham.croydon.sch.uk")
# Private SENDCo conversations: never fetched, whatever the lists say.
ALWAYS_EXCLUDED = ("sen@gresham.croydon.sch.uk",)
DEFAULT_EXCLUSIONS = ALWAYS_EXCLUDED
MAX_ENTRIES = 20

BACKFILL_DAYS = 14
# The checkpoint is set this far before the run started, so a message Gmail
# stamps a little late is still listed next time (already-read ones are
# skipped without being fetched).
CHECKPOINT_OVERLAP = timedelta(days=1)
MAX_MESSAGES_PER_RUN = 60
# Images smaller than this are logos and signature icons, not letters.
MIN_IMAGE_BYTES = 8_000

CHECK_SECONDS = 600
RETRY_AFTER_FAILURE = timedelta(hours=1)

SCHEDULES = {
    "daily": "Daily",
    "twice": "Twice daily",
    "weekly": "Weekly (Friday)",
    "off": "Off",
}
DEFAULT_SCHEDULE = {"mode": "daily", "time": "18:00"}
FRIDAY = 4

# Result codes (stored in STATUS_SETTING, shown in Admin).
OK = "ok"
# Nothing to try until someone changes something: not retried until the next slot.
SKIPPED = {"not_connected", "reconnect", "api_disabled", "not_configured", "no_senders", "google_off"}
RESULT_TEXT = {
    OK: "OK",
    "not_connected": "Google isn't connected",
    "reconnect": "Reconnect Google to enable school email import",
    "api_disabled": "The Gmail API isn't enabled in the Google Cloud project",
    "not_configured": "No Anthropic API key, so emails can't be read",
    "no_senders": "No school senders listed",
    "google_off": "Google isn't set up on this display",
    "offline": "Couldn't reach Google",
    "rate_limited": "Google is rate-limiting; will retry",
    "server_error": "Google had a problem; will retry",
    "extract_failed": "Claude couldn't read some emails; will retry",
    "error": "Something went wrong (see the server log)",
}
# Claude failures worth retrying (the rest are permanent for that email).
TRANSIENT_EXTRACT = {"offline", "rate_limited", "server_error", "error", "auth", "interrupted"}


# --- Sender lists ---

_ENTRY = re.compile(r"(\*|[a-z0-9._%+'-]+)@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}")


class InvalidEntry(ValueError):
    pass


def parse_entries(text: str) -> list[str]:
    """One address or "*@domain" per line (commas and spaces also split).
    Raises InvalidEntry for anything else, so nothing odd reaches the Gmail
    query."""
    entries: list[str] = []
    for raw in re.split(r"[\s,;]+", text or ""):
        entry = raw.strip().lower()
        if not entry:
            continue
        if not _ENTRY.fullmatch(entry) or len(entry) > 120:
            raise InvalidEntry(entry)
        if entry not in entries:
            entries.append(entry)
    if len(entries) > MAX_ENTRIES:
        raise InvalidEntry("too many")
    return entries


async def _entries(db, key: str, default: tuple[str, ...]) -> list[str]:
    raw = await get_setting(db, key)
    if raw is None:
        return list(default)
    try:
        return parse_entries(raw)
    except InvalidEntry:  # a hand-edited setting: fall back rather than query something odd
        return list(default)


async def get_senders(db) -> list[str]:
    return await _entries(db, SENDERS_SETTING, DEFAULT_SENDERS)


async def get_exclusions(db) -> list[str]:
    return await _entries(db, EXCLUSIONS_SETTING, DEFAULT_EXCLUSIONS)


async def set_lists(db, senders: list[str], exclusions: list[str]):
    await set_setting(db, SENDERS_SETTING, "\n".join(senders))
    await set_setting(db, EXCLUSIONS_SETTING, "\n".join(exclusions))
    await db.commit()


def _all_exclusions(exclusions: list[str]) -> list[str]:
    return [*ALWAYS_EXCLUDED, *(e for e in exclusions if e not in ALWAYS_EXCLUDED)]


def matches(address: str, entry: str) -> bool:
    address = address.lower()
    if entry.startswith("*@"):
        return address.endswith(entry[1:])
    return address == entry


def _gmail_term(entry: str) -> str:
    # Gmail has no wildcards: "@domain" matches every address at that domain.
    return entry[1:] if entry.startswith("*@") else entry


def build_query(senders: list[str], exclusions: list[str], after: int) -> str:
    """The Gmail search: from any sender, not from/to/cc any exclusion,
    received after `after` (epoch seconds)."""
    if not senders:
        raise ValueError("no senders")
    terms = " OR ".join(_gmail_term(s) for s in senders)
    parts = [f"from:({terms})"]
    for entry in _all_exclusions(exclusions):
        term = _gmail_term(entry)
        parts += [f"-from:{term}", f"-to:{term}", f"-cc:{term}"]
    parts.append(f"after:{int(after)}")
    return " ".join(parts)


def allowed(envelope: dict[str, list[str]], senders: list[str], exclusions: list[str]) -> bool:
    """The re-check before anything but headers is fetched: exactly one From
    address, on the allowlist, and no excluded address anywhere on it."""
    froms = envelope.get("from") or []
    if len(froms) != 1 or not any(matches(froms[0], s) for s in senders):
        return False
    everyone = [a for key in ("from", "to", "cc", "reply-to") for a in envelope.get(key) or []]
    return not any(matches(a, e) for a in everyone for e in _all_exclusions(exclusions))


# --- Schedule ---

_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


@dataclass(frozen=True)
class Schedule:
    mode: str
    time: str  # "HH:MM", family-local

    @property
    def clock(self) -> dtime:
        hours, minutes = self.time.split(":")
        return dtime(int(hours), int(minutes))

    def times(self) -> list[dtime]:
        if self.mode == "twice":
            other = dtime((self.clock.hour + 12) % 24, self.clock.minute)
            return sorted({self.clock, other})
        return [self.clock]

    def label(self) -> str:
        if self.mode == "off":
            return "Off"
        at = " and ".join(t.strftime("%H:%M") for t in self.times())
        if self.mode == "weekly":
            return f"Fridays at {at}"
        return f"{SCHEDULES[self.mode]} at {at}"


def parse_schedule(mode: str, time: str) -> Schedule:
    """Raises ValueError for an unknown mode or a time that isn't HH:MM."""
    time = (time or "").strip()
    if mode not in SCHEDULES or not _TIME.fullmatch(time):
        raise ValueError("bad schedule")
    return Schedule(mode, time)


async def get_schedule(db) -> Schedule:
    try:
        raw = json.loads(await get_setting(db, SCHEDULE_SETTING) or "{}")
        return parse_schedule(raw.get("mode", DEFAULT_SCHEDULE["mode"]), raw.get("time", DEFAULT_SCHEDULE["time"]))
    except (ValueError, TypeError, AttributeError):
        return parse_schedule(DEFAULT_SCHEDULE["mode"], DEFAULT_SCHEDULE["time"])


async def set_schedule(db, schedule: Schedule):
    await set_setting(db, SCHEDULE_SETTING, json.dumps({"mode": schedule.mode, "time": schedule.time}))
    await db.commit()


def _slots_on(day, schedule: Schedule, tz) -> list[datetime]:
    """The day's check instants in UTC. Worked in UTC because Python compares
    same-tz datetimes by wall time, ignoring DST. A time the clocks skip
    (spring) maps to the matching instant after the jump; a repeated one
    (autumn) is its first occurrence."""
    if schedule.mode == "off" or (schedule.mode == "weekly" and day.weekday() != FRIDAY):
        return []
    return [datetime.combine(day, t, tzinfo=tz).astimezone(UTC) for t in schedule.times()]


def last_slot(now: datetime, schedule: Schedule, tz) -> datetime | None:
    """The most recent scheduled check at or before `now` (UTC), or None."""
    now_utc = now.astimezone(UTC)
    day = now_utc.astimezone(tz).date() + timedelta(days=1)
    for _ in range(10):
        past = [s for s in _slots_on(day, schedule, tz) if s <= now_utc]
        if past:
            return max(past)
        day -= timedelta(days=1)
    return None


def next_slot(now: datetime, schedule: Schedule, tz) -> datetime | None:
    """The next scheduled check after `now` (UTC), or None when off."""
    now_utc = now.astimezone(UTC)
    day = now_utc.astimezone(tz).date() - timedelta(days=1)
    for _ in range(10):
        future = [s for s in _slots_on(day, schedule, tz) if s > now_utc]
        if future:
            return min(future)
        day += timedelta(days=1)
    return None


def is_due(status: dict, schedule: Schedule, now: datetime, tz) -> bool:
    """Due when a scheduled time has passed since the last successful check.
    A check that was skipped (not connected, no scope, ...) waits for the
    next slot; one that failed is retried after RETRY_AFTER_FAILURE."""
    slot = last_slot(now, schedule, tz)
    if slot is None:
        return False
    last_ok = _parse(status.get("last_success_at"))
    if last_ok and last_ok >= slot:
        return False
    last_try = _parse(status.get("last_attempt_at"))
    result = status.get("result")
    if last_try and last_try >= slot and result in SKIPPED:
        return False
    if last_try and result not in SKIPPED and result != OK and now - last_try < RETRY_AFTER_FAILURE:
        return False
    return True


# --- Status ---

def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def get_status(db) -> dict:
    try:
        status = json.loads(await get_setting(db, STATUS_SETTING) or "{}")
    except ValueError:
        status = {}
    return status if isinstance(status, dict) else {}


async def _record(db, now: datetime, result: str, emails: int = 0, items: int = 0):
    status = await get_status(db)
    status.update(last_attempt_at=now.isoformat(), result=result, emails=emails, items=items)
    if result == OK:
        status["last_success_at"] = now.isoformat()
    await set_setting(db, STATUS_SETTING, json.dumps(status))
    await db.commit()


async def get_checkpoint(db) -> int | None:
    raw = await get_setting(db, CHECKPOINT_SETTING)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


# --- The check ---

_lock = asyncio.Lock()


def check_in_progress() -> bool:
    return _lock.locked()


@dataclass
class CheckResult:
    result: str
    emails: int = 0   # new emails read this time
    items: int = 0    # items Claude found in them
    ran: bool = True  # False: another check was already running


def _gmail_code(exc: BaseException) -> str:
    """A log-safe code for a Gmail failure. A 403 for a missing scope (or no
    reason at all) means "reconnect"; the Gmail API switched off in the
    Cloud project has its own message."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403:
        if "accessnotconfigured" in sync_status._error_reasons(exc.response):
            return "api_disabled"
    code = sync_status.error_code(exc)
    if code in ("HTTP 401", "HTTP 403"):
        return "reconnect"
    if code == sync_status.RATE_LIMITED or code == "HTTP 429":
        return "rate_limited"
    if code == "offline":
        return "offline"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return "server_error"
    return "error"


async def _already_read(db, message_id: str) -> bool:
    """In the inbox and not waiting for a retry: skip without fetching."""
    row = await (await db.execute(
        "SELECT status FROM import_sources WHERE kind = 'gmail' AND source_ref = ?", (message_id,)
    )).fetchone()
    return row is not None and row["status"] not in ("failed", "not_configured")


def _ordered_parts(parts: list[google_gmail.AttachmentPart]) -> list[google_gmail.AttachmentPart]:
    """PDFs first (newsletters, letters), then the larger images; logos and
    anything that isn't a PDF or image are dropped."""
    pdfs = [p for p in parts if p.mime_type == extraction.PDF_TYPE or p.filename.lower().endswith(".pdf")]
    images = [p for p in parts if p.mime_type.startswith("image/") and p not in pdfs
              and (p.size == 0 or p.size >= MIN_IMAGE_BYTES)]
    return pdfs + sorted(images, key=lambda p: -p.size)


async def _attachments(access_token: str, message: google_gmail.Message) -> list[imports.Attachment]:
    """Downloads and checks the PDFs/images, within the inbox's own caps
    (count, per-file and total size). Anything too big, of another type or
    unreadable is skipped (counted in the log, never named)."""
    taken: list[imports.Attachment] = []
    total = skipped = 0
    for part in _ordered_parts(message.attachments):
        if len(taken) >= extraction.MAX_ATTACHMENTS:
            skipped += 1
            continue
        if part.size > extraction.MAX_ATTACHMENT_BYTES or total + part.size > extraction.MAX_TOTAL_BYTES:
            skipped += 1
            continue
        data = await google_gmail.get_attachment(access_token, message.id, part)
        if len(data) > extraction.MAX_ATTACHMENT_BYTES or total + len(data) > extraction.MAX_TOTAL_BYTES:
            skipped += 1
            continue
        try:
            attachment = await asyncio.to_thread(imports.prepare_attachment, part.filename, data)
        except imports.UploadRejected as exc:
            logger.info("School email: skipped an attachment (%s)", exc.code)
            skipped += 1
            continue
        taken.append(attachment)
        total += len(attachment.data)
    if skipped:
        logger.info("School email: %d attachment(s) not sent (type or size)", skipped)
    return taken


async def _document(access_token: str, message_id: str, senders, exclusions) -> imports.SourceDocument | None:
    message = await google_gmail.get_message(access_token, message_id)
    # The full message's own headers must pass the same check as its envelope.
    envelope = {"from": message.from_addresses, "to": message.to_addresses}
    if not allowed(envelope, senders, exclusions):
        return None
    attachments = await _attachments(access_token, message)
    return imports.SourceDocument(
        kind="gmail",
        source_ref=message.id or message_id,
        text=message.text[:extraction.MAX_TEXT_CHARS],
        attachments=tuple(attachments),
        subject=message.subject[:imports.MAX_SUBJECT_CHARS] or None,
        sender=message.sender[:200] or None,
        received_at=message.received_at,
    )


async def _extract_code(db, source_id: int) -> str | None:
    row = await (await db.execute("SELECT error_code FROM import_sources WHERE id = ?", (source_id,))).fetchone()
    return row["error_code"] if row else None


async def _check(db, now: datetime) -> CheckResult:
    if not google_oauth.is_configured():
        return CheckResult("google_off")
    if not extraction.is_configured():
        return CheckResult("not_configured")
    senders = await get_senders(db)
    if not senders:
        return CheckResult("no_senders")
    exclusions = await get_exclusions(db)
    access_token, offline = await google_oauth.connect(db)
    if offline:
        return CheckResult("offline")
    if not access_token:
        return CheckResult("not_connected")
    if not await google_oauth.has_scope(db, google_oauth.GMAIL_READ_SCOPE):
        return CheckResult("reconnect")

    checkpoint = await get_checkpoint(db)
    after = checkpoint if checkpoint is not None else int((now - timedelta(days=BACKFILL_DAYS)).timestamp())
    query = build_query(senders, exclusions, after)
    emails = items = blocked = 0
    retry_later = False
    try:
        ids = await google_gmail.list_message_ids(access_token, query, MAX_MESSAGES_PER_RUN + 1)
        capped = len(ids) > MAX_MESSAGES_PER_RUN
        for message_id in reversed(ids[:MAX_MESSAGES_PER_RUN]):  # oldest first
            if await _already_read(db, message_id):
                continue
            envelope = await google_gmail.get_envelope(access_token, message_id)
            if not allowed(envelope, senders, exclusions):
                blocked += 1
                continue
            doc = await _document(access_token, message_id, senders, exclusions)
            if doc is None:
                blocked += 1
                continue
            result = await imports.ingest(db, doc)
            if result.already:
                continue
            emails += 1
            items += result.candidate_count
            if result.status == "failed" and await _extract_code(db, result.source_id) in TRANSIENT_EXTRACT:
                retry_later = True
            elif result.status == "not_configured":
                retry_later = True
    except httpx.HTTPError as exc:
        code = _gmail_code(exc)
        if code in ("offline", "rate_limited", "server_error"):
            http_client.report_failure(logger, OUTAGE_KEY, "School email check failed: %s", http_client.describe(exc))
        else:
            logger.warning("School email check failed: %s (%s)", http_client.describe(exc), code)
        return CheckResult(code, emails, items)
    http_client.report_success(logger, OUTAGE_KEY)
    if blocked:
        logger.info("School email: %d message(s) not from an allowed sender were skipped", blocked)
    if retry_later:
        return CheckResult("extract_failed", emails, items)
    if not capped:
        # Only now: anything that failed above is listed again next time.
        new_checkpoint = int((now - CHECKPOINT_OVERLAP).timestamp())
        if checkpoint is None or new_checkpoint > checkpoint:
            await set_setting(db, CHECKPOINT_SETTING, str(new_checkpoint))
            await db.commit()
    return CheckResult(OK, emails, items)


async def check_now(db, now: datetime | None = None) -> CheckResult:
    """One check (Admin's Check now, and the scheduler). Returns ran=False
    without doing anything when a check is already running."""
    if _lock.locked():
        return CheckResult("busy", ran=False)
    async with _lock:
        now = now or datetime.now(UTC)
        try:
            result = await _check(db, now)
        except Exception as exc:  # never let the job die silently; type only (no content)
            logger.error("School email check crashed: %s", type(exc).__name__)
            try:
                await db.rollback()
            except Exception:
                pass
            result = CheckResult("error")
        await _record(db, now, result.result, result.emails, result.items)
        logger.info("School email check: %s, %d new email(s), %d item(s)", result.result, result.emails, result.items)
        return result


async def run_if_due(now: datetime | None = None) -> CheckResult | None:
    """Scheduler entry point: checked every CHECK_SECONDS and at startup."""
    now = now or datetime.now(UTC)
    async with get_db() as db:
        schedule = await get_schedule(db)
        tz = await family_timezone(db)
        if not is_due(await get_status(db), schedule, now, tz):
            return None
        return await check_now(db, now)


async def summary(db, now: datetime | None = None) -> dict:
    """For Admin's School email panel."""
    now = now or datetime.now(UTC)
    tz = await family_timezone(db)
    schedule = await get_schedule(db)
    status = await get_status(db)
    last = _parse(status.get("last_attempt_at"))
    upcoming = next_slot(now, schedule, tz)
    connected = await google_oauth.get_connected_account(db) is not None
    return {
        "schedule": schedule,
        "schedules": SCHEDULES,
        "senders": await get_senders(db),
        "exclusions": await get_exclusions(db),
        "always_excluded": ALWAYS_EXCLUDED,
        "connected": connected,
        "gmail_scope": await google_oauth.has_scope(db, google_oauth.GMAIL_READ_SCOPE),
        "events_scope": await google_oauth.has_scope(db, google_oauth.CALENDAR_EVENTS_SCOPE),
        "last_check_at": last.astimezone(tz) if last else None,
        "last_check_ago": sync_status.ago(last, now),
        "next_check_at": upcoming.astimezone(tz) if upcoming else None,
        "result": status.get("result"),
        "result_ok": status.get("result") == OK,
        "result_text": RESULT_TEXT.get(status.get("result") or "", status.get("result")),
        "emails_read": int(status.get("emails") or 0),
        "items_found": int(status.get("items") or 0),
        "running": check_in_progress(),
    }
