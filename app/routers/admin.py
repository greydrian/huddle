"""
Admin / Parent Controls (Section 4.7): single shared PIN, exponential backoff
on failed attempts, short-lived signed session cookie. Manages family
profiles, task schedules, and (eventually) Google account connections.
"""

import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app import appearance, backup, google_oauth, google_tasks, recurrence, sync_status, task_sync
from app.auth import (
    NEW_PIN_PATH,
    PIN_IS_DEFAULT_SETTING,
    SESSION_COOKIE,
    bump_session_generation,
    end_session,
    has_valid_session,
    require_admin,
    require_session,
    session_generation,
    start_session,
)
from app.database import family_timezone, family_today, get_db, get_setting, set_onscreen_keyboard, set_setting
from app.security import (
    FAILURE_DECAY_SECONDS,
    LONG_LOCKOUT_AFTER,
    hash_pin,
    is_weak_pin,
    lockout_seconds_for,
    verify_pin,
)
from app.services import extraction, homework, imports, weather
from app.services import tasks as task_service
from app.templating import templates

# Re-exported: older callers and tests import these from here.
__all__ = ["SESSION_COOKIE", "require_admin", "router"]

router = APIRouter(prefix="/admin")

MAX_SCHOOL_YEAR = 30  # "Year 4", "Reception", "Year 6 (Oak Class)"

# A form that can't be saved redirects back to its Admin section with
# ?error=<code> (like ?weather_error=), and the page shows that section's
# message. Only these fixed messages are shown, never text from the URL.
ADMIN_ERRORS = {
    "task-missing": ("tasks", "That task no longer exists. It may have been deleted in Google Tasks."),
    "task-unknown-person": ("tasks", "That family member no longer exists."),
    "task-no-list": ("tasks", "That family member has no Google list linked yet, so the task can't move to "
                              "them. Link one under Google Account → Task Sync first."),
    "homework-missing": ("homework", "That homework no longer exists. It may have just been deleted."),
    "words-missing": ("practice-words", "That word list no longer exists. It may have just been deleted."),
    "handwriting-style": ("practice-words", "Pick one of the handwriting styles."),
    "appearance": ("display", "Pick one of the appearance options."),
    "pin-invalid": ("pin", "A PIN must be 4 to 8 digits, numbers only. Your PIN hasn't changed."),
    "backup-failed": ("backups", "The backup didn't complete. Check the logs (docker compose logs)."),
    "pin-weak": ("pin", "That PIN is too easy to guess: avoid one digit repeated (like 0000) or a "
                        "straight run (like 1234 or 9876). Your PIN hasn't changed."),
    "pin-mismatch": ("pin", "The two PINs didn't match. Your PIN hasn't changed."),
    "profile-missing": ("family", "That family member no longer exists."),
    "profile-year": ("family", f"A year group can be at most {MAX_SCHOOL_YEAR} characters."),
    "import-empty": ("classroom", "Add a screenshot, a PDF or some pasted text first."),
    "import-too-big": ("classroom", "That's too big to read: at most 15 MB a file and 22 MB in all. "
                                    "Try a smaller screenshot or fewer pages."),
    "import-too-many": ("classroom", f"Add at most {extraction.MAX_ATTACHMENTS} files at a time."),
    "import-bad-type": ("classroom", "That file isn't one the inbox can read. Use a screenshot or photo "
                                     "(PNG, JPEG, WebP or HEIC) or a PDF."),
    "import-already": ("inbox", "That's already in the School inbox below."),
    "import-missing": ("inbox", "That item is no longer waiting in the inbox. It may have just been "
                                "approved or discarded."),
    "import-event": ("inbox", "Adding events to the calendar arrives with the Gmail import. "
                              "Discard this one for now."),
    "import-busy": ("inbox", "That's still being read. Try again in a moment."),
}

# The login form only takes digits (inputmode=numeric, maxlength=8), so a PIN
# it can't type would lock the family out of Admin.
PIN_PATTERN = re.compile(r"[0-9]{4,8}")


def _admin_error(code: str) -> RedirectResponse:
    section, _ = ADMIN_ERRORS[code]
    return RedirectResponse(url=f"/admin?error={code}#{section}", status_code=303)


def _form_error(code: str | None) -> dict | None:
    if code not in ADMIN_ERRORS:
        return None
    section, message = ADMIN_ERRORS[code]
    return {"section": section, "message": message}


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    # Lockouts stored before timestamps became tz-aware are naive UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Serialises the PIN check and the lockout update: without it, parallel
# guesses all read the lockout state before any of them records a failure,
# and the backoff never applies. Single uvicorn process (see scheduler.py).
# An asyncio.Lock belongs to one event loop, so it's made per loop (the app
# only ever has one; the tests start a fresh loop per test).
_login_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def _login_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _login_locks:
        _login_locks.clear()  # drop locks for loops that have gone
        _login_locks[loop] = asyncio.Lock()
    return _login_locks[loop]


def _format_wait(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes} min {secs}s" if secs else f"{minutes} min"


def _lockout_message(failed_attempts: int, wait_seconds: int) -> str:
    if failed_attempts >= LONG_LOCKOUT_AFTER:
        return (f"Admin is locked after {failed_attempts} wrong PINs in a row. "
                f"Try again in {_format_wait(wait_seconds)}.")
    return f"Too many attempts. Try again in {_format_wait(wait_seconds)}."


async def _active_lockout(db) -> tuple[dict, int]:
    """The stored lockout state and the whole seconds left on it (0 if none)."""
    lockout = json.loads(await get_setting(db, "pin_lockout", "{}"))
    locked_until = lockout.get("locked_until")
    if not locked_until:
        return lockout, 0
    remaining = (_parse_utc(locked_until) - datetime.now(timezone.utc)).total_seconds()
    # Round up, so the screen never says "0s" while still locked.
    return lockout, max(0, math.ceil(remaining))


def _failures_have_decayed(lockout: dict, now: datetime) -> bool:
    """True once the last failure is FAILURE_DECAY_SECONDS old. States saved
    before last_failed_at existed fall back to locked_until, which is never
    earlier than the failure that set it."""
    last = lockout.get("last_failed_at") or lockout.get("locked_until")
    if not last:
        return False
    return (now - _parse_utc(last)).total_seconds() >= FAILURE_DECAY_SECONDS


async def _login_page(request: Request, error: str | None, status_code: int = 200):
    async with get_db() as db:
        mode = await appearance.current_mode(db)
    return templates.TemplateResponse(
        request, "admin/login.html", {"error": error, "appearance": mode}, status_code=status_code
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    async with get_db() as db:
        lockout, wait = await _active_lockout(db)
    error = _lockout_message(lockout.get("failed_attempts", 0), wait) if wait else None
    return await _login_page(request, error)


@router.post("/login")
async def login_submit(request: Request, pin: str = Form(...)):
    async with _login_lock(), get_db() as db:
        lockout, wait = await _active_lockout(db)
        failed_attempts = lockout.get("failed_attempts", 0)
        if wait:
            # Refused by the lockout: doesn't count as another failure.
            return await _login_page(request, _lockout_message(failed_attempts, wait), 429)

        stored_hash = await get_setting(db, "pin_hash")
        if stored_hash and verify_pin(pin, stored_hash):
            await set_setting(db, "pin_lockout", json.dumps({}))
            await db.commit()
            response = RedirectResponse(url="/admin", status_code=303)
            start_session(response, request, await session_generation(db))
            return response

        # Failed attempt: bump the counter and set an exponential-backoff
        # lockout. After a quiet day the count starts again, so stray typos
        # weeks apart never add up to the long lockout. (Someone guessing
        # every 15 minutes keeps it; `python -m app.reset_pin` is the way out.)
        now = datetime.now(timezone.utc)
        if _failures_have_decayed(lockout, now):
            failed_attempts = 0
        failed_attempts += 1
        wait = lockout_seconds_for(failed_attempts)
        await set_setting(
            db,
            "pin_lockout",
            json.dumps({
                "failed_attempts": failed_attempts,
                "locked_until": (now + timedelta(seconds=wait)).isoformat(),
                "last_failed_at": now.isoformat(),
            }),
        )
        await db.commit()

    if failed_attempts >= LONG_LOCKOUT_AFTER:
        message = f"Incorrect PIN. {_lockout_message(failed_attempts, wait)}"
    else:
        message = f"Incorrect PIN. Try again in {_format_wait(wait)}." if wait else "Incorrect PIN."
    return await _login_page(request, message, 401)


@router.post("/logout")
async def logout(request: Request):
    # Bumping the generation ends every session, including any copy of this
    # cookie. Only a signed-in session can do that.
    if await has_valid_session(request):
        async with get_db() as db:
            await bump_session_generation(db)
            await db.commit()
    response = RedirectResponse(url="/admin/login", status_code=303)
    end_session(response)
    return response


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_home(request: Request, weather_error: str | None = None, error: str | None = None):
    return await _render_admin(request, weather_error=weather_error, error=error)


async def _render_admin(
    request: Request,
    weather_error: str | None = None,
    error: str | None = None,
    homework_error: str | None = None,
    words_error: str | None = None,
    homework_form: dict | None = None,
    words_form: dict | None = None,
    inbox_form: dict | None = None,
    inbox_error: str | None = None,
    status_code: int = 200,
):
    """Renders Admin. After a validation error, *_form carries what was
    submitted (with "id" None for the add form, else the row being edited)
    so nothing typed is lost."""
    async with get_db() as db:
        profiles = [dict(r) for r in await (await db.execute(
            "SELECT * FROM profiles ORDER BY sort_order"
        )).fetchall()]
        tasks = await task_service.get_admin_tasks(db)
        today = await family_today(db)
        homework_items, finished_homework = await homework.get_admin_homework(db, today)
        word_lists = await homework.get_admin_word_lists(db)
        handwriting_style = await homework.get_handwriting_style(db)
        google_account = await google_oauth.get_connected_account(db)
        weather_location = await weather.get_location(db)
        current_appearance = await appearance.get_appearance(db)
        mode = await appearance.current_mode(db)
        backup_status = backup.status(await family_timezone(db))
        sync = await sync_status.summary(db)
        inbox = await imports.get_inbox(db)

        available_calendars = []
        selected_calendar_ids = []
        available_tasklists = []
        tasklists_error = False
        google_offline = False
        shopping_tasklist = None
        if google_account:
            try:
                access_token = await google_oauth.get_valid_access_token(db)
            except httpx.HTTPError:
                access_token, google_offline = None, True
            if access_token:
                try:
                    available_calendars = await google_oauth.fetch_calendar_list(access_token)
                except httpx.HTTPError:
                    google_offline = True
                try:
                    available_tasklists = await google_tasks.fetch_tasklists(access_token)
                except httpx.HTTPStatusError as exc:
                    # 403 = this account connected before the `tasks` scope
                    # existed — settings.html shows a reconnect prompt.
                    if exc.response.status_code == 403:
                        tasklists_error = True
                    else:
                        google_offline = True
                except httpx.HTTPError:
                    google_offline = True
            selected_calendar_ids = [c["id"] for c in await google_oauth.get_selected_calendars(db)]
            shopping_tasklist = await task_sync.get_shopping_tasklist(db)

    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {
            "profiles": profiles,
            "tasks": tasks,
            "google_account": google_account,
            "google_configured": google_oauth.is_configured(),
            "available_calendars": available_calendars,
            "selected_calendar_ids": selected_calendar_ids,
            "available_tasklists": available_tasklists,
            "tasklists_error": tasklists_error,
            "google_offline": google_offline,
            "shopping_tasklist": shopping_tasklist,
            "weather_location": weather_location,
            "weather_error": weather_error,
            "form_error": _form_error(error),
            "homework_items": homework_items,
            "finished_homework": finished_homework,
            "homework_form": homework_form,
            "words_form": words_form,
            "word_lists": word_lists,
            "homework_error": homework_error,
            "words_error": words_error,
            "handwriting_style": handwriting_style,
            "handwriting_styles": homework.HANDWRITING_STYLES,
            "appearance": mode,
            "appearance_setting": current_appearance,
            "appearances": appearance.APPEARANCES,
            "weekdays": recurrence.WEEKDAYS,
            "backup_status": backup_status,
            "sync": sync,
            "inbox": inbox,
            "inbox_configured": extraction.is_configured(),
            "inbox_form": inbox_form,
            "inbox_error": inbox_error,
        },
        status_code=status_code,
    )


# --- Family member management ---

@router.post("/profiles", dependencies=[Depends(require_admin)])
async def add_profile(name: str = Form(...), colour_hex: str = Form(...)):
    async with get_db() as db:
        cursor = await db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM profiles")
        (next_order,) = await cursor.fetchone()
        await db.execute(
            "INSERT INTO profiles (name, colour_hex, sort_order) VALUES (?, ?, ?)",
            (name.strip(), colour_hex, next_order),
        )
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/profiles/{profile_id}/school-year", dependencies=[Depends(require_admin)])
async def save_school_year(profile_id: int, school_year: str = Form("")):
    """A child's year group ("Year 4"): the school inbox uses it to decide
    whose spellings and homework are whose. Blank clears it."""
    try:
        value = homework.clean_text(school_year, "Year group", MAX_SCHOOL_YEAR) or None
    except homework.ValidationError:
        return _admin_error("profile-year")
    async with get_db() as db:
        cursor = await db.execute("UPDATE profiles SET school_year = ? WHERE id = ?", (value, profile_id))
        await db.commit()
    if not cursor.rowcount:
        return _admin_error("profile-missing")
    return RedirectResponse(url="/admin#family", status_code=303)


@router.post("/profiles/{profile_id}/delete", dependencies=[Depends(require_admin)])
async def delete_profile(profile_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# --- Task schedules ---
# Tasks are added, renamed and deleted in Google Tasks; its API has no
# recurrence, so which days a task repeats (and whose it is) is set here.

@router.post("/tasks/{task_id}/edit", dependencies=[Depends(require_admin)])
async def edit_task(
    task_id: int,
    profile_id: int = Form(...),
    is_recurring: bool = Form(False),
    days: list[str] = Form(default=[]),
):
    # Ticking any day implies the task repeats; "Repeats" with no days ticked
    # means every day. Rules are only stored for recurring tasks.
    recurring = is_recurring or bool(days)
    rule = recurrence.normalize_rule(days) if recurring else None
    async with get_db() as db:
        task = await (await db.execute(
            "SELECT profile_id FROM tasks WHERE id = ? AND archived = 0", (task_id,)
        )).fetchone()
        if task is None:
            return _admin_error("task-missing")
        if profile_id == task["profile_id"]:
            # The schedule is local-only (never pushed, never reconciled), so
            # don't bump updated_at or queue a push: that would overwrite a
            # newer rename/tick in Google with this row's stale copy.
            await db.execute(
                "UPDATE tasks SET is_recurring = ?, recurrence_rule = ? WHERE id = ?",
                (int(recurring), rule, task_id),
            )
        else:
            target = await (await db.execute(
                "SELECT google_tasklist_id FROM profiles WHERE id = ?", (profile_id,)
            )).fetchone()
            if target is None:
                return _admin_error("task-unknown-person")
            if not target["google_tasklist_id"]:
                # Its Google copy would be deleted, leaving a task nothing can rename or remove.
                return _admin_error("task-no-list")
            await task_sync.detach_from_profile_list(db, task_id)
            await db.execute(
                """UPDATE tasks SET profile_id = ?, is_recurring = ?, recurrence_rule = ?,
                       updated_at = datetime('now') WHERE id = ?""",
                (profile_id, int(recurring), rule, task_id),
            )
            await task_sync.queue_sync(db, "tasks", {"task_id": task_id})
        await db.commit()
    return RedirectResponse(url="/admin#tasks", status_code=303)


# --- Homework + practice words (not synced to Google) ---

@router.post("/homework", dependencies=[Depends(require_admin)])
async def add_homework(
    request: Request,
    profile_id: str = Form(""),
    subject: str = Form(""),
    title: str = Form(""),
    details: str = Form(""),
    due_date: str = Form(""),
):
    homework_id = None
    async with get_db() as db:
        try:
            fields = await homework.homework_fields(db, profile_id, subject, title, details, due_date)
        except homework.ValidationError as exc:
            return await _render_admin(request, homework_error=str(exc), status_code=400, homework_form={
                "id": homework_id, "profile_id": profile_id, "subject": subject, "title": title,
                "details": details, "due_date": due_date,
            })
        await db.execute(
            "INSERT INTO homework (profile_id, subject, title, details, due_date) VALUES (?, ?, ?, ?, ?)", fields
        )
        await db.commit()
    return RedirectResponse(url="/admin#homework", status_code=303)


@router.post("/homework/{homework_id}/edit", dependencies=[Depends(require_admin)])
async def edit_homework(
    request: Request,
    homework_id: int,
    profile_id: str = Form(""),
    subject: str = Form(""),
    title: str = Form(""),
    details: str = Form(""),
    due_date: str = Form(""),
):
    async with get_db() as db:
        if not await (await db.execute("SELECT 1 FROM homework WHERE id = ?", (homework_id,))).fetchone():
            return _admin_error("homework-missing")
        try:
            fields = await homework.homework_fields(db, profile_id, subject, title, details, due_date)
        except homework.ValidationError as exc:
            return await _render_admin(request, homework_error=str(exc), status_code=400, homework_form={
                "id": homework_id, "profile_id": profile_id, "subject": subject, "title": title,
                "details": details, "due_date": due_date,
            })
        await db.execute(
            """UPDATE homework SET profile_id = ?, subject = ?, title = ?, details = ?, due_date = ?,
                   updated_at = datetime('now') WHERE id = ?""",
            (*fields, homework_id),
        )
        await db.commit()
    return RedirectResponse(url="/admin#homework", status_code=303)


@router.post("/homework/{homework_id}/archive", dependencies=[Depends(require_admin)])
async def archive_homework(homework_id: int):
    """Toggles: archived homework leaves the dashboard; restoring brings it back."""
    async with get_db() as db:
        await db.execute(
            "UPDATE homework SET archived = 1 - archived, updated_at = datetime('now') WHERE id = ?", (homework_id,)
        )
        await db.commit()
    return RedirectResponse(url="/admin#homework", status_code=303)


@router.post("/homework/{homework_id}/delete", dependencies=[Depends(require_admin)])
async def delete_homework(homework_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM homework WHERE id = ?", (homework_id,))
        await db.commit()
    return RedirectResponse(url="/admin#homework", status_code=303)



@router.post("/practice-words", dependencies=[Depends(require_admin)])
async def add_word_list(
    request: Request,
    profile_id: str = Form(""),
    title: str = Form(""),
    words: str = Form(""),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
):
    list_id = None
    async with get_db() as db:
        try:
            fields = await homework.word_list_fields(db, profile_id, title, words, starts_on, ends_on)
        except homework.ValidationError as exc:
            return await _render_admin(request, words_error=str(exc), status_code=400, words_form={
                "id": list_id, "profile_id": profile_id, "title": title, "words": words,
                "starts_on": starts_on, "ends_on": ends_on,
            })
        await db.execute(
            "INSERT INTO practice_word_lists (profile_id, title, words, starts_on, ends_on) VALUES (?, ?, ?, ?, ?)",
            fields,
        )
        await db.commit()
    return RedirectResponse(url="/admin#practice-words", status_code=303)


@router.post("/practice-words/{list_id}/edit", dependencies=[Depends(require_admin)])
async def edit_word_list(
    request: Request,
    list_id: int,
    profile_id: str = Form(""),
    title: str = Form(""),
    words: str = Form(""),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
):
    async with get_db() as db:
        if not await (await db.execute("SELECT 1 FROM practice_word_lists WHERE id = ?", (list_id,))).fetchone():
            return _admin_error("words-missing")
        try:
            fields = await homework.word_list_fields(db, profile_id, title, words, starts_on, ends_on)
        except homework.ValidationError as exc:
            return await _render_admin(request, words_error=str(exc), status_code=400, words_form={
                "id": list_id, "profile_id": profile_id, "title": title, "words": words,
                "starts_on": starts_on, "ends_on": ends_on,
            })
        await db.execute(
            """UPDATE practice_word_lists SET profile_id = ?, title = ?, words = ?, starts_on = ?, ends_on = ?,
                   updated_at = datetime('now') WHERE id = ?""",
            (*fields, list_id),
        )
        await db.commit()
    return RedirectResponse(url="/admin#practice-words", status_code=303)


@router.post("/practice-words/{list_id}/archive", dependencies=[Depends(require_admin)])
async def archive_word_list(list_id: int):
    """Toggles: an archived list leaves the dashboard; restoring brings it back."""
    async with get_db() as db:
        await db.execute(
            "UPDATE practice_word_lists SET archived = 1 - archived, updated_at = datetime('now') WHERE id = ?",
            (list_id,),
        )
        await db.commit()
    return RedirectResponse(url="/admin#practice-words", status_code=303)


@router.post("/practice-words/{list_id}/delete", dependencies=[Depends(require_admin)])
async def delete_word_list(list_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM practice_word_lists WHERE id = ?", (list_id,))
        await db.commit()
    return RedirectResponse(url="/admin#practice-words", status_code=303)


@router.post("/handwriting-style", dependencies=[Depends(require_admin)])
async def save_handwriting_style(style: str = Form("")):
    if style not in homework.HANDWRITING_STYLES:
        return _admin_error("handwriting-style")
    async with get_db() as db:
        await set_setting(db, homework.HANDWRITING_SETTING, style)
        await db.commit()
    return RedirectResponse(url="/admin#practice-words", status_code=303)


# --- School inbox (app/services/imports.py): nothing reaches the wall unapproved ---

@router.post("/inbox/add", dependencies=[Depends(require_admin)])
async def inbox_add(
    files: list[UploadFile] = File(default=[]),
    text: str = Form(""),
    child_id: str = Form(""),
):
    # An empty file input still posts one nameless, empty part.
    uploads = [f for f in files if f.filename or f.size]
    text = text.strip()
    if not uploads and not text:
        return _admin_error("import-empty")
    blobs = []
    try:
        imports.check_attachment_limits(len(uploads), 0)
        for upload in uploads:
            # Read one byte past the cap, so an oversized file is refused
            # without reading all of it.
            data = await upload.read(extraction.MAX_ATTACHMENT_BYTES + 1)
            if len(data) > extraction.MAX_ATTACHMENT_BYTES:
                raise imports.UploadRejected("import-too-big")
            blobs.append((upload.filename or "", data))
        imports.check_attachment_limits(len(blobs), sum(len(d) for _, d in blobs))
        attachments = [await asyncio.to_thread(imports.prepare_attachment, name, data) for name, data in blobs]
    except imports.UploadRejected as exc:
        return _admin_error(exc.code)
    async with get_db() as db:
        children = {c.profile_id for c in await imports.get_children(db)}
    child_hint = int(child_id) if child_id.isdigit() and int(child_id) in children else None
    doc = imports.SourceDocument(
        kind="upload" if attachments else "paste",
        source_ref=imports.content_ref(text, [data for _, data in blobs]),
        text=text[:extraction.MAX_TEXT_CHARS],
        attachments=tuple(attachments),
        subject=", ".join(name for name, _ in blobs if name) or None,
        child_hint=child_hint,
    )
    result = await imports.start_ingest(doc)
    if result.already:
        return _admin_error("import-already")
    return RedirectResponse(url="/admin#inbox", status_code=303)


@router.get("/inbox/sources/{source_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def inbox_source(request: Request, source_id: int):
    """One source's inbox block: polled while it says "Reading…"."""
    async with get_db() as db:
        source = await imports.get_source(db, source_id)
        profiles = [dict(r) for r in await (await db.execute(
            "SELECT id, name, colour_hex FROM profiles ORDER BY sort_order"
        )).fetchall()]
    if source is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, "admin/_inbox_source.html", {"source": source, "profiles": profiles}
    )


@router.post("/inbox/candidates/{candidate_id}/approve", dependencies=[Depends(require_admin)])
async def inbox_approve(
    request: Request,
    candidate_id: int,
    profile_id: str = Form(""),
    title: str = Form(""),
    subject: str = Form(""),
    details: str = Form(""),
    due_date: str = Form(""),
    words: str = Form(""),
    starts_on: str = Form(""),
    ends_on: str = Form(""),
):
    form = {
        "profile_id": profile_id, "title": title, "subject": subject, "details": details,
        "due_date": due_date, "words": words, "starts_on": starts_on, "ends_on": ends_on,
    }
    async with get_db() as db:
        try:
            await imports.approve_candidate(db, candidate_id, form)
        except imports.CandidateError as exc:
            return _admin_error(exc.code)
        except homework.ValidationError as exc:
            return await _render_admin(
                request, inbox_error=str(exc), inbox_form={"id": candidate_id, **form}, status_code=400
            )
    return RedirectResponse(url="/admin#inbox", status_code=303)


@router.post("/inbox/candidates/{candidate_id}/discard", dependencies=[Depends(require_admin)])
async def inbox_discard(candidate_id: int):
    async with get_db() as db:
        try:
            await imports.discard_candidate(db, candidate_id)
        except imports.CandidateError as exc:
            return _admin_error(exc.code)
    return RedirectResponse(url="/admin#inbox", status_code=303)


@router.post("/inbox/sources/{source_id}/approve-all", dependencies=[Depends(require_admin)])
async def inbox_approve_all(source_id: int):
    async with get_db() as db:
        try:
            await imports.approve_all(db, source_id)
        except imports.CandidateError as exc:
            return _admin_error(exc.code)
    return RedirectResponse(url="/admin#inbox", status_code=303)


@router.post("/inbox/sources/{source_id}/delete", dependencies=[Depends(require_admin)])
async def inbox_delete_source(source_id: int):
    async with get_db() as db:
        try:
            await imports.delete_source(db, source_id)
        except imports.CandidateError as exc:
            return _admin_error(exc.code)
    return RedirectResponse(url="/admin#inbox", status_code=303)


# --- Google Tasks sync ---

@router.post("/google/shopping-list", dependencies=[Depends(require_admin)])
async def save_shopping_tasklist(tasklist_id: str = Form(...)):
    async with get_db() as db:
        try:
            access_token = await google_oauth.get_valid_access_token(db)
            # Re-derive the title from Google rather than trusting the form.
            available = await google_tasks.fetch_tasklists(access_token) if access_token else []
        except httpx.HTTPError:
            available = []
        match = next((t for t in available if t["id"] == tasklist_id), None)
        current = await task_sync.get_shopping_tasklist(db)
        if match and (current is None or current["id"] != match["id"]):
            await task_sync.set_shopping_tasklist(db, match)
            await task_sync.relink_shopping(db)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/google/task-lists", dependencies=[Depends(require_admin)])
async def save_profile_tasklists(request: Request):
    form = await request.form()
    async with get_db() as db:
        profiles = await (await db.execute("SELECT id, google_tasklist_id FROM profiles")).fetchall()
        for profile in profiles:
            tasklist_id = form.get(f"tasklist_{profile['id']}") or None
            if tasklist_id == profile["google_tasklist_id"]:
                continue
            await db.execute(
                "UPDATE profiles SET google_tasklist_id = ? WHERE id = ?", (tasklist_id, profile["id"])
            )
            await task_sync.relink_profile(db, profile["id"])
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# --- Weather ---

@router.post("/weather-location", dependencies=[Depends(require_admin)])
async def save_weather_location(place: str = Form(...)):
    try:
        location = await weather.geocode(place)
    # A malformed geocoder response is treated like an outage, never a 500.
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError):
        return RedirectResponse(url="/admin?weather_error=offline#weather", status_code=303)
    if not location:
        return RedirectResponse(url="/admin?weather_error=notfound#weather", status_code=303)
    async with get_db() as db:
        await weather.set_location(db, location)
    return RedirectResponse(url="/admin#weather", status_code=303)


# --- Display ---

@router.post("/onscreen-keyboard", dependencies=[Depends(require_admin)])
async def save_onscreen_keyboard(enabled: bool = Form(False)):
    async with get_db() as db:
        await set_onscreen_keyboard(db, enabled)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/appearance", dependencies=[Depends(require_admin)])
async def save_appearance(value: str = Form("")):
    if value not in appearance.APPEARANCES:
        return _admin_error("appearance")
    async with get_db() as db:
        await appearance.set_appearance(db, value)
    return RedirectResponse(url="/admin#display", status_code=303)


# --- PIN management ---

async def _save_new_pin(new_pin: str, confirm_pin: str | None) -> tuple[str | None, int]:
    """Validates and stores a new PIN, clears the default-PIN flag and ends
    every session. Returns (error code or None, new session generation)."""
    if not PIN_PATTERN.fullmatch(new_pin):
        return "pin-invalid", 0
    if is_weak_pin(new_pin):
        return "pin-weak", 0
    if confirm_pin is not None and confirm_pin != new_pin:
        return "pin-mismatch", 0
    async with get_db() as db:
        await set_setting(db, "pin_hash", hash_pin(new_pin))
        await set_setting(db, PIN_IS_DEFAULT_SETTING, "0")
        generation = await bump_session_generation(db)
        await db.commit()
    return None, generation


def _signed_in_redirect(request: Request, generation: int) -> RedirectResponse:
    # The device that changed the PIN stays signed in, under the new generation.
    response = RedirectResponse(url="/admin", status_code=303)
    start_session(response, request, generation)
    return response


@router.post("/change-pin", dependencies=[Depends(require_admin)])
async def change_pin(request: Request, new_pin: str = Form(...), confirm_pin: str | None = Form(None)):
    error, generation = await _save_new_pin(new_pin, confirm_pin)
    if error:
        return _admin_error(error)
    return _signed_in_redirect(request, generation)


async def _pin_is_default() -> bool:
    async with get_db() as db:
        return await get_setting(db, PIN_IS_DEFAULT_SETTING) == "1"


async def _new_pin_page(request: Request, error: str | None = None, status_code: int = 200):
    async with get_db() as db:
        mode = await appearance.current_mode(db)
    return templates.TemplateResponse(
        request, "admin/new_pin.html", {"error": error, "appearance": mode}, status_code=status_code
    )


# The forced "Choose a new PIN" screen: while the PIN is still the default,
# require_admin sends every Admin page here. It needs only a session.
@router.get(NEW_PIN_PATH.removeprefix("/admin"), response_class=HTMLResponse,
            dependencies=[Depends(require_session)])
async def new_pin_page(request: Request):
    if not await _pin_is_default():
        return RedirectResponse(url="/admin", status_code=303)
    return await _new_pin_page(request)


@router.post(NEW_PIN_PATH.removeprefix("/admin"), dependencies=[Depends(require_session)])
async def new_pin_submit(request: Request, new_pin: str = Form(...), confirm_pin: str | None = Form(None)):
    if not await _pin_is_default():
        return RedirectResponse(url="/admin", status_code=303)
    error, generation = await _save_new_pin(new_pin, confirm_pin)
    if error:
        return await _new_pin_page(request, ADMIN_ERRORS[error][1], 400)
    return _signed_in_redirect(request, generation)


# --- Backups ---

@router.post("/backups/run", dependencies=[Depends(require_admin)])
async def run_backup_now():
    if await backup.create_backup() is None:
        return _admin_error("backup-failed")
    return RedirectResponse(url="/admin#backups", status_code=303)
