"""
Admin → Display → Idle screen and Photos (spec 10.2). The kiosk side
(/api/idle, /photos/{id}) is in app/idle.py; the Photos account, the
picker and the files are in app/google_photos.py.

Photos use their own sign-in (a second OAuth client), never the main
Google connection. "Choose photos" signs in first when there's no usable
token (never signed in, or a Testing project's 7-day expiry), then
creates a picker session and shows its link as a QR code; the status
fragment below polls while the family picks on a phone.
"""

import logging

import httpx
import segno
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import google_photos, http_client, idle
from app.admin_tabs import admin_url
from app.auth import require_admin
from app.database import get_db
from app.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

STATE_COOKIE = "google_photos_state"


def _qr_svg(uri: str) -> str:
    """The picker link as an inline SVG QR code (segno: pure Python). Dark
    on white in both palettes: phone cameras want the contrast."""
    return segno.make(uri, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")


def _picker_uri(state: dict) -> str | None:
    uri = state.get("picker_uri")
    return uri if isinstance(uri, str) and uri.startswith("https://") else None


async def panel_context(db) -> dict:
    """The Photos panel's context (settings.html and the status fragment)."""
    # A restart mid-copy leaves "importing" with nothing doing it: recover()
    # marks that failed; a waiting session gets its poll back.
    state = await google_photos.recover(db)
    uri = _picker_uri(state) if state.get("status") == "waiting" else None
    return {
        "photos_configured": google_photos.is_configured(),
        "photos_signed_in": await google_photos.is_signed_in(db),
        "photos_state": state,
        "photos_picker_uri": uri,
        "photos_qr": _qr_svg(uri) if uri else None,
        "photos": await google_photos.list_photos(db),
        "photos_max": google_photos.MAX_PHOTOS,
    }


def _callback_uri(request: Request) -> str:
    return str(request.url_for("photos_callback"))


@router.post("/idle", dependencies=[Depends(require_admin)])
async def save_idle_settings(request: Request):
    form = dict(await request.form())
    try:
        settings = idle.clean_settings(form)
    except idle.SettingsError as exc:
        return RedirectResponse(url=admin_url("idle", error=exc.code), status_code=303)
    async with get_db() as db:
        await idle.save_settings(db, settings)
    return RedirectResponse(url=admin_url("idle"), status_code=303)


@router.get("/photos/connect", dependencies=[Depends(require_admin)])
async def photos_connect(request: Request):
    """Off to Google to sign in to the Photos account."""
    if not google_photos.is_configured():
        return RedirectResponse(url=admin_url("photos"), status_code=303)
    state = google_photos.new_state()
    response = RedirectResponse(url=google_photos.build_auth_url(state, _callback_uri(request)), status_code=302)
    response.set_cookie(STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600)
    return response


@router.get("/photos/callback", name="photos_callback", dependencies=[Depends(require_admin)])
async def photos_callback(request: Request, code: str | None = None, state: str | None = None,
                          error: str | None = None):
    """Store the Photos token (encrypted, apart from the main connection),
    then carry straight on to picking: that's what signing in was for."""
    expected = request.cookies.get(STATE_COOKIE)
    if error or not code or not state or not expected or state != expected:
        response = RedirectResponse(url=admin_url("photos", error="photos-signin"), status_code=303)
        response.delete_cookie(STATE_COOKIE)
        return response
    target = admin_url("photos")
    try:
        tokens = await google_photos.exchange_code(code, _callback_uri(request))
    except httpx.HTTPError as exc:
        logger.warning("Google Photos sign-in failed: %s", http_client.describe(exc))
        target = admin_url("photos", error="photos-signin")
    else:
        async with get_db() as db:
            await google_photos.store_tokens(db, tokens)
            try:
                await google_photos.start_picking(db)
            except (httpx.HTTPError, google_photos.NotSignedIn, google_photos.Busy) as exc:
                logger.warning("Couldn't start picking photos: %s", http_client.describe(exc))
                target = admin_url("photos", error="photos-offline")
    response = RedirectResponse(url=target, status_code=303)
    response.delete_cookie(STATE_COOKIE)
    return response


@router.post("/photos/choose", dependencies=[Depends(require_admin)])
async def choose_photos():
    if not google_photos.is_configured():
        return RedirectResponse(url=admin_url("photos"), status_code=303)
    async with get_db() as db:
        try:
            await google_photos.start_picking(db)
        except google_photos.NotSignedIn:
            return RedirectResponse(url="/admin/photos/connect", status_code=303)
        except google_photos.Busy:
            return RedirectResponse(url=admin_url("photos", error="photos-busy"), status_code=303)
        except httpx.HTTPError as exc:
            logger.warning("Couldn't start picking photos: %s", http_client.describe(exc))
            return RedirectResponse(url=admin_url("photos", error="photos-offline"), status_code=303)
    return RedirectResponse(url=admin_url("photos"), status_code=303)


@router.get("/photos/status", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def photos_status(request: Request):
    """The panel's body, polled while a picker session is open."""
    async with get_db() as db:
        return templates.TemplateResponse(request, "admin/_photos_status.html", await panel_context(db))


@router.post("/photos/cancel", dependencies=[Depends(require_admin)])
async def cancel_picking():
    async with get_db() as db:
        await google_photos.cancel(db)
    return RedirectResponse(url=admin_url("photos"), status_code=303)


@router.post("/photos/remove", dependencies=[Depends(require_admin)])
async def remove_photos():
    async with get_db() as db:
        if (await google_photos.recover(db)).get("status") in ("waiting", "importing"):
            return RedirectResponse(url=admin_url("photos", error="photos-busy"), status_code=303)
        await google_photos.remove_all(db)
    return RedirectResponse(url=admin_url("photos"), status_code=303)


@router.post("/photos/sign-out", dependencies=[Depends(require_admin)])
async def photos_sign_out():
    async with get_db() as db:
        await google_photos.sign_out(db)
    return RedirectResponse(url=admin_url("photos"), status_code=303)
