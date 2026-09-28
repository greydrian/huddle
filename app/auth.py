"""
Admin session handling (Section 4.7): the signed, short-lived session cookie
set after a correct PIN, and the `require_admin` dependency that guards every
Admin route. PIN checking and lockout live in routers/admin.py.

Every token carries the session generation it was issued under. Logging out
or changing the PIN bumps the generation, so any copy of an older cookie
stops working at once rather than living out its 2 hours.
"""

from fastapi import HTTPException, Request
from fastapi.responses import Response

from app.database import get_db, get_setting, set_setting
from app.security import create_session_token, verify_session_token

SESSION_COOKIE = "admin_session"
SESSION_GENERATION_SETTING = "session_generation"
# "1" while the PIN is still the seeded default (set by init_db); Admin then
# shows only the "Choose a new PIN" screen until it's changed.
PIN_IS_DEFAULT_SETTING = "pin_is_default"
NEW_PIN_PATH = "/admin/new-pin"


async def session_generation(db) -> int:
    return int(await get_setting(db, SESSION_GENERATION_SETTING, "0"))


async def bump_session_generation(db) -> int:
    """Ends every admin session. The caller commits."""
    generation = await session_generation(db) + 1
    await set_setting(db, SESSION_GENERATION_SETTING, str(generation))
    return generation


async def admin_session_state(request: Request) -> tuple[bool, bool]:
    """(has a valid session, PIN is still the default), over one connection.
    Reads only headers, so upload_guard can call it before the body."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False, False
    async with get_db() as db:
        generation = await session_generation(db)
        pin_is_default = await get_setting(db, PIN_IS_DEFAULT_SETTING) == "1"
    return verify_session_token(token, generation), pin_is_default


async def has_valid_session(request: Request) -> bool:
    return (await admin_session_state(request))[0]


def _to_login() -> HTTPException:
    return HTTPException(status_code=303, headers={"Location": "/admin/login"})


async def require_session(request: Request) -> None:
    """FastAPI dependency: a valid session, even if the PIN is still the
    default. Only the forced "Choose a new PIN" screen uses it directly."""
    if not await has_valid_session(request):
        raise _to_login()


async def require_admin(request: Request) -> None:
    """FastAPI dependency: bounce to the login page without a valid session,
    and to the "Choose a new PIN" screen while the PIN is the default."""
    valid, pin_is_default = await admin_session_state(request)
    if not valid:
        raise _to_login()
    if pin_is_default:
        raise HTTPException(status_code=303, headers={"Location": NEW_PIN_PATH})


def _is_https(request: Request) -> bool:
    # Behind a TLS-terminating proxy the app itself sees plain HTTP.
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def start_session(response: Response, request: Request, generation: int) -> None:
    # Lax, not Strict: Google's OAuth return (/admin/google/callback) is a
    # cross-site top-level navigation that needs the cookie. Secure only over
    # HTTPS, so plain-HTTP LAN use keeps working.
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(generation),
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )


def end_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax")
