"""
Google accounts (spec 12): any number of connected Google accounts, each
with an owner (a family member, or Family for a shared account) and the jobs
it does. One `google_accounts` row per account (migration 13) holds its
address, owner, ticked jobs, its own Fernet-encrypted token (whose `scope`
field is what Google actually granted) and the outcome of its last check.
The OAuth flow and token refresh are in google_oauth.py; removing an account
and what changes with it is services/accounts.py.

Stage 1 of spec 12.10: Tasks & shopping, School email and Writing events can
each be done by one account at a time (EXCLUSIVE_JOBS); Calendars by any.
Every feature finds its account with job_account().

Logs name an account by its row id, never its address.
"""

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime

from app.database import get_setting, set_setting
from app.security import decrypt_token_json, encrypt_token_json

logger = logging.getLogger(__name__)

CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
# Asked of every account, to learn its address.
IDENTITY_SCOPES = ("openid", "email")
# What a grant stored before the `scope` field was kept is assumed to cover:
# the scopes this app asked for back then.
LEGACY_SCOPES = frozenset((CALENDAR_READ_SCOPE, TASKS_SCOPE))

# The four jobs (spec 12.1), in the order Admin lists them.
JOBS = {
    "calendars": "Calendars",
    "tasks": "Tasks & shopping",
    "school_email": "School email",
    "write_events": "Writing events",
}
JOB_SCOPES = {
    "calendars": CALENDAR_READ_SCOPE,
    "tasks": TASKS_SCOPE,
    "school_email": GMAIL_READ_SCOPE,
    "write_events": CALENDAR_EVENTS_SCOPE,
}
SCOPE_JOBS = {scope: job for job, scope in JOB_SCOPES.items()}
JOB_HINTS = {
    "calendars": "its calendars can be shown on the wall",
    "tasks": "a person's task list or the shopping list can be one of its Google Tasks lists",
    "school_email": "its inbox is read by the school email check",
    "write_events": "the wall's “+” and approved school events can go into one of its calendars",
}
# Unticked, a job stops being used at once, but Google keeps the permission.
JOB_UNTICKED = {
    "calendars": "Huddle no longer shows this account's calendars.",
    "tasks": "Huddle no longer uses Google Tasks for this account; its lists are now on this display only.",
    "school_email": "Huddle no longer uses Gmail for this account.",
    "write_events": "Huddle no longer adds events to this account's calendars.",
}
# Stage 1: these can only be ticked on one account at a time.
EXCLUSIVE_JOBS = ("tasks", "school_email", "write_events")
# Only for an account owned by a parent (profiles.is_parent) or Family.
PARENT_JOBS = ("school_email",)

CONNECTED = "connected"
OFFLINE = "offline"
RECONNECT = "reconnect"
PERMISSION = "permission"
STATE_LABELS = {
    CONNECTED: "Connected",
    OFFLINE: "Offline",
    RECONNECT: "Reconnect needed",
    PERMISSION: "Needs a permission",
}

REMOVED_NOTICE_SETTING = "google_removed_notice"


def normalise_jobs(jobs: Iterable[str]) -> list[str]:
    """Known jobs only, in JOBS order. Writing events needs Calendars too:
    Huddle only writes to a calendar it shows."""
    chosen = set(jobs)
    if "write_events" in chosen:
        chosen.add("calendars")
    return [job for job in JOBS if job in chosen]


def scope_request(jobs: Iterable[str]) -> str:
    """The scopes to ask Google for: the jobs' own, plus openid email."""
    return " ".join([*(JOB_SCOPES[job] for job in normalise_jobs(jobs)), *IDENTITY_SCOPES])


def scopes_of(tokens: dict | None) -> frozenset[str]:
    """The scopes a stored grant covers: the token response's own `scope`
    (space-separated), else LEGACY_SCOPES. Empty with no token."""
    if not tokens:
        return frozenset()
    scope = tokens.get("scope")
    if isinstance(scope, str) and scope.strip():
        return frozenset(scope.split())
    return LEGACY_SCOPES


def jobs_from_scopes(scopes: Iterable[str]) -> list[str]:
    granted = set(scopes)
    return normalise_jobs(job for job, scope in JOB_SCOPES.items() if scope in granted)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _decrypt(row) -> dict | None:
    encrypted = row["encrypted_token_json"]
    return decrypt_token_json(encrypted) if encrypted else None


def _account(row) -> dict:
    """A row as Admin and the features see it: never the token itself."""
    tokens = _decrypt(row)
    scopes = scopes_of(tokens)
    jobs = normalise_jobs((row["jobs"] or "").split())
    missing = [job for job in jobs if JOB_SCOPES[job] not in scopes]
    if tokens is None:
        state = RECONNECT
    elif missing:
        state = PERMISSION
    elif row["check_state"] == OFFLINE:
        state = OFFLINE
    else:
        state = CONNECTED
    return {
        "id": row["id"],
        "sub": row["google_sub"],
        "email": row["email"],
        "owner_id": row["owner_profile_id"],
        "jobs": jobs,
        "scopes": scopes,
        "missing_jobs": missing,
        "connected": tokens is not None,
        "state": state,
        "state_label": STATE_LABELS[state],
        "offline_since": row["offline_since"],
        "created_at": row["created_at"],
    }


async def list_accounts(db) -> list[dict]:
    """Every account, oldest first (not removed ones: see soft_remove)."""
    rows = await (await db.execute("SELECT * FROM google_accounts WHERE removed_at IS NULL ORDER BY id")).fetchall()
    return [_account(row) for row in rows]


async def get(db, account_id: int) -> dict | None:
    row = await (
        await db.execute("SELECT * FROM google_accounts WHERE id = ? AND removed_at IS NULL", (account_id,))
    ).fetchone()
    return _account(row) if row else None


def same_account(account: dict, sub: str | None, email: str | None) -> bool:
    """Whether a Google identity is this account. By OpenID `sub` once the
    row knows it. Only a row migrated from the single connection lacks one,
    until its first reconnect or refresh: it matches by the address it was
    migrated with. A row with neither never matches, so it can't adopt an
    arbitrary account."""
    if account["sub"]:
        return account["sub"] == sub
    return bool(email and account["email"] and account["email"].lower() == email.lower())


async def by_identity(db, sub: str | None, email: str | None, removed: bool = False) -> dict | None:
    """The account this identity is; with `removed`, also a removed one."""
    rows = await (await db.execute("SELECT * FROM google_accounts ORDER BY removed_at IS NOT NULL, id")).fetchall()
    for row in rows:
        if (removed or row["removed_at"] is None) and same_account(_account(row), sub, email):
            return {**_account(row), "removed": row["removed_at"] is not None}
    return None


async def job_account(db, job: str) -> dict | None:
    """The account with `job` ticked (the oldest, if more than one), or None.
    A feature runs only when its job is ticked AND its scope granted
    (job_ready): with include_granted_scopes Google returns old grants too."""
    return next((a for a in await list_accounts(db) if job in a["jobs"]), None)


def job_ready(account: dict | None, job: str) -> bool:
    """`job` is ticked on the account and Google granted its scope."""
    return bool(account and job in account["jobs"] and JOB_SCOPES[job] in account["scopes"])


async def any_connected(db) -> bool:
    return any(a["connected"] for a in await list_accounts(db))


async def load_tokens(db, account_id: int) -> dict | None:
    row = await (
        await db.execute("SELECT encrypted_token_json FROM google_accounts WHERE id = ?", (account_id,))
    ).fetchone()
    return _decrypt(row) if row else None


async def save_tokens(db, account_id: int, tokens: dict) -> None:
    await db.execute(
        "UPDATE google_accounts SET encrypted_token_json = ? WHERE id = ?",
        (encrypt_token_json(tokens), account_id),
    )
    await db.commit()


async def drop_token(db, account_id: int) -> None:
    """The sign-in was revoked or expired (invalid_grant): only the token
    goes. The row, its jobs and everything linked to it stay, so a
    Reconnect restores it all (spec 12.6)."""
    await db.execute(
        "UPDATE google_accounts SET encrypted_token_json = NULL, check_state = NULL, offline_since = NULL WHERE id = ?",
        (account_id,),
    )
    await db.commit()


async def note_check(db, account_id: int, ok: bool) -> None:
    """Records whether Google answered this account just now ("Offline"
    otherwise). Written only on a change: it's called on every render."""
    row = await (await db.execute("SELECT check_state FROM google_accounts WHERE id = ?", (account_id,))).fetchone()
    if row is None:
        return
    state = "ok" if ok else OFFLINE
    if row["check_state"] == state:
        return
    await db.execute(
        "UPDATE google_accounts SET check_state = ?, offline_since = ? WHERE id = ?",
        (state, None if ok else _now(), account_id),
    )
    await db.commit()


async def owner_is_parent_or_family(db, owner_id: int | None) -> bool:
    if owner_id is None:
        return True
    row = await (await db.execute("SELECT is_parent FROM profiles WHERE id = ?", (owner_id,))).fetchone()
    return bool(row and row["is_parent"])


async def job_holders(db, exclude_id: int | None = None) -> dict[str, dict]:
    """{exclusive job: the other account already doing it}."""
    holders: dict[str, dict] = {}
    for account in await list_accounts(db):
        if account["id"] == exclude_id:
            continue
        for job in account["jobs"]:
            if job in EXCLUSIVE_JOBS:
                holders.setdefault(job, account)
    return holders


async def allowed_jobs(
    db, jobs: Iterable[str], owner_id: int | None, account_id: int | None = None
) -> tuple[list[str], dict[str, str]]:
    """(the jobs this account may have, {refused job: why}). Refused:
    "taken", in stage 1 an exclusive job another account already does, and
    "parent", School email for a child's account."""
    wanted = normalise_jobs(jobs)
    holders = await job_holders(db, exclude_id=account_id)
    parent_ok = await owner_is_parent_or_family(db, owner_id)
    refused: dict[str, str] = {}
    for job in wanted:
        if job in holders:
            refused[job] = "taken"
        elif job in PARENT_JOBS and not parent_ok:
            refused[job] = "parent"
    return [job for job in wanted if job not in refused], refused


async def add_or_merge(
    db, sub: str | None, email: str, tokens: dict, owner_id: int | None, jobs: Iterable[str]
) -> tuple[int, bool]:
    """Stores a fresh grant. An account already connected (same `sub`) isn't
    added twice: its jobs are merged in and its token replaced (spec 12.2),
    and its owner stays as it is. A removed account added again gets its
    row back, with every list link and Google id it had (soft_remove), and
    this owner and these jobs. Returns (account id, whether it was already
    connected). Exclusive jobs held by another account are dropped, never
    doubled up."""
    existing = await by_identity(db, sub, email, removed=True)
    if existing is not None and existing["removed"]:
        kept, _ = await allowed_jobs(db, jobs, owner_id, existing["id"])
        await db.execute(
            "UPDATE google_accounts SET removed_at = NULL, owner_profile_id = ?, jobs = ?, encrypted_token_json = ?, "
            "check_state = 'ok', offline_since = NULL WHERE id = ?",
            (owner_id, " ".join(kept), encrypt_token_json(tokens), existing["id"]),
        )
        await db.commit()
        await set_identity(db, existing["id"], sub, email)
        logger.info("Google account %d added back", existing["id"])
        return existing["id"], False
    if existing is not None:
        merged, _ = await allowed_jobs(db, [*existing["jobs"], *jobs], existing["owner_id"], existing["id"])
        await db.execute(
            "UPDATE google_accounts SET jobs = ?, encrypted_token_json = ?, check_state = 'ok', "
            "offline_since = NULL WHERE id = ?",
            (" ".join(merged), encrypt_token_json(tokens), existing["id"]),
        )
        await db.commit()
        await set_identity(db, existing["id"], sub, email)
        return existing["id"], True
    kept, _ = await allowed_jobs(db, jobs, owner_id)
    cursor = await db.execute(
        "INSERT INTO google_accounts (google_sub, email, owner_profile_id, jobs, encrypted_token_json, check_state) "
        "VALUES (?, ?, ?, ?, ?, 'ok')",
        (sub, email, owner_id, " ".join(kept), encrypt_token_json(tokens)),
    )
    await db.commit()
    logger.info("Google account %d added", cursor.lastrowid)
    return int(cursor.lastrowid or 0), False


async def set_identity(db, account_id: int, sub: str | None, email: str | None) -> None:
    """Learns the `sub` of a row migrated without one (unless another row
    already is that account), and keeps the displayed address current (it
    can be changed in Google)."""
    taken = (
        sub
        and await (
            await db.execute("SELECT 1 FROM google_accounts WHERE google_sub = ? AND id != ?", (sub, account_id))
        ).fetchone()
    )
    await db.execute(
        "UPDATE google_accounts SET google_sub = COALESCE(google_sub, ?), email = COALESCE(?, email) WHERE id = ?",
        (None if taken else sub, email, account_id),
    )
    await db.commit()


async def update(db, account_id: int, owner_id: int | None, jobs: Iterable[str]) -> None:
    """Saves owner and jobs as given (the caller has checked allowed_jobs)."""
    await db.execute(
        "UPDATE google_accounts SET owner_profile_id = ?, jobs = ? WHERE id = ?",
        (owner_id, " ".join(normalise_jobs(jobs)), account_id),
    )
    await db.commit()


async def soft_remove(db, account_id: int) -> None:
    """Remove: the row stays, dormant (no token, no jobs), so everything
    still pointing at it (a person's task list, the shopping list, their
    items' Google ids) is left alone. Sync skips lists in an account that
    isn't doing Tasks & shopping, and the same account added again
    (add_or_merge, by `sub`) picks them all up with nothing pushed or pulled
    twice. Admin and every feature no longer see it. The caller commits."""
    await db.execute(
        "UPDATE google_accounts SET removed_at = ?, encrypted_token_json = NULL, jobs = '', check_state = NULL, "
        "offline_since = NULL WHERE id = ?",
        (_now(), account_id),
    )


# --- What a removal left to be re-picked (spec 12.6) --------------------------------------


async def removed_notice(db) -> dict:
    """{"family": summary, "school_events": summary, "calendars": [ids]}:
    what the last removals cleared, shown until it's picked again."""
    try:
        value = json.loads(await get_setting(db, REMOVED_NOTICE_SETTING) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def add_removed_notice(db, **cleared) -> None:
    """The caller commits."""
    notice = await removed_notice(db)
    for key, value in cleared.items():
        if key == "calendars":
            notice["calendars"] = sorted({*notice.get("calendars", []), *value})
        elif value:
            notice[key] = value
    await set_setting(db, REMOVED_NOTICE_SETTING, json.dumps(notice))


async def clear_removed_notice(db, key: str) -> None:
    notice = await removed_notice(db)
    if key in notice:
        del notice[key]
        await set_setting(db, REMOVED_NOTICE_SETTING, json.dumps(notice))
        await db.commit()


def worst_state(accounts: list[dict]) -> str | None:
    """The most serious account state, None with no account."""
    order = (RECONNECT, PERMISSION, OFFLINE, CONNECTED)
    states = {a["state"] for a in accounts}
    return next((s for s in order if s in states), None)
