"""
Gmail reads for the school email import (app/school_email.py): list the
messages matching a search, read one message's headers, its plain-text
body and its PDF/image attachments. Read-only (gmail.readonly), plain httpx
like app/google_tasks.py, no SDK.

Nothing here logs: message content (subjects, bodies, attachments) and
tokens must never reach a log. Errors propagate as httpx.HTTPError for the
caller to map to a log-safe code.
"""

import base64
import binascii
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses
from html.parser import HTMLParser
from urllib.parse import quote

from app import http_client
from app.google_oauth import get_all_pages

API = "https://gmail.googleapis.com/gmail/v1/users/me"
MESSAGES_ENDPOINT = f"{API}/messages"
PAGE_SIZE = 100

# Headers read before anything else is fetched: enough to re-check who a
# message is from and who else is on it. No subject, no body.
CHECK_HEADERS = ("From", "To", "Cc", "Reply-To")


def _auth(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _message_url(message_id: str) -> str:
    return f"{MESSAGES_ENDPOINT}/{quote(message_id, safe='')}"


async def list_message_ids(access_token: str, query: str, limit: int) -> list[str]:
    """Ids of the messages matching a Gmail search, newest first, every page
    (up to `limit`). Spam and Trash are left out, as in Gmail itself."""
    items = await get_all_pages(
        MESSAGES_ENDPOINT, access_token, {"q": query, "maxResults": PAGE_SIZE},
        items_key="messages", limit=limit,
    )
    return [item["id"] for item in items if isinstance(item, dict) and item.get("id")]


def _headers(payload: dict) -> dict[str, str]:
    """Header name (lower-cased) -> value; the first of repeated headers."""
    found: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = str(header.get("name", "")).lower()
        if name and name not in found:
            found[name] = str(header.get("value", ""))
    return found


def addresses(value: str | None) -> list[str]:
    """The lower-cased email addresses in an address header."""
    return [addr.strip().lower() for _, addr in getaddresses([value or ""]) if "@" in addr]


async def get_envelope(access_token: str, message_id: str) -> dict[str, list[str]]:
    """{"from": [...], "to": [...], "cc": [...], "reply-to": [...]}: who a
    message is from and to, fetched as metadata only (no body)."""
    params = [("format", "metadata"), *(("metadataHeaders", h) for h in CHECK_HEADERS)]
    async with http_client.client() as client:
        resp = await client.get(_message_url(message_id), headers=_auth(access_token), params=params)
        resp.raise_for_status()
        payload = resp.json().get("payload") or {}
    headers = _headers(payload)
    return {name.lower(): addresses(headers.get(name.lower())) for name in CHECK_HEADERS}


@dataclass
class AttachmentPart:
    filename: str
    mime_type: str
    size: int
    attachment_id: str | None = None
    inline_data: bytes | None = field(default=None, repr=False)


@dataclass
class Message:
    id: str
    sender: str                   # the From header as sent
    from_addresses: list[str]
    to_addresses: list[str]       # To, Cc and Reply-To together
    subject: str = field(repr=False)
    received_at: datetime | None
    text: str = field(repr=False)
    attachments: list[AttachmentPart]


def _decode(data: str | None) -> bytes:
    if not data:
        return b""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return b""


def _charset(part: dict) -> str:
    for header in part.get("headers") or []:
        if str(header.get("name", "")).lower() == "content-type":
            match = re.search(r'charset="?([\w.-]+)"?', str(header.get("value", "")), re.I)
            if match:
                return match.group(1)
    return "utf-8"


def _text_of(part: dict) -> str:
    raw = _decode((part.get("body") or {}).get("data"))
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:  # an unknown charset name
        return raw.decode("utf-8", errors="replace")


class _HTMLText(HTMLParser):
    """HTML -> readable plain text: block elements become line breaks,
    scripts/styles/heads are dropped, entities are decoded."""

    BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol",
              "section", "article", "header", "footer", "blockquote", "hr"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(markup)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # malformed markup: fall back to crude tag stripping
        text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _walk(part: dict, plain: list[str], rich: list[str], files: list[AttachmentPart]):
    mime = str(part.get("mimeType", "")).lower()
    body = part.get("body") or {}
    filename = str(part.get("filename") or "")
    if mime.startswith("multipart/"):
        for child in part.get("parts") or []:
            _walk(child, plain, rich, files)
        return
    if filename or body.get("attachmentId"):
        files.append(AttachmentPart(
            filename=filename or "attachment",
            mime_type=mime,
            size=int(body.get("size") or 0),
            attachment_id=body.get("attachmentId"),
            inline_data=None if body.get("attachmentId") else _decode(body.get("data")),
        ))
    elif mime == "text/plain":
        plain.append(_text_of(part))
    elif mime == "text/html":
        rich.append(_text_of(part))


def parse_message(raw: dict) -> Message:
    """A Gmail `format=full` message as a Message. The body is the text/plain
    part(s), else the HTML part(s) converted to text."""
    payload = raw.get("payload") or {}
    headers = _headers(payload)
    plain: list[str] = []
    rich: list[str] = []
    files: list[AttachmentPart] = []
    _walk(payload, plain, rich, files)
    text = "\n\n".join(t.strip() for t in plain if t.strip())
    if not text:
        text = "\n\n".join(html_to_text(t) for t in rich if t.strip())
    received = None
    try:
        received = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        pass
    return Message(
        id=str(raw.get("id", "")),
        sender=headers.get("from", ""),
        from_addresses=addresses(headers.get("from")),
        to_addresses=[a for name in ("to", "cc", "reply-to") for a in addresses(headers.get(name))],
        subject=headers.get("subject", ""),
        received_at=received,
        text=text,
        attachments=files,
    )


async def get_message(access_token: str, message_id: str) -> Message:
    async with http_client.client() as client:
        resp = await client.get(_message_url(message_id), headers=_auth(access_token), params={"format": "full"})
        resp.raise_for_status()
        return parse_message(resp.json())


async def get_attachment(access_token: str, message_id: str, part: AttachmentPart) -> bytes:
    """An attachment's bytes (inline ones are already in the message)."""
    if part.attachment_id is None:
        return part.inline_data or b""
    url = f"{_message_url(message_id)}/attachments/{quote(part.attachment_id, safe='')}"
    async with http_client.client() as client:
        resp = await client.get(url, headers=_auth(access_token))
        resp.raise_for_status()
        return _decode(resp.json().get("data"))
