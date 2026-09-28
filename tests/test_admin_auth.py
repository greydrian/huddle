import asyncio
import json
import re
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


def test_backoff_is_capped_at_five_minutes_then_goes_long():
    assert security.lockout_seconds_for(0) == 0
    assert security.lockout_seconds_for(1) == 5
    assert security.lockout_seconds_for(7) == 300  # 5 * 64 = 320, capped
    assert security.lockout_seconds_for(9) == 300
    # Sustained guessing: 15 minutes per attempt from the 10th failure on.
    assert security.lockout_seconds_for(10) == 15 * 60
    assert security.lockout_seconds_for(50) == 15 * 60


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
    resp = await client.post("/admin/change-pin", data={"new_pin": "5829"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert security.verify_pin(DEFAULT_PIN, await database.get_setting(db, "pin_hash"))


async def test_change_pin_swaps_old_for_new(client, db):
    # By design there's no "current PIN" field: holding an admin session is the proof.
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/change-pin", data={"new_pin": "5829"})
    assert resp.status_code == 303
    client.cookies.clear()

    assert (await _login(client, DEFAULT_PIN)).status_code == 401
    await _expire_lockout(db)
    assert (await _login(client, "5829")).status_code == 303


# The login form only takes up to 8 digits (inputmode=numeric, maxlength=8),
# so a PIN like these would lock the family out of Admin.
@pytest.mark.parametrize("bad_pin", [" ", "12ab", "123456789", "123", " 1234", "１２３４", "١٢٣٤"])
async def test_change_pin_rejects_a_pin_the_login_form_cannot_type(client, db, bad_pin):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/change-pin", data={"new_pin": bad_pin})

    assert (resp.status_code, resp.headers["location"]) == (303, "/admin?tab=system&error=pin-invalid#pin")
    assert security.verify_pin(DEFAULT_PIN, await database.get_setting(db, "pin_hash"))
    page = (await client.get(resp.headers["location"])).text
    assert "A PIN must be 4 to 8 digits" in page


@pytest.mark.parametrize("good_pin", ["2580", "73920615"])
async def test_change_pin_accepts_four_to_eight_digits(client, db, good_pin):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/change-pin", data={"new_pin": good_pin})

    assert resp.headers["location"] == "/admin?tab=system#pin"
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


# --- Forced change away from the default PIN ---

async def _pin_is_default(db) -> str | None:
    return await database.get_setting(db, "pin_is_default")


async def _set_default_flag(db, value: str):
    await database.set_setting(db, "pin_is_default", value)
    await db.commit()


async def test_fresh_install_flags_the_default_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    await database.init_db()
    async with database.get_db() as db:
        assert await _pin_is_default(db) == "1"


@pytest.mark.parametrize(("stored_pin", "expected"), [(DEFAULT_PIN, "1"), ("2580", "0")])
async def test_migration_flags_an_existing_db_by_its_stored_hash(db, stored_pin, expected):
    # A DB from before PIN hardening has a pin_hash but no flag.
    await database.set_setting(db, "pin_hash", security.hash_pin(stored_pin))
    await db.execute("DELETE FROM app_settings WHERE key = 'pin_is_default'")
    await db.commit()

    await database.init_db()
    assert await _pin_is_default(db) == expected

    # One-time: once set, a later boot leaves it alone.
    await _set_default_flag(db, "0" if expected == "1" else "1")
    await database.init_db()
    assert await _pin_is_default(db) != expected


@pytest.mark.parametrize("path", ["/admin", "/admin/google/connect"])
async def test_default_pin_login_is_sent_to_choose_a_new_pin(client, db, path):
    await _set_default_flag(db, "1")
    assert (await _login(client, DEFAULT_PIN)).status_code == 303

    resp = await client.get(path)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/new-pin"
    page = await client.get("/admin/new-pin")
    assert page.status_code == 200
    assert "Choose a new PIN" in page.text
    assert "Not a straight run, like 1234 or 9876" in page.text
    # Admin's own change-pin form is off limits until then, too.
    resp = await client.post("/admin/change-pin", data={"new_pin": "2580"})
    assert resp.headers["location"] == "/admin/new-pin"


async def test_new_pin_screen_needs_a_session(client, db):
    await _set_default_flag(db, "1")
    for resp in (await client.get("/admin/new-pin"),
                 await client.post("/admin/new-pin", data={"new_pin": "2580", "confirm_pin": "2580"})):
        assert resp.headers["location"] == "/admin/login"
    assert await _pin_is_default(db) == "1"


@pytest.mark.parametrize("weak", ["1234", "0000", "1111", "99999999", "9876", "012345", "3456"])
async def test_weak_new_pins_are_rejected(client, db, weak):
    await _set_default_flag(db, "1")
    await _login(client, DEFAULT_PIN)

    resp = await client.post("/admin/new-pin", data={"new_pin": weak, "confirm_pin": weak})

    assert resp.status_code == 400
    assert "too easy to guess" in resp.text
    assert await _pin_is_default(db) == "1"
    assert security.verify_pin(DEFAULT_PIN, await database.get_setting(db, "pin_hash"))


async def test_weak_pin_is_rejected_from_admin_settings_too(client, db):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/change-pin", data={"new_pin": "4444"})

    assert resp.headers["location"] == "/admin?tab=system&error=pin-weak#pin"
    assert "too easy to guess" in (await client.get(resp.headers["location"])).text


async def test_mismatched_confirmation_is_rejected(client, db):
    await _set_default_flag(db, "1")
    await _login(client, DEFAULT_PIN)

    resp = await client.post("/admin/new-pin", data={"new_pin": "2580", "confirm_pin": "2581"})

    assert resp.status_code == 400
    assert "didn&#39;t match" in resp.text or "didn't match" in resp.text
    assert await _pin_is_default(db) == "1"


async def test_choosing_a_new_pin_clears_the_flag_and_opens_admin(client, db):
    await _set_default_flag(db, "1")
    await _login(client, DEFAULT_PIN)

    resp = await client.post("/admin/new-pin", data={"new_pin": "2580", "confirm_pin": "2580"})

    assert (resp.status_code, resp.headers["location"]) == (303, "/admin")
    assert await _pin_is_default(db) == "0"
    assert security.verify_pin("2580", await database.get_setting(db, "pin_hash"))
    # This device stays signed in (a fresh cookie under the new generation).
    assert await _is_admin(client)
    assert (await client.get("/admin/new-pin")).headers["location"] == "/admin"


# --- Logout and PIN change end sessions ---

async def test_logout_kills_copies_of_the_cookie(client):
    await _login(client, DEFAULT_PIN)
    copied = client.cookies[admin.SESSION_COOKIE]

    await client.post("/admin/logout")
    client.cookies.set(admin.SESSION_COOKIE, copied)

    assert not await _is_admin(client)


async def test_logout_without_a_session_ends_nobody_elses(client):
    await _login(client, DEFAULT_PIN)
    cookie = client.cookies[admin.SESSION_COOKIE]
    client.cookies.clear()

    await client.post("/admin/logout")
    client.cookies.set(admin.SESSION_COOKIE, cookie)

    assert await _is_admin(client)


async def test_pin_change_ends_every_other_session(client):
    await _login(client, DEFAULT_PIN)
    other_device = client.cookies[admin.SESSION_COOKIE]

    resp = await client.post("/admin/change-pin", data={"new_pin": "2580", "confirm_pin": "2580"})
    assert resp.headers["location"] == "/admin?tab=system#pin"
    assert await _is_admin(client)  # the device that changed it

    client.cookies.set(admin.SESSION_COOKIE, other_device)
    assert not await _is_admin(client)


# --- Login race and long lockout ---

async def test_parallel_wrong_guesses_hit_the_lockout(client, db):
    resps = await asyncio.gather(*(_login(client, f"{n:04d}") for n in range(2000, 2040)))

    assert sorted(r.status_code for r in resps) == [401] + [429] * 39
    assert (await _lockout(db))["failed_attempts"] == 1


async def test_tenth_failure_brings_a_long_lockout(client, db):
    await database.set_setting(db, "pin_lockout", json.dumps({"failed_attempts": 9}))
    await db.commit()

    resp = await _login(client, "2222")

    assert resp.status_code == 401
    assert "Admin is locked after 10 wrong PINs in a row. Try again in 15 min." in resp.text
    state = await _lockout(db)
    locked_for = (datetime.fromisoformat(state["locked_until"]) - datetime.now(timezone.utc)).total_seconds()
    assert 15 * 60 - 5 < locked_for <= 15 * 60

    # Coming back to the login screen shows the time left, and even the
    # right PIN is refused until it's up.
    page = (await client.get("/admin/login")).text
    assert re.search(r"Admin is locked after 10 wrong PINs in a row\. Try again in 1[45] min( \d+s)?\.", page)
    assert (await _login(client, DEFAULT_PIN)).status_code == 429


async def test_login_page_is_quiet_without_a_lockout(client):
    page = (await client.get("/admin/login")).text
    assert 'class="pin-error"' not in page


# --- Cookie flags ---

async def test_session_cookie_flags_over_plain_http(client):
    set_cookie = (await _login(client, DEFAULT_PIN)).headers["set-cookie"].lower()

    assert "httponly" in set_cookie
    # Lax, not Strict: Google's OAuth return to /admin/google/callback is a
    # cross-site navigation that must carry the session.
    assert "samesite=lax" in set_cookie
    assert "secure" not in set_cookie


async def test_session_cookie_is_secure_behind_an_https_proxy(client):
    resp = await client.post("/admin/login", data={"pin": DEFAULT_PIN}, headers={"X-Forwarded-Proto": "https"})

    assert "; secure" in resp.headers["set-cookie"].lower()


# --- Failures decay after a quiet day ---

async def _set_failures(db, count: int, last_failed_ago: timedelta, key: str = "last_failed_at"):
    at = datetime.now(timezone.utc) - last_failed_ago
    await database.set_setting(db, "pin_lockout", json.dumps({
        "failed_attempts": count, "locked_until": at.isoformat(), key: at.isoformat(),
    }))
    await db.commit()


@pytest.mark.parametrize("key", ["last_failed_at", "locked_until"])  # locked_until: states saved before
async def test_old_failures_decay_so_a_stray_typo_gets_the_short_backoff(client, db, key):
    await _set_failures(db, 9, timedelta(hours=25), key)

    resp = await _login(client, "2222")

    assert "Incorrect PIN. Try again in 5s." in resp.text
    assert (await _lockout(db))["failed_attempts"] == 1


async def test_recent_failures_still_count(client, db):
    await _set_failures(db, 9, timedelta(hours=23))

    resp = await _login(client, "2222")

    assert "Admin is locked after 10 wrong PINs in a row" in resp.text
    state = await _lockout(db)
    assert state["failed_attempts"] == 10
    last = datetime.fromisoformat(state["last_failed_at"])
    assert (datetime.now(timezone.utc) - last).total_seconds() < 5


async def test_parallel_guesses_after_decay_are_still_serialised(client, db):
    await _set_failures(db, 9, timedelta(days=3))

    resps = await asyncio.gather(*(_login(client, f"{n:04d}") for n in range(3000, 3020)))

    assert sorted(r.status_code for r in resps) == [401] + [429] * 19
    assert (await _lockout(db))["failed_attempts"] == 1


# --- Corrupt hash and forgotten-PIN recovery ---

async def test_corrupt_pin_hash_does_not_crash_startup_or_login(client, db):
    assert not security.verify_pin(DEFAULT_PIN, "not-hex$abcd")
    await database.set_setting(db, "pin_hash", "zz$nothex")
    await db.execute("DELETE FROM app_settings WHERE key = 'pin_is_default'")
    await db.commit()

    await database.init_db()

    assert await _pin_is_default(db) == "0"
    assert (await _login(client, DEFAULT_PIN)).status_code == 401


async def test_deleting_the_pin_hash_reseeds_1234_with_the_forced_change(db):
    await _set_default_flag(db, "0")
    await db.execute("DELETE FROM app_settings WHERE key = 'pin_hash'")
    await db.commit()

    await database.init_db()

    assert security.verify_pin(DEFAULT_PIN, await database.get_setting(db, "pin_hash"))
    assert await _pin_is_default(db) == "1"


async def test_reset_pin_command(client, db, capsys):
    from app import reset_pin

    # A family that changed its PIN, forgot it, and locked itself out.
    await database.set_setting(db, "pin_hash", security.hash_pin("2580"))
    await db.commit()
    await _login(client, "2580")
    old_cookie = client.cookies[admin.SESSION_COOKIE]
    await _set_failures(db, 12, timedelta(minutes=1))
    await database.set_setting(db, "pin_lockout", json.dumps({
        "failed_attempts": 12,
        "locked_until": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }))
    await db.commit()

    # main() runs its own event loop, so call it from a worker thread.
    await asyncio.to_thread(reset_pin.main)

    out = capsys.readouterr().out
    assert "reset to 1234" in out
    assert "$" not in out  # never prints a hash
    stored = await database.get_setting(db, "pin_hash")
    assert stored.split("$")[1] not in out
    assert security.verify_pin(DEFAULT_PIN, stored)
    assert await _pin_is_default(db) == "1"
    assert await _lockout(db) == {}
    # Every old session is gone...
    assert not await _is_admin(client)
    # ...and 1234 now leads straight to the forced change.
    client.cookies.clear()
    assert (await _login(client, DEFAULT_PIN)).status_code == 303
    assert old_cookie != client.cookies[admin.SESSION_COOKIE]
    assert (await client.get("/admin")).headers["location"] == "/admin/new-pin"
