"""Photos: the security review's cases (PR #46). A restart mid-import,
Remove all and Choose during an import, cancelling mid-save, and the Bearer
token only ever going to Google's content hosts."""

import asyncio
import io
import threading
import time

import pytest
from PIL import Image

from app import google_photos
from app.admin_tabs import admin_url
from app.routers import admin
from app.security import create_session_token

ACCESS = "photos-access-token-ABC"
SESSION = "sess-123"
SESSION_URL = f"{google_photos.SESSIONS_ENDPOINT}/{SESSION}"
BASE = "https://lh3.googleusercontent.com/base"


def jpeg(colour=(200, 80, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 40), colour).save(out, "JPEG")
    return out.getvalue()


def item(n, base=BASE):
    return {"id": f"m{n}", "type": "PHOTO", "mediaFile": {"baseUrl": f"{base}/{n}", "mimeType": "image/jpeg"}}


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


@pytest.fixture
async def signed_in(db, monkeypatch):
    monkeypatch.setenv("GOOGLE_PHOTOS_CLIENT_ID", "photos-client-id")
    monkeypatch.setenv("GOOGLE_PHOTOS_CLIENT_SECRET", "photos-client-secret")
    await google_photos.store_tokens(
        db, {"access_token": ACCESS, "refresh_token": "r", "expires_at": time.time() + 3600}
    )


async def seed_set(db, n=2):
    saved = [google_photos._save_image(jpeg((10, 10, 10 + i))) for i in range(n)]
    await google_photos.replace_set(db, saved)
    return saved


def files():
    return sorted(p.name for p in google_photos.PHOTOS_DIR.iterdir()) if google_photos.PHOTOS_DIR.is_dir() else []


async def set_state(db, status, **extra):
    await google_photos._set_state(
        db,
        {
            "status": status,
            "session_id": SESSION,
            "expires_at": time.time() + 600,
            "picker_uri": "https://photos.google.com/picker/x",
            "poll_seconds": 5,
            **extra,
        },
    )


@pytest.fixture
async def running_poller():
    """A stand-in for a poll task that is busy importing."""
    gate = asyncio.Event()
    google_photos._poller = asyncio.get_running_loop().create_task(gate.wait())
    yield
    gate.set()
    google_photos.stop_poller()


# --- 1. A restart mid-import ---


async def test_an_interrupted_import_is_marked_failed_and_its_files_removed(admin_client, db, signed_in):
    await seed_set(db)
    kept = files()
    half = google_photos._save_image(jpeg((1, 2, 3)))  # copied, but no row yet: the restart hit here
    assert len(files()) == len(kept) + 2
    await set_state(db, "importing")

    html = (await admin_client.get("/admin?tab=display")).text  # no poll task is running: recover
    state = await google_photos.get_state(db)
    assert (state["status"], state["message"]) == ("failed", "interrupted")
    assert files() == kept and half["filename"] not in files()
    assert "Copying was interrupted" in html
    assert 'action="/admin/photos/choose"' in html  # Choose again is offered
    assert 'hx-trigger="every 3s"' not in html  # and it no longer polls forever


async def test_startup_recovers_an_interrupted_import(db, monkeypatch):
    from app import scheduler
    from app.main import app, lifespan

    monkeypatch.setattr(scheduler, "start", lambda: None)  # no jobs: just the startup steps
    monkeypatch.setattr(scheduler, "stop", lambda: None)
    await set_state(db, "importing")
    async with lifespan(app):
        pass
    assert (await google_photos.get_state(db))["status"] == "failed"


async def test_a_waiting_session_gets_its_poll_back(db, signed_in, monkeypatch):
    started = []
    monkeypatch.setattr(google_photos, "ensure_poller", started.append)
    await set_state(db, "waiting")
    await google_photos.recover(db)
    assert [s["status"] for s in started] == ["waiting"]


async def test_every_state_offers_a_way_out(admin_client, db, signed_in, running_poller, monkeypatch):
    monkeypatch.setattr(google_photos, "ensure_poller", lambda state: None)
    for status in ("waiting", "importing"):
        await set_state(db, status)
        html = (await admin_client.get("/admin/photos/status")).text
        assert 'action="/admin/photos/cancel"' in html, status
    for status in ("done", "failed", "expired"):
        await set_state(db, status, imported=1, skipped=0, message="offline")
        html = (await admin_client.get("/admin/photos/status")).text
        assert 'action="/admin/photos/choose"' in html, status


# --- 2. Remove all during an import ---


async def test_remove_all_only_deletes_its_own_rows_files(db):
    await seed_set(db)
    incoming = google_photos._save_image(jpeg((5, 5, 5)))  # an import's file, not yet a row
    await google_photos.remove_all(db)
    assert files() == sorted([incoming["filename"], incoming["thumb"]])
    assert await google_photos.list_photos(db) == []


@pytest.mark.parametrize("status", ["waiting", "importing"])
async def test_remove_all_is_refused_while_picking(admin_client, db, signed_in, running_poller, monkeypatch, status):
    monkeypatch.setattr(google_photos, "ensure_poller", lambda state: None)
    await seed_set(db)
    before = files()
    await set_state(db, status)
    html = (await admin_client.get("/admin/photos/status")).text
    assert 'action="/admin/photos/remove"' not in html
    resp = await admin_client.post("/admin/photos/remove")
    assert resp.headers["location"] == admin_url("photos", error="photos-busy")
    assert files() == before and len(await google_photos.list_photos(db)) == 2


# --- 3. The token only goes to Google's content hosts ---


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/photo",
        "https://googleusercontent.com.evil.example/p",
        "http://lh3.googleusercontent.com/p",
        "https://user:pw@lh3.googleusercontent.com/p",
        "https://lh3.googleusercontent.com:8443/p",
        "javascript:x",
        None,
    ],
)
def test_only_google_content_urls_are_accepted(url):
    assert not google_photos.is_google_content_url(url)


def test_google_content_urls_are_accepted():
    assert google_photos.is_google_content_url("https://lh3.googleusercontent.com/abc")
    assert google_photos.is_google_content_url("https://video-downloads.googleusercontent.com/abc")


async def test_a_foreign_base_url_is_skipped_and_never_gets_the_token(db, signed_in, google):
    google.get(SESSION_URL).respond(200, json={"mediaItemsSet": True})
    google.delete(SESSION_URL).respond(200, json={})
    google.get(google_photos.MEDIA_ITEMS_ENDPOINT).respond(
        200, json={"mediaItems": [item(1), item(2, base="https://evil.example/steal")]}
    )
    good = google.get(url__startswith=BASE).respond(200, content=jpeg())
    evil = google.get(url__startswith="https://evil.example").respond(200, content=jpeg())
    await set_state(db, "waiting")
    state = await google_photos.poll_once(db)
    assert (state["imported"], state["skipped"]) == (1, 1)
    assert good.call_count == 1 and not evil.called
    assert await google_photos.download(ACCESS, "https://evil.example/x") is None
    assert not evil.called


# --- 4. Choose while an import is running ---


async def test_choose_is_refused_while_importing(admin_client, db, signed_in, running_poller, google):
    create = google.post(google_photos.SESSIONS_ENDPOINT).respond(200, json={"id": "new", "pickerUri": "https://x"})
    await set_state(db, "importing")
    with pytest.raises(google_photos.Busy):
        await google_photos.start_picking(db)
    resp = await admin_client.post("/admin/photos/choose")
    assert resp.headers["location"] == admin_url("photos", error="photos-busy")
    assert not create.called
    assert (await google_photos.get_state(db))["status"] == "importing"
    page = (await admin_client.get(resp.headers["location"].split("#")[0])).text
    assert "being picked or copied right now" in page


async def test_choose_again_while_waiting_stops_the_old_poll_first(db, signed_in, google, monkeypatch):
    started = []
    monkeypatch.setattr(google_photos, "ensure_poller", started.append)  # the new session's poll isn't under test
    google.post(google_photos.SESSIONS_ENDPOINT).respond(200, json={"id": "new", "pickerUri": "https://p/new"})
    google.delete(SESSION_URL).respond(200, json={})
    google.get(f"{google_photos.SESSIONS_ENDPOINT}/new").respond(200, json={"mediaItemsSet": False})
    old = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    google_photos._poller = old
    await set_state(db, "waiting")
    state = await google_photos.start_picking(db)
    assert old.cancelled()
    assert state["session_id"] == "new" and (await google_photos.get_state(db))["session_id"] == "new"
    assert started[-1]["session_id"] == "new"


# --- 8. Cancel while a save is still running in its thread ---


async def test_cancel_mid_save_leaves_no_files(db, signed_in, google, monkeypatch):
    google.get(SESSION_URL).respond(200, json={"mediaItemsSet": True})
    google.delete(SESSION_URL).respond(200, json={})
    google.get(google_photos.MEDIA_ITEMS_ENDPOINT).respond(200, json={"mediaItems": [item(1), item(2)]})
    google.get(url__startswith=BASE).respond(200, content=jpeg())
    await seed_set(db, 1)
    before = files()

    entered, release = threading.Event(), threading.Event()
    real_save = google_photos._save_image

    def slow_save(data):
        entered.set()
        release.wait(5)
        return real_save(data)

    monkeypatch.setattr(google_photos, "_save_image", slow_save)
    await set_state(db, "waiting", poll_seconds=2)
    google_photos.ensure_poller(await google_photos.get_state(db))
    await asyncio.to_thread(entered.wait, 5)  # the first save is running in its thread
    cancelling = asyncio.create_task(google_photos.cancel(db))
    await asyncio.sleep(0.05)
    release.set()  # the thread finishes and writes its files after the cancel
    await cancelling
    assert files() == before
    assert await google_photos.get_state(db) == {}
