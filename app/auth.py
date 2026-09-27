"""
Admin session handling (Section 4.7): the signed, short-lived session cookie
set after a correct PIN, and the `require_admin` dependency that guards every
Admin route. PIN checking and lockout live in routers/admin.py.
"""

from fastapi import HTTPException, Request
from fastapi.responses import Response

from app.security import create_session_token, verify_session_token

SESSION_COOKIE = "admin_session"


async def require_admin(request: Request) -> None:
    """FastAPI dependency: bounce to the login page without a valid session."""
    token = request.cookies.get(SESSION_COOKIE)
    if not verify_session_token(token):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


def start_session(response: Response) -> None:
    response.set_cookie(SESSION_COOKIE, create_session_token(), httponly=True, samesite="lax")


def end_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)
