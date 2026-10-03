"""
School email import: once a day (by default) read the school's emails from
the connected Gmail account and feed each one into the school inbox
(services/imports.ingest), where Claude finds the spellings, homework and
events in it and a parent approves them in Admin. Nothing reaches the wall
unapproved.

Which emails: From an Admin-editable sender allowlist ("office@school",
"*@school-domain"), minus an exclusion list, received after a stored
checkpoint (the first run looks back BACKFILL_DAYS).

The SENDCo address is excluded always, whatever the lists say: those are
private conversations, never fetched or sent anywhere. Defence in depth:
1. the Gmail search leaves out messages from/to/cc it or mentioning it;
2. each message's From/To/Cc/Reply-To are fetched as metadata only and
   re-checked (plus-tags and case normalised) before its body is fetched;
3. the full message is re-checked, and dropped if its headers or any of its
   text (quoted history included) mention an excluded address;
4. quoted history is stripped from what is sent (google_gmail.strip_quoted),
   so a reply doesn't carry the family's own earlier message to Claude.
Messages whose sender fails Gmail's own DMARC (or both SPF and DKIM) are
dropped too; ones with no verdict are read but marked unverified.

Only the school emails are sent to the Anthropic API; that's what the
family consented to.

Scheduling (scheduler.py checks `run_if_due` every CHECK_SECONDS and once
at startup, like the nightly backup): daily at a family-local time (default
18:00), twice daily (that time and 12 hours before it), weekly on Friday at
that time, or off. The due-check is done in UTC from the family timezone,
so DST changes neither skip nor double a check. A failed check is retried
after RETRY_AFTER_FAILURE; a run that hit MAX_MESSAGES_PER_RUN continues
after CONTINUE_AFTER. "Check now" (a background task) and the scheduler
share one lock, so two checks never run at once.

Order and the checkpoint: messages are read oldest first, at most
MAX_MESSAGES_PER_RUN Claude reads per run (and imports.DAILY_CLAUDE_CAP a
day across the whole inbox). The checkpoint moves to the last message
settled (minus CHECKPOINT_OVERLAP), so a capped run carries on where it
stopped. One message never holds it back: a school email Claude couldn't
read is retried from the inbox itself (not the search), and given up on
after imports.MAX_ATTEMPTS (Admin then offers Retry). Messages already read
or skipped are passed over without being fetched.

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

from app import google_accounts, google_gmail, google_oauth, http_client, sync_status
from app.database import family_timezone, get_db, get_setting, set_setting
from app.services import extraction, imports

logger = logging.getLogger(__name__)

OUTAGE_KEY = "Gmail (school email)"

EXCLUSIONS_SETTING = "school_email_exclusions"
SCHEDULE_SETTING = "school_email_schedule"
CHECKPOINT_SETTING = "school_email_checkpoint"  # epoch seconds
# Spec 12 stage 1: one account does School email at a time, and the job can
# move. The checkpoint belongs to the account that set it (another account
# starts with a BACKFILL_DAYS look-back), and failed emails waiting for a
# retry belong to the account that last checked: when the job moves, they're
# parked under that account (never fetched with another's token, never given
# up as gone) and come back if it does School email again.
CHECKPOINT_ACCOUNT_SETTING = "school_email_checkpoint_account"  # google_accounts.id
READER_SETTING = "school_email_reader"  # google_accounts.id of the last check
PARKED_RETRIES_SETTING = "school_email_parked_retries"  # {account id: [Gmail ids]}
STATUS_SETTING = "school_email_status"

# Private SENDCo conversations: never fetched, whatever the lists say.
ALWAYS_EXCLUDED = ("sen@gresham.croydon.sch.uk",)
DEFAULT_EXCLUSIONS = ALWAYS_EXCLUDED
MAX_ENTRIES = 20

BACKFILL_DAYS = 14
# The checkpoint is set this far before the last settled message, so a
# message Gmail stamps a little late is still listed next time (already-read
# ones are passed over without being fetched).
CHECKPOINT_OVERLAP = timedelta(days=1)
# Cost guard: at most this many emails sent to Claude per run; the rest wait
# for the next run (CONTINUE_AFTER), oldest first.
MAX_MESSAGES_PER_RUN = 25
# How many ids one search lists at most (newest first, so a backlog bigger
# than this loses its oldest; far more than a school sends in 14 days).
LIST_CEILING = 500
# Images smaller than this are logos and signature icons, not letters.
MIN_IMAGE_BYTES = 8_000

CHECK_SECONDS = 600
RETRY_AFTER_FAILURE = timedelta(hours=1)
CONTINUE_AFTER = timedelta(minutes=15)

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
CAPPED = "capped"
DAILY_CAP = "daily_cap"
# Nothing to try until someone changes something: not retried until the next slot.
SKIPPED = {"not_connected", "reconnect", "api_disabled", "not_configured", "no_senders", "google_off"}
RESULT_TEXT = {
    OK: "OK",
    CAPPED: "Capped",
    DAILY_CAP: f"Reached today's limit of {imports.DAILY_CLAUDE_CAP} documents read by Claude; continuing tomorrow",
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
# Claude failures worth retrying automatically (up to imports.MAX_ATTEMPTS);
# any other code is permanent for that email.
TRANSIENT_EXTRACT = {"offline", "rate_limited", "server_error", "error", "auth"}
# Failed for a reason that isn't the email's fault: retried, no attempt used up.
RETRY_CODES = {"daily_cap", "interrupted", "retry"}

# gmail_skipped codes (neutral: they say why, never what the email said).
SKIP_NOT_ALLOWED = "not_allowed"
SKIP_EXCLUDED = "excluded"
SKIP_UNVERIFIED = "unverified_sender"
SKIP_GONE = "gone"


# --- Sender lists ---

# An address or "*@domain", nothing else: no Gmail operator characters
# (( ) { } " : *), no spaces, and neither the local part nor a domain label
# may start with "-" or "." (Gmail reads a leading "-" as NOT, which would
# widen the search). Internal hyphens ("year-4@", "st-marys.sch.uk") are
# ordinary address characters and fine.
_LOCAL = r"[a-z0-9](?:[a-z0-9._%+-]*[a-z0-9_%+])?"
_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_ENTRY = re.compile(rf"(?:\*|{_LOCAL})@{_LABEL}(?:\.{_LABEL})*\.[a-z]{{2,}}")


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
        if not _ENTRY.fullmatch(entry) or len(entry) > 120 or ".." in entry:
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
    """Every school's senders (spec 11.2: each school keeps its own list,
    services/schools.py); one Gmail search covers them all."""
    from app.services import schools  # schools imports this module

    return await schools.all_senders(db)


async def get_exclusions(db) -> list[str]:
    return await _entries(db, EXCLUSIONS_SETTING, DEFAULT_EXCLUSIONS)


async def set_exclusions(db, exclusions: list[str]):
    await set_setting(db, EXCLUSIONS_SETTING, "\n".join(exclusions))
    await db.commit()


def _all_exclusions(exclusions: list[str]) -> list[str]:
    return [*ALWAYS_EXCLUDED, *(e for e in exclusions if e not in ALWAYS_EXCLUDED)]


def normalise(address: str) -> str:
    """Lower-cased, with any "+tag" dropped: sen+x@school is sen@school."""
    local, at, domain = address.strip().lower().rpartition("@")
    if not at:
        return address.strip().lower()
    return f"{local.split('+', 1)[0]}@{domain}"


def matches(address: str, entry: str) -> bool:
    address, entry = normalise(address), normalise(entry)
    if entry.startswith("*@"):
        return address.rpartition("@")[2] == entry[2:]
    return address == entry


def _gmail_term(entry: str) -> str:
    # Gmail has no wildcards: "@domain" matches every address at that domain.
    return entry[1:] if entry.startswith("*@") else entry


def build_query(senders: list[str], exclusions: list[str], after: int) -> str:
    """The Gmail search: from any sender, not from/to/cc any exclusion, not
    mentioning an excluded address anywhere, received after `after` (epoch
    seconds)."""
    if not senders:
        raise ValueError("no senders")
    terms = " OR ".join(_gmail_term(s) for s in senders)
    parts = [f"from:({terms})"]
    for entry in _all_exclusions(exclusions):
        term = _gmail_term(entry)
        parts += [f"-from:{term}", f"-to:{term}", f"-cc:{term}"]
        if not entry.startswith("*@"):
            parts.append(f'-"{term}"')  # also forwarded or quoted in the text
    parts.append(f"after:{int(after)}")
    return " ".join(parts)


def allowed(envelope: dict[str, list[str]], senders: list[str], exclusions: list[str]) -> bool:
    """Exactly one From address, on the allowlist, and no excluded address
    anywhere on the message's headers."""
    froms = envelope.get("from") or []
    if len(froms) != 1 or not any(matches(froms[0], s) for s in senders):
        return False
    everyone = [a for key in ("from", "to", "cc", "reply-to") for a in envelope.get(key) or []]
    return not any(matches(a, e) for a in everyone for e in _all_exclusions(exclusions))


def _mention_pattern(entry: str) -> re.Pattern:
    local, _, domain = normalise(entry).partition("@")
    return re.compile(
        rf"(?<![a-z0-9._%+-]){re.escape(local)}(?:\+[^@\s]*)?(?:@|%40|\s*\[at\]\s*){re.escape(domain)}(?![a-z0-9-]|\.[a-z0-9])",
        re.I,
    )


def mentions_excluded(text: str, exclusions: list[str]) -> bool:
    """Whether an excluded address (an exact one, not a *@domain) appears
    anywhere in the text: a forwarded or quoted SENDCo email is dropped
    whole, never sent on."""
    return any(_mention_pattern(e).search(text or "") for e in _all_exclusions(exclusions) if not e.startswith("*@"))


def sender_verdict(auth: dict[str, list[str]] | None) -> bool | None:
    """From Gmail's own Authentication-Results: False (drop it) when DMARC
    failed, or SPF and DKIM both failed; True when any of them passed; None
    (read it, marked unverified) when there's no usable result."""
    if not auth:
        return None
    dmarc, spf, dkim = auth.get("dmarc", []), auth.get("spf", []), auth.get("dkim", [])
    if "fail" in dmarc:
        return False
    spf_failed = bool(spf) and all(r in ("fail", "softfail") for r in spf)
    dkim_failed = bool(dkim) and "pass" not in dkim and any(r == "fail" for r in dkim)
    if spf_failed and dkim_failed:
        return False
    if "pass" in dmarc or "pass" in spf or "pass" in dkim:
        return True
    return None


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
    except ValueError, TypeError, AttributeError:
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
    next slot; one that failed is retried after RETRY_AFTER_FAILURE; one
    that stopped at the per-run cap carries on after CONTINUE_AFTER."""
    if schedule.mode == "off":
        return False
    last_try = _parse(status.get("last_attempt_at"))
    result = status.get("result")
    if result == CAPPED:
        return last_try is None or now - last_try >= CONTINUE_AFTER
    slot = last_slot(now, schedule, tz)
    if slot is None:
        return False
    last_ok = _parse(status.get("last_success_at"))
    if last_ok and last_ok >= slot:
        return False
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
    except TypeError, ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def get_status(db) -> dict:
    try:
        status = json.loads(await get_setting(db, STATUS_SETTING) or "{}")
    except ValueError:
        status = {}
    return status if isinstance(status, dict) else {}


async def _record(db, now: datetime, result: "CheckResult"):
    status = await get_status(db)
    status.update(
        last_attempt_at=now.isoformat(),
        result=result.result,
        emails=result.emails,
        items=result.items,
        skipped=result.skipped,
        remaining=result.remaining,
    )
    if result.result == OK:
        status["last_success_at"] = now.isoformat()
    await set_setting(db, STATUS_SETTING, json.dumps(status))
    await db.commit()


async def _account_setting(db, key: str) -> int | None:
    raw = await get_setting(db, key)
    return int(raw) if raw and raw.isdigit() else None


async def checkpoint_account(db) -> int | None:
    """The account the checkpoint belongs to (one set before accounts is
    account 1's)."""
    if await get_setting(db, CHECKPOINT_SETTING) is None:
        return None
    return await _account_setting(db, CHECKPOINT_ACCOUNT_SETTING) or 1


async def get_checkpoint(db, account_id: int | None = None) -> int | None:
    """The checkpoint (epoch seconds); with `account_id`, only if it's that
    account's, else None (that account's first check looks back BACKFILL_DAYS)."""
    raw = await get_setting(db, CHECKPOINT_SETTING)
    if account_id is not None and await checkpoint_account(db) != account_id:
        return None
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


async def clamp_checkpoint(db, now: datetime, account_id: int) -> None:
    """School email ticked again on `account_id` (spec 12.2): it reads from
    the later of its kept checkpoint and BACKFILL_DAYS ago, so a long pause
    never sends months of backlog to Claude. Another account's checkpoint is
    left alone: this account looks back BACKFILL_DAYS anyway."""
    checkpoint = await get_checkpoint(db, account_id)
    if checkpoint is not None:
        await _advance_checkpoint(db, checkpoint, int((now - timedelta(days=BACKFILL_DAYS)).timestamp()), account_id)


async def forget_account(db, account_id: int) -> None:
    """Remove (spec 12.6): this account's checkpoint and status go. Its
    failed emails stay in the School inbox, parked under it."""
    if await checkpoint_account(db) == account_id:
        await db.execute(
            "DELETE FROM app_settings WHERE key IN (?, ?)", (CHECKPOINT_SETTING, CHECKPOINT_ACCOUNT_SETTING)
        )
    if await _account_setting(db, READER_SETTING) in (account_id, None):
        await db.execute("DELETE FROM app_settings WHERE key = ?", (STATUS_SETTING,))
    await db.commit()


async def _advance_checkpoint(db, old: int | None, to: int, account_id: int | None = None):
    if old is None or to > old:
        await set_setting(db, CHECKPOINT_SETTING, str(to))
        if account_id is not None:
            await set_setting(db, CHECKPOINT_ACCOUNT_SETTING, str(account_id))
        await db.commit()


async def _parked_retries(db) -> dict[str, list[str]]:
    try:
        parked = json.loads(await get_setting(db, PARKED_RETRIES_SETTING) or "{}")
    except ValueError:
        return {}
    return parked if isinstance(parked, dict) else {}


async def _hand_over(db, account_id: int) -> None:
    """Before `account_id` checks: if another account checked last, the
    emails waiting for a retry are its (they came from its mailbox) and are
    parked under it; this account's own parked ones come back."""
    previous = await _account_setting(db, READER_SETTING)
    parked = await _parked_retries(db)
    if previous is not None and previous != account_id:
        already = {ref for refs in parked.values() for ref in refs}
        waiting = [ref for ref in await _retry_queue(db) if ref not in already]
        parked[str(previous)] = sorted({*parked.get(str(previous), []), *waiting})
    parked.pop(str(account_id), None)
    await set_setting(db, PARKED_RETRIES_SETTING, json.dumps(parked))
    await set_setting(db, READER_SETTING, str(account_id))
    await db.commit()


# --- What's already known about a message ---


async def _record_skip(db, message_id: str, code: str, internal_date: int | None):
    """Remember a message that won't be read (id, neutral code, Gmail's
    timestamp; nothing from the message itself)."""
    await db.execute(
        "INSERT OR REPLACE INTO gmail_skipped (message_id, code, internal_date) VALUES (?, ?, ?)",
        (message_id, code, internal_date),
    )
    await db.commit()


def _retryable(row) -> bool:
    if row["status"] == "not_configured":
        return True
    if row["status"] != "failed":
        return False
    if row["error_code"] in RETRY_CODES:
        return True
    return row["error_code"] in TRANSIENT_EXTRACT and row["attempts"] < imports.MAX_ATTEMPTS


def _epoch_ms(iso: str | None) -> int | None:
    parsed = _parse(iso)
    return int(parsed.timestamp() * 1000) if parsed else None


async def _known(db, message_id: str) -> tuple[str | None, int | None]:
    """("done" | "retry" | None, Gmail timestamp in ms if known). "done" is
    read, being read, given up on, failed for good, or skipped: passed over
    without fetching anything."""
    skipped = await (
        await db.execute("SELECT internal_date FROM gmail_skipped WHERE message_id = ?", (message_id,))
    ).fetchone()
    if skipped is not None:
        return "done", skipped["internal_date"]
    row = await (
        await db.execute(
            "SELECT status, error_code, attempts, received_at FROM import_sources WHERE kind = 'gmail' AND source_ref = ?",
            (message_id,),
        )
    ).fetchone()
    if row is None:
        return None, None
    return ("retry" if _retryable(row) else "done"), _epoch_ms(row["received_at"])


async def _retry_queue(db) -> list[str]:
    """School emails to read again, oldest first: Claude failed for a
    transient reason (attempts left), hit the daily cap, or Retry was
    pressed. Read by id, whatever the search window. Not those parked under
    another account (see _hand_over)."""
    rows = await (
        await db.execute(
            """SELECT source_ref, status, error_code, attempts FROM import_sources
           WHERE kind = 'gmail' AND status IN ('failed', 'not_configured') ORDER BY received_at, id"""
        )
    ).fetchall()
    parked = {ref for refs in (await _parked_retries(db)).values() for ref in refs}
    return [r["source_ref"] for r in rows if _retryable(r) and r["source_ref"] not in parked]


# --- Reading one message ---


def _ordered_parts(parts: list[google_gmail.AttachmentPart]) -> list[google_gmail.AttachmentPart]:
    """PDFs first (newsletters, letters), then the larger images; logos and
    anything that isn't a PDF or image are dropped."""
    pdfs = [p for p in parts if p.mime_type == extraction.PDF_TYPE or p.filename.lower().endswith(".pdf")]
    images = [
        p
        for p in parts
        if p.mime_type.startswith("image/") and p not in pdfs and (p.size == 0 or p.size >= MIN_IMAGE_BYTES)
    ]
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


@dataclass
class CheckResult:
    result: str
    emails: int = 0  # emails sent to Claude this time
    items: int = 0  # items Claude found in them
    skipped: int = 0  # not read (not from the school, excluded, unverified)
    remaining: int = 0  # left for the next run (per-run cap)
    ran: bool = True  # False: another check was already running


@dataclass
class _Outcome:
    kind: str  # "read" / "failed" / "skipped" / "cap"
    internal_date: int | None = None
    items: int = 0
    retry_later: bool = False


async def _read_one(db, token: str, message_id: str, senders, exclusions) -> _Outcome:
    """Checks and reads one message. Raises httpx.HTTPError for Gmail
    trouble (except a message that's gone)."""
    try:
        envelope = await google_gmail.get_envelope(token, message_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        await _record_skip(db, message_id, SKIP_GONE, None)
        await _give_up(db, message_id, "gone")
        return _Outcome("skipped")
    when = envelope.internal_date
    if not allowed(envelope.addresses, senders, exclusions):
        await _record_skip(db, message_id, SKIP_NOT_ALLOWED, when)
        return _Outcome("skipped", when)
    message = await google_gmail.get_message(token, message_id)
    when = message.internal_date or when
    # The full message's own headers must pass the same check as its envelope,
    # and no excluded address may appear anywhere in it (forwarded, quoted).
    if not allowed({"from": message.from_addresses, "to": message.to_addresses}, senders, exclusions):
        await _record_skip(db, message_id, SKIP_NOT_ALLOWED, when)
        return _Outcome("skipped", when)
    if mentions_excluded(message.everything, exclusions):
        await _record_skip(db, message_id, SKIP_EXCLUDED, when)
        return _Outcome("skipped", when)
    verified = sender_verdict(message.auth)
    if verified is False:
        await _record_skip(db, message_id, SKIP_UNVERIFIED, when)
        return _Outcome("skipped", when)
    if await imports.claude_calls_left(db) <= 0:
        return _Outcome("cap", when)
    doc = imports.SourceDocument(
        kind="gmail",
        source_ref=message_id,
        text=message.text[: extraction.MAX_TEXT_CHARS],
        attachments=tuple(await _attachments(token, message)),
        subject=message.subject[: imports.MAX_SUBJECT_CHARS] or None,
        sender=message.sender[:200] or None,
        received_at=message.received_at,
        sender_verified=verified is True,  # no verdict at all: read, but marked unverified
        school_id=await _school_for(db, message.from_addresses),
    )
    result = await imports.ingest(db, doc)
    if result.already:
        return _Outcome("skipped", when)
    if result.status == "extracted":
        return _Outcome("read", when, items=result.candidate_count)
    row = await (
        await db.execute("SELECT status, error_code, attempts FROM import_sources WHERE id = ?", (result.source_id,))
    ).fetchone()
    if row["error_code"] == "daily_cap":
        return _Outcome("cap", when)
    if row["status"] == "failed" and row["error_code"] in TRANSIENT_EXTRACT and row["attempts"] >= imports.MAX_ATTEMPTS:
        await _give_up(db, message_id, row["error_code"])
        return _Outcome("failed", when)
    return _Outcome("failed", when, retry_later=_retryable(row))


async def _school_for(db, from_addresses: list[str]) -> int | None:
    """The school whose senders list this email's (single, checked) sender."""
    from app.services import schools  # schools imports this module

    return await schools.for_sender(db, from_addresses[0]) if from_addresses else None


async def _give_up(db, message_id: str, code: str):
    """No more automatic retries for this email (Admin shows Retry)."""
    await db.execute(
        """UPDATE import_sources SET status = 'failed_permanently', error_code = ?, updated_at = datetime('now')
           WHERE kind = 'gmail' AND source_ref = ? AND status = 'failed'""",
        (code, message_id),
    )
    await db.commit()


# --- The check ---

_lock = asyncio.Lock()
_task: asyncio.Task | None = None


def check_in_progress() -> bool:
    return _lock.locked() or (_task is not None and not _task.done())


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


async def _check(db, now: datetime) -> CheckResult:
    if not google_oauth.is_configured():
        return CheckResult("google_off")
    if not extraction.is_configured():
        return CheckResult("not_configured")
    senders = await get_senders(db)
    if not senders:
        return CheckResult("no_senders")
    exclusions = await get_exclusions(db)
    # The account doing School email (stage 1 of spec 12: one at a time).
    account = await google_accounts.job_account(db, "school_email")
    if account is None:
        return CheckResult("not_connected")
    access_token, offline = await google_oauth.connect(db, account["id"])
    if offline:
        return CheckResult("offline")
    if not access_token:
        return CheckResult("not_connected")
    if not await google_oauth.has_scope(db, google_oauth.GMAIL_READ_SCOPE):
        return CheckResult("reconnect")
    if await imports.claude_calls_left(db) <= 0:
        return CheckResult(DAILY_CAP)

    account_id = account["id"]
    await _hand_over(db, account_id)
    checkpoint = await get_checkpoint(db, account_id)
    after = checkpoint if checkpoint is not None else int((now - timedelta(days=BACKFILL_DAYS)).timestamp())
    query = build_query(senders, exclusions, after)
    run = CheckResult(OK)
    retry_later = hit_daily_cap = capped = False
    settled_ms: int | None = None  # newest message settled in the search, oldest first
    listed_all = False
    done: set[str] = set()

    async def read(message_id: str) -> _Outcome:
        nonlocal retry_later
        outcome = await _read_one(db, access_token, message_id, senders, exclusions)
        done.add(message_id)
        if outcome.kind == "skipped":
            run.skipped += 1
        elif outcome.kind in ("read", "failed"):
            run.emails += 1
            run.items += outcome.items
            retry_later |= outcome.retry_later
        return outcome

    try:
        # 1. Emails Claude couldn't read last time (from the inbox, not the search).
        for message_id in await _retry_queue(db):
            if run.emails >= MAX_MESSAGES_PER_RUN:
                capped = True
                break
            if (await read(message_id)).kind == "cap":
                hit_daily_cap = True
                break
        # 2. New emails, oldest first.
        if not capped and not hit_daily_cap:
            ids = list(reversed(await google_gmail.list_message_ids(access_token, query, LIST_CEILING)))
            if len(ids) >= LIST_CEILING:
                logger.warning("School email: the search hit its %d-message ceiling", LIST_CEILING)
            for index, message_id in enumerate(ids):
                if message_id in done:
                    continue
                state, when = await _known(db, message_id)
                if state == "done":
                    if when:
                        settled_ms = max(settled_ms or 0, when)
                    continue
                if run.emails >= MAX_MESSAGES_PER_RUN:
                    capped = True
                    for later in ids[index:]:
                        if later not in done and (await _known(db, later))[0] != "done":
                            run.remaining += 1
                    break
                outcome = await read(message_id)
                if outcome.kind == "cap":
                    hit_daily_cap = True
                    break
                if outcome.internal_date:
                    settled_ms = max(settled_ms or 0, outcome.internal_date)
            else:
                listed_all = True
    except httpx.HTTPError as exc:
        code = _gmail_code(exc)
        if code in ("offline", "rate_limited", "server_error"):
            http_client.report_failure(logger, OUTAGE_KEY, "School email check failed: %s", http_client.describe(exc))
        else:
            logger.warning("School email check failed: %s (%s)", http_client.describe(exc), code)
        if settled_ms:
            await _advance_checkpoint(
                db, checkpoint, settled_ms // 1000 - int(CHECKPOINT_OVERLAP.total_seconds()), account_id
            )
        run.result = code
        return run
    http_client.report_success(logger, OUTAGE_KEY)
    if run.skipped:
        logger.info("School email: %d message(s) skipped (not allowed, excluded or unverified)", run.skipped)

    # The checkpoint follows what was settled; a failed email is retried from
    # the inbox, so it never holds the checkpoint back.
    if listed_all:
        await _advance_checkpoint(db, checkpoint, int((now - CHECKPOINT_OVERLAP).timestamp()), account_id)
    elif settled_ms:
        await _advance_checkpoint(
            db, checkpoint, settled_ms // 1000 - int(CHECKPOINT_OVERLAP.total_seconds()), account_id
        )

    if hit_daily_cap:
        run.result = DAILY_CAP
    elif capped:
        run.result = CAPPED
        run.remaining = max(run.remaining, 1)
    elif retry_later:
        run.result = "extract_failed"
    return run


async def check_now(db, now: datetime | None = None) -> CheckResult:
    """One check (the scheduler, and Check now's background task). Returns
    ran=False without doing anything when a check is already running."""
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
            except Exception:  # noqa: S110 (the connection is being dropped anyway)
                pass
            result = CheckResult("error")
        await _record(db, now, result)
        logger.info(
            "School email check: %s, %d email(s) read, %d item(s), %d skipped, %d remaining",
            result.result,
            result.emails,
            result.items,
            result.skipped,
            result.remaining,
        )
        return result


def start_check() -> bool:
    """Admin's Check now: runs a check in the background (up to
    MAX_MESSAGES_PER_RUN Claude calls is too long for a request). False if
    one is already running."""
    global _task
    if check_in_progress():
        return False

    async def run():
        async with get_db() as db:
            await check_now(db)

    _task = asyncio.create_task(run())
    return True


async def wait_for_background():
    """Waits for a Check now task (tests, demo scripts)."""
    if _task is not None:
        await asyncio.gather(_task, return_exceptions=True)


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
    result = status.get("result")
    upcoming = next_slot(now, schedule, tz)
    if result == CAPPED and last and schedule.mode != "off":
        upcoming = min(filter(None, [upcoming, max(now, last + CONTINUE_AFTER)]))
    remaining = int(status.get("remaining") or 0)
    text = RESULT_TEXT.get(result or "", result)
    if result == CAPPED:
        text = f"Capped at {MAX_MESSAGES_PER_RUN} emails: {remaining} remaining, continuing next check"
    account = await google_accounts.job_account(db, "school_email")
    return {
        "schedule": schedule,
        "schedules": SCHEDULES,
        "senders": await get_senders(db),
        "exclusions": await get_exclusions(db),
        "always_excluded": ALWAYS_EXCLUDED,
        "connected": bool(account and account["connected"]),
        "account": account,
        "gmail_scope": await google_oauth.has_scope(db, google_oauth.GMAIL_READ_SCOPE),
        "events_scope": await google_oauth.has_scope(db, google_oauth.CALENDAR_EVENTS_SCOPE),
        "last_check_at": last.astimezone(tz) if last else None,
        "last_check_ago": sync_status.ago(last, now),
        "next_check_at": upcoming.astimezone(tz) if upcoming else None,
        "result": result,
        "result_ok": result == OK,
        "result_text": text,
        "emails_read": int(status.get("emails") or 0),
        "items_found": int(status.get("items") or 0),
        "skipped_count": int(status.get("skipped") or 0),
        "remaining": remaining,
        "calls_left": await imports.claude_calls_left(db),
        "daily_cap": imports.DAILY_CLAUDE_CAP,
        "per_run_cap": MAX_MESSAGES_PER_RUN,
        "running": check_in_progress(),
    }
