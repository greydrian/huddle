"""
Google Tasks sync health: what the last cycles did, persisted in the
one-row `sync_status` table so Admin, the dashboard's status dot and /health
can say whether sync is working — instead of it failing silently.

task_sync.run_sync() records every cycle here. Only log-safe summaries are
stored ("offline", "HTTP 403", "not connected"), never tokens, URLs or
response bodies.

States (summary()["state"]), most serious first:
- off           Google isn't configured on this install — nothing to show
- disconnected  configured, but no Google account connected       (red)
- attention     Google keeps refusing access (the same 401/403 for
                ATTENTION_AFTER cycles in a row) — reconnect in Admin (red)
- failing       every cycle has failed for FAILING_AFTER or longer   (amber)
- stalled       no cycle has run for STALLED_AFTER (scheduler stuck)  (amber)
- retrying      failing, but not for long yet: a blip, not shown on the dashboard
- waiting       connected, no cycle has run yet
- ok            the last cycle succeeded
"""

from datetime import datetime, timedelta, timezone

import httpx

from app import google_oauth
from app.database import family_timezone

# Consecutive cycles (one a minute) of the same 401/403 before it stops being
# "maybe transient" and needs a person to reconnect.
ATTENTION_AFTER = 10
# How long every cycle has to fail before the dashboard shows amber: a short
# Wi-Fi or Google blip shouldn't light the wall up.
FAILING_AFTER = timedelta(minutes=5)
# No cycle at all for this long means the scheduler isn't running.
STALLED_AFTER = timedelta(minutes=15)

AUTH_ERRORS = ("HTTP 401", "HTTP 403")
NOT_CONNECTED = "not connected"

# The dashboard dot's colour for each state; None = nothing shown.
LEVELS = {
    "off": None, "ok": None, "waiting": None, "retrying": None,
    "failing": "amber", "stalled": "amber",
    "attention": "red", "disconnected": "red",
}


def error_code(exc: BaseException) -> str:
    """A short, log-safe description, like http_client.describe() but in the
    family's words for the common case: every network failure is "offline"."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TransportError):
        return "offline"
    return type(exc).__name__


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _queue_depth(db) -> int:
    (count,) = await (await db.execute("SELECT COUNT(*) FROM sync_queue")).fetchone()
    return count


async def _row(db):
    row = await (await db.execute("SELECT * FROM sync_status WHERE id = 1")).fetchone()
    if row is None:  # init_db seeds it; this only guards a hand-emptied table
        await db.execute("INSERT OR IGNORE INTO sync_status (id) VALUES (1)")
        row = await (await db.execute("SELECT * FROM sync_status WHERE id = 1")).fetchone()
    return row


# --- Recording (called by task_sync.run_sync once per cycle) ----------------

async def record_success(db, now: datetime | None = None) -> None:
    now = now or _now()
    await db.execute(
        """UPDATE sync_status SET connected = 1, last_cycle_at = ?, last_success_at = ?,
               failing_since = NULL, consecutive_failures = 0, auth_failures = 0, queue_depth = ?
           WHERE id = 1""",
        (_iso(now), _iso(now), await _queue_depth(db)),
    )
    await db.commit()


async def record_not_connected(db, now: datetime | None = None) -> None:
    """No Google account (or its grant was revoked): not a failing cycle —
    there's nothing to retry until someone connects in Admin."""
    now = now or _now()
    await db.execute(
        """UPDATE sync_status SET connected = 0, last_cycle_at = ?, last_error = ?,
               failing_since = NULL, consecutive_failures = 0, auth_failures = 0, queue_depth = ?
           WHERE id = 1""",
        (_iso(now), NOT_CONNECTED, await _queue_depth(db)),
    )
    await db.commit()


async def record_failure(db, exc: BaseException, now: datetime | None = None) -> bool:
    """Records a failed cycle. True when sync now needs attention: the same
    401/403 has come back ATTENTION_AFTER cycles in a row."""
    now = now or _now()
    code = error_code(exc)
    row = await _row(db)
    if code in AUTH_ERRORS:
        auth_failures = row["auth_failures"] + 1 if row["last_error"] == code else 1
    else:
        auth_failures = 0
    await db.execute(
        """UPDATE sync_status SET connected = 1, last_cycle_at = ?, last_failure_at = ?, last_error = ?,
               failing_since = COALESCE(failing_since, ?), consecutive_failures = consecutive_failures + 1,
               auth_failures = ?, queue_depth = ?
           WHERE id = 1""",
        (_iso(now), _iso(now), code, _iso(now), auth_failures, await _queue_depth(db)),
    )
    await db.commit()
    return auth_failures >= ATTENTION_AFTER


# --- Reading ---------------------------------------------------------------

def ago(then: datetime | None, now: datetime) -> str | None:
    """'just now', '3 minutes ago', '2 hours ago', '4 days ago'."""
    if then is None:
        return None
    seconds = max(0, int((now - then).total_seconds()))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


def _duration(then: datetime | None, now: datetime) -> str | None:
    """'7 minutes' — ago() without the ' ago'."""
    text = ago(then, now)
    return text.removesuffix(" ago") if text else None


def _describe(state: str, row, failing_for: str | None, stalled_for: str | None) -> tuple[str, str]:
    """(headline, what to do) in plain words for each state."""
    error = row["last_error"]
    if state == "off":
        return "Not set up", "Google isn't configured on this display, so nothing syncs."
    if state == "disconnected":
        return "Google not connected", "Connect a Google account under Google Account to sync tasks and shopping."
    if state == "attention":
        return ("Needs attention",
                "Google refused access — reconnect in Admin (Disconnect, then Connect Google Account). "
                "Changes made here are kept and will sync once it's reconnected.")
    if state == "failing":
        reason = "Can't reach Google" if error == "offline" else f"Google keeps failing ({error})"
        return ("Sync delayed",
                f"{reason} — failing for {failing_for}. Changes made here are kept and will sync "
                "when it's back.")
    if state == "stalled":
        return "Sync not running", f"No sync has run for {stalled_for}. Try Sync now, or restart the display server."
    if state == "retrying":
        return "Retrying", f"The last sync failed ({error}); it retries every minute."
    if state == "waiting":
        return "Waiting for the first sync", "Sync runs every minute."
    return "Syncing normally", "Tasks and the shopping list sync with Google every minute."


async def summary(db, now: datetime | None = None) -> dict:
    """Everything the dot, Admin and /health show. Connection and queue depth
    are read live, so they're right straight after a connect/disconnect or a
    tick on the dashboard, not up to a minute later."""
    now = now or _now()
    row = await _row(db)
    connected = await google_oauth.get_connected_account(db) is not None
    last_cycle = _parse(row["last_cycle_at"])
    failing_since = _parse(row["failing_since"])

    if not google_oauth.is_configured() and not connected:
        state = "off"
    elif not connected:
        state = "disconnected"
    elif row["auth_failures"] >= ATTENTION_AFTER:
        state = "attention"
    elif row["consecutive_failures"] and failing_since and now - failing_since >= FAILING_AFTER:
        state = "failing"
    elif last_cycle and now - last_cycle >= STALLED_AFTER:
        state = "stalled"
    elif row["consecutive_failures"]:
        state = "retrying"
    elif last_cycle is None or not row["connected"]:
        state = "waiting"
    else:
        state = "ok"

    headline, detail = _describe(
        state, row,
        failing_for=_duration(failing_since, now), stalled_for=_duration(last_cycle, now),
    )
    tz = await family_timezone(db)
    last_success = _parse(row["last_success_at"])
    last_failure = _parse(row["last_failure_at"])
    return {
        "state": state,
        "level": LEVELS[state],
        "headline": headline,
        "detail": detail,
        "connected": connected,
        "queue_depth": await _queue_depth(db),
        "consecutive_failures": row["consecutive_failures"],
        "last_error": row["last_error"],
        # Aware datetimes in the family's timezone, for display.
        "last_success_at": last_success.astimezone(tz) if last_success else None,
        "last_success_ago": ago(last_success, now),
        "last_failure_at": last_failure.astimezone(tz) if last_failure else None,
        "last_failure_ago": ago(last_failure, now),
        "last_cycle_at": last_cycle.astimezone(tz) if last_cycle else None,
    }


def health_summary(s: dict) -> dict:
    """The JSON-safe part of summary() for /health (unauthenticated on the
    LAN): state and counters only — no account email, no error bodies."""
    def iso(dt):
        return dt.isoformat() if dt else None
    return {
        "state": s["state"],
        "connected": s["connected"],
        "queue_depth": s["queue_depth"],
        "consecutive_failures": s["consecutive_failures"],
        "last_error": s["last_error"],
        "last_success_at": iso(s["last_success_at"]),
        "last_failure_at": iso(s["last_failure_at"]),
    }
