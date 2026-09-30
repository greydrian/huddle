"""app/upload_guard.py: every Admin write has its session checked and its
body capped before the body is read (the upload routes' own tests are in
test_school_inbox.py and test_avatars.py)."""

import pytest

from app import database, upload_guard
from app.routers import admin
from app.security import create_session_token

MB = 1024 * 1024
FORM = {"content-type": "application/x-www-form-urlencoded"}
MULTIPART = {"content-type": "multipart/form-data; boundary=abc"}
HEAD = (
    b'--abc\r\nContent-Disposition: form-data; name="kind"\r\n\r\ninitial\r\n'
    b'--abc\r\nContent-Disposition: form-data; name="x"; filename="x.bin"\r\n\r\n'
)


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


def _streamed(chunks: int, sent: list, head: bytes = HEAD, chunk: bytes = b"\x00" * MB):
    async def body():
        sent.append(len(head))
        yield head
        for _ in range(chunks):
            sent.append(len(chunk))
            yield chunk

    return body()


@pytest.mark.parametrize(
    "path",
    [
        "/admin/profiles/1/avatar",  # Form() params: FastAPI would parse the body before require_admin
        "/admin/profiles/1/details",
        "/admin/weather-location",
        "/admin/change-pin",
        "/admin/new-pin",
        "/admin/profiles/1/avatar/photo",
        "/admin/no-such-route",
    ],
)
async def test_signed_out_writes_are_refused_before_the_body_is_read(db, client, path):
    sent = []
    r = await client.post(path, content=_streamed(30, sent), headers=MULTIPART)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert sum(sent) <= len(HEAD)
    profile = dict(await (await db.execute("SELECT avatar_kind FROM profiles WHERE id = 1")).fetchone())
    assert profile["avatar_kind"] == "initial"


async def test_default_pin_goes_to_the_new_pin_screen_unread(db, admin_client):
    await database.set_setting(db, "pin_is_default", "1")
    await db.commit()
    sent = []
    r = await admin_client.post("/admin/profiles/1/avatar", content=_streamed(30, sent), headers=MULTIPART)
    assert r.status_code == 303 and r.headers["location"] == "/admin/new-pin"
    assert sum(sent) <= len(HEAD)
    # The new-pin form itself still gets through (it's how the default is changed).
    r = await admin_client.post("/admin/new-pin", data={"new_pin": "2580", "confirm_pin": "2580"})
    assert r.status_code == 303 and r.headers["location"] != "/admin/new-pin"
    assert await database.get_setting(db, "pin_is_default") == "0"


async def test_a_normal_admin_form_still_works(db, admin_client):
    r = await admin_client.post("/admin/profiles/1/details", data={"school_year": "Year 4"})
    assert r.status_code == 303 and r.headers["location"] == "/admin?tab=family#family"
    row = await (await db.execute("SELECT school_year FROM profiles WHERE id = 1")).fetchone()
    assert row["school_year"] == "Year 4"


async def test_an_oversized_admin_form_gets_413(db, admin_client):
    # Declared too big: refused from the header.
    sent = []
    r = await admin_client.post(
        "/admin/profiles/1/details",
        content=_streamed(2, sent, b"school_year="),
        headers={**FORM, "content-length": str(2 * MB)},
    )
    assert r.status_code == 413 and sum(sent) <= len(b"school_year=")

    # Streamed with no length: cut off just past the cap, not read to the end.
    sent.clear()
    r = await admin_client.post(
        "/admin/profiles/1/details", headers=FORM, content=_streamed(100, sent, b"school_year=", b"a" * 16 * 1024)
    )
    assert r.status_code == 413
    assert sum(sent) <= upload_guard.MAX_FORM_BODY + 32 * 1024
    row = await (await db.execute("SELECT school_year FROM profiles WHERE id = 1")).fetchone()
    assert row["school_year"] is None


async def test_login_needs_no_session_and_still_works(db, client):
    r = await client.post("/admin/login", data={"pin": "1234"})
    assert r.status_code == 303 and admin.SESSION_COOKIE in r.cookies
    r = await client.get("/admin")
    assert r.status_code == 200 and 'id="family"' in r.text


async def test_login_body_is_capped_too(client):
    sent = []
    r = await client.post(
        "/admin/login", content=_streamed(2, sent, b"pin="), headers={**FORM, "content-length": str(2 * MB)}
    )
    assert r.status_code == 413 and sum(sent) <= len(b"pin=")


async def test_signed_out_logout_still_goes_to_the_pin_page(client):
    r = await client.post("/admin/logout")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_caps():
    assert upload_guard.body_cap("/admin/inbox/add") == upload_guard.MAX_UPLOAD_BODY
    assert upload_guard.body_cap("/admin/profiles/3/avatar/photo") == upload_guard.MAX_AVATAR_BODY
    assert upload_guard.body_cap("/admin/profiles/3/avatar") == upload_guard.MAX_FORM_BODY
    assert upload_guard.body_cap("/admin") == upload_guard.MAX_FORM_BODY
    assert upload_guard.body_cap("/api/tasks") is None
    assert upload_guard.body_cap("/administrator") is None
