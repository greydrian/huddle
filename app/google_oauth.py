"""
Google OAuth (Section 4.2, 9.5 of the spec): single account, standard
in-browser consent flow, tokens stored locally and encrypted, gated behind
the admin PIN. Also the generic list-endpoint pager and the Admin calendar
picker's settings.

Talks to Google's REST endpoints directly via httpx rather than pulling in
google-api-python-client — the APIs are plain JSON over HTTPS and this
matches the project's minimal-dependency approach. The Calendar reads
(app/google_calendar.py) and Tasks client (app/google_tasks.py) share this
module's OAuth plumbing — get_valid_access_token() is generic across
whatever's in SCOPES.

Scopes: calendar.readonly (the month grid and the calendar picker), the
full `tasks` scope (read/write — shopping list and per-person task list sync
push local changes), openid/email for the account label in Admin, and for
the school email import (app/school_email.py) gmail.readonly plus
calendar.events (approved school events are added to a calendar; it can't
read the calendar list, so calendar.readonly stays).

Which scopes the account actually granted is kept with the tokens (the
token response's `scope` field) and each feature checks its own scope
(has_scope): an account connected before a scope was added keeps working
for everything else and Admin asks for a reconnect. A refresh never asks
for new scopes, it returns the ones originally granted.
"""

import json
import logging
import os
import time

import httpx

from app import calendar_cache, http_client
from app.database import get_setting, set_setting
from app.security import decrypt_token_json, encrypt_token_json

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

SELECTED_CALENDARS_SETTING = "google_selected_calendars"
DEFAULT_SELECTED_CALENDARS = [{"id": "primary", "summary": "Calendar", "color": DEFAULT_EVENT_COLOR}]

CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
SCOPES = " ".join((CALENDAR_READ_SCOPE, TASKS_SCOPE, GMAIL_READ_SCOPE, CALENDAR_EVENTS_SCOPE, "openid", "email"))
# What an account connected before the `scope` field was kept is assumed to
# have: the scopes this app asked for back then.
LEGACY_SCOPES = frozenset((CALENDAR_READ_SCOPE, TASKS_SCOPE))
TOKEN_REFRESH_BUFFER_SECONDS = 60
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600  # when a token response omits expires_in


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
    """Every calendar the connected account can see, for the Admin picker."""
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


async def get_selected_calendars(db) -> list[dict]:
    raw = await get_setting(db, SELECTED_CALENDARS_SETTING)
    if not raw:
        return DEFAULT_SELECTED_CALENDARS
    try:
        parsed = json.loads(raw)
        return parsed or DEFAULT_SELECTED_CALENDARS
    except ValueError, TypeError:
        return DEFAULT_SELECTED_CALENDARS


async def set_selected_calendars(db, calendars: list[dict]):
    await set_setting(db, SELECTED_CALENDARS_SETTING, json.dumps(calendars))
    # Cached events carry the old selection's calendars and colours.
    await calendar_cache.clear(db)
    await db.commit()


async def _load_stored_tokens(db) -> dict | None:
    cursor = await db.execute("SELECT encrypted_token_json FROM auth_tokens WHERE service_name = 'google'")
    row = await cursor.fetchone()
    if row is None or not row["encrypted_token_json"]:
        return None
    return decrypt_token_json(row["encrypted_token_json"])


async def store_tokens(db, tokens: dict, account_email: str | None = None):
    encrypted = encrypt_token_json(tokens)
    if account_email is not None:
        # A (re)connect — maybe a different account: drop the old one's
        # cached events. Token refreshes pass no email and keep the cache.
        await calendar_cache.clear(db)
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
    cursor = await db.execute("SELECT account_email FROM auth_tokens WHERE service_name = 'google'")
    row = await cursor.fetchone()
    return row["account_email"] if row else None


def _is_revoked_grant(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code != 400:
        return False
    try:
        return exc.response.json().get("error") == "invalid_grant"
    except ValueError:
        return False


async def get_valid_access_token(db) -> str | None:
    """A usable access token, refreshing if needed. None means 'not
    connected' — callers should render the disconnected/stub state.

    Raises httpx.HTTPError when Google is temporarily unreachable or
    erroring: that's "offline", not "disconnected", so callers must not
    treat it as a reason to throw away the stored tokens."""
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
    except httpx.HTTPStatusError as exc:
        if not _is_revoked_grant(exc):
            raise
        # Genuinely revoked/expired (e.g. the 7-day "Testing" consent-screen
        # expiry) — treat as disconnected; Admin shows "Connect" again.
        logger.warning("Google refresh token was revoked or expired (invalid_grant); disconnecting")
        await db.execute("DELETE FROM auth_tokens WHERE service_name = 'google'")
        await calendar_cache.clear(db)
        await db.commit()
        return None

    tokens["access_token"] = refreshed["access_token"]
    tokens["expires_at"] = expires_at(refreshed)
    if refreshed.get("scope"):
        # Tokens stored before the granted scopes were kept learn them here.
        tokens["scope"] = refreshed["scope"]
    # Google doesn't re-send refresh_token on a refresh call — keep the one we have.
    await store_tokens(db, tokens)
    return tokens["access_token"]


def scopes_of(tokens: dict | None) -> frozenset[str]:
    """The scopes a stored grant covers: the token response's own `scope`
    (space-separated), else LEGACY_SCOPES for a grant stored before it was
    kept. Empty when not connected."""
    if not tokens:
        return frozenset()
    scope = tokens.get("scope")
    if isinstance(scope, str) and scope.strip():
        return frozenset(scope.split())
    return LEGACY_SCOPES


async def granted_scopes(db) -> frozenset[str]:
    return scopes_of(await _load_stored_tokens(db))


async def has_scope(db, scope: str) -> bool:
    """Whether the connected account granted `scope` (False if not connected)."""
    return scope in await granted_scopes(db)


async def connect(db) -> tuple[str | None, bool]:
    """(access_token, offline) for widgets that render an offline state.
    (None, False) = never connected; (None, True) = connected but Google
    is unreachable right now."""
    if await _load_stored_tokens(db) is None:
        return None, False
    try:
        token = await get_valid_access_token(db)
    except httpx.HTTPError as exc:
        http_client.report_failure(
            logger,
            "Google token refresh",
            "Google token refresh failed; showing offline: %s",
            http_client.describe(exc),
        )
        return None, True
    http_client.report_success(logger, "Google token refresh")
    return token, False


async def revoke_and_clear(db):
    tokens = await _load_stored_tokens(db)
    if tokens and tokens.get("refresh_token"):
        try:
            async with http_client.client() as client:
                await client.post(REVOKE_ENDPOINT, params={"token": tokens["refresh_token"]})
        except httpx.HTTPError as exc:
            # Best-effort — still clear the local row either way.
            logger.warning("Couldn't revoke the Google token: %s", http_client.describe(exc))
    await db.execute("DELETE FROM auth_tokens WHERE service_name = 'google'")
    await calendar_cache.clear(db)
    await db.commit()
