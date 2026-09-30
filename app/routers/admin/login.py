"""Signing in and out, the lockout, and the PIN itself (spec 4.7): the
single shared PIN, exponential backoff on failed attempts, the forced
"Choose a new PIN" screen while the PIN is the default, and Change PIN
(System tab). Sessions themselves are app/auth.py."""

import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import admin_tabs, appearance
from app.admin_tabs import admin_url
from app.auth import (
    NEW_PIN_PATH,
    PIN_IS_DEFAULT_SETTING,
    bump_session_generation,
    end_session,
    has_valid_session,
    require_admin,
    require_session,
    session_generation,
    start_session,
)
from app.database import get_db, get_setting, set_setting
from app.routers.admin.common import ADMIN_ERRORS, admin_error
from app.security import (
    FAILURE_DECAY_SECONDS,
    LONG_LOCKOUT_AFTER,
    hash_pin,
    is_weak_pin,
    lockout_seconds_for,
    verify_pin,
)
from app.templating import templates

router = APIRouter(prefix="/admin")

# The login form only takes digits (inputmode=numeric, maxlength=8), so a PIN
# it can't type would lock the family out of Admin.
PIN_PATTERN = re.compile(r"[0-9]{4,8}")


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
        return (
            f"Admin is locked after {failed_attempts} wrong PINs in a row. Try again in {_format_wait(wait_seconds)}."
        )
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
        request,
        "admin/login.html",
        {
            "error": error,
            "appearance": mode,
            "next_url": "" if back_to == "/admin" else back_to,
            "section": section if section in admin_tabs.SECTIONS else "",
        },
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
            json.dumps(
                {
                    "failed_attempts": failed_attempts,
                    "locked_until": (now + timedelta(seconds=wait)).isoformat(),
                    "last_failed_at": now.isoformat(),
                }
            ),
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
        return admin_error(error)
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
@router.get(NEW_PIN_PATH.removeprefix("/admin"), response_class=HTMLResponse, dependencies=[Depends(require_session)])
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
