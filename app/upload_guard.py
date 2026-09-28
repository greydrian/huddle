"""
Guards Admin's upload route before its body is read.

FastAPI parses a multipart form (spooling files to disk) before any route
dependency runs, so `require_admin` alone would let anyone on the LAN post
hundreds of MB before being turned away. This ASGI middleware checks the
admin session from the headers first — refusing without touching the body —
and caps the body at MAX_UPLOAD_BODY: by Content-Length when sent, and by
counting streamed bytes otherwise (413 either way).
"""

from fastapi import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from app.auth import NEW_PIN_PATH, admin_session_state

GUARDED_PATHS = ("/admin/inbox/add",)
MAX_UPLOAD_BODY = 25 * 1024 * 1024


class BodyTooLarge(Exception):
    pass


class UploadGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in GUARDED_PATHS:
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
        if declared > MAX_UPLOAD_BODY:
            return await too_large(scope, receive, send)

        received = 0
        started = False

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_UPLOAD_BODY:
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
