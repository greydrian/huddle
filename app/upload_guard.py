"""
Guards Admin's upload routes before their body is read.

FastAPI parses a multipart form (spooling files to disk) before any route
dependency runs, so `require_admin` alone would let anyone on the LAN post
hundreds of MB before being turned away. This ASGI middleware checks the
admin session from the headers first — refusing without touching the body —
and caps the body at the route's limit: by Content-Length when sent, and by
counting streamed bytes otherwise (413 either way).
"""

import re

from fastapi import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from app.auth import NEW_PIN_PATH, admin_session_state

MAX_UPLOAD_BODY = 25 * 1024 * 1024  # the School inbox: up to 3 files
# A family member's avatar photo (spec 10.9): one file of at most 8 MB
# (avatars.MAX_PHOTO_BYTES), plus room for the multipart framing.
MAX_AVATAR_BODY = 8 * 1024 * 1024 + 64 * 1024

# Each guarded path (a full match) and its body cap.
GUARDED_PATHS = (
    (re.compile(r"/admin/inbox/add"), MAX_UPLOAD_BODY),
    # Any id segment (not just digits): "-1" or "+1" still reach the route.
    (re.compile(r"/admin/profiles/[^/]+/avatar/photo"), MAX_AVATAR_BODY),
)


def body_cap(path: str) -> int | None:
    """The body cap for a guarded path, or None if it isn't guarded."""
    return next((cap for pattern, cap in GUARDED_PATHS if pattern.fullmatch(path)), None)


class BodyTooLarge(Exception):
    pass


class UploadGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        cap = body_cap(scope["path"]) if scope["type"] == "http" and scope["method"] == "POST" else None
        if cap is None:
            return await self.app(scope, receive, send)

        request = Request(scope)  # headers and cookies only; receive isn't passed
        valid, pin_is_default = await admin_session_state(request)
        if not valid:
            return await RedirectResponse("/admin/login", status_code=303)(scope, receive, send)
        if pin_is_default:
            return await RedirectResponse(NEW_PIN_PATH, status_code=303)(scope, receive, send)

        too_large = PlainTextResponse("Upload too large", status_code=413)
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > cap:
            return await too_large(scope, receive, send)

        received = 0
        started = False

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    raise BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await too_large(scope, receive, send)
