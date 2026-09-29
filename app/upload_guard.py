"""
Guards every Admin write before its body is read.

FastAPI parses a form (spooling multipart files to disk) before any route
dependency runs, so `require_admin` alone would let anyone on the LAN post
hundreds of MB to any Admin route before being turned away. This ASGI
middleware runs first for every non-GET request under /admin:

- It checks the admin session from the headers alone, and refuses without
  touching the body: to the PIN page without a valid session, and to the
  "Choose a new PIN" screen while the PIN is the default (as require_admin
  does). /admin/login and /admin/logout need no session; /admin/new-pin
  needs one but is allowed while the PIN is the default (as require_session).
- It caps the body: the two upload routes at their own limits, every other
  Admin form at MAX_FORM_BODY. By Content-Length when sent, and by counting
  streamed bytes otherwise; 413 either way.
"""

import re

from fastapi import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from app.auth import NEW_PIN_PATH, admin_session_state

MAX_UPLOAD_BODY = 25 * 1024 * 1024  # the School inbox: up to 3 files
# A family member's avatar photo (spec 10.9): one file of at most 8 MB
# (avatars.MAX_PHOTO_BYTES), plus room for the multipart framing.
MAX_AVATAR_BODY = 8 * 1024 * 1024 + 64 * 1024
# Every other Admin form: text fields only (the longest, a word list or a
# school sender list, is a few KB).
MAX_FORM_BODY = 64 * 1024

# Upload routes (a full match) and their body caps.
UPLOAD_PATHS = (
    (re.compile(r"/admin/inbox/add"), MAX_UPLOAD_BODY),
    # Any id segment (not just digits): "-1" or "+1" still reach the route.
    (re.compile(r"/admin/profiles/[^/]+/avatar/photo"), MAX_AVATAR_BODY),
)
# Reachable without a session (each is safe signed out), still capped.
NO_SESSION_PATHS = ("/admin/login", "/admin/logout")
READ_METHODS = ("GET", "HEAD", "OPTIONS")


def is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def body_cap(path: str) -> int | None:
    """The body cap for an Admin path, or None if it isn't one."""
    if not is_admin_path(path):
        return None
    return next((cap for pattern, cap in UPLOAD_PATHS if pattern.fullmatch(path)), MAX_FORM_BODY)


class BodyTooLarge(Exception):
    pass


class UploadGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in READ_METHODS:
            return await self.app(scope, receive, send)
        path = scope["path"]
        cap = body_cap(path)
        if cap is None:
            return await self.app(scope, receive, send)

        request = Request(scope)  # headers and cookies only; receive isn't passed
        if path not in NO_SESSION_PATHS:
            valid, pin_is_default = await admin_session_state(request)
            if not valid:
                return await RedirectResponse("/admin/login", status_code=303)(scope, receive, send)
            if pin_is_default and path != NEW_PIN_PATH:
                return await RedirectResponse(NEW_PIN_PATH, status_code=303)(scope, receive, send)

        too_large = PlainTextResponse("Upload too large", status_code=413)
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > cap:
            return await too_large(scope, receive, send)

        received = 0
        exceeded = False
        started = False

        async def counting_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    exceeded = True
                    raise BodyTooLarge()
            return message

        async def tracking_send(message):
            # FastAPI turns an error while parsing a Form() body into its own
            # 400; once the cap is hit, the answer is our 413 instead.
            nonlocal started
            if exceeded:
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await too_large(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await too_large(scope, receive, send)
