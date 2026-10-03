"""
The live Calendar widget (Section 4.2, 10.5 of the spec): month grid, week,
agenda and day views, and the PIN-free "+" that adds to the Family calendar.

The widget falls back to the illustrated "not connected" stub state when no
Google account does Calendars. The Calendar reads themselves live in
app/google_calendar.py; connecting accounts and the calendar picker are
Admin's (routers/admin/google.py).
"""

import logging
from datetime import date as date_cls
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import calendar_add, calendar_view
from app.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()


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
