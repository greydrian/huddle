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
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import google_oauth, http_client, sync_status
from app.admin_tabs import admin_url
from app.auth import require_admin
from app.database import get_db
from app.services import calendar_add, calendar_view
from app.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()

STATE_COOKIE = "google_oauth_state"


def _callback_redirect_uri(request: Request) -> str:
    return str(request.url_for("google_callback"))


@router.get("/admin/google/connect", dependencies=[Depends(require_admin)])
async def google_connect(request: Request):
    if not google_oauth.is_configured():
        return RedirectResponse(url=admin_url("google"), status_code=303)

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
    response = RedirectResponse(url=admin_url("google"), status_code=303)
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
    return RedirectResponse(url=admin_url("google"), status_code=303)


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
        # "writable" is only for the School email picker; the selection keeps its old shape.
        selected = [{k: v for k, v in by_id[cid].items() if k != "writable"} for cid in calendar_id if cid in by_id]
        if selected:
            await google_oauth.set_selected_calendars(db, selected)
    return RedirectResponse(url=admin_url("calendars"), status_code=303)


def _date_or_400(value: str | None) -> date_cls | None:
    if not value:
        return None
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date") from None


def _person(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None  # a stale or odd link just shows everyone


def _render(request: Request, context: dict):
    return templates.TemplateResponse(request, "widgets/calendar.html", context)


@router.get("/widgets/calendar", response_class=HTMLResponse)
async def calendar_widget(
    request: Request,
    view: Literal["month", "week", "agenda"] | None = None,
    year: int | None = Query(default=None, ge=1970, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    start: str | None = None,
    person: str | None = None,
):
    """No parameters: the Admin default view for today, unfiltered (the
    widget's idle poll). Otherwise the view and period asked for."""
    week = _date_or_400(start)
    async with get_db() as db:
        context = await calendar_view.widget_context(
            db, view, year=year, month=month, start=week, person=_person(person)
        )
    return _render(request, context)


@router.get("/widgets/calendar/day/{date}", response_class=HTMLResponse)
async def calendar_day_widget(request: Request, date: str, back: str | None = None, person: str | None = None):
    parsed = _date_or_400(date)
    async with get_db() as db:
        context = await calendar_view.widget_context(db, "day", day=parsed, back=back, person=_person(person))
    return _render(request, context)


@router.post("/widgets/calendar/events", response_class=HTMLResponse)
async def add_calendar_event(request: Request):
    """The widget's PIN-free "+" (spec 10.5): adds to the Family calendar
    only (services/calendar_add). The widget re-renders in the view it was
    in: with a note on success, or with the form still open, what was typed
    and a friendly message (200, so htmx swaps it in)."""
    form = await request.form()
    fields = {
        key: str(form.get(key) or "")[:200]
        for key in (
            "title",
            "day",
            "start_time",
            "end_time",
            "person_id",
            "request_key",
            "view",
            "year",
            "month",
            "start",
            "date",
            "back",
            "person",
        )
    }
    view = fields["view"] if fields["view"] in calendar_view.VIEWS else None
    year = int(fields["year"]) if fields["year"].isdigit() and 1970 <= int(fields["year"]) <= 2100 else None
    month = int(fields["month"]) if fields["month"].isdigit() and 1 <= int(fields["month"]) <= 12 else None
    day = calendar_view.parse_date(fields["date"])
    if view == "day" and day is None:
        view = None

    async def render(**extra):
        return await calendar_view.widget_context(
            db,
            view,
            year=year,
            month=month,
            start=calendar_view.parse_date(fields["start"]),
            day=day,
            back=fields["back"] or None,
            person=_person(fields["person"]),
            **extra,
        )

    async with get_db() as db:
        try:
            added = await calendar_add.add_family_event(
                db,
                title=fields["title"],
                day=fields["day"],
                start_time=fields["start_time"],
                end_time=fields["end_time"],
                person_id=fields["person_id"] or None,
                request_key=fields["request_key"],
            )
        except calendar_add.AddEventError as exc:
            context = await render(
                add_error=exc.message,
                add_form={
                    key: fields[key] for key in ("title", "day", "start_time", "end_time", "person_id", "request_key")
                },
            )
        else:
            context = await render(added=added)
    return _render(request, context)
