"""Photos for the idle screen (spec 10.2, app/google_photos.py and
app/routers/photos.py): the separate Photos sign-in, the Photos Picker
session, downloading, replacing the set, the weekly shuffle, serving the
files and Admin. Every Google call is mocked with respx."""

import io
import json
import logging
import sqlite3
import time
from datetime import date
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from app import database, google_oauth, google_photos, migrations
from app.admin_tabs import admin_url
from app.routers import admin, photos
from app.security import create_session_token, decrypt_token_json

CLIENT_SECRET = "photos-client-secret-XYZ"
ACCESS = "photos-access-token-ABC"
REFRESH = "photos-refresh-token-DEF"
SESSION = "sess-123"
PICKER_URI = "https://photos.google.com/picker/abc123"
SESSION_URL = f"{google_photos.SESSIONS_ENDPOINT}/{SESSION}"
BASE = "https://lh3.googleusercontent.com/secret-base-url"


def jpeg(width=64, height=40, colour=(200, 80, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, "JPEG")
    return out.getvalue()


def item(n, kind="PHOTO", mime="image/jpeg"):
    return {"id": f"m{n}", "type": kind, "mediaFile": {"baseUrl": f"{BASE}/{n}", "mimeType": mime, "filename": f"{n}.jpg"}}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_PHOTOS_CLIENT_ID", "photos-client-id")
    monkeypatch.setenv("GOOGLE_PHOTOS_CLIENT_SECRET", CLIENT_SECRET)


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


@pytest.fixture
async def signed_in(db, configured):
    await google_photos.store_tokens(db, {"access_token": ACCESS, "refresh_token": REFRESH,
                                          "expires_at": time.time() + 3600})


def mock_session(google, media_items_set=False):
    google.post(google_photos.SESSIONS_ENDPOINT, name="create").respond(200, json={
        "id": SESSION, "pickerUri": PICKER_URI, "mediaItemsSet": False,
        "pollingConfig": {"pollInterval": "3s", "timeoutIn": "1800s"},
    })
    google.get(SESSION_URL).respond(200, json={"id": SESSION, "mediaItemsSet": media_items_set})
    return google.delete(SESSION_URL).respond(200, json={})


def mock_items(google, items, pages=1):
    """mediaItems.list over `pages` pages."""
    size = -(-len(items) // pages)
    chunks = [items[i:i + size] for i in range(0, len(items), size)] or [[]]
    if pages == 1:
        return google.get(google_photos.MEDIA_ITEMS_ENDPOINT).respond(200, json={"mediaItems": items})
    responses = []
    for n, chunk in enumerate(chunks):
        body = {"mediaItems": chunk}
        if n + 1 < len(chunks):
            body["nextPageToken"] = f"p{n + 1}"
        responses.append(httpx.Response(200, json=body))
    return google.get(google_photos.MEDIA_ITEMS_ENDPOINT).mock(side_effect=responses)


def mock_downloads(google, body=None):
    return google.get(url__startswith=BASE).mock(return_value=httpx.Response(200, content=body or jpeg()))


async def waiting_state(db):
    await google_photos._set_state(db, {"status": "waiting", "session_id": SESSION, "picker_uri": PICKER_URI,
                                        "poll_seconds": 3, "expires_at": time.time() + 1800})


async def rows(db):
    return await google_photos.list_photos(db)


async def seed_old_set(db, n=2):
    saved = [google_photos._save_image(jpeg(colour=(10, 10, 10 + i))) for i in range(n)]
    await google_photos.replace_set(db, saved)
    return saved


def files():
    return sorted(p.name for p in google_photos.PHOTOS_DIR.iterdir()) if google_photos.PHOTOS_DIR.is_dir() else []


# --- Sign-in ---

async def test_connect_asks_only_for_the_picker_scope(admin_client, configured):
    resp = await admin_client.get("/admin/photos/connect")
    assert resp.status_code == 302
    url = urlsplit(resp.headers["location"])
    query = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert query["scope"] == [google_photos.SCOPE]
    assert query["client_id"] == ["photos-client-id"]
    assert query["redirect_uri"] == ["http://testserver/admin/photos/callback"]
    assert query["state"][0] == resp.cookies[photos.STATE_COOKIE]


async def test_callback_stores_the_token_encrypted_and_apart_then_starts_picking(
    admin_client, db, configured, connected, google
):
    main_before = await (await db.execute("SELECT * FROM auth_tokens WHERE service_name = 'google'")).fetchone()
    google.post(google_photos.TOKEN_ENDPOINT).respond(200, json={
        "access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3599, "scope": google_photos.SCOPE,
    })
    mock_session(google)
    create = google.routes["create"]
    admin_client.cookies.set(photos.STATE_COOKIE, "st8")
    resp = await admin_client.get("/admin/photos/callback", params={"code": "the-code", "state": "st8"})
    assert (resp.status_code, resp.headers["location"]) == (303, admin_url("photos"))

    row = await (await db.execute("SELECT * FROM auth_tokens WHERE service_name = 'google_photos'")).fetchone()
    assert ACCESS not in row["encrypted_token_json"] and REFRESH not in row["encrypted_token_json"]
    stored = decrypt_token_json(row["encrypted_token_json"])
    assert (stored["access_token"], stored["refresh_token"]) == (ACCESS, REFRESH)
    assert row["account_email"] is None
    # The main Google connection is untouched.
    main_after = await (await db.execute("SELECT * FROM auth_tokens WHERE service_name = 'google'")).fetchone()
    assert dict(main_after) == dict(main_before)
    assert await google_oauth.get_connected_account(db) == "family@example.com"

    # ...and picking has started: a 30-photo session, shown as a QR code and a link.
    assert json.loads(create.calls.last.request.content) == {"pickingConfig": {"maxItemCount": "30"}}
    assert create.calls.last.request.headers["authorization"] == f"Bearer {ACCESS}"
    state = await google_photos.get_state(db)
    assert (state["status"], state["session_id"], state["poll_seconds"]) == ("waiting", SESSION, 3.0)
    google_photos.stop_poller()
    html = (await admin_client.get("/admin?tab=display")).text
    assert f'href="{PICKER_URI}"' in html and "<svg" in html
    assert 'hx-get="/admin/photos/status"' in html


@pytest.mark.parametrize("params", [{"code": "c", "state": "wrong"}, {"error": "access_denied", "state": "st8"},
                                    {"state": "st8"}])
async def test_a_bad_callback_stores_nothing(admin_client, db, configured, google, params):
    admin_client.cookies.set(photos.STATE_COOKIE, "st8")
    resp = await admin_client.get("/admin/photos/callback", params=params)
    assert resp.headers["location"] == admin_url("photos", error="photos-signin")
    assert not await google_photos.is_signed_in(db)
    assert not google.calls


async def test_choose_signs_in_first_when_there_is_no_token(admin_client, db, configured):
    resp = await admin_client.post("/admin/photos/choose")
    assert (resp.status_code, resp.headers["location"]) == (303, "/admin/photos/connect")


async def test_an_expired_refresh_token_means_sign_in_again(admin_client, db, configured, google):
    await google_photos.store_tokens(db, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": 0})
    google.post(google_photos.TOKEN_ENDPOINT).respond(400, json={"error": "invalid_grant"})
    resp = await admin_client.post("/admin/photos/choose")
    assert resp.headers["location"] == "/admin/photos/connect"
    assert not await google_photos.is_signed_in(db)


async def test_a_refresh_keeps_the_refresh_token(db, configured, google):
    await google_photos.store_tokens(db, {"access_token": "old", "refresh_token": REFRESH, "expires_at": 0})
    google.post(google_photos.TOKEN_ENDPOINT).respond(200, json={"access_token": ACCESS, "expires_in": 3600})
    assert await google_photos.access_token(db) == ACCESS
    stored = await google_photos._load_tokens(db)
    assert (stored["access_token"], stored["refresh_token"]) == (ACCESS, REFRESH)


async def test_choose_while_google_is_down_says_so(admin_client, db, signed_in, google):
    google.post(google_photos.SESSIONS_ENDPOINT).mock(side_effect=httpx.ConnectError("down"))
    resp = await admin_client.post("/admin/photos/choose")
    assert resp.headers["location"] == admin_url("photos", error="photos-offline")
    assert "reach Google Photos just now" in (await admin_client.get(resp.headers["location"].split("#")[0])).text


async def test_sign_out_revokes_and_forgets_only_the_photos_token(admin_client, db, signed_in, connected, google):
    revoke = google.post(google_photos.REVOKE_ENDPOINT).respond(200)
    await admin_client.post("/admin/photos/sign-out")
    assert revoke.called and REFRESH.encode() in revoke.calls.last.request.content
    assert not await google_photos.is_signed_in(db)
    assert await google_oauth.get_connected_account(db) == "family@example.com"


# --- Picking and importing ---

async def test_poll_waits_until_items_are_set(db, signed_in, google):
    mock_session(google, media_items_set=False)
    await waiting_state(db)
    state = await google_photos.poll_once(db)
    assert state["status"] == "waiting"
    assert await rows(db) == []


async def test_import_lists_every_page_downloads_at_panel_size_and_cleans_up(db, signed_in, google):
    delete = mock_session(google, media_items_set=True)
    listing = mock_items(google, [item(n) for n in range(4)], pages=2)
    download = mock_downloads(google)
    await waiting_state(db)
    state = await google_photos.poll_once(db)
    assert (state["status"], state["imported"], state["skipped"]) == ("done", 4, 0)
    assert listing.call_count == 2
    assert parse_qs(urlsplit(str(listing.calls[1].request.url)).query)["pageToken"] == ["p1"]
    urls = [str(c.request.url) for c in download.calls]
    assert urls == [f"{BASE}/{n}=w2560-h1600" for n in range(4)]
    assert all(c.request.headers["authorization"] == f"Bearer {ACCESS}" for c in download.calls)
    assert delete.called  # sessions.delete
    saved = await rows(db)
    assert len(saved) == 4
    assert sorted(files()) == sorted(n for r in saved for n in (r["filename"], r["thumb"]))


async def test_at_most_30_photos_and_videos_are_skipped(db, signed_in, google):
    mock_session(google, media_items_set=True)
    items = [item("v1", kind="VIDEO", mime="video/mp4"), *[item(n) for n in range(33)],
             item("x", mime="application/pdf")]
    mock_items(google, items)
    download = mock_downloads(google)
    await waiting_state(db)
    state = await google_photos.poll_once(db)
    assert (state["imported"], state["skipped"]) == (30, 5)
    assert download.call_count == 30
    assert not any("/v1=" in str(c.request.url) for c in download.calls)
    assert len(await rows(db)) == 30


async def test_oversized_and_non_image_files_are_skipped(db, signed_in, google, monkeypatch):
    monkeypatch.setattr(google_photos, "MAX_FILE_BYTES", 5000)
    mock_session(google, media_items_set=True)
    mock_items(google, [item(1), item(2), item(3)])
    google.get(f"{BASE}/1=w2560-h1600").respond(200, content=jpeg())
    google.get(f"{BASE}/2=w2560-h1600").respond(200, content=jpeg(1500, 1500) + b"\0" * 6000)
    google.get(f"{BASE}/3=w2560-h1600").respond(200, content=b"<html>not a photo</html>")
    await waiting_state(db)
    state = await google_photos.poll_once(db)
    assert (state["status"], state["imported"], state["skipped"]) == ("done", 1, 2)
    assert len(files()) == 2  # the one photo and its thumbnail


async def test_a_new_set_replaces_the_old_one(db, signed_in, google):
    old = await seed_old_set(db)
    mock_session(google, media_items_set=True)
    mock_items(google, [item(1)])
    mock_downloads(google)
    await waiting_state(db)
    await google_photos.poll_once(db)
    saved = await rows(db)
    assert len(saved) == 1
    assert not {o["filename"] for o in old} & set(files())
    assert set(files()) == {saved[0]["filename"], saved[0]["thumb"]}


async def test_a_failed_download_keeps_the_old_set(db, signed_in, google, caplog):
    old = await seed_old_set(db)
    before_rows, before_files = await rows(db), files()
    delete = mock_session(google, media_items_set=True)
    mock_items(google, [item(1), item(2), item(3)])
    google.get(f"{BASE}/1=w2560-h1600").respond(200, content=jpeg())
    google.get(f"{BASE}/2=w2560-h1600").mock(side_effect=httpx.ConnectError("gone"))
    await waiting_state(db)
    with caplog.at_level(logging.WARNING):
        state = await google_photos.poll_once(db)
        await waiting_state(db)
        await google_photos.poll_once(db)  # still down: logged once per outage
    assert (state["status"], state["message"]) == ("failed", "offline")
    assert await rows(db) == before_rows and files() == before_files  # half-downloaded files are gone too
    assert [o["filename"] for o in old] == [r["filename"] for r in before_rows]
    assert delete.called
    assert sum("Google Photos import failed" in r.message for r in caplog.records) == 1


async def test_a_list_failure_or_nothing_usable_keeps_the_old_set(db, signed_in, google):
    await seed_old_set(db)
    before = await rows(db)
    mock_session(google, media_items_set=True)
    route = mock_items(google, [item("v", kind="VIDEO", mime="video/mp4")])
    await waiting_state(db)
    state = await google_photos.poll_once(db)
    assert (state["status"], state["message"]) == ("failed", "empty")
    assert await rows(db) == before

    route.mock(side_effect=httpx.ReadTimeout("slow"))
    await waiting_state(db)
    state = await google_photos.poll_once(db)
    assert (state["status"], state["message"]) == ("failed", "offline")
    assert await rows(db) == before


async def test_an_expired_session_is_deleted(db, signed_in, google):
    delete = mock_session(google)
    await waiting_state(db)
    await google_photos._set_state(db, {**await google_photos.get_state(db), "expires_at": time.time() - 1})
    state = await google_photos.poll_once(db)
    assert state["status"] == "expired" and delete.called


async def test_the_background_poll_imports_and_stops(db, signed_in, google, monkeypatch):
    mock_session(google, media_items_set=True)
    mock_items(google, [item(1)])
    mock_downloads(google)
    await google_photos.start_picking(db)
    task = google_photos.poller()
    assert task is not None
    await task
    assert (await google_photos.get_state(db))["status"] == "done"
    assert len(await rows(db)) == 1


async def test_cancel_deletes_the_session(admin_client, db, signed_in, google):
    delete = mock_session(google)
    await waiting_state(db)
    resp = await admin_client.post("/admin/photos/cancel")
    assert resp.headers["location"] == admin_url("photos")
    assert delete.called and await google_photos.get_state(db) == {}


async def test_status_fragment_polls_only_while_picking(admin_client, db, signed_in, monkeypatch):
    monkeypatch.setattr(google_photos, "ensure_poller", lambda state: None)
    await waiting_state(db)
    html = (await admin_client.get("/admin/photos/status")).text
    assert 'hx-trigger="every 3s"' in html and PICKER_URI in html
    await google_photos._set_state(db, {"status": "done", "imported": 3, "skipped": 1})
    html = (await admin_client.get("/admin/photos/status")).text
    assert "hx-trigger" not in html and "Done: 3 photos copied (1 skipped" in html


async def test_a_picker_uri_that_isnt_https_is_never_linked(admin_client, db, signed_in, monkeypatch):
    monkeypatch.setattr(google_photos, "ensure_poller", lambda state: None)
    await google_photos._set_state(db, {"status": "waiting", "session_id": SESSION,
                                        "picker_uri": "javascript:alert(1)", "expires_at": time.time() + 60})
    html = (await admin_client.get("/admin/photos/status")).text
    assert "javascript:" not in html and "<svg" not in html


# --- Storage, order and serving ---

def test_the_weekly_shuffle_is_deterministic():
    ids = list(range(1, 31))
    monday, sunday, next_monday = date(2026, 9, 28), date(2026, 10, 4), date(2026, 10, 5)
    week = google_photos.weekly_order(ids, monday)
    assert sorted(week) == ids and week != ids
    assert google_photos.weekly_order(list(reversed(ids)), sunday) == week  # every reload that week agrees
    assert google_photos.weekly_order(ids, next_monday) != week


async def test_photos_are_served_by_id_only(client, db):
    saved = await seed_old_set(db, 1)
    photo_id = (await rows(db))[0]["id"]
    resp = await client.get(f"/photos/{photo_id}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert "immutable" in resp.headers["cache-control"]
    assert resp.content == (google_photos.PHOTOS_DIR / saved[0]["filename"]).read_bytes()
    assert (await client.get(f"/photos/{photo_id}/thumb")).status_code == 200
    for bad in ("999", "0", "-1", "abc", "1.jpg", "..%2F..%2Fdata", str(2**70)):
        assert (await client.get(f"/photos/{bad}")).status_code in (404, 422), bad
    # A row whose name isn't one we generated is never opened.
    await db.execute("UPDATE photos SET filename = '../.secret_key' WHERE id = ?", (photo_id,))
    await db.commit()
    assert (await client.get(f"/photos/{photo_id}")).status_code == 404


async def test_the_slideshow_lists_existing_files_in_weekly_order(db):
    await seed_old_set(db, 3)
    saved = await rows(db)
    (google_photos.PHOTOS_DIR / saved[0]["filename"]).unlink()  # e.g. a restored backup
    today = date(2026, 10, 7)
    expected = [f"/photos/{i}" for i in google_photos.weekly_order([r["id"] for r in saved[1:]], today)]
    assert await google_photos.slideshow(db, today) == expected


async def test_remove_all(admin_client, db):
    await seed_old_set(db)
    resp = await admin_client.post("/admin/photos/remove")
    assert resp.headers["location"] == admin_url("photos")
    assert await rows(db) == [] and files() == []


# --- Admin ---

async def test_missing_env_vars_show_the_setup_steps(admin_client):
    html = (await admin_client.get("/admin?tab=display")).text
    assert 'id="photos"' in html
    assert "GOOGLE_PHOTOS_CLIENT_ID" in html and "/admin/photos/callback" in html
    assert 'action="/admin/photos/choose"' not in html
    resp = await admin_client.post("/admin/photos/choose")
    assert resp.headers["location"] == admin_url("photos")
    assert (await admin_client.get("/admin/photos/connect")).headers["location"] == admin_url("photos")


async def test_configured_shows_choose_and_the_thumbnails(admin_client, db, configured):
    await seed_old_set(db, 2)
    html = (await admin_client.get("/admin?tab=display")).text
    assert 'action="/admin/photos/choose"' in html and "not signed in" in html
    assert "2 photos on the display." in html
    assert html.count('src="/photos/') == 2 and "/thumb" in html
    assert 'action="/admin/photos/remove"' in html


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/admin/photos/connect"), ("GET", "/admin/photos/callback"), ("POST", "/admin/photos/choose"),
    ("GET", "/admin/photos/status"), ("POST", "/admin/photos/cancel"), ("POST", "/admin/photos/remove"),
    ("POST", "/admin/photos/sign-out"), ("POST", "/admin/idle"),
])
async def test_every_photos_route_needs_admin(client, db, method, path):
    await seed_old_set(db, 1)
    resp = await client.request(method, path)
    assert (resp.status_code, resp.headers["location"]) == (303, "/admin/login")
    assert len(await rows(db)) == 1


async def test_no_secret_or_token_is_logged(admin_client, db, configured, google, caplog):
    google.post(google_photos.TOKEN_ENDPOINT).respond(200, json={
        "access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3599})
    mock_session(google, media_items_set=True)
    mock_items(google, [item(1), item(2)])
    google.get(f"{BASE}/1=w2560-h1600").respond(200, content=jpeg())
    google.get(f"{BASE}/2=w2560-h1600").respond(500)
    admin_client.cookies.set(photos.STATE_COOKIE, "st8")
    with caplog.at_level(logging.DEBUG):
        await admin_client.get("/admin/photos/callback", params={"code": "the-one-time-code", "state": "st8"})
        google_photos.stop_poller()
        await google_photos.poll_once(db)
        google.post(google_photos.REVOKE_ENDPOINT).mock(side_effect=httpx.ConnectError("down"))
        await google_photos.sign_out(db)
    assert caplog.records  # something was logged...
    for secret in (ACCESS, REFRESH, CLIENT_SECRET, "the-one-time-code", BASE, "secret-base-url"):
        assert secret not in caplog.text, secret  # ...and none of these


# --- Migration 6 ---

async def test_migration_6_adds_the_photos_table_and_is_a_no_op_again(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:5])
    await database.init_db()
    with sqlite3.connect(path) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'photos'").fetchall()
        before = conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    monkeypatch.undo()
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    with sqlite3.connect(path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(photos)")]
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        assert conn.execute("SELECT * FROM profiles ORDER BY id").fetchall() == before
    assert columns == ["id", "filename", "thumb", "width", "height", "bytes", "created_at"]
    assert 6 in versions
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO photos (filename, thumb) VALUES ('a', 'b')")
    await database.init_db()  # a later start: nothing to apply, nothing touched
    with sqlite3.connect(path) as conn:
        assert {row[0] for row in conn.execute("SELECT version FROM schema_migrations")} == versions
        assert conn.execute("SELECT filename, thumb FROM photos").fetchall() == [("a", "b")]
        # And the migration itself is idempotent (a restored older backup runs it again).
        conn.execute("DELETE FROM schema_migrations WHERE version = 6")
    await database.init_db()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT filename, thumb FROM photos").fetchall() == [("a", "b")]
