"""Google & Sync tab: the Google accounts panel (spec 12.2: add, edit,
reconnect, remove, and the OAuth callback), the calendar picker grouped by
account, the calendar options (the Family calendar, default view, whose
calendar is whose) and the Google Tasks list links. "Sync now" is
routers/sync.py."""

import logging
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import google_accounts, google_oauth, google_tasks, http_client, sync_status, task_sync
from app.admin_tabs import admin_url
from app.auth import require_admin
from app.database import get_db
from app.routers.admin import common
from app.routers.admin.common import admin_error, tab_context
from app.services import accounts, calendar_prefs

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

STATE_COOKIE = "google_oauth_state"
# What each consent screen is for, by its state: {"mode": "add", "owner",
# "jobs"} or {"mode": "reconnect", "account"}. In memory: single process, and
# a restart mid-consent just asks for another try.
PENDING_SECONDS = 600
_pending: dict[str, dict] = {}


@tab_context("google")
async def google_context(db, base: dict, extra: dict) -> dict:
    accounts_list = base["google_accounts"]
    context = await common.google_lists(db, accounts_list)
    context["sync"] = await sync_status.summary(db)
    selected = await google_oauth.get_selected_calendars(db)
    selected_keys = {c["key"] for c in selected}
    emails = {a["id"]: a["email"] or f"Account {a['id']}" for a in accounts_list}
    through = {c["id"]: c["account"] for c in selected if c["id"] != "primary"}
    notice = await google_accounts.removed_notice(db)
    for group in context["calendar_groups"]:
        account_id = group["account"]["id"]
        for cal in group["calendars"]:
            cal["selected"] = cal["key"] in selected_keys or (
                cal["primary"] and google_oauth.calendar_key(account_id, "primary") in selected_keys
            )
            other = through.get(cal["id"])
            cal["shown_through"] = emails.get(other) if other is not None and other != account_id else None
            cal["was_removed"] = cal["id"] in notice.get("calendars", []) and cal["id"] not in through
    writer = await google_accounts.job_account(db, "write_events")
    removing = extra.get("google_remove")
    removal = None
    if removing is not None and (account := await google_accounts.get(db, removing)) is not None:
        removal = {"account": account, "preview": await accounts.removal_preview(db, account)}
    context.update(
        {
            "jobs": google_accounts.JOBS,
            "job_hints": google_accounts.JOB_HINTS,
            "job_unticked": google_accounts.JOB_UNTICKED,
            "exclusive_jobs": google_accounts.EXCLUSIVE_JOBS,
            "parent_jobs": google_accounts.PARENT_JOBS,
            "job_holders": await google_accounts.job_holders(db),
            "account_emails": emails,
            "removal": removal,
            "removed_notice": notice,
            "selected_calendars": selected,
            "shopping_tasklist": await task_sync.get_shopping_tasklist(db),
            # Calendar options (spec 10.5): the Family calendar is picked from the
            # shown calendars the account doing Writing events can write to.
            "writer_account": writer,
            "family_choices": [
                c
                for c in context["available_calendars"]
                if writer and c["account"] == writer["id"] and c.get("writable") and c["key"] in selected_keys
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


# --- Google accounts (spec 12.2) ----------------------------------------------------------


def _callback_redirect_uri(request: Request) -> str:
    return str(request.url_for("google_callback"))


def _go_to_google(request: Request, intent: dict, jobs, login_hint: str | None = None) -> RedirectResponse:
    now = time.monotonic()
    for stale in [s for s, p in _pending.items() if p["at"] < now - PENDING_SECONDS]:
        del _pending[stale]
    state = secrets.token_urlsafe(24)
    _pending[state] = {**intent, "at": now}
    auth_url = google_oauth.build_auth_url(state, _callback_redirect_uri(request), jobs, login_hint)
    response = RedirectResponse(url=auth_url, status_code=303)
    response.set_cookie(STATE_COOKIE, state, httponly=True, samesite="lax", max_age=PENDING_SECONDS)
    return response


async def _owner(db, value: str) -> tuple[bool, int | None]:
    """(valid, profile id or None for Family) from an owner field."""
    if value in ("", "family"):
        return True, None
    if not value.isdigit():
        return False, None
    row = await (await db.execute("SELECT id FROM profiles WHERE id = ?", (int(value),))).fetchone()
    return row is not None, int(value) if row else None


def _refused(refused: dict[str, str]) -> RedirectResponse:
    return admin_error("google-job-taken" if "taken" in refused.values() else "google-job-parent")


@router.post("/google/accounts", dependencies=[Depends(require_admin)])
async def add_google_account(request: Request, owner: str = Form(""), job: list[str] = Form(default=[])):
    """Add account: the owner and the ticked jobs, then off to Google for
    only those jobs' scopes. Google's account chooser picks the account;
    the callback learns its address."""
    if not google_oauth.is_configured():
        return RedirectResponse(url=admin_url("google"), status_code=303)
    async with get_db() as db:
        valid, owner_id = await _owner(db, owner)
        if not valid:
            return admin_error("google-owner")
        jobs = google_accounts.normalise_jobs(job)
        if not jobs:
            return admin_error("google-jobs")
        _kept, refused = await google_accounts.allowed_jobs(db, jobs, owner_id)
    if refused:
        return _refused(refused)
    return _go_to_google(request, {"mode": "add", "owner": owner_id, "jobs": jobs}, jobs)


@router.get("/google/accounts/{account_id}/reconnect", dependencies=[Depends(require_admin)])
async def reconnect_google_account(request: Request, account_id: int):
    """Google's consent screen again for the same address (login_hint) and
    its ticked jobs: renews a revoked sign-in or grants a missing permission."""
    if not google_oauth.is_configured():
        return RedirectResponse(url=admin_url("google"), status_code=303)
    async with get_db() as db:
        account = await google_accounts.get(db, account_id)
    if account is None:
        return admin_error("google-missing")
    return _go_to_google(
        request, {"mode": "reconnect", "account": account_id}, account["jobs"], login_hint=account["email"]
    )


@router.get("/google/callback", name="google_callback", dependencies=[Depends(require_admin)])
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    expected_state = request.cookies.get(STATE_COOKIE)
    intent = _pending.pop(state, None) if state and state == expected_state else None

    def back(error_code: str | None = None) -> RedirectResponse:
        if error_code:
            response = admin_error(error_code)
        else:
            response = RedirectResponse(url=admin_url("google"), status_code=303)
        response.delete_cookie(STATE_COOKIE)
        return response

    if error:
        # Cancelled, or refused (e.g. a Workspace admin's policy on unverified apps).
        return back("google-denied")
    if not code or intent is None:
        # No code, or a missing/mismatched/expired state: nothing to salvage.
        return back("google-signin" if code else None)

    try:
        tokens = await google_oauth.exchange_code_for_tokens(code, _callback_redirect_uri(request))
        tokens["expires_at"] = google_oauth.expires_at(tokens)
        userinfo = await google_oauth.fetch_userinfo(tokens["access_token"])
    except httpx.HTTPError as exc:
        logger.warning("Google OAuth callback failed: %s", http_client.describe(exc))
        return back("google-signin")
    email = userinfo.get("email")
    if not isinstance(email, str) or not email:
        await google_oauth.revoke(tokens)
        return back("google-signin")

    async with get_db() as db:
        if intent["mode"] == "reconnect":
            account = await google_accounts.get(db, intent["account"])
            if account is None:
                await google_oauth.revoke(tokens)
                return back("google-missing")
            if account["email"] and account["email"].lower() != email.lower():
                # Never another account's grant under this row: withdraw it again.
                await google_oauth.revoke(tokens)
                return back("google-wrong-account")
            old = await google_accounts.load_tokens(db, account["id"]) or {}
            if not tokens.get("refresh_token") and old.get("refresh_token"):
                tokens["refresh_token"] = old["refresh_token"]
            await google_accounts.save_tokens(db, account["id"], tokens)
            await google_accounts.set_address(db, account["id"], email)
            await google_accounts.note_check(db, account["id"], ok=True)
            account_id = account["id"]
            logger.info("Google account %d reconnected", account_id)
        else:
            account_id = await google_accounts.add_or_merge(db, email, tokens, intent["owner"], intent["jobs"])
        connected = await google_accounts.get(db, account_id)
        if connected and "tasks" in connected["jobs"]:
            # A fresh grant: whatever the old one's sync failures were, they're over.
            await sync_status.reset(db)
    return back()


@router.post("/google/accounts/{account_id}/edit", dependencies=[Depends(require_admin)])
async def edit_google_account(request: Request, account_id: int, owner: str = Form(""), job: list[str] = Form([])):
    """Owner and jobs. A job unticked stops at once (its permission stays
    until the account is removed); a job ticked that Google hasn't allowed
    goes through the consent screen for it."""
    async with get_db() as db:
        account = await google_accounts.get(db, account_id)
        if account is None:
            return admin_error("google-missing")
        valid, owner_id = await _owner(db, owner)
        if not valid:
            return admin_error("google-owner")
        jobs = google_accounts.normalise_jobs(job)
        if not jobs:
            return admin_error("google-jobs")
        kept, refused = await google_accounts.allowed_jobs(db, jobs, owner_id, account_id)
        if refused:
            return _refused(refused)
        dropped = await accounts.apply_job_changes(db, account, kept)
        await google_accounts.update(db, account_id, owner_id, kept)
    added = [job for job in kept if job not in account["jobs"]]
    if google_oauth.is_configured() and any(google_accounts.JOB_SCOPES[j] not in account["scopes"] for j in added):
        return await reconnect_google_account(request, account_id)
    if dropped:
        return RedirectResponse(url=admin_url("google", unticked=" ".join(dropped)), status_code=303)
    return RedirectResponse(url=admin_url("google"), status_code=303)


@router.post("/google/accounts/{account_id}/remove", dependencies=[Depends(require_admin)])
async def remove_google_account(account_id: int):
    """After the confirm step (/admin?tab=google&remove=<id>): revokes the
    token and deletes the account; services/accounts says what changes."""
    async with get_db() as db:
        if not await accounts.remove(db, account_id):
            return admin_error("google-missing")
    return RedirectResponse(url=admin_url("google", removed="1"), status_code=303)


# --- Calendars shown on the wall (spec 12.3) ----------------------------------------------


@router.post("/google/calendars", dependencies=[Depends(require_admin)])
async def save_selected_calendars(calendar_id: list[str] = Form(default=[])):
    """Each ticked "<account>:<calendar id>". Summary and colour come from
    Google rather than the form. An account whose list couldn't be loaded
    keeps its calendars as they were; a calendar shared into two accounts
    is kept once, through the first."""
    ticked = {key for key in calendar_id if google_oauth.split_calendar_key(key)}
    async with get_db() as db:
        saved = await google_oauth.get_saved_calendars(db)
        if saved is None:
            saved = [{k: v for k, v in c.items() if k != "key"} for c in await google_oauth.get_selected_calendars(db)]
        lists = await common.google_lists(db, await google_accounts.list_accounts(db), tasklists=False)
        loaded = {group["account"]["id"] for group in lists["calendar_groups"] if not group["error"]}
        selected: list[dict] = [cal for cal in saved if cal["account"] not in loaded]
        seen = {cal["id"] for cal in selected if cal["id"] != "primary"}
        for cal in lists["available_calendars"]:
            if cal["key"] in ticked and cal["id"] not in seen:
                seen.add(cal["id"])
                selected.append({k: cal[k] for k in ("account", "id", "summary", "color", "primary")})
        selected.sort(key=lambda cal: cal["account"])  # stable: each account's in Google's order
        if any(cal["account"] in loaded for cal in selected):
            await google_oauth.set_selected_calendars(db, selected)
            await google_accounts.clear_removed_notice(db, "calendars")
    return RedirectResponse(url=admin_url("calendars"), status_code=303)


# --- Calendar options (spec 10.5): the Family calendar, default view, whose calendar ---


@router.post("/google/family-calendar", dependencies=[Depends(require_admin)])
async def save_family_calendar(calendar_id: str = Form("")):
    """The one calendar the wall's "+" adds to: shown on the wall and
    writable in the account doing Writing events, re-checked with Google
    rather than trusting the form. Blank turns adding off."""
    async with get_db() as db:
        if not calendar_id:
            await calendar_prefs.set_family_calendar(db, None)
            return RedirectResponse(url=admin_url("calendar-options"), status_code=303)
        selected = {cal["key"] for cal in await google_oauth.get_selected_calendars(db)}
        match = await _writable_calendar(db, calendar_id)
        if match is None or match["key"] not in selected:
            return admin_error("calendar-family")
        await calendar_prefs.set_family_calendar(db, match)
        await google_accounts.clear_removed_notice(db, "family")
    return RedirectResponse(url=admin_url("calendar-options"), status_code=303)


async def _writable_calendar(db, key: str) -> dict | None:
    """The calendar `key` names, if it's in the account doing Writing events
    and Google says that account can edit it."""
    split = google_oauth.split_calendar_key(key)
    writer = await google_accounts.job_account(db, "write_events")
    if split is None or writer is None or split[0] != writer["id"]:
        return None
    try:
        access_token = await google_oauth.get_valid_access_token(db, writer["id"])
        available = await google_oauth.fetch_calendar_list(access_token) if access_token else []
    except httpx.HTTPError:
        available = []
    cal = next((c for c in available if c["id"] == split[1] and c["writable"]), None)
    return {**cal, "account": writer["id"], "key": key} if cal else None


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
    "everyone" for calendar_<n> (a calendar key). Only calendars shown on
    the wall and profiles that exist are kept; anything else counts as
    Everyone."""
    form = await request.form()
    async with get_db() as db:
        selected = {cal["key"] for cal in await google_oauth.get_selected_calendars(db)}
        profile_ids = {row["id"] for row in await (await db.execute("SELECT id FROM profiles")).fetchall()}
        links: dict[str, int | str] = {}
        for key, cal_key in form.multi_items():
            if not key.startswith("calendar_") or not isinstance(cal_key, str) or cal_key not in selected:
                continue
            owner = str(form.get("owner_" + key.removeprefix("calendar_")) or "")
            if owner.isdigit() and int(owner) in profile_ids:
                links[cal_key] = int(owner)
            else:
                links[cal_key] = calendar_prefs.EVERYONE
        await calendar_prefs.set_people_links(db, links)
    return RedirectResponse(url=admin_url("calendar-options"), status_code=303)


# --- Google Tasks sync ---


@router.post("/google/shopping-list", dependencies=[Depends(require_admin)])
async def save_shopping_tasklist(tasklist_id: str = Form(...)):
    async with get_db() as db:
        account = await google_accounts.job_account(db, "tasks")
        try:
            access_token = await google_oauth.get_valid_access_token(db, account["id"]) if account else None
            # Re-derive the title from Google rather than trusting the form.
            available = await google_tasks.fetch_tasklists(access_token) if access_token else []
        except httpx.HTTPError:
            available = []
        match = next((t for t in available if t["id"] == tasklist_id), None)
        current = await task_sync.get_shopping_tasklist(db)
        if match and account and (current is None or current["id"] != match["id"]):
            await task_sync.set_shopping_tasklist(db, {**match, "account": account["id"]})
            await task_sync.relink_shopping(db)
    return RedirectResponse(url=admin_url("task-lists"), status_code=303)


@router.post("/google/task-lists", dependencies=[Depends(require_admin)])
async def save_profile_tasklists(request: Request):
    form = await request.form()
    async with get_db() as db:
        account = await google_accounts.job_account(db, "tasks")
        profiles = await (await db.execute("SELECT id, google_tasklist_id FROM profiles")).fetchall()
        for profile in profiles:
            tasklist_id = form.get(f"tasklist_{profile['id']}") or None
            if tasklist_id == profile["google_tasklist_id"]:
                continue
            if tasklist_id and account is None:
                continue  # no account does Tasks & shopping any more
            await db.execute(
                "UPDATE profiles SET google_tasklist_id = ?, google_account_id = ? WHERE id = ?",
                (tasklist_id, account["id"] if tasklist_id and account else None, profile["id"]),
            )
            await task_sync.relink_profile(db, profile["id"])
        await db.commit()
    return RedirectResponse(url=admin_url("task-lists"), status_code=303)
