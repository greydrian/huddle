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
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import (
    admin_tabs,
    appearance,
    backup,
    bank_holidays,
    google_oauth,
    google_tasks,
    idle,
    recurrence,
    school_email,
    sync_status,
    task_sync,
)
from app.admin_tabs import admin_url
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
from app.routers import photos as photos_admin
from app.security import (
    FAILURE_DECAY_SECONDS,
    LONG_LOCKOUT_AFTER,
    hash_pin,
    is_weak_pin,
    lockout_seconds_for,
    verify_pin,
)
from app.services import banners, extraction, homework, imports, layout, school_events, term_dates, weather
from app.services import tasks as task_service
from app.templating import templates
from app.widgets import WIDGETS

# Re-exported: older callers and tests import these from here.
__all__ = ["SESSION_COOKIE", "require_admin", "router"]

router = APIRouter(prefix="/admin")

MAX_SCHOOL_YEAR = 30  # "Year 4", "Reception", "Year 6 (Oak Class)"
# A parent's email: one plain address (no name, no list, no control
# characters), checked loosely.
MAX_EMAIL = 254
_NOT_IN_EMAIL = r"@\s,;:<>()\[\]\"'\x00-\x1f\x7f"  # a regex character class's contents
EMAIL_PATTERN = re.compile(rf"[^{_NOT_IN_EMAIL}]+@[^{_NOT_IN_EMAIL}.]+(?:\.[^{_NOT_IN_EMAIL}.]+)*\.[A-Za-z]{{2,}}")

# A form that can't be saved redirects back to its Admin section with
# ?error=<code> (like ?weather_error=), and the page shows that section's
# message. Only these fixed messages are shown, never text from the URL.
ADMIN_ERRORS = {
    "task-missing": ("tasks", "That task no longer exists. It may have been deleted in Google Tasks."),
    "task-unknown-person": ("tasks", "That family member no longer exists."),
    "task-no-list": ("tasks", "That family member has no Google list linked yet, so the task can't move to "
                              "them. Link one under Google & Sync → Task Sync first."),
    "task-group": ("tasks", "Pick Any time, Morning, After school or Evening."),
    "task-group-times": ("tasks", "Use times like 12:00, with After school starting after midnight and "
                                  "before Evening. Nothing was changed."),
    "homework-missing": ("homework", "That homework no longer exists. It may have just been deleted."),
    "words-missing": ("practice-words", "That word list no longer exists. It may have just been deleted."),
    "handwriting-style": ("practice-words", "Pick one of the handwriting styles."),
    "appearance": ("display", "Pick one of the appearance options."),
    "idle-mode": ("idle", "Pick what the wall does when idle, by day and at night."),
    "idle-numbers": ("idle", "Use whole numbers: an idle delay of 1 to 120 minutes, dimming to 1 to 50% and "
                             "5 to 300 seconds a photo. Nothing was changed."),
    "idle-night": ("idle", "Give night both a start and an end, like 21:00 and 07:00 (different times), or "
                           "leave both blank to follow Appearance. Nothing was changed."),
    "photos-signin": ("photos", "Signing in to the Photos account didn't finish. Try Choose photos again."),
    "photos-offline": ("photos", "Couldn't reach Google Photos just now. Try again in a minute."),
    "photos-busy": ("photos", "Photos are being picked or copied right now. Wait for that to finish, or "
                              "Cancel it first."),
    "widget-missing": ("widgets", "That widget no longer exists. Nothing was changed."),
    "banner-lead": ("banners", f"The lead time must be a whole number of minutes from {banners.MIN_LEAD} to "
                               f"{banners.MAX_LEAD}. Nothing was changed."),
    "banner-quiet": ("banners", "Quiet hours need a start and an end like 21:00 and 07:00, and they can't be "
                                "the same. Nothing was changed."),
    "pin-invalid": ("pin", "A PIN must be 4 to 8 digits, numbers only. Your PIN hasn't changed."),
    "backup-failed": ("backups", "The backup didn't complete. Check the logs (docker compose logs)."),
    "pin-weak": ("pin", "That PIN is too easy to guess: avoid one digit repeated (like 0000) or a "
                        "straight run (like 1234 or 9876). Your PIN hasn't changed."),
    "pin-mismatch": ("pin", "The two PINs didn't match. Your PIN hasn't changed."),
    "profile-missing": ("family", "That family member no longer exists."),
    "profile-year": ("family", f"A year group can be at most {MAX_SCHOOL_YEAR} characters."),
    "profile-email": ("family", "That email address doesn't look right. Use one address, like "
                                "name@example.com, or leave it blank. Nothing was changed."),
    "import-empty": ("classroom", "Add a screenshot, a PDF or some pasted text first."),
    "import-too-big": ("classroom", "That's too big to read: at most 15 MB a file and 22 MB in all. "
                                    "Try a smaller screenshot or fewer pages."),
    "import-too-many": ("classroom", f"Add at most {extraction.MAX_ATTACHMENTS} files at a time."),
    "import-bad-type": ("classroom", "That file isn't one the inbox can read. Use a screenshot or photo "
                                     "(PNG, JPEG, WebP or HEIC) or a PDF."),
    "import-already": ("inbox", "That's already in the School inbox below."),
    "import-missing": ("inbox", "That item is no longer waiting in the inbox. It may have just been "
                                "approved or discarded."),
    "import-event": ("inbox", "Pick the calendar school events go to (School email panel), then approve it."),
    "import-calendar-scope": ("inbox", "Reconnect to allow adding to your calendar: Disconnect, then Connect "
                                       "Google Account. The event is still waiting here."),
    "import-calendar-offline": ("inbox", "Couldn't reach Google Calendar, so the event wasn't added. It's "
                                         "still waiting here; try again in a minute."),
    "import-calendar-missing": ("inbox", "The calendar for school events can't be found any more. Pick another "
                                         "in the School email panel. The event is still waiting here."),
    "import-calendar-failed": ("inbox", "Google Calendar didn't accept the event, so it wasn't added. It's "
                                        "still waiting here."),
    "school-senders": ("school-email", f"Each sender must be an email address or *@domain, one per line "
                                       f"(at most {school_email.MAX_ENTRIES}). Nothing was changed."),
    "school-schedule": ("school-email", "Pick one of the schedule options and a time like 18:00."),
    "school-calendar": ("school-email", "Pick a calendar this Google account can add events to."),
    "import-busy": ("inbox", "That's still being read. Try again in a moment."),
    "import-pdf-pages": ("classroom", f"That PDF has more than {extraction.MAX_PDF_PAGES} pages. "
                                      "Try just the pages you need."),
    "import-term-none": ("inbox", "Tick at least one period to add, or Discard the term dates."),
    "term-kind": ("term-dates", "Pick what kind of period it is: term, half term, holiday, INSET day or closure."),
    "term-dates": ("term-dates", "Enter a start and an end date, with the end on or after the start. "
                                 "Nothing was saved."),
    "term-length": ("term-dates", "That's too long for its kind (at most: term "
                                  f"{term_dates.MAX_DAYS['term']} days, half term {term_dates.MAX_DAYS['half_term']}, "
                                  f"holiday {term_dates.MAX_DAYS['holiday']}, INSET {term_dates.MAX_DAYS['inset']}, "
                                  f"closure {term_dates.MAX_DAYS['closure']}). Check the dates; nothing was saved."),
    "term-label": ("term-dates", f"A name can be at most {term_dates.MAX_LABEL} characters. Nothing was saved."),
    "term-overlap": ("term-dates", "That overlaps a period it can't: only half terms, INSET days and closures "
                                   "may fall inside a term, and INSET days inside a holiday. Nothing was saved."),
    "term-missing": ("term-dates", "That period no longer exists. It may have just been deleted."),
}

# The login form only takes digits (inputmode=numeric, maxlength=8), so a PIN
# it can't type would lock the family out of Admin.
PIN_PATTERN = re.compile(r"[0-9]{4,8}")


def _admin_error(code: str) -> RedirectResponse:
    section, _ = ADMIN_ERRORS[code]
    return RedirectResponse(url=admin_url(section, error=code), status_code=303)


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


async def _login_page(
    request: Request, error: str | None, status_code: int = 200, next_url: str = "", section: str = ""
):
    """The PIN page. `next_url` / `section` (where to go after the PIN, see
    admin_tabs.login_return) are only ever re-shown after validation."""
    async with get_db() as db:
        mode = await appearance.current_mode(db)
    back_to = admin_tabs.login_return(next_url)
    return templates.TemplateResponse(
        request, "admin/login.html",
        {"error": error, "appearance": mode, "next_url": "" if back_to == "/admin" else back_to,
         "section": section if section in admin_tabs.SECTIONS else ""},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = ""):
    async with get_db() as db:
        lockout, wait = await _active_lockout(db)
    error = _lockout_message(lockout.get("failed_attempts", 0), wait) if wait else None
    return await _login_page(request, error, next_url=next)


@router.post("/login")
async def login_submit(request: Request, pin: str = Form(...), next: str = Form(""), section: str = Form("")):
    async with _login_lock(), get_db() as db:
        lockout, wait = await _active_lockout(db)
        failed_attempts = lockout.get("failed_attempts", 0)
        if wait:
            # Refused by the lockout: doesn't count as another failure.
            return await _login_page(request, _lockout_message(failed_attempts, wait), 429, next, section)

        stored_hash = await get_setting(db, "pin_hash")
        if stored_hash and verify_pin(pin, stored_hash):
            await set_setting(db, "pin_lockout", json.dumps({}))
            await db.commit()
            # Back to the tab + section that asked for the PIN. A default PIN
            # still goes to "Choose a new PIN" first (require_admin sends it).
            response = RedirectResponse(url=admin_tabs.login_return(next, section), status_code=303)
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
    return await _login_page(request, message, 401, next, section)


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
async def admin_home(
    request: Request, tab: str | None = None, weather_error: str | None = None, error: str | None = None
):
    return await _render_admin(request, tab=tab, weather_error=weather_error, error=error)


async def _google_lists(db, google_account: str | None, tasklists: bool = True) -> dict:
    """The Google account's calendars and task lists, for the Google & Sync
    tab (the School tab's calendar picker skips the task lists). Never raises on a Google
    error: google_offline / tasklists_error say what went wrong."""
    found: dict = {"available_calendars": [], "available_tasklists": [], "tasklists_error": False,
                   "google_offline": False}
    if not google_account:
        return found
    try:
        access_token = await google_oauth.get_valid_access_token(db)
    except httpx.HTTPError:
        access_token, found["google_offline"] = None, True
    if not access_token:
        return found
    try:
        found["available_calendars"] = await google_oauth.fetch_calendar_list(access_token)
    except httpx.HTTPError:
        found["google_offline"] = True
    if not tasklists:
        return found
    try:
        found["available_tasklists"] = await google_tasks.fetch_tasklists(access_token)
    except httpx.HTTPStatusError as exc:
        # 403 = this account connected before the `tasks` scope
        # existed — settings.html shows a reconnect prompt.
        if exc.response.status_code == 403:
            found["tasklists_error"] = True
        else:
            found["google_offline"] = True
    except httpx.HTTPError:
        found["google_offline"] = True
    return found


def _tab_from_query(request: Request, form_error: dict | None, weather_error: str | None) -> str:
    if form_error:
        return admin_tabs.SECTIONS[form_error["section"]]
    for param, section in (("sync", "sync"), ("school_email", "school-email")):
        if request.query_params.get(param):
            return admin_tabs.SECTIONS[section]
    if weather_error:
        return admin_tabs.SECTIONS["weather"]
    return admin_tabs.DEFAULT_TAB


async def _render_admin(
    request: Request,
    tab: str | None = None,
    weather_error: str | None = None,
    error: str | None = None,
    homework_error: str | None = None,
    words_error: str | None = None,
    homework_form: dict | None = None,
    words_form: dict | None = None,
    inbox_form: dict | None = None,
    inbox_error: str | None = None,
    term_form: dict | None = None,
    error_message: str | None = None,
    status_code: int = 200,
):
    """Renders one Admin tab (app/admin_tabs.py), loading only what that
    tab shows. After a validation error, *_form carries what was submitted
    (with "id" None for the add form, else the row being edited) so nothing
    typed is lost."""
    form_error = _form_error(error)
    if form_error and error_message:
        form_error["message"] = error_message  # e.g. naming the period a new one clashes with
    if tab not in admin_tabs.TABS:
        # No (valid) tab: an older link such as /admin?error=pin-invalid#pin
        # still opens the tab its message or status belongs to.
        tab = _tab_from_query(request, form_error, weather_error)
    if form_error and admin_tabs.SECTIONS[form_error["section"]] != tab:
        form_error = None  # it belongs to another tab's section; never shown out of place
    context: dict = {
        "admin_tab": tab,
        "admin_tabs": admin_tabs.TABS,
        "admin_sections": admin_tabs.SECTIONS,
        "form_error": form_error,
    }
    async with get_db() as db:
        context["appearance"] = await appearance.current_mode(db)
        context["profiles"] = [dict(r) for r in await (await db.execute(
            "SELECT * FROM profiles ORDER BY sort_order"
        )).fetchall()]
        google_account = await google_oauth.get_connected_account(db)
        context["google_account"] = google_account
        context["google_configured"] = google_oauth.is_configured()
        context["inbox_configured"] = extraction.is_configured()

        if tab == "family":
            today = await family_today(db)
            homework_items, finished_homework = await homework.get_admin_homework(db, today)
            context.update({
                "tasks": await task_service.get_admin_tasks(db),
                "weekdays": recurrence.WEEKDAYS,
                "task_groups": [(g, task_service.GROUP_LABELS[g]) for g in task_service.GROUPS],
                "task_group_bounds": await task_service.get_group_boundaries(db),
                "term_dates_missing": await term_dates.missing_years(db, today),
                "homework_items": homework_items,
                "finished_homework": finished_homework,
                "homework_form": homework_form,
                "homework_error": homework_error,
                "word_lists": await homework.get_admin_word_lists(db),
                "words_form": words_form,
                "words_error": words_error,
                "handwriting_style": await homework.get_handwriting_style(db),
                "handwriting_styles": homework.HANDWRITING_STYLES,
            })
        elif tab == "school":
            lists = await _google_lists(db, google_account, tasklists=False)  # calendars only
            context.update({
                "inbox": await imports.get_inbox(db),
                "inbox_form": inbox_form,
                "inbox_error": inbox_error,
                "school": await school_email.summary(db),
                "event_setup": await _event_setup(db),
                "writable_calendars": [c for c in lists["available_calendars"] if c.get("writable")],
                **await _term_dates_context(db),
                "term_form": term_form,
            })
        elif tab == "display":
            context.update({
                "weather_location": await weather.get_location(db),
                "weather_error": weather_error,
                "appearance_setting": await appearance.get_appearance(db),
                "appearances": appearance.APPEARANCES,
                "widget_settings": await layout.admin_widgets(db),
                "school_day_today": await term_dates.is_school_day(db, await family_today(db)),
                "banner_settings": await banners.get_settings(db),
                "banner_triggers": banners.TRIGGERS,
                "banner_lead_range": (banners.MIN_LEAD, banners.MAX_LEAD),
                "idle_settings": await idle.get_settings(db),
                "idle_modes": idle.MODES,
                "idle_ranges": idle.RANGES,
                **await photos_admin.panel_context(db),
            })
        elif tab == "google":
            context.update(await _google_lists(db, google_account))
            context["sync"] = await sync_status.summary(db)
            if google_account:
                context["selected_calendar_ids"] = [c["id"] for c in await google_oauth.get_selected_calendars(db)]
                context["shopping_tasklist"] = await task_sync.get_shopping_tasklist(db)
        elif tab == "assistant":
            context["assistant_model"] = extraction.model_name()
        elif tab == "system":
            context["backup_status"] = backup.status(await family_timezone(db))

    return templates.TemplateResponse(request, "admin/settings.html", context, status_code=status_code)


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
    return RedirectResponse(url=admin_url("family"), status_code=303)


@router.post("/profiles/{profile_id}/details", dependencies=[Depends(require_admin)])
async def save_profile_details(
    profile_id: int, school_year: str = Form(""), is_parent: bool = Form(False), email: str = Form("")
):
    """A family member's details. A child's year group ("Year 4"): the school
    inbox uses it to decide whose spellings and homework are whose. Parent +
    email (spec 10.0): for assistant parent tasks and the weekly digest; only
    shown in Admin so far. Blank clears the year group or the email."""
    try:
        year = homework.clean_text(school_year, "Year group", MAX_SCHOOL_YEAR) or None
    except homework.ValidationError:
        return _admin_error("profile-year")
    address = email.strip() or None
    if address is not None and (len(address) > MAX_EMAIL or not EMAIL_PATTERN.fullmatch(address)):
        return _admin_error("profile-email")
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE profiles SET school_year = ?, is_parent = ?, email = ? WHERE id = ?",
            (year, int(is_parent), address, profile_id),
        )
        await db.commit()
    if not cursor.rowcount:
        return _admin_error("profile-missing")
    return RedirectResponse(url=admin_url("family"), status_code=303)


@router.post("/profiles/{profile_id}/delete", dependencies=[Depends(require_admin)])
async def delete_profile(profile_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await db.commit()
    return RedirectResponse(url=admin_url("family"), status_code=303)


# --- Task schedules ---
# Tasks are added, renamed and deleted in Google Tasks; its API has no
# recurrence, so which days a task repeats (and whose it is) is set here.

@router.post("/tasks/{task_id}/edit", dependencies=[Depends(require_admin)])
async def edit_task(
    task_id: int,
    profile_id: int = Form(...),
    is_recurring: bool = Form(False),
    days: list[str] = Form(default=[]),
    school_days: bool = Form(False),
    time_of_day: str | None = Form(None),
):
    # Ticking any day (or School days) implies the task repeats; "Repeats"
    # with no days ticked means every day. Rules are only stored for
    # recurring tasks. No time_of_day field at all leaves the group as it is
    # ("any" clears it: FastAPI reads an empty form value as missing).
    recurring = is_recurring or bool(days) or school_days
    rule: str | None = None
    if school_days:
        rule = recurrence.SCHOOL_DAYS
    elif recurring:
        rule = recurrence.normalize_rule(days)
    try:
        group = task_service.clean_group(time_of_day)
    except ValueError:
        return _admin_error("task-group")
    async with get_db() as db:
        task = await (await db.execute(
            "SELECT profile_id, is_recurring, time_of_day, due_on FROM tasks WHERE id = ? AND archived = 0",
            (task_id,),
        )).fetchone()
        if task is None:
            return _admin_error("task-missing")
        if time_of_day is None:
            group = task["time_of_day"]
        # A task made a one-off is due from today, not marked late for the
        # days it spent repeating. Any other edit leaves due_on alone (a
        # pre-0004 one-off's NULL means "never late").
        due_on = task["due_on"]
        if not recurring and task["is_recurring"]:
            due_on = (await family_today(db)).isoformat()
        if profile_id == task["profile_id"]:
            # The schedule and group are local-only (never pushed, never
            # reconciled), so don't bump updated_at or queue a push: that would
            # overwrite a newer rename/tick in Google with this row's stale copy.
            await db.execute(
                "UPDATE tasks SET is_recurring = ?, recurrence_rule = ?, time_of_day = ?, due_on = ? WHERE id = ?",
                (int(recurring), rule, group, due_on, task_id),
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
                """UPDATE tasks SET profile_id = ?, is_recurring = ?, recurrence_rule = ?, time_of_day = ?,
                       due_on = ?, updated_at = datetime('now') WHERE id = ?""",
                (profile_id, int(recurring), rule, group, due_on, task_id),
            )
            await task_sync.queue_sync(db, "tasks", {"task_id": task_id})
        await db.commit()
    return RedirectResponse(url=admin_url("tasks"), status_code=303)


@router.post("/tasks/groups", dependencies=[Depends(require_admin)])
async def save_task_groups(after_school_start: str = Form(""), evening_start: str = Form("")):
    """When the widget's After school and Evening groups start (Morning runs
    from midnight)."""
    async with get_db() as db:
        try:
            await task_service.set_group_boundaries(db, after_school_start, evening_start)
        except ValueError:
            return _admin_error("task-group-times")
    return RedirectResponse(url=admin_url("tasks"), status_code=303)


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
            return await _render_admin(request, tab="family", homework_error=str(exc), status_code=400, homework_form={
                "id": homework_id, "profile_id": profile_id, "subject": subject, "title": title,
                "details": details, "due_date": due_date,
            })
        await db.execute(
            "INSERT INTO homework (profile_id, subject, title, details, due_date) VALUES (?, ?, ?, ?, ?)", fields
        )
        await db.commit()
    return RedirectResponse(url=admin_url("homework"), status_code=303)


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
            return await _render_admin(request, tab="family", homework_error=str(exc), status_code=400, homework_form={
                "id": homework_id, "profile_id": profile_id, "subject": subject, "title": title,
                "details": details, "due_date": due_date,
            })
        await db.execute(
            """UPDATE homework SET profile_id = ?, subject = ?, title = ?, details = ?, due_date = ?,
                   updated_at = datetime('now') WHERE id = ?""",
            (*fields, homework_id),
        )
        await db.commit()
    return RedirectResponse(url=admin_url("homework"), status_code=303)


@router.post("/homework/{homework_id}/archive", dependencies=[Depends(require_admin)])
async def archive_homework(homework_id: int):
    """Toggles: archived homework leaves the dashboard; restoring brings it back."""
    async with get_db() as db:
        await db.execute(
            "UPDATE homework SET archived = 1 - archived, updated_at = datetime('now') WHERE id = ?", (homework_id,)
        )
        await db.commit()
    return RedirectResponse(url=admin_url("homework"), status_code=303)


@router.post("/homework/{homework_id}/delete", dependencies=[Depends(require_admin)])
async def delete_homework(homework_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM homework WHERE id = ?", (homework_id,))
        await db.commit()
    return RedirectResponse(url=admin_url("homework"), status_code=303)



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
            return await _render_admin(request, tab="family", words_error=str(exc), status_code=400, words_form={
                "id": list_id, "profile_id": profile_id, "title": title, "words": words,
                "starts_on": starts_on, "ends_on": ends_on,
            })
        await db.execute(
            "INSERT INTO practice_word_lists (profile_id, title, words, starts_on, ends_on) VALUES (?, ?, ?, ?, ?)",
            fields,
        )
        await db.commit()
    return RedirectResponse(url=admin_url("practice-words"), status_code=303)


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
            return await _render_admin(request, tab="family", words_error=str(exc), status_code=400, words_form={
                "id": list_id, "profile_id": profile_id, "title": title, "words": words,
                "starts_on": starts_on, "ends_on": ends_on,
            })
        await db.execute(
            """UPDATE practice_word_lists SET profile_id = ?, title = ?, words = ?, starts_on = ?, ends_on = ?,
                   updated_at = datetime('now') WHERE id = ?""",
            (*fields, list_id),
        )
        await db.commit()
    return RedirectResponse(url=admin_url("practice-words"), status_code=303)


@router.post("/practice-words/{list_id}/archive", dependencies=[Depends(require_admin)])
async def archive_word_list(list_id: int):
    """Toggles: an archived list leaves the dashboard; restoring brings it back."""
    async with get_db() as db:
        await db.execute(
            "UPDATE practice_word_lists SET archived = 1 - archived, updated_at = datetime('now') WHERE id = ?",
            (list_id,),
        )
        await db.commit()
    return RedirectResponse(url=admin_url("practice-words"), status_code=303)


@router.post("/practice-words/{list_id}/delete", dependencies=[Depends(require_admin)])
async def delete_word_list(list_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM practice_word_lists WHERE id = ?", (list_id,))
        await db.commit()
    return RedirectResponse(url=admin_url("practice-words"), status_code=303)


@router.post("/handwriting-style", dependencies=[Depends(require_admin)])
async def save_handwriting_style(style: str = Form("")):
    if style not in homework.HANDWRITING_STYLES:
        return _admin_error("handwriting-style")
    async with get_db() as db:
        await set_setting(db, homework.HANDWRITING_SETTING, style)
        await db.commit()
    return RedirectResponse(url=admin_url("practice-words"), status_code=303)


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
            uploads = [f for f in form.getlist("files")
                       if isinstance(f, StarletteUploadFile) and (f.filename or f.size)]
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
        return _admin_error("import-too-many")
    except imports.UploadRejected as exc:
        return _admin_error(exc.code)
    if not blobs and not text:
        return _admin_error("import-empty")
    try:
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
        profiles = [dict(r) for r in await (await db.execute(
            "SELECT id, name, colour_hex FROM profiles ORDER BY sort_order"
        )).fetchall()]
        event_setup = await _event_setup(db)
    if source is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, "admin/_inbox_source.html",
        {"source": source, "profiles": profiles, "event_setup": event_setup, "term_kinds": term_dates.KINDS,
         **extra},
    )


async def _inbox_done(request: Request, source_id: int | None, error: str | None = None):
    """The response to an inbox action: the source's block for HTMX (with
    the ADMIN_ERRORS message, if any), else a redirect back to Admin."""
    if _is_htmx(request) and source_id is not None:
        return await _source_fragment(request, source_id, source_error=ADMIN_ERRORS[error][1] if error else None)
    return _admin_error(error) if error else RedirectResponse(url=admin_url("inbox"), status_code=303)


async def _candidate_source(candidate_id: int) -> int | None:
    return (await _candidate_row(candidate_id))[0]


async def _candidate_row(candidate_id: int) -> tuple[int | None, str | None]:
    """(source id, kind) of a candidate, or (None, None)."""
    async with get_db() as db:
        row = await (await db.execute(
            "SELECT source_id, kind FROM import_candidates WHERE id = ?", (candidate_id,)
        )).fetchone()
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
        "profile_id": profile_id, "title": title, "subject": subject, "details": details,
        "due_date": due_date, "words": words, "starts_on": starts_on, "ends_on": ends_on,
        "date": event_date, "start_time": start_time, "end_time": end_time, "notes": notes,
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
            return await _render_admin(request, tab="school", inbox_error=str(exc), inbox_form=inbox_form, status_code=400)
    return await _inbox_done(request, source_id)


MAX_TERM_ROWS = extraction.MAX_TERM_PERIODS


async def _approve_term_dates(request: Request, candidate_id: int, source_id: int | None):
    """A term-dates candidate's rows (period_kind/start/end/label, with
    period_include naming the ticked rows) into the term dates."""
    form = await request.form()
    columns = [form.getlist(f"period_{name}")[:MAX_TERM_ROWS] for name in ("kind", "start", "end", "label")]
    included = set(form.getlist("period_include"))
    rows = [
        {"kind": str(k), "start_date": str(s), "end_date": str(e), "label": str(lab), "include": str(i) in included}
        for i, (k, s, e, lab) in enumerate(zip(*columns, strict=False))
    ]
    async with get_db() as db:
        try:
            await imports.approve_term_dates(db, candidate_id, [r for r in rows if r["include"]])
        except imports.CandidateError as exc:
            if exc.code != "import-term-none":
                return await _inbox_done(request, source_id, exc.code)
            error = ADMIN_ERRORS[exc.code][1]
        except term_dates.PeriodError as exc:
            error = _period_error(exc)
        else:
            return await _inbox_done(request, source_id)
    inbox_form = {"id": candidate_id, "periods": rows}
    if _is_htmx(request) and source_id is not None:
        return await _source_fragment(request, source_id, inbox_form=inbox_form, inbox_error=error)
    return await _render_admin(request, tab="school", inbox_error=error, inbox_form=inbox_form, status_code=400)


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
    """The School tab's Term dates panel: periods by school year, the years
    with none (the warning), and when the bank holidays were last updated."""
    holidays = await bank_holidays.status(db)
    today = await family_today(db)
    if holidays["updated_at"]:
        local = holidays["updated_at"].astimezone(await family_timezone(db))
        holidays["updated_label"] = f"{local.day} {local:%b %Y}"
    return {
        "term_years": term_dates.group_by_school_year(await term_dates.list_periods(db)),
        "term_missing_years": await term_dates.missing_years(db, today),
        "term_incomplete": await term_dates.incomplete_years(db, today),
        "term_kinds": term_dates.KINDS,
        "bank_holidays": holidays,
    }


def _period_error(exc: term_dates.PeriodError) -> str:
    """The message for a period that couldn't be saved: a clash names the
    period it clashes with (a stored or listed one, never URL text)."""
    if exc.code == "term-overlap" and exc.clash:
        return term_dates.clash_message(exc.clash)
    return ADMIN_ERRORS[exc.code][1]


def _term_form(period_id: int | None, kind: str, start_date: str, end_date: str, label: str) -> dict:
    return {"id": period_id, "kind": kind, "start_date": start_date, "end_date": end_date, "label": label}


@router.post("/term-dates", dependencies=[Depends(require_admin)])
async def add_term_period(
    request: Request, kind: str = Form(""), start_date: str = Form(""), end_date: str = Form(""),
    label: str = Form(""),
):
    async with get_db() as db:
        try:
            await term_dates.add_period(db, kind, start_date, end_date, label)
        except term_dates.PeriodError as exc:
            return await _render_admin(request, tab="school", error=exc.code, status_code=400,
                                       error_message=_period_error(exc), term_form=_term_form(None, kind, start_date, end_date, label))
    return RedirectResponse(url=admin_url("term-dates"), status_code=303)


@router.post("/term-dates/{period_id}/edit", dependencies=[Depends(require_admin)])
async def edit_term_period(
    request: Request, period_id: int, kind: str = Form(""), start_date: str = Form(""),
    end_date: str = Form(""), label: str = Form(""),
):
    async with get_db() as db:
        try:
            await term_dates.update_period(db, period_id, kind, start_date, end_date, label)
        except term_dates.PeriodError as exc:
            if exc.code == "term-missing":
                return _admin_error(exc.code)
            return await _render_admin(request, tab="school", error=exc.code, status_code=400,
                                       error_message=_period_error(exc), term_form=_term_form(period_id, kind, start_date, end_date, label))
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
        return _admin_error("school-schedule")
    async with get_db() as db:
        await school_email.set_schedule(db, schedule)
    return RedirectResponse(url=admin_url("school-email"), status_code=303)


@router.post("/school-email/senders", dependencies=[Depends(require_admin)])
async def save_school_email_senders(senders: str = Form(""), exclusions: str = Form("")):
    try:
        allow = school_email.parse_entries(senders)
        deny = school_email.parse_entries(exclusions)
    except school_email.InvalidEntry:
        return _admin_error("school-senders")
    async with get_db() as db:
        await school_email.set_lists(db, allow, deny)
    return RedirectResponse(url=admin_url("school-email"), status_code=303)


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
            return _admin_error("school-calendar")
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
    return RedirectResponse(url=admin_url("task-lists"), status_code=303)


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
    return RedirectResponse(url=admin_url("task-lists"), status_code=303)


# --- Weather ---

@router.post("/weather-location", dependencies=[Depends(require_admin)])
async def save_weather_location(place: str = Form(...)):
    try:
        location = await weather.geocode(place)
    # A malformed geocoder response is treated like an outage, never a 500.
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError):
        return RedirectResponse(url=admin_url("weather", weather_error="offline"), status_code=303)
    if not location:
        return RedirectResponse(url=admin_url("weather", weather_error="notfound"), status_code=303)
    async with get_db() as db:
        await weather.set_location(db, location)
    return RedirectResponse(url=admin_url("weather"), status_code=303)


# --- Display ---

@router.post("/onscreen-keyboard", dependencies=[Depends(require_admin)])
async def save_onscreen_keyboard(enabled: bool = Form(False)):
    async with get_db() as db:
        await set_onscreen_keyboard(db, enabled)
    return RedirectResponse(url=admin_url("keyboard"), status_code=303)


@router.post("/widgets/{widget_id}/visibility", dependencies=[Depends(require_admin)])
async def save_widget_visibility(widget_id: str, visible: bool = Form(False)):
    """Show or hide a widget (spec 10.3). Hiding closes its gap; showing puts
    it back where it was, or the nearest free space."""
    if widget_id not in WIDGETS:
        return _admin_error("widget-missing")
    async with get_db() as db:
        await (layout.show_widget if visible else layout.hide_widget)(db, widget_id)
        await db.commit()
    return RedirectResponse(url=admin_url("widgets"), status_code=303)


@router.post("/widgets/{widget_id}/school-days", dependencies=[Depends(require_admin)])
async def save_widget_school_days(widget_id: str, enabled: bool = Form(False)):
    if widget_id not in WIDGETS:
        return _admin_error("widget-missing")
    async with get_db() as db:
        await layout.set_school_days_only(db, widget_id, enabled)
        await db.commit()
    return RedirectResponse(url=admin_url("widgets"), status_code=303)


@router.post("/appearance", dependencies=[Depends(require_admin)])
async def save_appearance(value: str = Form("")):
    if value not in appearance.APPEARANCES:
        return _admin_error("appearance")
    async with get_db() as db:
        await appearance.set_appearance(db, value)
    return RedirectResponse(url=admin_url("display"), status_code=303)


@router.post("/banners", dependencies=[Depends(require_admin)])
async def save_banners(request: Request):
    """The notification banner's triggers, sounds, default lead time and
    quiet hours (spec 10.1)."""
    form = await request.form()
    try:
        settings = banners.clean_settings({key: form.get(key) for key in form.keys()})
    except banners.SettingsError as exc:
        return _admin_error(exc.code)
    async with get_db() as db:
        await banners.save_settings(db, settings)
    return RedirectResponse(url=admin_url("banners"), status_code=303)


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


def _signed_in_redirect(request: Request, generation: int, url: str = "/admin") -> RedirectResponse:
    # The device that changed the PIN stays signed in, under the new generation.
    response = RedirectResponse(url=url, status_code=303)
    start_session(response, request, generation)
    return response


@router.post("/change-pin", dependencies=[Depends(require_admin)])
async def change_pin(request: Request, new_pin: str = Form(...), confirm_pin: str | None = Form(None)):
    error, generation = await _save_new_pin(new_pin, confirm_pin)
    if error:
        return _admin_error(error)
    return _signed_in_redirect(request, generation, admin_url("pin"))


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
    return RedirectResponse(url=admin_url("backups"), status_code=303)
