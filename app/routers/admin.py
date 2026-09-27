"""
Admin / Parent Controls (Section 4.7): single shared PIN, exponential backoff
on failed attempts, short-lived signed session cookie. Manages family
profiles, task schedules, and (eventually) Google account connections.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import appearance, google_oauth, google_tasks, recurrence, task_sync
from app.database import family_today, get_db, get_setting, set_onscreen_keyboard, set_setting
from app.routers import homework, weather
from app.security import create_session_token, hash_pin, lockout_seconds_for, verify_pin, verify_session_token
from app.templating import templates

router = APIRouter(prefix="/admin")

SESSION_COOKIE = "admin_session"


def _queue_sync(db, service: str, payload: dict):
    return db.execute(
        "INSERT INTO sync_queue (service, payload_json) VALUES (?, ?)",
        (service, json.dumps(payload)),
    )


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    # Lockouts stored before timestamps became tz-aware are naive UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def require_admin(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not verify_session_token(token):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


async def _login_page(request: Request, error: str | None, status_code: int = 200):
    async with get_db() as db:
        mode = await appearance.current_mode(db)
    return templates.TemplateResponse(
        request, "admin/login.html", {"error": error, "appearance": mode}, status_code=status_code
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return await _login_page(request, None)


@router.post("/login")
async def login_submit(request: Request, pin: str = Form(...)):
    async with get_db() as db:
        lockout_raw = await get_setting(db, "pin_lockout", "{}")
        lockout = json.loads(lockout_raw)
        locked_until = lockout.get("locked_until")

        now = datetime.now(timezone.utc)
        if locked_until and now < _parse_utc(locked_until):
            wait_seconds = int((_parse_utc(locked_until) - now).total_seconds())
            return await _login_page(request, f"Too many attempts. Try again in {wait_seconds}s.", 429)

        stored_hash = await get_setting(db, "pin_hash")
        if stored_hash and verify_pin(pin, stored_hash):
            await set_setting(db, "pin_lockout", json.dumps({}))
            await db.commit()
            response = RedirectResponse(url="/admin", status_code=303)
            response.set_cookie(
                SESSION_COOKIE, create_session_token(), httponly=True, samesite="lax"
            )
            return response

        # Failed attempt: bump the counter and set an exponential-backoff lockout
        failed_attempts = lockout.get("failed_attempts", 0) + 1
        wait = lockout_seconds_for(failed_attempts)
        locked_until_new = (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat()
        await set_setting(
            db,
            "pin_lockout",
            json.dumps({"failed_attempts": failed_attempts, "locked_until": locked_until_new}),
        )
        await db.commit()

    return await _login_page(request, f"Incorrect PIN. Try again in {wait}s." if wait else "Incorrect PIN.", 401)


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_home(request: Request, weather_error: str | None = None):
    return await _render_admin(request, weather_error=weather_error)


async def _render_admin(
    request: Request,
    weather_error: str | None = None,
    homework_error: str | None = None,
    words_error: str | None = None,
    homework_form: dict | None = None,
    words_form: dict | None = None,
    status_code: int = 200,
):
    """Renders Admin. After a validation error, *_form carries what was
    submitted (with "id" None for the add form, else the row being edited)
    so nothing typed is lost."""
    async with get_db() as db:
        profiles = [dict(r) for r in await (await db.execute(
            "SELECT * FROM profiles ORDER BY sort_order"
        )).fetchall()]
        tasks = [dict(r) for r in await (await db.execute(
            "SELECT tasks.* FROM tasks "
            "JOIN profiles ON profiles.id = tasks.profile_id "
            "WHERE archived = 0 ORDER BY profiles.sort_order, tasks.created_at"
        )).fetchall()]
        for task in tasks:
            task["schedule"] = recurrence.describe(task["recurrence_rule"]) if task["is_recurring"] else None
            # Day chips to pre-tick in the edit form; empty = every day (or one-off).
            days = recurrence.parse_rule(task["recurrence_rule"]) if task["is_recurring"] else None
            task["days"] = [recurrence.WEEKDAYS[i] for i in sorted(days)] if days else []
        today = await family_today(db)
        homework_items, finished_homework = [], []
        for row in await (await db.execute(
            "SELECT homework.*, profiles.name AS profile_name FROM homework "
            "JOIN profiles ON profiles.id = homework.profile_id "
            "ORDER BY homework.done, homework.due_date IS NULL, homework.due_date, homework.id"
        )).fetchall():
            item = dict(row)
            # Archived, or done and already off the widget: tucked away so the panel doesn't grow all term.
            (homework_items if homework.is_visible(item, today) else finished_homework).append(item)
        word_lists = [dict(r) for r in await (await db.execute(
            "SELECT practice_word_lists.*, profiles.name AS profile_name FROM practice_word_lists "
            "JOIN profiles ON profiles.id = practice_word_lists.profile_id "
            "ORDER BY practice_word_lists.archived, profiles.sort_order, practice_word_lists.id"
        )).fetchall()]
        handwriting_style = await homework.get_handwriting_style(db)
        google_account = await google_oauth.get_connected_account(db)
        weather_location = await weather.get_location(db)
        current_appearance = await appearance.get_appearance(db)
        mode = await appearance.current_mode(db)

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
            raise HTTPException(status_code=404, detail="Task not found")
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
                raise HTTPException(status_code=400, detail="Unknown family member")
            if not target["google_tasklist_id"]:
                # Its Google copy would be deleted, leaving a task nothing can rename or remove.
                raise HTTPException(status_code=400, detail="That family member has no Google list linked")
            await task_sync.detach_from_profile_list(db, task_id)
            await db.execute(
                """UPDATE tasks SET profile_id = ?, is_recurring = ?, recurrence_rule = ?,
                       updated_at = datetime('now') WHERE id = ?""",
                (profile_id, int(recurring), rule, task_id),
            )
            await _queue_sync(db, "tasks", {"task_id": task_id})
        await db.commit()
    return RedirectResponse(url="/admin#tasks", status_code=303)


# --- Homework + practice words (not synced to Google) ---

async def _homework_fields(db, profile_id, subject, title, details, due_date) -> tuple:
    return (
        await homework.parse_profile_id(db, profile_id),
        homework.clean_text(subject, "Subject", homework.MAX_SUBJECT),
        homework.clean_text(title, "Title", homework.MAX_TITLE, required=True),
        homework.clean_text(details, "Details", homework.MAX_DETAILS) or None,
        homework.parse_optional_date(due_date, "Due date"),
    )


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
            fields = await _homework_fields(db, profile_id, subject, title, details, due_date)
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
            raise HTTPException(status_code=404, detail="Homework not found")
        try:
            fields = await _homework_fields(db, profile_id, subject, title, details, due_date)
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


async def _word_list_fields(db, profile_id, title, words, starts_on, ends_on) -> tuple:
    profile = await homework.parse_profile_id(db, profile_id)
    title = homework.clean_text(title, "Title", homework.MAX_TITLE, required=True)
    word_items = homework.normalise_words(words)
    if not word_items:
        raise homework.ValidationError("Add at least one word.")
    starts = homework.parse_optional_date(starts_on, "Start date")
    ends = homework.parse_optional_date(ends_on, "End date")
    if starts and ends and ends < starts:
        raise homework.ValidationError("The end date is before the start date.")
    return profile, title, "\n".join(word_items), starts, ends


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
            fields = await _word_list_fields(db, profile_id, title, words, starts_on, ends_on)
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
            raise HTTPException(status_code=404, detail="Word list not found")
        try:
            fields = await _word_list_fields(db, profile_id, title, words, starts_on, ends_on)
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
        raise HTTPException(status_code=400, detail="Unknown handwriting style")
    async with get_db() as db:
        await set_setting(db, homework.HANDWRITING_SETTING, style)
        await db.commit()
    return RedirectResponse(url="/admin#practice-words", status_code=303)


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
        raise HTTPException(status_code=400, detail="Unknown appearance")
    async with get_db() as db:
        await appearance.set_appearance(db, value)
    return RedirectResponse(url="/admin#display", status_code=303)


# --- PIN management ---

@router.post("/change-pin", dependencies=[Depends(require_admin)])
async def change_pin(new_pin: str = Form(...)):
    async with get_db() as db:
        await set_setting(db, "pin_hash", hash_pin(new_pin))
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)
