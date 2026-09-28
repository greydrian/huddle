"""
Google OAuth connect/disconnect, the calendar-selection picker, and the
live month-grid Calendar widget (Section 4.2, 9.5 of the spec).

The widget falls back to the illustrated "not connected" stub state when
app.google_oauth.get_valid_access_token() returns None. The Calendar reads
themselves live in app/google_calendar.py.
"""

import logging
import secrets
from datetime import date as date_cls

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import google_calendar, google_oauth, http_client, sync_status
from app.auth import require_admin
from app.database import get_db
from app.templating import templates

logger = logging.getLogger(__name__)
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

    try:
        tokens = await google_oauth.exchange_code_for_tokens(code, _callback_redirect_uri(request))
        tokens["expires_at"] = google_oauth.expires_at(tokens)
        userinfo = await google_oauth.fetch_userinfo(tokens["access_token"])
    except httpx.HTTPError as exc:
        logger.warning("Google OAuth callback failed: %s", http_client.describe(exc))
        return response

    async with get_db() as db:
        await google_oauth.store_tokens(db, tokens, userinfo.get("email"))
        # A fresh grant: whatever the old one's sync failures were, they're over.
        await sync_status.reset(db)

    return response


@router.post("/admin/google/disconnect", dependencies=[Depends(require_admin)])
async def google_disconnect():
    async with get_db() as db:
        await google_oauth.revoke_and_clear(db)
        # A deliberate disconnect isn't a sync fault: forget the history, so
        # the dashboard dot stays hidden (sync_status.summary, "never connected").
        await sync_status.reset(db)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/google/calendars", dependencies=[Depends(require_admin)])
async def save_selected_calendars(calendar_id: list[str] = Form(default=[])):
    async with get_db() as db:
        try:
            access_token = await google_oauth.get_valid_access_token(db)
            # Re-derive summary/colour from Google rather than trusting
            # whatever the submitted form says — the form only tells us
            # which IDs were checked.
            available = await google_oauth.fetch_calendar_list(access_token) if access_token else []
        except httpx.HTTPError as exc:
            logger.warning("Couldn't load the calendar list to save a selection: %s", http_client.describe(exc))
            available = []
        by_id = {cal["id"]: cal for cal in available}
        selected = [by_id[cid] for cid in calendar_id if cid in by_id]
        if selected:
            await google_oauth.set_selected_calendars(db, selected)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/widgets/calendar", response_class=HTMLResponse)
async def calendar_widget(
    request: Request,
    year: int | None = Query(default=None, ge=1970, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
):
    async with get_db() as db:
        calendar_month = await google_calendar.get_month_grid(db, year, month)
    return templates.TemplateResponse(
        request, "widgets/calendar.html", {"view": "month", "calendar_month": calendar_month}
    )


@router.get("/widgets/calendar/day/{date}", response_class=HTMLResponse)
async def calendar_day_widget(request: Request, date: str):
    try:
        parsed = date_cls.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date") from None
    async with get_db() as db:
        day = await google_calendar.get_day_events(db, parsed)
    day_label = parsed.strftime("%A, %d %B").replace(" 0", " ")  # no leading zero, cross-platform
    return templates.TemplateResponse(
        request,
        "widgets/calendar.html",
        {
            "view": "day",
            "day_date": parsed.isoformat(),
            "day_year": parsed.year,
            "day_month": parsed.month,
            "day_label": day_label,
            "day_events": day["events"] if day else None,
            "day_offline": bool(day and day["offline"]),
        },
    )
