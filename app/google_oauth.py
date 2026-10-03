"""
Google OAuth (spec 9.5, 12): one OAuth client for any number of Google
accounts (app/google_accounts.py), the standard in-browser consent flow,
each account's tokens stored locally and encrypted, gated behind the admin
PIN. Also the generic list-endpoint pager and the Admin calendar picker's
settings.

Talks to Google's REST endpoints directly via httpx rather than pulling in
google-api-python-client — the APIs are plain JSON over HTTPS and this
matches the project's minimal-dependency approach. The Calendar reads
(app/google_calendar.py) and Tasks client (app/google_tasks.py) share this
module's OAuth plumbing.

Each account asks only for the scopes of its ticked jobs (plus openid
email, for its address); include_granted_scopes keeps what it granted
before, so ticking a job later only asks for the new one. Which scopes an
account actually granted is kept with its tokens (the token response's
`scope` field), and each feature checks the scope of the account doing its
job (has_scope). A refresh never asks for new scopes.
"""

import json
import logging
import os
import time

import httpx

from app import calendar_cache, google_accounts, http_client
from app.database import get_setting, set_setting
from app.google_accounts import (  # noqa: F401 - re-exported: features and tests name them from here
    CALENDAR_EVENTS_SCOPE,
    CALENDAR_READ_SCOPE,
    GMAIL_READ_SCOPE,
    LEGACY_SCOPES,
    TASKS_SCOPE,
    scopes_of,
)

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
CALENDAR_LIST_ENDPOINT = "https://www.googleapis.com/calendar/v3/users/me/calendarList"

# Ochre fallback for a calendar with no colour of its own. Not a CSS token:
# it's stored per-calendar in the selection setting and inlined on each bar.
DEFAULT_EVENT_COLOR = "#D6A02C"

# [{"account": account id, "id", "summary", "color", "primary"}]: a calendar
# is its account and its id together ("primary" is a different calendar in
# each account). Unset = the oldest calendar account's primary calendar.
SELECTED_CALENDARS_SETTING = "google_selected_calendars"
DEFAULT_CALENDAR = {"id": "primary", "summary": "Calendar", "color": DEFAULT_EVENT_COLOR}

# Every scope Huddle can ask for (an account with all four jobs). Each
# account is asked only for its own jobs' (build_auth_url).
SCOPES = google_accounts.scope_request(google_accounts.JOBS)
TOKEN_REFRESH_BUFFER_SECONDS = 60
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600  # when a token response omits expires_in


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def build_auth_url(state: str, redirect_uri: str, jobs, login_hint: str | None = None) -> str:
    """Google's consent screen for `jobs`' scopes only. login_hint (an
    account's address) is for Reconnect."""
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": google_accounts.scope_request(jobs),
        "access_type": "offline",
        "prompt": "consent",  # always return a refresh_token, even on re-auth
        "include_granted_scopes": "true",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_ENDPOINT}?{httpx.QueryParams(params)}"


def expires_at(token_response: dict) -> float:
    """Absolute expiry (epoch seconds) for a fresh token response."""
    return time.time() + token_response.get("expires_in", DEFAULT_TOKEN_LIFETIME_SECONDS)


async def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    async with http_client.client() as client:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    async with http_client.client() as client:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "refresh_token": refresh_token,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_userinfo(access_token: str) -> dict:
    async with http_client.client() as client:
        resp = await client.get(USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        return resp.json()


async def get_all_pages(
    url: str,
    access_token: str,
    params: dict | None = None,
    items_key: str = "items",
    limit: int | None = None,
    meta: dict | None = None,
) -> list[dict]:
    """GET every page of a Google list endpoint (items + nextPageToken).
    Stopping at the first page silently truncates results — and the Tasks
    reconcile treats "missing from Google" as "deleted on Google". Gmail
    names its list "messages" (items_key). `limit` stops once that many
    items are in (for callers that only take so many anyway). `meta`, if
    given, gets the first page's other top-level fields (e.g. Calendar's
    defaultReminders)."""
    params = dict(params or {})
    items: list[dict] = []
    async with http_client.client() as client:
        while True:
            resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"}, params=params)
            resp.raise_for_status()
            body = resp.json()
            if meta is not None and "pageToken" not in params:
                meta.update({k: v for k, v in body.items() if k not in (items_key, "nextPageToken")})
            items.extend(body.get(items_key) or [])
            token = body.get("nextPageToken")
            if not token or (limit is not None and len(items) >= limit):
                return items if limit is None else items[:limit]
            params["pageToken"] = token


async def fetch_calendar_list(access_token: str) -> list[dict]:
    """Every calendar one account can see, for the Admin picker."""
    items = await get_all_pages(CALENDAR_LIST_ENDPOINT, access_token)
    return [
        {
            "id": item["id"],
            "summary": item.get("summaryOverride") or item.get("summary", item["id"]),
            "color": item.get("backgroundColor", DEFAULT_EVENT_COLOR),
            "primary": bool(item.get("primary")),
            # School events can only be added to a calendar the account can edit.
            "writable": item.get("accessRole") in ("owner", "writer"),
        }
        for item in items
    ]


# --- The calendar selection (spec 12.3) ---------------------------------------------------


def calendar_key(account_id: int, calendar_id: str) -> str:
    """One calendar across accounts: "<account id>:<calendar id>"."""
    return f"{account_id}:{calendar_id}"


def split_calendar_key(key: str) -> tuple[int, str] | None:
    """(account id, calendar id), or None for anything else (e.g. a link
    saved before accounts existed). Calendar ids may contain colons."""
    account, sep, calendar_id = key.partition(":")
    if not sep or not account.isdigit() or not calendar_id:
        return None
    return int(account), calendar_id


async def get_saved_calendars(db) -> list[dict] | None:
    """The saved selection as stored (entries with an account), or None if
    nothing was ever saved."""
    raw = await get_setting(db, SELECTED_CALENDARS_SETTING)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError, TypeError:
        return None
    if not isinstance(parsed, list):
        return None
    valid = [
        cal
        for cal in parsed
        if isinstance(cal, dict)
        and isinstance(cal.get("account"), int)
        and isinstance(cal.get("id"), str)
        and cal["id"]
    ]
    return valid or None


async def get_selected_calendars(db) -> list[dict]:
    """The calendars shown on the wall: the saved ones whose account still
    does Calendars, each with its "key" (calendar_key). Nothing saved: the
    oldest calendar account's primary calendar. A calendar shared into two
    accounts is shown once, through the first."""
    calendar_accounts = [a["id"] for a in await google_accounts.list_accounts(db) if "calendars" in a["jobs"]]
    saved = await get_saved_calendars(db)
    if saved is None:
        saved = [{**DEFAULT_CALENDAR, "account": calendar_accounts[0]}] if calendar_accounts else []
    shown: list[dict] = []
    seen: set[str] = set()
    for cal in saved:
        if cal["account"] not in calendar_accounts:
            continue
        if cal["id"] != "primary":
            if cal["id"] in seen:
                continue
            seen.add(cal["id"])
        shown.append({**cal, "key": calendar_key(cal["account"], cal["id"])})
    return shown


async def set_selected_calendars(db, calendars: list[dict]):
    stored = [{k: v for k, v in cal.items() if k not in ("key", "writable")} for cal in calendars]
    await set_setting(db, SELECTED_CALENDARS_SETTING, json.dumps(stored))
    # Cached events carry the old selection's calendars and colours.
    await calendar_cache.clear(db)
    await db.commit()


# --- Tokens, per account ------------------------------------------------------------------


def _is_revoked_grant(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code != 400:
        return False
    try:
        return exc.response.json().get("error") == "invalid_grant"
    except ValueError:
        return False


async def get_valid_access_token(db, account_id: int) -> str | None:
    """A usable access token for one account, refreshing if needed. None
    means that account isn't connected (no token: Reconnect needed).

    Raises httpx.HTTPError when Google is temporarily unreachable or
    erroring: that's "offline", not "disconnected", so callers must not
    treat it as a reason to throw away the stored tokens."""
    tokens = await google_accounts.load_tokens(db, account_id)
    if tokens is None:
        return None

    if time.time() < tokens.get("expires_at", 0) - TOKEN_REFRESH_BUFFER_SECONDS:
        return tokens["access_token"]

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return None

    try:
        refreshed = await refresh_access_token(refresh_token)
    except httpx.HTTPStatusError as exc:
        if not _is_revoked_grant(exc):
            raise
        # Genuinely revoked/expired (e.g. the 7-day "Testing" consent-screen
        # expiry). Only this account's token goes: its row, links and cached
        # events stay, and Admin shows Reconnect needed (spec 12.6).
        logger.warning("Google account %d: sign-in revoked or expired (invalid_grant); reconnect needed", account_id)
        await google_accounts.drop_token(db, account_id)
        return None

    tokens["access_token"] = refreshed["access_token"]
    tokens["expires_at"] = expires_at(refreshed)
    if refreshed.get("scope"):
        # Tokens stored before the granted scopes were kept learn them here.
        tokens["scope"] = refreshed["scope"]
    # Google doesn't re-send refresh_token on a refresh call — keep the one we have.
    await google_accounts.save_tokens(db, account_id, tokens)
    return tokens["access_token"]


async def granted_scopes(db, account_id: int) -> frozenset[str]:
    return scopes_of(await google_accounts.load_tokens(db, account_id))


async def has_scope(db, scope: str) -> bool:
    """Whether the account doing `scope`'s job (Writing events for
    calendar.events, School email for gmail.readonly, ...) granted it.
    False with no such account."""
    account = await google_accounts.job_account(db, google_accounts.SCOPE_JOBS[scope])
    return account is not None and scope in account["scopes"]


async def connect(db, account_id: int) -> tuple[str | None, bool]:
    """(access_token, offline) for one account. (None, False) = not
    connected (no token); (None, True) = connected but Google is unreachable
    right now. Records the account's Offline state either way."""
    if await google_accounts.load_tokens(db, account_id) is None:
        return None, False
    key = f"Google token refresh (account {account_id})"
    try:
        token = await get_valid_access_token(db, account_id)
    except httpx.HTTPError as exc:
        http_client.report_failure(
            logger,
            key,
            "Google account %d: token refresh failed; showing offline: %s",
            account_id,
            http_client.describe(exc),
        )
        await google_accounts.note_check(db, account_id, ok=False)
        return None, True
    http_client.report_success(logger, key)
    if token:
        await google_accounts.note_check(db, account_id, ok=True)
    return token, False


async def connect_job(db, job: str) -> tuple[str | None, bool]:
    """connect() for the account doing `job`; (None, False) if none does."""
    account = await google_accounts.job_account(db, job)
    if account is None:
        return None, False
    return await connect(db, account["id"])


async def revoke(tokens: dict | None) -> None:
    """Withdraws a grant with Google. Best-effort: a failure is logged and
    the caller deletes its copy either way."""
    token = (tokens or {}).get("refresh_token") or (tokens or {}).get("access_token")
    if not token:
        return
    try:
        async with http_client.client() as client:
            await client.post(REVOKE_ENDPOINT, params={"token": token})
    except httpx.HTTPError as exc:
        logger.warning("Couldn't revoke the Google token: %s", http_client.describe(exc))


async def store_tokens(db, tokens: dict, account_email: str, owner_id: int | None = None) -> int:
    """Connects an account straight from a token response, with every job
    its granted scopes allow (exclusive ones only if no other account has
    them): tests and seed scripts. The OAuth callback goes through Add
    account / Reconnect (routers/calendar.py)."""
    jobs = google_accounts.jobs_from_scopes(scopes_of(tokens))
    return await google_accounts.add_or_merge(db, account_email, tokens, owner_id, jobs)
