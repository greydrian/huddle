"""Family tab: family members and their avatars, task schedules and
time-of-day groups, homework and practice words, the handwriting style."""

import asyncio
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import avatars, recurrence, task_sync
from app.admin_tabs import admin_url
from app.auth import require_admin
from app.database import get_db, set_setting
from app.routers.admin import common
from app.routers.admin.common import MAX_SCHOOL_YEAR, admin_error, tab_context
from app.routers.admin.page import render_admin
from app.services import homework, term_dates
from app.services import tasks as task_service

router = APIRouter(prefix="/admin")

# A parent's email: one plain address (no name, no list, no control
# characters), checked loosely.
MAX_EMAIL = 254
_NOT_IN_EMAIL = r"@\s,;:<>()\[\]\"'\x00-\x1f\x7f"  # a regex character class's contents
EMAIL_PATTERN = re.compile(rf"[^{_NOT_IN_EMAIL}]+@[^{_NOT_IN_EMAIL}.]+(?:\.[^{_NOT_IN_EMAIL}.]+)*\.[A-Za-z]{{2,}}")


@tab_context("family")
async def family_context(db, base: dict, extra: dict) -> dict:
    today = await common.family_today(db)
    homework_items, finished_homework = await homework.get_admin_homework(db, today)
    return {
        "tasks": await task_service.get_admin_tasks(db),
        "weekdays": recurrence.WEEKDAYS,
        "task_groups": [(g, task_service.GROUP_LABELS[g]) for g in task_service.GROUPS],
        "task_group_bounds": await task_service.get_group_boundaries(db),
        "term_dates_missing": await term_dates.missing_years(db, today),
        "homework_items": homework_items,
        "finished_homework": finished_homework,
        "homework_events": await homework.get_recent_homework_events(db),
        "reading_history": await homework.get_reading_history(db, today),
        "homework_form": extra.get("homework_form"),
        "homework_error": extra.get("homework_error"),
        "word_lists": await homework.get_admin_word_lists(db),
        "words_form": extra.get("words_form"),
        "words_error": extra.get("words_error"),
        "handwriting_style": await homework.get_handwriting_style(db),
        "handwriting_styles": homework.HANDWRITING_STYLES,
    }


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
        return admin_error("profile-year")
    address = email.strip() or None
    if address is not None and (len(address) > MAX_EMAIL or not EMAIL_PATTERN.fullmatch(address)):
        return admin_error("profile-email")
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE profiles SET school_year = ?, is_parent = ?, email = ? WHERE id = ?",
            (year, int(is_parent), address, profile_id),
        )
        await db.commit()
    if not cursor.rowcount:
        return admin_error("profile-missing")
    return RedirectResponse(url=admin_url("family"), status_code=303)


# --- Avatars (spec 10.9) ---
# Like every Admin write, these are also guarded by app/upload_guard.py
# (session and size checked before the body is read; the photo gets 8 MB).


@router.post("/profiles/{profile_id}/avatar", dependencies=[Depends(require_admin)])
async def save_avatar(profile_id: int, kind: str = Form(...), emoji: str = Form(""), custom_emoji: str = Form("")):
    """Initial, Emoji or Photo. A typed emoji wins over the grid's; Photo
    needs a photo uploaded first. The stored emoji and photo are kept when
    another kind is picked, so switching back is one tap."""
    if kind not in avatars.KINDS:
        return admin_error("avatar-kind")
    chosen = None
    submitted = custom_emoji.strip() or emoji.strip()
    if kind == "emoji" and submitted:
        try:
            chosen = avatars.clean_emoji(submitted)
        except avatars.AvatarError as exc:
            return admin_error(exc.code)
    async with get_db() as db:
        row = await (
            await db.execute("SELECT avatar_emoji, avatar_hash FROM profiles WHERE id = ?", (profile_id,))
        ).fetchone()
        if row is None:
            return admin_error("profile-missing")
        if kind == "photo" and not row["avatar_hash"]:
            return admin_error("avatar-no-photo")
        if kind == "emoji" and not (chosen or row["avatar_emoji"]):
            return admin_error("avatar-emoji")
        await db.execute(
            "UPDATE profiles SET avatar_kind = ?, avatar_emoji = ? WHERE id = ?",
            (kind, chosen or row["avatar_emoji"], profile_id),
        )
        await db.commit()
    return RedirectResponse(url=admin_url("family"), status_code=303)


@router.post("/profiles/{profile_id}/avatar/photo", dependencies=[Depends(require_admin)])
async def upload_avatar_photo(request: Request, profile_id: int):
    """A new photo: cropped round (square here; the circle is CSS), 256 px,
    EXIF stripped, stored in the database, and made the avatar."""
    try:
        async with request.form(max_files=1, max_fields=2) as form:
            upload = form.get("photo")
            if not isinstance(upload, StarletteUploadFile):
                return admin_error("avatar-empty")
            # One byte past the cap, so an oversized file is refused unread.
            data = await upload.read(avatars.MAX_PHOTO_BYTES + 1)
    except StarletteHTTPException:  # more parts than max_files / max_fields
        return admin_error("avatar-bad-type")
    if not data:
        return admin_error("avatar-empty")
    try:
        photo = await asyncio.to_thread(avatars.process_photo, data)
    except avatars.AvatarError as exc:
        return admin_error(exc.code)
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE profiles SET avatar_kind = 'photo', avatar_photo = ?, avatar_hash = ? WHERE id = ?",
            (photo, avatars.photo_hash(photo), profile_id),
        )
        await db.commit()
    if not cursor.rowcount:
        return admin_error("profile-missing")
    return RedirectResponse(url=admin_url("family"), status_code=303)


@router.post("/profiles/{profile_id}/avatar/photo/remove", dependencies=[Depends(require_admin)])
async def remove_avatar_photo(profile_id: int):
    """Deletes the photo; a photo avatar goes back to the initial."""
    async with get_db() as db:
        cursor = await db.execute(
            """UPDATE profiles SET avatar_photo = NULL, avatar_hash = NULL,
                   avatar_kind = CASE avatar_kind WHEN 'photo' THEN 'initial' ELSE avatar_kind END
               WHERE id = ?""",
            (profile_id,),
        )
        await db.commit()
    if not cursor.rowcount:
        return admin_error("profile-missing")
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
        return admin_error("task-group")
    async with get_db() as db:
        task = await (
            await db.execute(
                "SELECT profile_id, is_recurring, time_of_day, due_on FROM tasks WHERE id = ? AND archived = 0",
                (task_id,),
            )
        ).fetchone()
        if task is None:
            return admin_error("task-missing")
        if time_of_day is None:
            group = task["time_of_day"]
        # A task made a one-off is due from today, not marked late for the
        # days it spent repeating. Any other edit leaves due_on alone (a
        # pre-0004 one-off's NULL means "never late").
        due_on = task["due_on"]
        if not recurring and task["is_recurring"]:
            due_on = (await common.family_today(db)).isoformat()
        if profile_id == task["profile_id"]:
            # The schedule and group are local-only (never pushed, never
            # reconciled), so don't bump updated_at or queue a push: that would
            # overwrite a newer rename/tick in Google with this row's stale copy.
            await db.execute(
                "UPDATE tasks SET is_recurring = ?, recurrence_rule = ?, time_of_day = ?, due_on = ? WHERE id = ?",
                (int(recurring), rule, group, due_on, task_id),
            )
        else:
            target = await (
                await db.execute("SELECT google_tasklist_id FROM profiles WHERE id = ?", (profile_id,))
            ).fetchone()
            if target is None:
                return admin_error("task-unknown-person")
            if not target["google_tasklist_id"]:
                # Its Google copy would be deleted, leaving a task nothing can rename or remove.
                return admin_error("task-no-list")
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
            return admin_error("task-group-times")
    return RedirectResponse(url=admin_url("tasks"), status_code=303)


# --- Homework + practice words (not synced to Google) ---


@router.post("/homework", dependencies=[Depends(require_admin)])
async def add_homework(
    request: Request,
    profile_id: str = Form(""),
    subject: str = Form(""),
    subject_key: str = Form(""),
    title: str = Form(""),
    details: str = Form(""),
    due_date: str = Form(""),
):
    homework_id = None
    async with get_db() as db:
        try:
            fields = await homework.homework_fields(db, profile_id, subject, title, details, due_date, subject_key)
        except homework.ValidationError as exc:
            return await render_admin(
                request,
                tab="family",
                homework_error=str(exc),
                status_code=400,
                homework_form={
                    "id": homework_id,
                    "profile_id": profile_id,
                    "subject": subject,
                    "subject_pick": subject_key,
                    "title": title,
                    "details": details,
                    "due_date": due_date,
                },
            )
        await db.execute(
            "INSERT INTO homework (profile_id, subject, title, details, due_date, subject_key) VALUES (?, ?, ?, ?, ?, ?)",
            fields,
        )
        await db.commit()
    return RedirectResponse(url=admin_url("homework"), status_code=303)


@router.post("/homework/{homework_id}/edit", dependencies=[Depends(require_admin)])
async def edit_homework(
    request: Request,
    homework_id: int,
    profile_id: str = Form(""),
    subject: str = Form(""),
    subject_key: str = Form(""),
    title: str = Form(""),
    details: str = Form(""),
    due_date: str = Form(""),
):
    async with get_db() as db:
        if not await (await db.execute("SELECT 1 FROM homework WHERE id = ?", (homework_id,))).fetchone():
            return admin_error("homework-missing")
        try:
            fields = await homework.homework_fields(db, profile_id, subject, title, details, due_date, subject_key)
        except homework.ValidationError as exc:
            return await render_admin(
                request,
                tab="family",
                homework_error=str(exc),
                status_code=400,
                homework_form={
                    "id": homework_id,
                    "profile_id": profile_id,
                    "subject": subject,
                    "subject_pick": subject_key,
                    "title": title,
                    "details": details,
                    "due_date": due_date,
                },
            )
        await db.execute(
            """UPDATE homework SET profile_id = ?, subject = ?, title = ?, details = ?, due_date = ?,
                   subject_key = ?, updated_at = datetime('now') WHERE id = ?""",
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
            return await render_admin(
                request,
                tab="family",
                words_error=str(exc),
                status_code=400,
                words_form={
                    "id": list_id,
                    "profile_id": profile_id,
                    "title": title,
                    "words": words,
                    "starts_on": starts_on,
                    "ends_on": ends_on,
                },
            )
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
            return admin_error("words-missing")
        try:
            fields = await homework.word_list_fields(db, profile_id, title, words, starts_on, ends_on)
        except homework.ValidationError as exc:
            return await render_admin(
                request,
                tab="family",
                words_error=str(exc),
                status_code=400,
                words_form={
                    "id": list_id,
                    "profile_id": profile_id,
                    "title": title,
                    "words": words,
                    "starts_on": starts_on,
                    "ends_on": ends_on,
                },
            )
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
        return admin_error("handwriting-style")
    async with get_db() as db:
        await set_setting(db, homework.HANDWRITING_SETTING, style)
        await db.commit()
    return RedirectResponse(url=admin_url("practice-words"), status_code=303)
