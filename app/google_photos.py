"""
Photos for the idle slideshow (spec 10.2): a separate "Photos account"
sign-in, the Google Photos Picker, and the local copies.

Why a separate sign-in: the family's photos live in a personal Google
account, while the main OAuth app (google_oauth.py) is Internal to the
Workspace organisation and can't reach personal accounts. So this uses a
second OAuth client (GOOGLE_PHOTOS_CLIENT_ID / _SECRET, an External/Testing
Cloud project) with one scope, photospicker.mediaitems.readonly. Its token is
stored encrypted in auth_tokens under its own service_name, apart from the
main connection, which it never touches. The token is only needed while
picking and downloading, so a Testing project's 7-day refresh-token expiry
doesn't matter: an expired or missing token just means "Choose photos"
signs in again.

The picker flow (Admin → Display → Photos):
1. sessions.create with pickingConfig.maxItemCount = 30.
2. Admin shows the session's pickerUri as a QR code and a link, to open on a
   phone signed in to the personal account (it can't go in an iframe).
3. A background task polls sessions.get at the session's pollingConfig
   interval until mediaItemsSet (or the session times out).
4. mediaItems.list (every page), photos only: videos and anything that isn't
   an image are skipped.
5. Each photo is downloaded from its baseUrl + "=w2560-h1600" (the Idea Tab
   Plus panel; fine on the Tab A9+ too) with the Bearer token, straight away
   since baseUrls expire after 60 minutes, size-capped and checked with
   Pillow, into DATA_DIR/photos/ under names we generate.
6. The new set replaces the old one in one step; if anything goes wrong
   before that (Google unreachable, nothing usable), the old set stays.
7. sessions.delete, whatever happened.

The copies are a snapshot, not a live album, at most 30, and not backed up
(app/backup.py copies only the database and the key; re-pick instead). The
slideshow's order is reshuffled weekly with a seed from the ISO week, so
every page load in a week agrees.

No Google SDK: plain httpx, like the rest of the app. Nothing here logs a
token, a code or a URL (baseUrls are bearer-ish): http_client.describe only.
"""

import asyncio
import io
import json
import logging
import os
import random
import re
import secrets
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app import database, http_client
from app.database import get_db, get_setting, set_setting
from app.security import decrypt_token_json, encrypt_token_json

logger = logging.getLogger(__name__)

SERVICE = "google_photos"  # auth_tokens.service_name; the main connection is "google"
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
PICKER_API = "https://photospicker.googleapis.com/v1"
SESSIONS_ENDPOINT = f"{PICKER_API}/sessions"
MEDIA_ITEMS_ENDPOINT = f"{PICKER_API}/mediaItems"

MAX_PHOTOS = 30
CONTENT_HOST_SUFFIX = ".googleusercontent.com"  # where Picker baseUrls point (lh3.googleusercontent.com, ...)
# The Lenovo Idea Tab Plus panel (spec 3); Google scales to fit inside it.
SIZE_PARAM = "=w2560-h1600"
MAX_FILE_BYTES = 20 * 1024 * 1024  # a 2560x1600 photo is 1-3 MB; anything far bigger is skipped
MAX_PIXELS = 40_000_000            # and so is anything that would decode to more than this
IMAGE_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
THUMB_SIZE = (320, 200)
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
TOKEN_REFRESH_BUFFER_SECONDS = 60

STATE_SETTING = "photos_picker"  # the picker session in progress (JSON), see _state()
DEFAULT_POLL_SECONDS = 5.0
MIN_POLL_SECONDS, MAX_POLL_SECONDS = 2.0, 30.0
DEFAULT_SESSION_SECONDS = 30 * 60
OUTAGE_KEY = "Google Photos"
POLL_OUTAGE_KEY = "Google Photos picker"

# What's on disk: our own names only, so a row can never point anywhere else.
FILE_NAME = re.compile(r"[0-9a-f]{32}(?:\.(?:jpg|png|webp|gif)|_t\.jpg)")
PHOTOS_DIR: Path = database.DATA_DIR / "photos"


class NotSignedIn(Exception):
    """No usable Photos token: sign in (again)."""


class ImportFailed(Exception):
    """The picked photos couldn't be imported; the old set is kept."""


class Busy(Exception):
    """Photos are being copied right now: wait, or Cancel first."""


# --- Configuration and sign-in ---------------------------------------------------------

def client_id() -> str:
    return os.environ.get("GOOGLE_PHOTOS_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("GOOGLE_PHOTOS_CLIENT_SECRET", "").strip()


def is_configured() -> bool:
    return bool(client_id() and _client_secret())


def new_state() -> str:
    return secrets.token_urlsafe(24)


def build_auth_url(state: str, redirect_uri: str) -> str:
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{httpx.QueryParams(params)}"


async def _token_request(data: dict) -> dict:
    async with http_client.client() as client:
        resp = await client.post(TOKEN_ENDPOINT, data={
            **data, "client_id": client_id(), "client_secret": _client_secret(),
        })
        resp.raise_for_status()
        return resp.json()


async def exchange_code(code: str, redirect_uri: str) -> dict:
    tokens = await _token_request({"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    return tokens


async def store_tokens(db, tokens: dict) -> None:
    await db.execute(
        """INSERT INTO auth_tokens (service_name, account_email, encrypted_token_json)
           VALUES (?, NULL, ?)
           ON CONFLICT(service_name) DO UPDATE SET encrypted_token_json = excluded.encrypted_token_json""",
        (SERVICE, encrypt_token_json(tokens)),
    )
    await db.commit()


async def _load_tokens(db) -> dict | None:
    row = await (await db.execute(
        "SELECT encrypted_token_json FROM auth_tokens WHERE service_name = ?", (SERVICE,)
    )).fetchone()
    if row is None or not row["encrypted_token_json"]:
        return None
    return decrypt_token_json(row["encrypted_token_json"])


async def _forget_tokens(db) -> None:
    await db.execute("DELETE FROM auth_tokens WHERE service_name = ?", (SERVICE,))
    await db.commit()


async def is_signed_in(db) -> bool:
    return await _load_tokens(db) is not None


async def access_token(db) -> str | None:
    """A usable Photos access token, refreshed if needed. None = sign in
    (again): never signed in, or the refresh token has expired (a Testing
    project's 7 days) or been revoked. Raises httpx.HTTPError when Google is
    unreachable, which is "try later", not "sign in"."""
    tokens = await _load_tokens(db)
    if tokens is None:
        return None
    if time.time() < tokens.get("expires_at", 0) - TOKEN_REFRESH_BUFFER_SECONDS:
        return tokens.get("access_token")
    if not tokens.get("refresh_token"):
        await _forget_tokens(db)
        return None
    try:
        refreshed = await _token_request({"refresh_token": tokens["refresh_token"], "grant_type": "refresh_token"})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (400, 401):
            raise
        logger.info("Google Photos sign-in has expired; Choose photos will sign in again")
        await _forget_tokens(db)
        return None
    tokens["access_token"] = refreshed["access_token"]
    tokens["expires_at"] = time.time() + refreshed.get("expires_in", 3600)
    await store_tokens(db, tokens)
    return tokens["access_token"]


async def sign_out(db) -> None:
    """Revoke (best effort) and forget the Photos token. The photos stay."""
    tokens = await _load_tokens(db)
    token = tokens and (tokens.get("refresh_token") or tokens.get("access_token"))
    if token:
        try:
            async with http_client.client() as client:
                await client.post(REVOKE_ENDPOINT, data={"token": token})
        except httpx.HTTPError as exc:
            logger.warning("Couldn't revoke the Google Photos token: %s", http_client.describe(exc))
    await _forget_tokens(db)


# --- The Picker API -------------------------------------------------------------------

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seconds(duration, default: float) -> float:
    """A protobuf Duration string ("5s", "1799.5s") -> seconds."""
    try:
        return float(str(duration).removesuffix("s"))
    except (TypeError, ValueError):
        return default


async def create_session(token: str) -> dict:
    async with http_client.client() as client:
        resp = await client.post(SESSIONS_ENDPOINT, headers=_auth(token),
                                 json={"pickingConfig": {"maxItemCount": str(MAX_PHOTOS)}})
        resp.raise_for_status()
        return resp.json()


async def get_session(token: str, session_id: str) -> dict:
    async with http_client.client() as client:
        resp = await client.get(f"{SESSIONS_ENDPOINT}/{session_id}", headers=_auth(token))
        resp.raise_for_status()
        return resp.json()


async def delete_session(token: str, session_id: str) -> None:
    """Best effort: a session left behind expires on its own."""
    try:
        async with http_client.client() as client:
            resp = await client.delete(f"{SESSIONS_ENDPOINT}/{session_id}", headers=_auth(token))
            if resp.status_code != 404:
                resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.info("Couldn't delete the Google Photos picker session: %s", http_client.describe(exc))


async def list_media_items(token: str, session_id: str) -> list[dict]:
    items: list[dict] = []
    params = {"sessionId": session_id, "pageSize": "100"}
    async with http_client.client() as client:
        while True:
            resp = await client.get(MEDIA_ITEMS_ENDPOINT, headers=_auth(token), params=params)
            resp.raise_for_status()
            body = resp.json()
            items.extend(body.get("mediaItems") or [])
            page = body.get("nextPageToken")
            if not page:
                return items
            params["pageToken"] = page


def is_google_content_url(url) -> bool:
    """Only Google's own photo content hosts ever get the Bearer token."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and port in (None, 443) and not parts.username and not parts.password
            and host.endswith(CONTENT_HOST_SUFFIX))


def is_photo(item: dict) -> bool:
    media = item.get("mediaFile") or {}
    return (item.get("type") == "PHOTO" and str(media.get("mimeType", "")).startswith("image/")
            and is_google_content_url(media.get("baseUrl")))


async def download(token: str, base_url: str) -> bytes | None:
    """The photo sized for the panel, or None if it's over MAX_FILE_BYTES.
    Raises httpx.HTTPError (Google unreachable, or the URL has expired).
    A URL on any host but Google's content hosts is refused (None) and
    never sees the token."""
    if not is_google_content_url(base_url):
        return None
    async with http_client.client(DOWNLOAD_TIMEOUT) as client:
        async with client.stream("GET", base_url + SIZE_PARAM, headers=_auth(token)) as resp:
            resp.raise_for_status()
            try:
                if int(resp.headers.get("content-length", 0)) > MAX_FILE_BYTES:
                    return None
            except ValueError:
                pass
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_FILE_BYTES:
                    return None
            return bytes(data)


# --- Files on disk --------------------------------------------------------------------

def _save_image(data: bytes) -> dict | None:
    """Check `data` is an image we can show, then write it and a thumbnail
    under new names. None (nothing written) if it isn't one."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt, (width, height) = probe.format, probe.size
            if fmt not in IMAGE_FORMATS or width * height > MAX_PIXELS:
                return None
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            thumb = ImageOps.exif_transpose(img).convert("RGB")
            thumb.thumbnail(THUMB_SIZE)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return None
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    stem = secrets.token_hex(16)
    filename, thumb_name = f"{stem}.{IMAGE_FORMATS[fmt]}", f"{stem}_t.jpg"
    (PHOTOS_DIR / filename).write_bytes(data)
    thumb.save(PHOTOS_DIR / thumb_name, "JPEG", quality=80)
    return {"filename": filename, "thumb": thumb_name, "width": width, "height": height, "bytes": len(data)}


def _remove_files(names) -> None:
    for name in names:
        if FILE_NAME.fullmatch(name):
            (PHOTOS_DIR / name).unlink(missing_ok=True)


def _prune_unlisted(keep: set[str]) -> None:
    """Delete every photo file of ours that no row lists (an old set, or
    leftovers from an import that was interrupted)."""
    if not PHOTOS_DIR.is_dir():
        return
    for path in PHOTOS_DIR.iterdir():
        if FILE_NAME.fullmatch(path.name) and path.name not in keep:
            path.unlink(missing_ok=True)


async def replace_set(db, saved: list[dict]) -> None:
    """The new photos replace the old ones in one transaction; only then
    are the old files deleted. If the database write fails, the new files
    go and the old set stays."""
    try:
        await db.execute("DELETE FROM photos")
        await db.executemany(
            "INSERT INTO photos (filename, thumb, width, height, bytes) VALUES (?, ?, ?, ?, ?)",
            [(p["filename"], p["thumb"], p["width"], p["height"], p["bytes"]) for p in saved],
        )
        await db.commit()
    except Exception:
        await db.rollback()
        _remove_files(name for p in saved for name in (p["filename"], p["thumb"]))
        raise
    _prune_unlisted({name for p in saved for name in (p["filename"], p["thumb"])})


async def remove_all(db) -> None:
    """Delete every photo row, and only those rows' files: an import in
    progress may have new files on disk that aren't rows yet (Admin hides
    Remove all while picking and the route refuses it, but belt and braces)."""
    names = [name for r in await list_photos(db) for name in (r["filename"], r["thumb"])]
    await db.execute("DELETE FROM photos")
    await db.commit()
    _remove_files(names)


def photo_key(row) -> str:
    """The photo's URL key, "<row id>-<file stem>". The stem is random per
    file, so a row id reused after a database restore never matches a URL a
    browser has cached (the files are served as immutable)."""
    return f"{row['id']}-{str(row['filename'])[:32]}"


async def list_photos(db) -> list[dict]:
    rows = [dict(r) for r in await (await db.execute("SELECT * FROM photos ORDER BY id")).fetchall()]
    for row in rows:
        row["key"] = photo_key(row)
    return rows


async def photo_file(db, photo_id: int, stem: str, thumb: bool = False) -> Path | None:
    """The file for a photo row, or None: no such row, a stem that isn't
    that row's (an old URL), a name that isn't ours, or the file is gone
    (e.g. a restored backup: photos aren't in it)."""
    row = await (await db.execute("SELECT filename, thumb FROM photos WHERE id = ?", (photo_id,))).fetchone()
    if row is None:
        return None
    name = row["thumb" if thumb else "filename"]
    if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
        return None
    if not name.startswith(stem + ("_" if thumb else ".")):
        return None
    path = PHOTOS_DIR / name
    return path if path.is_file() else None


def weekly_order(ids: list[int], today: date) -> list[int]:
    """`ids` shuffled with a seed from today's ISO week: the same order all
    week (every reload agrees), a new one each Monday."""
    year, week, _ = today.isocalendar()
    ordered = sorted(ids)
    random.Random(f"huddle-photos-{year}-W{week:02d}").shuffle(ordered)  # noqa: S311 (a repeatable order, not security)
    return ordered


async def slideshow(db, today: date) -> list[str]:
    """The slideshow's photo URLs, in this week's order (files that exist only)."""
    rows = {r["id"]: r for r in await list_photos(db)
            if FILE_NAME.fullmatch(r["filename"]) and (PHOTOS_DIR / r["filename"]).is_file()}
    return [f"/photos/{rows[photo_id]['key']}" for photo_id in weekly_order(list(rows), today)]


# --- The picking session --------------------------------------------------------------
# One at a time. Its state lives in app_settings so the Admin page (and a
# restart) can see it:
#   {"status": "waiting" | "importing" | "done" | "failed" | "expired",
#    "session_id", "picker_uri", "poll_seconds", "expires_at" (epoch),
#    "imported", "skipped", "message"}

async def get_state(db) -> dict:
    try:
        state = json.loads(await get_setting(db, STATE_SETTING) or "{}")
    except ValueError:
        state = {}
    return state if isinstance(state, dict) else {}


async def _set_state(db, state: dict) -> None:
    await set_setting(db, STATE_SETTING, json.dumps(state))
    await db.commit()


def poller_running() -> bool:
    return _poller is not None and not _poller.done()


async def recover(db) -> dict:
    """Make the saved state match what's really running (at startup, and
    whenever Admin shows the panel). An import with no task doing it (the
    app restarted mid-copy) is marked failed, and its half-copied files,
    which no row lists, are deleted. A waiting session gets its poll back."""
    state = await get_state(db)
    if state.get("status") == "importing" and not poller_running():
        state.update(status="failed", message="interrupted")
        await _set_state(db, state)
        _prune_unlisted({name for r in await list_photos(db) for name in (r["filename"], r["thumb"])})
        logger.warning("A Google Photos import was interrupted; keeping the old photos")
    elif state.get("status") == "waiting":
        ensure_poller(state)
    return state


async def start_picking(db) -> dict:
    """Create a picker session. Raises NotSignedIn (sign in first), Busy (an
    import is running) or httpx.HTTPError (Google unreachable). Starts the
    background poll."""
    old = await recover(db)
    if old.get("status") == "importing":
        raise Busy
    token = await access_token(db)
    if not token:
        raise NotSignedIn
    if old.get("status") == "waiting":
        await stop_poller_and_wait()  # its poll must never act on the new session's state
        if old.get("session_id"):
            await delete_session(token, old["session_id"])
    session = await create_session(token)
    polling = session.get("pollingConfig") or {}
    state = {
        "status": "waiting",
        "session_id": session["id"],
        "picker_uri": session["pickerUri"],
        "poll_seconds": min(MAX_POLL_SECONDS, max(MIN_POLL_SECONDS, _seconds(polling.get("pollInterval"),
                                                                             DEFAULT_POLL_SECONDS))),
        "expires_at": time.time() + _seconds(polling.get("timeoutIn"), DEFAULT_SESSION_SECONDS),
    }
    await _set_state(db, state)
    ensure_poller(state)
    return state


async def cancel(db) -> None:
    """Stop picking or copying. The poll task is cancelled and waited for,
    so any files it had already copied are gone before this returns."""
    state = await get_state(db)
    await stop_poller_and_wait()
    if state.get("session_id") and state.get("status") in ("waiting", "importing"):
        try:
            token = await access_token(db)
        except httpx.HTTPError:
            token = None
        if token:
            await delete_session(token, state["session_id"])
    await _set_state(db, {})


async def _save_in_thread(data: bytes, saved: list[dict]) -> dict | None:
    """_save_image off the event loop. A thread can't be stopped: if the
    import is cancelled meanwhile, wait for it and record what it wrote in
    `saved`, so the caller's cleanup deletes those files too."""
    future = asyncio.ensure_future(asyncio.to_thread(_save_image, data))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        written = await future
        if written is not None:
            saved.append(written)
        raise


async def import_session(db, token: str, session_id: str) -> tuple[int, int]:
    """mediaItems.list -> download -> replace the set. Returns (imported,
    skipped). Raises httpx.HTTPError or ImportFailed; the old set is kept then."""
    items = await list_media_items(token, session_id)
    photos = [item for item in items if is_photo(item)][:MAX_PHOTOS]
    skipped = len(items) - len(photos)
    saved: list[dict] = []
    try:
        for item in photos:
            data = await download(token, item["mediaFile"]["baseUrl"])
            result = await _save_in_thread(data, saved) if data is not None else None
            if result is None:
                skipped += 1
            else:
                saved.append(result)
    except BaseException:
        _remove_files(name for p in saved for name in (p["filename"], p["thumb"]))
        raise
    if not saved:
        raise ImportFailed("none of the chosen items is a photo that can be shown")
    await replace_set(db, saved)
    return len(saved), skipped


async def poll_once(db) -> dict:
    """One step of a waiting session: expired, still waiting, or picked
    (then import it). Returns the new state. Raises httpx.HTTPError while
    Google is unreachable (the caller keeps polling until the session ends)."""
    state = await get_state(db)
    if state.get("status") != "waiting":
        return state
    session_id = state["session_id"]
    token = await access_token(db)
    if not token:
        state.update(status="failed", message="signin")
        await _set_state(db, state)
        return state
    if time.time() >= state.get("expires_at", 0):
        await delete_session(token, session_id)
        state.update(status="expired")
        await _set_state(db, state)
        return state
    session = await get_session(token, session_id)
    if not session.get("mediaItemsSet"):
        return state
    state["status"] = "importing"
    await _set_state(db, state)
    try:
        imported, skipped = await import_session(db, token, session_id)
    except (httpx.HTTPError, ImportFailed, OSError) as exc:
        http_client.report_failure(logger, OUTAGE_KEY, "Google Photos import failed; keeping the old photos: %s",
                                   http_client.describe(exc))
        state.update(status="failed", message="empty" if isinstance(exc, ImportFailed) else "offline")
    else:
        http_client.report_success(logger, OUTAGE_KEY)
        logger.info("Imported %d photo(s) for the idle slideshow (%d skipped)", imported, skipped)
        state.update(status="done", imported=imported, skipped=skipped)
    finally:
        await delete_session(token, session_id)
    await _set_state(db, state)
    return state


_poller: asyncio.Task | None = None


async def _poll_loop(poll_seconds: float) -> None:
    while True:
        try:
            async with get_db() as db:
                state = await poll_once(db)
            http_client.report_success(logger, POLL_OUTAGE_KEY)
        except httpx.HTTPError as exc:
            http_client.report_failure(logger, POLL_OUTAGE_KEY, "Google Photos picker unreachable; still waiting: %s",
                                       http_client.describe(exc))
            state = {"status": "waiting"}
        except Exception:
            logger.exception("Google Photos picker poll failed")
            return
        if state.get("status") != "waiting":
            return
        await asyncio.sleep(poll_seconds)


def ensure_poller(state: dict) -> None:
    """Poll a waiting session in the background (single process: see
    scheduler.py). Called when a session starts and whenever Admin shows its
    status, so a restart picks a waiting session back up."""
    global _poller
    if state.get("status") != "waiting" or (_poller is not None and not _poller.done()):
        return
    _poller = asyncio.get_running_loop().create_task(
        _poll_loop(float(state.get("poll_seconds") or DEFAULT_POLL_SECONDS)), name="photos-picker"
    )


def stop_poller() -> None:
    global _poller
    if _poller is not None and not _poller.done():
        _poller.cancel()
    _poller = None


async def stop_poller_and_wait() -> None:
    """stop_poller, then wait until the task has really finished (and so
    has cleaned up after itself)."""
    task = _poller
    stop_poller()
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: S110 (the loop has logged it already)
            pass


def poller() -> asyncio.Task | None:
    """The running poll task (tests await it)."""
    return _poller
