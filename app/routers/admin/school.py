"""School tab: the School inbox (uploads, approvals), the schools (spec
11.2) with their term dates and senders, and the school email import's
settings and status."""

import asyncio

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import bank_holidays, google_oauth, school_email
from app.admin_tabs import admin_url
from app.auth import require_admin
from app.database import get_db
from app.routers.admin import common
from app.routers.admin.common import ADMIN_ERRORS, admin_error, tab_context
from app.routers.admin.page import render_admin
from app.services import extraction, homework, imports, school_events, schools, term_dates
from app.templating import templates

router = APIRouter(prefix="/admin")


@tab_context("school")
async def school_context(db, base: dict, extra: dict) -> dict:
    lists = await common.google_lists(db, base["google_account"], tasklists=False)  # calendars only
    return {
        "inbox": await imports.get_inbox(db),
        "inbox_form": extra.get("inbox_form"),
        "inbox_error": extra.get("inbox_error"),
        "school": await school_email.summary(db),
        "event_setup": await _event_setup(db),
        "schools": await schools.list_schools(db),
        "writable_calendars": [c for c in lists["available_calendars"] if c.get("writable")],
        **await _term_dates_context(db),
        "term_form": extra.get("term_form"),
    }


# --- School inbox (app/services/imports.py): nothing reaches the wall unapproved ---
# /admin/inbox/add is also guarded by app/upload_guard.py (session and size
# checked before the body is read). The inbox's own buttons post via HTMX and
# get back just their source's block, so unsaved edits elsewhere survive;
# without HTMX they fall back to a redirect.

MAX_FORM_FIELDS = 10


@router.post("/inbox/add", dependencies=[Depends(require_admin)])
async def inbox_add(request: Request):
    # Parsed here rather than via File()/Form() params, to cap the part count.
    try:
        async with request.form(max_files=extraction.MAX_ATTACHMENTS, max_fields=MAX_FORM_FIELDS) as form:
            uploads = [
                f for f in form.getlist("files") if isinstance(f, StarletteUploadFile) and (f.filename or f.size)
            ]
            text = str(form.get("text") or "").strip()
            child_id = str(form.get("child_id") or "")
            blobs = []
            imports.check_attachment_limits(len(uploads), 0)
            for upload in uploads:
                # Read one byte past the cap, so an oversized file is refused
                # without reading all of it.
                data = await upload.read(extraction.MAX_ATTACHMENT_BYTES + 1)
                if len(data) > extraction.MAX_ATTACHMENT_BYTES:
                    raise imports.UploadRejected("import-too-big")
                blobs.append((upload.filename or "", data))
    except StarletteHTTPException:  # more parts than max_files / max_fields
        return admin_error("import-too-many")
    except imports.UploadRejected as exc:
        return admin_error(exc.code)
    if not blobs and not text:
        return admin_error("import-empty")
    try:
        imports.check_attachment_limits(len(blobs), sum(len(d) for _, d in blobs))
        attachments = [await asyncio.to_thread(imports.prepare_attachment, name, data) for name, data in blobs]
    except imports.UploadRejected as exc:
        return admin_error(exc.code)
    async with get_db() as db:
        children = {c.profile_id for c in await imports.get_children(db)}
    child_hint = int(child_id) if child_id.isdigit() and int(child_id) in children else None
    doc = imports.SourceDocument(
        kind="upload" if attachments else "paste",
        source_ref=imports.content_ref(text, [data for _, data in blobs]),
        text=text[: extraction.MAX_TEXT_CHARS],
        attachments=tuple(attachments),
        subject=", ".join(name for name, _ in blobs if name) or None,
        child_hint=child_hint,
    )
    result = await imports.start_ingest(doc)
    if result.already:
        return admin_error("import-already")
    return RedirectResponse(url=admin_url("inbox"), status_code=303)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


async def _event_setup(db) -> dict:
    """Whether event candidates can be approved (a calendar picked, the
    calendar.events scope granted); _inbox_source.html needs it both from
    the Admin page and from its own fragment route."""
    return {
        "calendar": await school_events.get_target_calendar(db),
        "scope": await google_oauth.has_scope(db, google_oauth.CALENDAR_EVENTS_SCOPE),
        "connected": await google_oauth.get_connected_account(db) is not None,
    }


async def _source_fragment(request: Request, source_id: int, **extra) -> HTMLResponse:
    """One source's inbox block (empty once it's gone). `extra` carries
    inbox_form / inbox_error / source_error after a failed action."""
    async with get_db() as db:
        source = await imports.get_source(db, source_id)
        profiles = [
            dict(r)
            for r in await (
                await db.execute("SELECT id, name, colour_hex FROM profiles ORDER BY sort_order")
            ).fetchall()
        ]
        event_setup = await _event_setup(db)
        school_list = await schools.list_schools(db)
    if source is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "admin/_inbox_source.html",
        {
            "source": source,
            "profiles": profiles,
            "event_setup": event_setup,
            "term_kinds": term_dates.KINDS,
            "schools": school_list,
            **extra,
        },
    )


async def _inbox_done(request: Request, source_id: int | None, error: str | None = None):
    """The response to an inbox action: the source's block for HTMX (with
    the ADMIN_ERRORS message, if any), else a redirect back to Admin."""
    if _is_htmx(request) and source_id is not None:
        return await _source_fragment(request, source_id, source_error=ADMIN_ERRORS[error][1] if error else None)
    return admin_error(error) if error else RedirectResponse(url=admin_url("inbox"), status_code=303)


async def _candidate_source(candidate_id: int) -> int | None:
    return (await _candidate_row(candidate_id))[0]


async def _candidate_row(candidate_id: int) -> tuple[int | None, str | None]:
    """(source id, kind) of a candidate, or (None, None)."""
    async with get_db() as db:
        row = await (
            await db.execute("SELECT source_id, kind FROM import_candidates WHERE id = ?", (candidate_id,))
        ).fetchone()
    return (row["source_id"], row["kind"]) if row else (None, None)


@router.get("/inbox/sources/{source_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def inbox_source(request: Request, source_id: int):
    """One source's inbox block: polled while it says "Reading…"."""
    return await _source_fragment(request, source_id)


@router.post("/inbox/candidates/{candidate_id}/approve", dependencies=[Depends(require_admin)])
async def inbox_approve(
    request: Request,
    candidate_id: int,
    profile_id: str = Form(""),
    title: str = Form(""),
    subject: str = Form(""),
    subject_key: str = Form(""),
    details: str = Form(""),
    due_date: str = Form(""),
    words: str = Form(""),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
    event_date: str = Form("", alias="date"),
    start_time: str = Form(""),
    end_time: str = Form(""),
    notes: str = Form(""),
):
    form = {
        "profile_id": profile_id,
        "title": title,
        "subject": subject,
        "subject_key": subject_key,
        "details": details,
        "due_date": due_date,
        "words": words,
        "starts_on": starts_on,
        "ends_on": ends_on,
        "date": event_date,
        "start_time": start_time,
        "end_time": end_time,
        "notes": notes,
    }
    source_id, kind = await _candidate_row(candidate_id)
    if kind == "term_dates":
        return await _approve_term_dates(request, candidate_id, source_id)
    async with get_db() as db:
        try:
            if kind == "event":
                # Into the Google calendar picked under School email.
                await school_events.approve_event(db, candidate_id, form)
            else:
                await imports.approve_candidate(db, candidate_id, form)
        except imports.CandidateError as exc:
            return await _inbox_done(request, source_id, exc.code)
        except homework.ValidationError as exc:
            inbox_form = {"id": candidate_id, **form}
            if _is_htmx(request) and source_id is not None:
                return await _source_fragment(request, source_id, inbox_form=inbox_form, inbox_error=str(exc))
            return await render_admin(
                request, tab="school", inbox_error=str(exc), inbox_form=inbox_form, status_code=400
            )
    return await _inbox_done(request, source_id)


MAX_TERM_ROWS = extraction.MAX_TERM_PERIODS


async def _approve_term_dates(request: Request, candidate_id: int, source_id: int | None):
    """A term-dates candidate's rows (period_kind/start/end/label, with
    period_include naming the ticked rows) into the term dates."""
    form = await request.form()
    school_field = str(form.get("school_id") or "")
    school_id = schools.parse_id(school_field)
    columns = [form.getlist(f"period_{name}")[:MAX_TERM_ROWS] for name in ("kind", "start", "end", "label")]
    included = set(form.getlist("period_include"))
    rows = [
        {"kind": str(k), "start_date": str(s), "end_date": str(e), "label": str(lab), "include": str(i) in included}
        for i, (k, s, e, lab) in enumerate(zip(*columns, strict=False))
    ]
    async with get_db() as db:
        try:
            if (school_field.strip() and school_id is None) or (
                school_id is not None and not await schools.exists(db, school_id)
            ):
                raise term_dates.PeriodError("term-school")
            await imports.approve_term_dates(db, candidate_id, [r for r in rows if r["include"]], school_id)
        except imports.CandidateError as exc:
            if exc.code != "import-term-none":
                return await _inbox_done(request, source_id, exc.code)
            error = ADMIN_ERRORS[exc.code][1]
        except term_dates.PeriodError as exc:
            error = _period_error(exc)
        else:
            return await _inbox_done(request, source_id)
    inbox_form = {"id": candidate_id, "periods": rows, "school_id": school_id}
    if _is_htmx(request) and source_id is not None:
        return await _source_fragment(request, source_id, inbox_form=inbox_form, inbox_error=error)
    return await render_admin(request, tab="school", inbox_error=error, inbox_form=inbox_form, status_code=400)


@router.post("/inbox/candidates/{candidate_id}/discard", dependencies=[Depends(require_admin)])
async def inbox_discard(request: Request, candidate_id: int):
    source_id = await _candidate_source(candidate_id)
    async with get_db() as db:
        try:
            await imports.discard_candidate(db, candidate_id)
        except imports.CandidateError as exc:
            return await _inbox_done(request, source_id, exc.code)
    return await _inbox_done(request, source_id)


@router.post("/inbox/sources/{source_id}/approve-all", dependencies=[Depends(require_admin)])
async def inbox_approve_all(request: Request, source_id: int):
    async with get_db() as db:
        try:
            await imports.approve_all(db, source_id)
        except imports.CandidateError as exc:
            return await _inbox_done(request, source_id, exc.code)
    return await _inbox_done(request, source_id)


@router.post("/inbox/sources/{source_id}/retry", dependencies=[Depends(require_admin)])
async def inbox_retry_source(request: Request, source_id: int):
    """A school email Claude gave up on: read it again (a check starts now)."""
    async with get_db() as db:
        try:
            await imports.retry_source(db, source_id)
        except imports.CandidateError as exc:
            return await _inbox_done(request, source_id, exc.code)
    school_email.start_check()
    return await _inbox_done(request, source_id)


@router.post("/inbox/sources/{source_id}/delete", dependencies=[Depends(require_admin)])
async def inbox_delete_source(request: Request, source_id: int):
    async with get_db() as db:
        try:
            await imports.delete_source(db, source_id)
        except imports.CandidateError as exc:
            return await _inbox_done(request, source_id, exc.code)
    return await _inbox_done(request, source_id)


# --- Term dates (app/services/term_dates.py, spec 10.6) ---


async def _term_dates_context(db) -> dict:
    """The School tab's Term dates panel: each school's periods by school
    year, the years with none (the warning), and when the bank holidays
    were last updated."""
    holidays = await bank_holidays.status(db)
    today = await common.family_today(db)
    if holidays["updated_at"]:
        local = holidays["updated_at"].astimezone(await common.family_timezone(db))
        holidays["updated_label"] = f"{local.day} {local:%b %Y}"
    term_schools = [
        {
            "id": school["id"],
            "name": school["name"],
            "years": term_dates.group_by_school_year(await term_dates.list_periods(db, school["id"])),
            "missing_years": await term_dates.missing_years(db, today, school["id"]),
            "incomplete": await term_dates.incomplete_years(db, today, school["id"]),
        }
        for school in await schools.list_schools(db)
    ]
    return {
        "term_schools": term_schools,
        "term_kinds": term_dates.KINDS,
        "bank_holidays": holidays,
    }


def _period_error(exc: term_dates.PeriodError) -> str:
    """The message for a period that couldn't be saved: a clash names the
    period it clashes with (a stored or listed one, never URL text)."""
    if exc.code == "term-overlap" and exc.clash:
        return term_dates.clash_message(exc.clash)
    return ADMIN_ERRORS[exc.code][1]


def _term_form(
    period_id: int | None, kind: str, start_date: str, end_date: str, label: str, school_id: str = ""
) -> dict:
    return {
        "id": period_id,
        "kind": kind,
        "start_date": start_date,
        "end_date": end_date,
        "label": label,
        "school_id": school_id,
    }


@router.post("/term-dates", dependencies=[Depends(require_admin)])
async def add_term_period(
    request: Request,
    kind: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    label: str = Form(""),
    school_id: str = Form(""),
):
    async with get_db() as db:
        try:
            # Blank: the oldest school (the form sends one when there are two or more).
            if school_id.strip() and not await schools.exists(db, school_id):
                raise term_dates.PeriodError("term-school")
            chosen = schools.parse_id(school_id)
            await term_dates.add_period(db, kind, start_date, end_date, label, chosen)
        except term_dates.PeriodError as exc:
            return await render_admin(
                request,
                tab="school",
                error=exc.code,
                status_code=400,
                error_message=_period_error(exc),
                term_form=_term_form(None, kind, start_date, end_date, label, school_id),
            )
    return RedirectResponse(url=admin_url("term-dates"), status_code=303)


@router.post("/term-dates/{period_id}/edit", dependencies=[Depends(require_admin)])
async def edit_term_period(
    request: Request,
    period_id: int,
    kind: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    label: str = Form(""),
):
    async with get_db() as db:
        try:
            await term_dates.update_period(db, period_id, kind, start_date, end_date, label)
        except term_dates.PeriodError as exc:
            if exc.code == "term-missing":
                return admin_error(exc.code)
            return await render_admin(
                request,
                tab="school",
                error=exc.code,
                status_code=400,
                error_message=_period_error(exc),
                term_form=_term_form(period_id, kind, start_date, end_date, label),
            )
    return RedirectResponse(url=admin_url("term-dates"), status_code=303)


@router.post("/term-dates/{period_id}/delete", dependencies=[Depends(require_admin)])
async def delete_term_period(period_id: int):
    async with get_db() as db:
        await term_dates.delete_period(db, period_id)
    return RedirectResponse(url=admin_url("term-dates"), status_code=303)


# --- School email (app/school_email.py) ---


@router.post("/school-email/schedule", dependencies=[Depends(require_admin)])
async def save_school_email_schedule(mode: str = Form(""), time: str = Form("")):
    try:
        schedule = school_email.parse_schedule(mode, time)
    except ValueError:
        return admin_error("school-schedule")
    async with get_db() as db:
        await school_email.set_schedule(db, schedule)
    return RedirectResponse(url=admin_url("school-email"), status_code=303)


@router.post("/school-email/exclusions", dependencies=[Depends(require_admin)])
async def save_school_email_exclusions(exclusions: str = Form("")):
    """Addresses never read, whichever school's senders they match. Each
    school's own senders are under Schools."""
    try:
        deny = school_email.parse_entries(exclusions)
    except school_email.InvalidEntry:
        return admin_error("school-senders")
    async with get_db() as db:
        await school_email.set_exclusions(db, deny)
    return RedirectResponse(url=admin_url("school-email"), status_code=303)


# --- Schools (app/services/schools.py, spec 11.2) ---


@router.post("/schools", dependencies=[Depends(require_admin)])
async def add_school(name: str = Form("")):
    async with get_db() as db:
        try:
            await schools.add(db, name)
        except schools.SchoolError as exc:
            return admin_error(str(exc))
    return RedirectResponse(url=admin_url("schools"), status_code=303)


@router.post("/schools/{school_id}/edit", dependencies=[Depends(require_admin)])
async def edit_school(school_id: int, name: str = Form(""), senders: str = Form("")):
    async with get_db() as db:
        try:
            await schools.update(db, school_id, name, senders)
        except schools.SchoolError as exc:
            return admin_error(str(exc))
    return RedirectResponse(url=admin_url("schools"), status_code=303)


@router.post("/schools/{school_id}/delete", dependencies=[Depends(require_admin)])
async def delete_school(school_id: int):
    """Its term dates go with it; its children are left with no school."""
    async with get_db() as db:
        await schools.delete(db, school_id)
    return RedirectResponse(url=admin_url("schools"), status_code=303)


@router.post("/school-email/calendar", dependencies=[Depends(require_admin)])
async def save_school_events_calendar(calendar_id: str = Form("")):
    async with get_db() as db:
        if not calendar_id:
            await school_events.set_target_calendar(db, None)
            return RedirectResponse(url=admin_url("school-email"), status_code=303)
        try:
            access_token = await google_oauth.get_valid_access_token(db)
            # Re-derive the name and access from Google rather than trusting the form.
            available = await google_oauth.fetch_calendar_list(access_token) if access_token else []
        except httpx.HTTPError:
            available = []
        match = next((c for c in available if c["id"] == calendar_id and c["writable"]), None)
        if match is None:
            return admin_error("school-calendar")
        await school_events.set_target_calendar(db, match)
    return RedirectResponse(url=admin_url("school-email"), status_code=303)


@router.post("/school-email/check", dependencies=[Depends(require_admin)])
async def check_school_email_now():
    """Starts one check in the background (it can take minutes: up to
    MAX_MESSAGES_PER_RUN Claude reads). If a check is already running, this
    doesn't start a second one; Admin says so instead."""
    status = "started" if school_email.start_check() else "busy"
    return RedirectResponse(url=admin_url("school-email", school_email=status), status_code=303)


@router.get("/school-email/status", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def school_email_status(request: Request):
    """The School email status line: polled while a check is running."""
    async with get_db() as db:
        school = await school_email.summary(db)
    return templates.TemplateResponse(request, "admin/_school_email_status.html", {"school": school})
