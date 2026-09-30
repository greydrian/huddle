"""Google & Sync tab: the calendar options (the Family calendar, default
view, whose calendar is whose) and the Google Tasks list links. Connecting,
the calendar picker and "Sync now" are routers/calendar.py and routers/sync.py."""

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import google_oauth, google_tasks, sync_status, task_sync
from app.admin_tabs import admin_url
from app.auth import require_admin
from app.database import get_db
from app.routers.admin import common
from app.routers.admin.common import admin_error, tab_context
from app.services import calendar_prefs

router = APIRouter(prefix="/admin")


@tab_context("google")
async def google_context(db, base: dict, extra: dict) -> dict:
    google_account = base["google_account"]
    context = await common.google_lists(db, google_account)
    context["sync"] = await sync_status.summary(db)
    if google_account:
        selected = await google_oauth.get_selected_calendars(db)
        context["selected_calendar_ids"] = [c["id"] for c in selected]
        context["shopping_tasklist"] = await task_sync.get_shopping_tasklist(db)
        # Calendar options (spec 10.5): the Family calendar is picked from
        # the shown calendars this account can write to.
        selected_ids = set(context["selected_calendar_ids"])
        context.update(
            {
                "selected_calendars": selected,
                "family_choices": [
                    c for c in context["available_calendars"] if c.get("writable") and c["id"] in selected_ids
                ],
                "family_calendar": await calendar_prefs.get_family_setting(db),
                "family_calendar_active": await calendar_prefs.get_family_calendar(db),
                "calendar_events_scope": await google_oauth.has_scope(db, google_oauth.CALENDAR_EVENTS_SCOPE),
                "calendar_default_view": await calendar_prefs.get_default_view(db),
                "calendar_views": calendar_prefs.VIEWS,
                "calendar_people": await calendar_prefs.get_people_links(db),
            }
        )
    return context


# --- Calendar options (spec 10.5): the Family calendar, default view, whose calendar ---


@router.post("/google/family-calendar", dependencies=[Depends(require_admin)])
async def save_family_calendar(calendar_id: str = Form("")):
    """The one calendar the wall's "+" adds to: shown on the wall and
    writable, re-checked with Google rather than trusting the form. Blank
    turns adding off."""
    async with get_db() as db:
        if not calendar_id:
            await calendar_prefs.set_family_calendar(db, None)
            return RedirectResponse(url=admin_url("calendar-options"), status_code=303)
        selected = {cal.get("id") for cal in await google_oauth.get_selected_calendars(db)}
        try:
            access_token = await google_oauth.get_valid_access_token(db)
            available = await google_oauth.fetch_calendar_list(access_token) if access_token else []
        except httpx.HTTPError:
            available = []
        match = next((c for c in available if c["id"] == calendar_id and c["writable"] and c["id"] in selected), None)
        if match is None:
            return admin_error("calendar-family")
        await calendar_prefs.set_family_calendar(db, match)
    return RedirectResponse(url=admin_url("calendar-options"), status_code=303)


@router.post("/google/calendar-view", dependencies=[Depends(require_admin)])
async def save_calendar_view(view: str = Form("")):
    async with get_db() as db:
        try:
            await calendar_prefs.set_default_view(db, view)
        except ValueError:
            return admin_error("calendar-view")
    return RedirectResponse(url=admin_url("calendar-options"), status_code=303)


@router.post("/google/calendar-people", dependencies=[Depends(require_admin)])
async def save_calendar_people(request: Request):
    """Whose events each shown calendar holds: owner_<n> is a profile id or
    "everyone" for calendar_<n>. Only calendars shown on the wall and
    profiles that exist are kept; anything else counts as Everyone."""
    form = await request.form()
    async with get_db() as db:
        selected = {cal.get("id") for cal in await google_oauth.get_selected_calendars(db)}
        profile_ids = {row["id"] for row in await (await db.execute("SELECT id FROM profiles")).fetchall()}
        links: dict[str, int | str] = {}
        for key, cal_id in form.multi_items():
            if not key.startswith("calendar_") or not isinstance(cal_id, str) or cal_id not in selected:
                continue
            owner = str(form.get("owner_" + key.removeprefix("calendar_")) or "")
            if owner.isdigit() and int(owner) in profile_ids:
                links[cal_id] = int(owner)
            else:
                links[cal_id] = calendar_prefs.EVERYONE
        await calendar_prefs.set_people_links(db, links)
    return RedirectResponse(url=admin_url("calendar-options"), status_code=303)


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
            await db.execute("UPDATE profiles SET google_tasklist_id = ? WHERE id = ?", (tasklist_id, profile["id"]))
            await task_sync.relink_profile(db, profile["id"])
        await db.commit()
    return RedirectResponse(url=admin_url("task-lists"), status_code=303)
