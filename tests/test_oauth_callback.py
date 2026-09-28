from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import google_oauth, security
from app.routers import admin, calendar
from app.security import create_session_token

TOKEN_URL = google_oauth.TOKEN_ENDPOINT
USERINFO_URL = google_oauth.USERINFO_ENDPOINT
CALLBACK = "/admin/google/callback"
REDIRECT_URI = "http://testserver/admin/google/callback"


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


async def _raw_token_row(db):
    return await (await db.execute(
        "SELECT account_email, encrypted_token_json FROM auth_tokens WHERE service_name = 'google'"
    )).fetchone()


def _assert_bounced_to_admin(resp):
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin?tab=google#google"
    # The one-shot state cookie is always cleared, success or not.
    assert f"{calendar.STATE_COOKIE}=" in resp.headers["set-cookie"]
    assert "max-age=0" in resp.headers["set-cookie"].lower()


async def test_connect_sets_a_state_cookie_matching_the_auth_url(admin_client):
    resp = await admin_client.get("/admin/google/connect")

    assert resp.status_code == 302
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["state"] == [resp.cookies[calendar.STATE_COOKIE]]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["access_type"] == ["offline"]


async def test_success_exchanges_code_and_stores_encrypted_tokens(admin_client, db, google):
    token_route = google.post(TOKEN_URL).respond(200, json={
        "access_token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 3599,
    })
    google.get(USERINFO_URL).respond(200, json={"email": "family@example.com"})
    admin_client.cookies.set(calendar.STATE_COOKIE, "the-state")

    resp = await admin_client.get(CALLBACK, params={"code": "auth-code", "state": "the-state"})

    _assert_bounced_to_admin(resp)
    sent = parse_qs(token_route.calls.last.request.content.decode())
    assert sent["code"] == ["auth-code"]
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["redirect_uri"] == [REDIRECT_URI]

    row = await _raw_token_row(db)
    assert row["account_email"] == "family@example.com"
    assert "fresh-access" not in row["encrypted_token_json"]
    assert "fresh-refresh" not in row["encrypted_token_json"]
    stored = security.decrypt_token_json(row["encrypted_token_json"])
    assert stored["access_token"] == "fresh-access"
    assert stored["refresh_token"] == "fresh-refresh"
    assert stored["expires_at"] > 0
    assert await google_oauth.get_valid_access_token(db) == "fresh-access"


@pytest.mark.parametrize("params, state_cookie", [
    ({"code": "auth-code", "state": "attacker-state"}, "the-state"),  # mismatched
    ({"code": "auth-code"}, "the-state"),                             # missing state param
    ({"code": "auth-code", "state": "the-state"}, None),              # no state cookie
    ({"state": "the-state"}, "the-state"),                            # missing code
    ({"error": "access_denied", "state": "the-state"}, "the-state"),  # consent denied
])
async def test_bad_callbacks_are_refused_without_calling_google(admin_client, db, google, params, state_cookie):
    token_route = google.post(TOKEN_URL).respond(200, json={"access_token": "x"})
    if state_cookie:
        admin_client.cookies.set(calendar.STATE_COOKIE, state_cookie)

    resp = await admin_client.get(CALLBACK, params=params)

    _assert_bounced_to_admin(resp)
    assert not token_route.called
    assert await _raw_token_row(db) is None


@pytest.mark.parametrize("token_response", [
    httpx.Response(400, json={"error": "invalid_grant"}),
    httpx.Response(500),
    httpx.ConnectError("offline"),
])
async def test_token_endpoint_failure_does_not_500(admin_client, db, google, token_response):
    route = google.post(TOKEN_URL)
    if isinstance(token_response, Exception):
        route.mock(side_effect=token_response)
    else:
        route.mock(return_value=token_response)
    admin_client.cookies.set(calendar.STATE_COOKIE, "the-state")

    resp = await admin_client.get(CALLBACK, params={"code": "auth-code", "state": "the-state"})

    _assert_bounced_to_admin(resp)
    assert await _raw_token_row(db) is None


async def test_userinfo_failure_does_not_500_or_store_tokens(admin_client, db, google):
    google.post(TOKEN_URL).respond(200, json={"access_token": "a", "refresh_token": "r"})
    google.get(USERINFO_URL).respond(503)
    admin_client.cookies.set(calendar.STATE_COOKIE, "the-state")

    resp = await admin_client.get(CALLBACK, params={"code": "auth-code", "state": "the-state"})

    _assert_bounced_to_admin(resp)
    assert await _raw_token_row(db) is None


async def test_callback_requires_admin(client, db, google):
    token_route = google.post(TOKEN_URL).respond(200, json={"access_token": "x"})
    client.cookies.set(calendar.STATE_COOKIE, "the-state")

    resp = await client.get(CALLBACK, params={"code": "auth-code", "state": "the-state"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert not token_route.called
    assert await _raw_token_row(db) is None
