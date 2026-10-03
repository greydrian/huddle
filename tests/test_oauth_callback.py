"""The Google consent flow (spec 12.2): Add account and Reconnect send Google
only the ticked jobs' scopes; the callback stores the grant encrypted in
google_accounts, and anything off is refused without a 500."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import google_accounts, google_oauth, security
from app.routers import admin
from app.routers.admin import google as admin_google
from app.security import create_session_token

TOKEN_URL = google_oauth.TOKEN_ENDPOINT
USERINFO_URL = google_oauth.USERINFO_ENDPOINT
CALLBACK = "/admin/google/callback"
REDIRECT_URI = "http://testserver/admin/google/callback"


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


async def _rows(db):
    return [
        dict(r)
        for r in await (
            await db.execute("SELECT id, email, jobs, owner_profile_id, encrypted_token_json FROM google_accounts")
        ).fetchall()
    ]


async def _start_add(admin_client, jobs=("calendars", "tasks"), owner="family") -> str:
    """Add account up to Google's consent screen; returns its state."""
    resp = await admin_client.post("/admin/google/accounts", data={"owner": owner, "job": list(jobs)})
    assert resp.status_code == 303
    return resp.cookies[admin_google.STATE_COOKIE]


def _assert_bounced_to_admin(resp, error=None):
    assert resp.status_code == 303
    expected = f"/admin?tab=google&error={error}#google" if error else "/admin?tab=google#google"
    assert resp.headers["location"] == expected
    # The one-shot state cookie is always cleared, success or not.
    assert f"{admin_google.STATE_COOKIE}=" in resp.headers["set-cookie"]
    assert "max-age=0" in resp.headers["set-cookie"].lower()


async def test_add_sets_a_state_cookie_matching_the_auth_url(admin_client):
    resp = await admin_client.post("/admin/google/accounts", data={"owner": "family", "job": ["calendars"]})

    assert resp.status_code == 303
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["state"] == [resp.cookies[admin_google.STATE_COOKIE]]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["true"]


async def test_success_exchanges_code_and_stores_encrypted_tokens(admin_client, db, google):
    token_route = google.post(TOKEN_URL).respond(
        200,
        json={
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "expires_in": 3599,
        },
    )
    google.get(USERINFO_URL).respond(200, json={"email": "family@example.com", "sub": "sub-family"})
    state = await _start_add(admin_client)

    resp = await admin_client.get(CALLBACK, params={"code": "auth-code", "state": state})

    _assert_bounced_to_admin(resp)
    sent = parse_qs(token_route.calls.last.request.content.decode())
    assert sent["code"] == ["auth-code"]
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["redirect_uri"] == [REDIRECT_URI]

    [row] = await _rows(db)
    assert row["email"] == "family@example.com" and row["jobs"] == "calendars tasks"
    assert row["owner_profile_id"] is None  # Family
    assert "fresh-access" not in row["encrypted_token_json"]
    assert "fresh-refresh" not in row["encrypted_token_json"]
    stored = security.decrypt_token_json(row["encrypted_token_json"])
    assert stored["access_token"] == "fresh-access"
    assert stored["refresh_token"] == "fresh-refresh"
    assert stored["expires_at"] > 0
    assert await google_oauth.get_valid_access_token(db, row["id"]) == "fresh-access"


async def test_a_state_is_used_once(admin_client, db, google):
    google.post(TOKEN_URL).respond(200, json={"access_token": "a", "refresh_token": "r"})
    google.get(USERINFO_URL).respond(200, json={"email": "family@example.com", "sub": "sub-family"})
    state = await _start_add(admin_client)
    await admin_client.get(CALLBACK, params={"code": "auth-code", "state": state})
    admin_client.cookies.set(admin_google.STATE_COOKIE, state)

    again = await admin_client.get(CALLBACK, params={"code": "auth-code", "state": state})

    _assert_bounced_to_admin(again, "google-signin")
    assert len(await _rows(db)) == 1


@pytest.mark.parametrize(
    "params, state_cookie, error",
    [
        ({"code": "auth-code", "state": "attacker-state"}, "the-state", "google-signin"),  # mismatched
        ({"code": "auth-code"}, "the-state", "google-signin"),  # missing state param
        ({"code": "auth-code", "state": "the-state"}, None, "google-signin"),  # no state cookie
        ({"code": "auth-code", "state": "the-state"}, "the-state", "google-signin"),  # no Add/Reconnect started it
        ({"state": "the-state"}, "the-state", None),  # missing code
        ({"error": "access_denied", "state": "the-state"}, "the-state", "google-denied"),  # consent denied
    ],
)
async def test_bad_callbacks_are_refused_without_calling_google(admin_client, db, google, params, state_cookie, error):
    token_route = google.post(TOKEN_URL).respond(200, json={"access_token": "x"})
    if state_cookie:
        admin_client.cookies.set(admin_google.STATE_COOKIE, state_cookie)

    resp = await admin_client.get(CALLBACK, params=params)

    _assert_bounced_to_admin(resp, error)
    assert not token_route.called
    assert await _rows(db) == []


@pytest.mark.parametrize(
    "token_response",
    [
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(500),
        httpx.ConnectError("offline"),
    ],
)
async def test_token_endpoint_failure_does_not_500(admin_client, db, google, token_response):
    route = google.post(TOKEN_URL)
    if isinstance(token_response, Exception):
        route.mock(side_effect=token_response)
    else:
        route.mock(return_value=token_response)
    state = await _start_add(admin_client)

    resp = await admin_client.get(CALLBACK, params={"code": "auth-code", "state": state})

    _assert_bounced_to_admin(resp, "google-signin")
    assert await _rows(db) == []


async def test_userinfo_failure_does_not_500_or_store_tokens(admin_client, db, google):
    google.post(TOKEN_URL).respond(200, json={"access_token": "a", "refresh_token": "r"})
    google.get(USERINFO_URL).respond(503)
    state = await _start_add(admin_client)

    resp = await admin_client.get(CALLBACK, params={"code": "auth-code", "state": state})

    _assert_bounced_to_admin(resp, "google-signin")
    assert await _rows(db) == []


async def test_a_grant_without_an_address_is_withdrawn(admin_client, db, google):
    google.post(TOKEN_URL).respond(200, json={"access_token": "a", "refresh_token": "r"})
    google.get(USERINFO_URL).respond(200, json={"sub": "123"})
    revoke = google.post(google_oauth.REVOKE_ENDPOINT).respond(200)
    state = await _start_add(admin_client)

    resp = await admin_client.get(CALLBACK, params={"code": "auth-code", "state": state})

    _assert_bounced_to_admin(resp, "google-signin")
    assert revoke.called and await _rows(db) == []


async def test_callback_requires_admin(client, db, google):
    token_route = google.post(TOKEN_URL).respond(200, json={"access_token": "x"})
    client.cookies.set(admin_google.STATE_COOKIE, "the-state")

    resp = await client.get(CALLBACK, params={"code": "auth-code", "state": "the-state"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert not token_route.called
    assert await _rows(db) == []
    assert await google_accounts.list_accounts(db) == []
