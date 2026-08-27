"""
Google OAuth connect/disconnect, plus the live Calendar and Upcoming
Events widgets (Section 4.2, 9.5 of the spec).

Calendar shows today's agenda; Upcoming Events shows the next 7 days.
Both fall back to the illustrated "not connected" stub state when
app.google_oauth.get_valid_access_token() returns None.
"""

import secrets
import time as time_module

from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, HTMLResponse

from app.database import get_db
from app.templating import templates
from app.routers.admin import require_admin
from app import google_oauth

router = APIRouter()

STATE_COOKIE = "google_oauth_state"


def _callback_redirect_uri(request: Request) -> str:
    return str(request.url_for("google_callback"))


@router.get("/admin/google/connect", dependencies=[Depends(require_admin)])
async def google_connect(request: Request):
    if not google_oauth.is_configured():
        return RedirectResponse(url="/admin", status_code=303)

    state = secrets.token_urlsafe(24)
    auth_url = google_oauth.build_auth_url(state, _callback_redirect_uri(request))
    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600)
    return response


@router.get("/admin/google/callback", name="google_callback", dependencies=[Depends(require_admin)])
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    expected_state = request.cookies.get(STATE_COOKIE)
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie(STATE_COOKIE)

    # Anything off here (denied consent, missing/mismatched state, no code)
    # just bounces back to Admin still disconnected — nothing to salvage.
    if error or not code or not state or not expected_state or state != expected_state:
        return response

    tokens = await google_oauth.exchange_code_for_tokens(code, _callback_redirect_uri(request))
    tokens["expires_at"] = time_module.time() + tokens.get("expires_in", 3600)
    userinfo = await google_oauth.fetch_userinfo(tokens["access_token"])

    async with get_db() as db:
        await google_oauth.store_tokens(db, tokens, userinfo.get("email"))

    return response


@router.post("/admin/google/disconnect", dependencies=[Depends(require_admin)])
async def google_disconnect():
    async with get_db() as db:
        await google_oauth.revoke_and_clear(db)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/widgets/calendar", response_class=HTMLResponse)
async def calendar_widget(request: Request):
    async with get_db() as db:
        events = await google_oauth.get_today_events(db)
    return templates.TemplateResponse(request, "widgets/calendar.html", {"events": events})


@router.get("/widgets/upcoming_events", response_class=HTMLResponse)
async def upcoming_events_widget(request: Request):
    async with get_db() as db:
        events = await google_oauth.get_upcoming_events(db)
    return templates.TemplateResponse(request, "widgets/upcoming_events.html", {"events": events})
