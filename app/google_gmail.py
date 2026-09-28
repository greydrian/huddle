"""
Gmail reads for the school email import (app/school_email.py): list the
messages matching a search, read one message's headers, its plain-text
body and its PDF/image attachments. Read-only (gmail.readonly), plain httpx
like app/google_tasks.py, no SDK.

Bodies have their quoted history removed (strip_quoted, and quote blocks in
HTML): a reply quotes the family's own earlier message, and a quoted thread
can carry things that were never meant for the inbox.

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
from urllib.parse import quote, unquote

from app import http_client
from app.google_oauth import get_all_pages

API = "https://gmail.googleapis.com/gmail/v1/users/me"
MESSAGES_ENDPOINT = f"{API}/messages"
PAGE_SIZE = 100

# Headers read before anything else is fetched: enough to re-check who a
# message is from and who else is on it. No subject, no body.
CHECK_HEADERS = ("From", "To", "Cc", "Reply-To")
# The Authentication-Results header Gmail itself adds (a sender can add
# their own further down; only this authserv-id is trusted).
TRUSTED_AUTHSERV = "mx.google.com"


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


def _header_list(payload: dict) -> list[tuple[str, str]]:
    return [(str(h.get("name", "")).lower(), str(h.get("value", ""))) for h in payload.get("headers") or []]


def _headers(payload: dict) -> dict[str, str]:
    """Header name (lower-cased) -> value; the first of repeated headers."""
    found: dict[str, str] = {}
    for name, value in _header_list(payload):
        if name and name not in found:
            found[name] = value
    return found


def addresses(value: str | None) -> list[str]:
    """The lower-cased email addresses in an address header."""
    return [addr.strip().lower() for _, addr in getaddresses([value or ""]) if "@" in addr]


def _internal_date(raw: dict) -> int | None:
    try:
        return int(raw["internalDate"])
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class Envelope:
    addresses: dict[str, list[str]]  # "from" / "to" / "cc" / "reply-to"
    internal_date: int | None        # epoch ms


async def get_envelope(access_token: str, message_id: str) -> Envelope:
    """Who a message is from and to, and when Gmail got it, fetched as
    metadata only (no subject, no body)."""
    params = [("format", "metadata"), *(("metadataHeaders", h) for h in CHECK_HEADERS)]
    async with http_client.client() as client:
        resp = await client.get(_message_url(message_id), headers=_auth(access_token), params=params)
        resp.raise_for_status()
        raw = resp.json()
    headers = _headers(raw.get("payload") or {})
    return Envelope({name.lower(): addresses(headers.get(name.lower())) for name in CHECK_HEADERS},
                    _internal_date(raw))


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
    internal_date: int | None
    text: str = field(repr=False)  # the body without quoted history
    # Everything readable in the message (every header value, every text/HTML
    # part including quoted history), only for checking for excluded addresses.
    everything: str = field(repr=False)
    attachments: list[AttachmentPart]
    auth: dict[str, list[str]] | None  # Gmail's own SPF/DKIM/DMARC results, None if absent


def _decode(data: str | None) -> bytes:
    if not data:
        return b""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return b""


def _charset(part: dict) -> str:
    for name, value in _header_list(part):
        if name == "content-type":
            match = re.search(r'charset="?([\w.-]+)"?', value, re.I)
            if match:
                return match.group(1)
    return "utf-8"


def _text_of(part: dict) -> str:
    raw = _decode((part.get("body") or {}).get("data"))
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:  # an unknown charset name
        return raw.decode("utf-8", errors="replace")


# --- Quoted history ---

class _HTMLText(HTMLParser):
    """HTML -> readable plain text: block elements become line breaks,
    scripts/styles/heads are dropped, entities are decoded, and quoted
    history is left out: <blockquote>, Gmail's div.gmail_quote, and
    everything after Outlook's reply markers."""

    BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol",
              "section", "article", "header", "footer", "blockquote", "hr"}
    SKIP = {"script", "style", "head", "title"}
    QUOTE_CLASSES = {"gmail_quote", "gmail_quote_container", "yahoo_quoted", "moz-cite-prefix"}
    # Outlook: the quoted message follows these, as siblings, to the end.
    END_IDS = {"appendonsend", "divrplyfwdmsg", "mail-editor-reference-message-container", "stopspelling"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0
        self._quote: list[list] = []  # [tag, depth] of the quote container being skipped
        self._ended = False

    def _is_quote(self, tag, attrs) -> bool:
        values = dict(attrs)
        classes = set((values.get("class") or "").lower().split())
        return tag == "blockquote" or bool(classes & self.QUOTE_CLASSES)

    def handle_starttag(self, tag, attrs):
        if self._ended:
            return
        if (dict(attrs).get("id") or "").lower() in self.END_IDS:
            self._ended = True
            return
        if self._quote:
            if tag == self._quote[-1][0]:
                self._quote[-1][1] += 1
            return
        if self._is_quote(tag, attrs):
            self._quote.append([tag, 1])
        elif tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self._ended:
            return
        if self._quote:
            if tag == self._quote[-1][0]:
                self._quote[-1][1] -= 1
                if not self._quote[-1][1]:
                    self._quote.pop()
            return
        if tag in self.SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skipping and not self._quote and not self._ended:
            self.parts.append(data)


def _tidy(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(markup)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # malformed markup: fall back to crude tag stripping
        text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return _tidy(text)


def html_all_text(markup: str) -> str:
    """Everything in the HTML, quotes and attributes (mailto: links)
    included, entities decoded: only for the exclusion check."""
    unescaped = html.unescape(markup)
    return f"{unescaped}\n{unquote(unescaped)}"


# Where quoted history starts in a plain-text body.
_QUOTE_STARTS = (
    re.compile(r"^\s*On\b(?=.*\d).{0,300}\bwrote:\s*$", re.I | re.S),  # Gmail/Apple: "On <date>, <name> wrote:"
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I),      # Outlook, older
    re.compile(r"^\s*_{8,}\s*$"),                                       # Outlook's rule before "From:"
    re.compile(r"^\s*Le\b.{0,300}\ba écrit\s*:\s*$", re.I | re.S),
)
_FROM_LINE = re.compile(r"^\s*\*?From:\*?\s", re.I)
_SENT_LINE = re.compile(r"^\s*\*?(Sent|Date):\*?\s", re.I)


def strip_quoted(text: str) -> str:
    """The body without quoted history: lines starting with ">", and
    everything from "On … wrote:", "-----Original Message-----", or an
    Outlook "From: … Sent: …" header block onwards. Forwarded content
    ("---------- Forwarded message ---------") is kept: a forwarded
    newsletter is the point of the email."""
    lines = (text or "").splitlines()
    kept: list[str] = []
    for index, line in enumerate(lines):
        # "On … wrote:" is often wrapped over two lines.
        pair = line + " " + (lines[index + 1] if index + 1 < len(lines) else "")
        if any(p.match(line) for p in _QUOTE_STARTS) or (
            _QUOTE_STARTS[0].match(pair) and line.strip().lower().startswith("on ")
        ):
            break
        if _FROM_LINE.match(line) and any(_SENT_LINE.match(n) for n in lines[index + 1:index + 5]):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return _tidy("\n".join(kept))


# --- Sender authentication ---

_RESULT = re.compile(r"\b(spf|dkim|dmarc)=([a-z]+)", re.I)


def auth_results(header_values: list[str]) -> dict[str, list[str]] | None:
    """{"spf": [...], "dkim": [...], "dmarc": [...]} from the
    Authentication-Results headers Gmail added (authserv-id mx.google.com),
    or None when there are none."""
    found: dict[str, list[str]] = {}
    for value in header_values:
        authserv = value.split(";", 1)[0].strip().lower()
        if authserv != TRUSTED_AUTHSERV:
            continue
        for method, result in _RESULT.findall(value):
            found.setdefault(method.lower(), []).append(result.lower())
    return found or None


# --- Reading a message ---

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
    part(s), else the HTML part(s) converted to text, without quoted history."""
    payload = raw.get("payload") or {}
    headers = _headers(payload)
    plain: list[str] = []
    rich: list[str] = []
    files: list[AttachmentPart] = []
    _walk(payload, plain, rich, files)
    text = "\n\n".join(strip_quoted(t) for t in plain if t.strip())
    if not text.strip():
        text = "\n\n".join(strip_quoted(html_to_text(t)) for t in rich if t.strip())
    everything = "\n".join([
        *(value for _, value in _header_list(payload)),
        *plain, *(html_all_text(t) for t in rich), *(f.filename for f in files),
    ])
    internal = _internal_date(raw)
    received = None
    if internal is not None:
        try:
            received = datetime.fromtimestamp(internal / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return Message(
        id=str(raw.get("id", "")),
        sender=headers.get("from", ""),
        from_addresses=addresses(headers.get("from")),
        to_addresses=[a for name in ("to", "cc", "reply-to") for a in addresses(headers.get(name))],
        subject=headers.get("subject", ""),
        received_at=received,
        internal_date=internal,
        text=text.strip(),
        everything=everything,
        attachments=files,
        auth=auth_results([v for n, v in _header_list(payload) if n == "authentication-results"]),
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
