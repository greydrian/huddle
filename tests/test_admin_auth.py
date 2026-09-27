import json
from datetime import datetime, timedelta, timezone

import pytest

from app import database, security
from app.routers import admin
from app.security import create_session_token

DEFAULT_PIN = "1234"  # seeded by init_db()


async def _lockout(db) -> dict:
    return json.loads(await database.get_setting(db, "pin_lockout", "{}"))


async def _expire_lockout(db):
    """Pretend the backoff window has passed, keeping the failure count."""
    state = await _lockout(db)
    state["locked_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    await database.set_setting(db, "pin_lockout", json.dumps(state))
    await db.commit()


async def _login(client, pin):
    return await client.post("/admin/login", data={"pin": pin})


async def _is_admin(client) -> bool:
    resp = await client.get("/admin")
    return resp.status_code == 200


async def test_correct_pin_logs_in(client):
    resp = await _login(client, DEFAULT_PIN)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    set_cookie = resp.headers["set-cookie"]
    assert set_cookie.startswith(f"{admin.SESSION_COOKIE}=")
    assert "httponly" in set_cookie.lower()
    assert security.verify_session_token(resp.cookies[admin.SESSION_COOKIE])
    assert await _is_admin(client)


async def test_wrong_pin_is_refused(client, db):
    resp = await _login(client, "9999")

    assert resp.status_code == 401
    assert "Incorrect PIN" in resp.text
    assert admin.SESSION_COOKIE not in resp.cookies
    assert not await _is_admin(client)
    assert (await _lockout(db))["failed_attempts"] == 1


async def test_backoff_doubles_per_consecutive_failure(client, db):
    for attempt in range(1, 5):
        resp = await _login(client, "0000")
        assert resp.status_code == 401
        wait = security.lockout_seconds_for(attempt)
        assert wait == 5 * 2 ** (attempt - 1)
        assert f"Try again in {wait}s" in resp.text

        state = await _lockout(db)
        assert state["failed_attempts"] == attempt
        locked_for = (datetime.fromisoformat(state["locked_until"]) - datetime.now(timezone.utc)).total_seconds()
        assert wait - 5 < locked_for <= wait
        await _expire_lockout(db)


def test_backoff_is_capped_at_five_minutes():
    assert security.lockout_seconds_for(0) == 0
    assert security.lockout_seconds_for(1) == 5
    assert security.lockout_seconds_for(7) == 300  # 5 * 64 = 320, capped
    assert security.lockout_seconds_for(50) == 300


async def test_locked_out_even_with_the_correct_pin(client, db):
    await _login(client, "0000")

    resp = await _login(client, DEFAULT_PIN)

    assert resp.status_code == 429
    assert "Too many attempts" in resp.text
    assert admin.SESSION_COOKIE not in resp.cookies
    # An attempt refused by the lockout doesn't itself count as a failure.
    assert (await _lockout(db))["failed_attempts"] == 1


async def test_legacy_naive_lockout_timestamp_is_still_honoured(client, db):
    # Lockouts stored before timestamps became tz-aware are naive UTC.
    until = (datetime.now(timezone.utc) + timedelta(seconds=60)).replace(tzinfo=None)
    await database.set_setting(
        db, "pin_lockout", json.dumps({"failed_attempts": 3, "locked_until": until.isoformat()})
    )
    await db.commit()

    assert (await _login(client, DEFAULT_PIN)).status_code == 429


async def test_successful_login_resets_the_failure_count(client, db):
    for _ in range(3):
        await _login(client, "0000")
        await _expire_lockout(db)

    assert (await _login(client, DEFAULT_PIN)).status_code == 303
    assert await _lockout(db) == {}

    # The next mistake starts the backoff from the bottom again.
    resp = await _login(client, "0000")
    assert "Try again in 5s" in resp.text


async def test_change_pin_requires_admin(client, db):
    resp = await client.post("/admin/change-pin", data={"new_pin": "5678"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert security.verify_pin(DEFAULT_PIN, await database.get_setting(db, "pin_hash"))


async def test_change_pin_swaps_old_for_new(client, db):
    # By design there's no "current PIN" field: holding an admin session is the proof.
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/change-pin", data={"new_pin": "5678"})
    assert resp.status_code == 303
    client.cookies.clear()

    assert (await _login(client, DEFAULT_PIN)).status_code == 401
    await _expire_lockout(db)
    assert (await _login(client, "5678")).status_code == 303


# The login form only takes up to 8 digits (inputmode=numeric, maxlength=8),
# so a PIN like these would lock the family out of Admin.
@pytest.mark.parametrize("bad_pin", [" ", "12ab", "123456789", "123", " 1234", "１２３４", "١٢٣٤"])
async def test_change_pin_rejects_a_pin_the_login_form_cannot_type(client, db, bad_pin):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/change-pin", data={"new_pin": bad_pin})

    assert (resp.status_code, resp.headers["location"]) == (303, "/admin?error=pin-invalid#pin")
    assert security.verify_pin(DEFAULT_PIN, await database.get_setting(db, "pin_hash"))
    page = (await client.get(resp.headers["location"])).text
    assert "A PIN must be 4 to 8 digits" in page


@pytest.mark.parametrize("good_pin", ["0000", "12345678"])
async def test_change_pin_accepts_four_to_eight_digits(client, db, good_pin):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/change-pin", data={"new_pin": good_pin})

    assert resp.headers["location"] == "/admin"
    assert security.verify_pin(good_pin, await database.get_setting(db, "pin_hash"))


async def test_unknown_error_code_shows_nothing(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    page = (await client.get("/admin?error=<b>nope</b>")).text
    assert "nope" not in page
    assert 'class="admin-error" role="alert"' not in page


async def test_logout_clears_the_session_cookie(client):
    await _login(client, DEFAULT_PIN)
    assert await _is_admin(client)

    resp = await client.post("/admin/logout")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    set_cookie = resp.headers["set-cookie"]
    assert set_cookie.startswith(f"{admin.SESSION_COOKIE}=")
    assert "max-age=0" in set_cookie.lower()
    assert admin.SESSION_COOKIE not in client.cookies
    assert not await _is_admin(client)


async def test_expired_session_token_is_rejected(client, monkeypatch):
    import itsdangerous.timed

    issued = itsdangerous.timed.time.time() - security.SESSION_MAX_AGE_SECONDS - 60
    with monkeypatch.context() as m:
        m.setattr(itsdangerous.timed.time, "time", lambda: issued)
        old_token = create_session_token()

    assert not security.verify_session_token(old_token)
    client.cookies.set(admin.SESSION_COOKIE, old_token)
    resp = await client.get("/admin")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


async def test_tampered_session_token_is_rejected(client):
    token = create_session_token()
    payload, rest = token.split(".", 1)
    # Flip one character of the signed payload.
    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + rest

    assert not security.verify_session_token(tampered)
    assert not security.verify_session_token("not-a-token")
    assert not security.verify_session_token("")
    client.cookies.set(admin.SESSION_COOKIE, tampered)
    resp = await client.get("/admin")
    assert resp.status_code == 303


async def test_token_signed_with_another_key_is_rejected(tmp_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(security, "SECRET_KEY_PATH", tmp_path / "other.key")
        foreign = create_session_token()

    assert not security.verify_session_token(foreign)
