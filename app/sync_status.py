"""
Google Tasks sync health: what the last cycles did, persisted in the
one-row `sync_status` table so Admin, the dashboard's status dot and /health
can say whether sync is working — instead of it failing silently.

task_sync.run_sync() records every cycle here. Only log-safe summaries are
stored ("offline", "HTTP 403", "rate limited", "error", "not connected"),
never tokens, URLs or response bodies.

States (summary()["state"]), most serious first:
- off           Google isn't configured on this install — nothing to show
- unlinked      no account connected since install or the last deliberate
                Disconnect — a choice, not a fault: Admin explains, no dot
- disconnected  Google dropped the connection (e.g. a revoked grant) (red)
- attention     Google keeps refusing access (the same 401/403 for
                ATTENTION_AFTER cycles in a row) — reconnect in Admin (red)
- failing       every cycle has failed for FAILING_AFTER or longer   (amber)
- stalled       no cycle for STALLED_AFTER since the last one or since
                this process started (scheduler stuck)              (amber)
- retrying      failing, but not for long yet: a blip, not shown on the dashboard
- waiting       connected, no cycle has run yet
- ok            the last cycle succeeded
"""

from datetime import datetime, timedelta, timezone

import httpx

from app import google_accounts, google_oauth
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
RATE_LIMITED = "rate limited"
UNEXPECTED = "error"  # anything that isn't an HTTP/network failure (a bug, a locked DB)

# Google's 403 reasons (error.errors[].reason, error.status,
# error.details[].reason), lower-cased. Rate limits come back as 403 too and
# are transient; only a permission/scope refusal needs a person to reconnect.
PERMISSION_REASONS = {
    "insufficientpermissions",
    "forbidden",
    "permission_denied",
    "access_token_scope_insufficient",
    "autherror",
    "accessnotconfigured",
}
RATE_LIMIT_REASONS = {
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quotaexceeded",
    "dailylimitexceeded",
    "resource_exhausted",
    "rate_limit_exceeded",
}

# "stalled" is measured from the later of the last cycle and this: after
# downtime the stored last_cycle_at is old, and the scheduler's first cycle
# only runs a minute after start.
PROCESS_STARTED_AT = datetime.now(timezone.utc)

# The dashboard dot's colour for each state; None = nothing shown.
LEVELS = {
    "off": None,
    "unlinked": None,
    "ok": None,
    "waiting": None,
    "retrying": None,
    "failing": "amber",
    "stalled": "amber",
    "attention": "red",
    "disconnected": "red",
}


def _error_reasons(response: httpx.Response) -> set[str]:
    """Google's machine-readable reasons from an error body, lower-cased.
    Only compared against the fixed sets above — never stored or logged."""
    try:
        error = response.json().get("error")
    except Exception:  # no body, not JSON, not an object
        return set()
    if not isinstance(error, dict):
        return set()
    reasons = {error.get("status")}
    for key in ("errors", "details"):
        items = error.get(key)
        if isinstance(items, list):
            reasons.update(item.get("reason") for item in items if isinstance(item, dict))
    return {r.lower() for r in reasons if isinstance(r, str)}


def error_code(exc: BaseException) -> str:
    """A short, log-safe code, like http_client.describe() but in the
    family's words: every network failure is "offline", and anything that
    isn't an HTTP failure at all is "error".

    A 403 is "HTTP 403" (counts towards needs-attention) when Google says
    it's a permission/scope refusal or gives no reason at all; "rate
    limited" for a rate limit; "HTTP 403 other" for any other reason."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 403:
            reasons = _error_reasons(exc.response)
            if reasons & RATE_LIMIT_REASONS:
                return RATE_LIMITED
            if reasons and not reasons & PERMISSION_REASONS:
                return "HTTP 403 other"
        return f"HTTP {status}"
    if isinstance(exc, httpx.TransportError):
        return "offline"
    return UNEXPECTED


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


async def reset(db) -> None:
    """Forget all sync history: on a new Google grant (the old one's failures
    are over) and on a deliberate Disconnect (not a fault)."""
    await db.execute("DELETE FROM sync_status")
    await db.execute("INSERT INTO sync_status (id) VALUES (1)")
    await db.commit()


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
        return (
            "Google disconnected",
            "Google ended the connection (access was revoked or expired). Reconnect the account under "
            "Google accounts; changes made here are kept and will sync once it's reconnected.",
        )
    if state == "unlinked":
        return (
            "Google not connected",
            "Tick Tasks & shopping on an account under Google accounts to sync tasks and shopping.",
        )
    if state == "attention":
        return (
            "Needs attention",
            "Google refused access — Reconnect the account doing Tasks & shopping under Google accounts. "
            "Changes made here are kept and will sync once it's reconnected.",
        )
    if state == "failing":
        if error == "offline":
            reason = "Can't reach Google"
        elif error == UNEXPECTED:
            reason = "Sync keeps hitting an unexpected error (details are in the server log)"
        else:
            reason = f"Google keeps failing ({error})"
        return (
            "Sync delayed",
            f"{reason} — failing for {failing_for}. Changes made here are kept and will sync when it's back.",
        )
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
    accounts = await google_accounts.list_accounts(db)
    tasks_account = next((a for a in accounts if "tasks" in a["jobs"]), None)
    connected = bool(tasks_account and tasks_account["connected"])
    last_cycle = _parse(row["last_cycle_at"])
    failing_since = _parse(row["failing_since"])

    if not google_oauth.is_configured() and not accounts:
        state = "off"
    elif not connected:
        # A cycle recorded since the last reset() means an account was
        # connected and then dropped without anyone pressing Disconnect.
        ever_connected = bool(row["last_success_at"] or row["last_failure_at"])
        state = "disconnected" if ever_connected else "unlinked"
    elif row["auth_failures"] >= ATTENTION_AFTER:
        state = "attention"
    elif row["consecutive_failures"] and failing_since and now - failing_since >= FAILING_AFTER:
        state = "failing"
    elif last_cycle and now - max(last_cycle, PROCESS_STARTED_AT) >= STALLED_AFTER:
        state = "stalled"
    elif row["consecutive_failures"]:
        state = "retrying"
    elif last_cycle is None or not row["connected"]:
        state = "waiting"
    else:
        state = "ok"

    headline, detail = _describe(
        state,
        row,
        failing_for=_duration(failing_since, now),
        stalled_for=_duration(last_cycle, now),
    )
    tz = await family_timezone(db)
    last_success = _parse(row["last_success_at"])
    last_failure = _parse(row["last_failure_at"])
    dot = {"state": state, "level": LEVELS[state], "headline": headline, "detail": detail}
    account_dot = _account_dot(accounts, now)
    if account_dot and _LEVEL_ORDER[account_dot["level"]] > _LEVEL_ORDER[dot["level"]]:
        dot = account_dot
    return {
        "state": state,
        "level": LEVELS[state],
        "headline": headline,
        "detail": detail,
        # The top-bar dot: this, or the worst Google account's state when that's worse (spec 12.4).
        "dot": dot,
        "accounts": [{"id": a["id"], "state": a["state"]} for a in accounts],
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
        # Each Google account by its row number, never its address.
        "accounts": s["accounts"],
    }


_LEVEL_ORDER = {None: 0, "amber": 1, "red": 2}


def _account_dot(accounts: list[dict], now: datetime) -> dict | None:
    """The dot for the worst Google account, or None while they're all fine.
    Offline only counts once it has lasted FAILING_AFTER, like sync: a blip
    shouldn't light the wall up."""
    states = {a["state"] for a in accounts}
    if google_accounts.RECONNECT in states:
        return {
            "state": "account-reconnect",
            "level": "red",
            "headline": "Reconnect Google",
            "detail": "A Google account's sign-in was revoked or expired. Reconnect it under Google & Sync → "
            "Google accounts; everything linked to it is kept.",
        }
    if google_accounts.PERMISSION in states:
        return {
            "state": "account-permission",
            "level": "red",
            "headline": "Google needs a permission",
            "detail": "A Google account does a job it hasn't allowed. Reconnect it under Google & Sync → "
            "Google accounts and allow every permission.",
        }
    offline_since = [
        since
        for a in accounts
        if a["state"] == google_accounts.OFFLINE and (since := _parse(a["offline_since"])) is not None
    ]
    if offline_since and now - min(offline_since) >= FAILING_AFTER:
        return {
            "state": "account-offline",
            "level": "amber",
            "headline": "Google offline",
            "detail": f"Can't reach Google for a connected account for {_duration(min(offline_since), now)}; "
            "its calendars show their last saved copy.",
        }
    return None
