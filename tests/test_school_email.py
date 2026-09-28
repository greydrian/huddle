"""School email import (app/school_email.py, app/google_gmail.py) and adding
approved school events to Google Calendar (services/school_events.py).
Gmail and Calendar are mocked with respx; Claude with an SDK-level fake."""

import asyncio
import base64
import io
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import httpx
import pytest
from PIL import Image

from app import calendar_cache, google_gmail, google_oauth, school_email
from app.routers import admin
from app.security import create_session_token
from app.services import extraction, imports, school_events

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 9, 28, 17, 30, tzinfo=UTC)  # a Monday, 18:30 in London
API_KEY = "sk-ant-SECRET-KEY-do-not-log"
ACCESS = "ya29.SECRET-ACCESS-TOKEN"
REFRESH = "SECRET-REFRESH-TOKEN"
MESSAGES = google_gmail.MESSAGES_ENDPOINT
MESSAGE_URL = re.compile(re.escape(MESSAGES) + r"/(?P<mid>[^/?]+)(\?.*)?$")
ATTACHMENT_URL = re.compile(re.escape(MESSAGES) + r"/(?P<mid>[^/?]+)/attachments/(?P<aid>[^/?]+)$")
EVENTS_URL = re.compile(r"https://www\.googleapis\.com/calendar/v3/calendars/(?P<cal>[^/]+)/events")
ALL_SCOPES = google_oauth.SCOPES
LEGACY = f"{google_oauth.CALENDAR_READ_SCOPE} {google_oauth.TASKS_SCOPE} openid email"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

SCOPE_403 = {"error": {
    "code": 403, "status": "PERMISSION_DENIED", "message": "Request had insufficient authentication scopes.",
    "errors": [{"reason": "insufficientPermissions"}],
}}
DISABLED_403 = {"error": {"code": 403, "errors": [{"reason": "accessNotConfigured"}]}}


# --- Fixtures ---

async def _store(db, scope: str | None = ALL_SCOPES, expires_in: float = 3600):
    tokens = {"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": time.time() + expires_in}
    if scope is not None:
        tokens["scope"] = scope
    await google_oauth.store_tokens(db, tokens, "family@example.com")


@pytest.fixture
async def gmail(db):
    """Connected, with every scope granted."""
    await _store(db)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)


def _reply(items=None):
    items = items or {"word_lists": [], "homework": [], "events": []}
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": extraction.TOOL_NAME, "input": items}],
        "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
    }


READING = {"subject": "English", "title": "Read chapter 3", "details": "", "due_date": None,
           "child": None, "evidence": "read chapter 3"}


class FakeClaude:
    """anthropic.AsyncAnthropic stand-in: records messages.create() kwargs."""

    def __init__(self):
        self.calls: list[dict] = []
        self.reply = _reply({"word_lists": [], "homework": [READING], "events": []})

    def factory(self, api_key):
        return self

    @property
    def messages(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return anthropic.types.Message.model_validate(self.reply)

    def text_sent(self) -> str:
        return json.dumps(self.calls)


@pytest.fixture
def claude(monkeypatch, configured):
    fake = FakeClaude()
    monkeypatch.setattr(extraction, "client_factory", fake.factory)
    return fake


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class Mailbox:
    """A fake Gmail: messages by id, served over respx. Records which
    messages were fetched in full and which attachments were downloaded."""

    def __init__(self, google):
        self.messages: dict[str, dict] = {}
        self.list_ids: list[str] | None = None  # what the search returns (default: all, newest first)
        self.page_size = 100
        self.queries: list[str] = []
        self.full_fetches: list[str] = []
        self.metadata_fetches: list[str] = []
        self.downloads: list[str] = []
        self.list_route = google.get(MESSAGES).mock(side_effect=self._list)
        self.message_route = google.get(url__regex=MESSAGE_URL.pattern).mock(side_effect=self._get)
        self.attachment_route = google.get(url__regex=ATTACHMENT_URL.pattern).mock(side_effect=self._attachment)

    def add(self, mid, sender="Year 4 <year4@gresham.croydon.sch.uk>", subject="Homework this week",
            text="Please read chapter 3.", html=None, to="family@example.com", cc=None, attachments=(),
            received=NOW - timedelta(hours=3)):
        self.messages[mid] = {"from": sender, "subject": subject, "text": text, "html": html, "to": to,
                              "cc": cc, "attachments": list(attachments), "received": received}
        return mid

    def _ids(self):
        if self.list_ids is not None:
            return self.list_ids
        return sorted(self.messages, key=lambda m: self.messages[m]["received"], reverse=True)

    def _list(self, request):
        self.queries.append(request.url.params["q"])
        ids = self._ids()
        start = int(request.url.params.get("pageToken") or 0)
        page = ids[start:start + self.page_size]
        body = {"messages": [{"id": m, "threadId": m} for m in page]} if page else {"resultSizeEstimate": 0}
        if start + self.page_size < len(ids):
            body["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=body)

    def _headers(self, msg, names=None):
        headers = [("From", msg["from"]), ("To", msg["to"]), ("Subject", msg["subject"])]
        if msg["cc"]:
            headers.append(("Cc", msg["cc"]))
        return [{"name": n, "value": v} for n, v in headers if names is None or n in names]

    def _get(self, request, **_groups):
        mid = MESSAGE_URL.match(str(request.url).split("?")[0])["mid"]
        msg = self.messages[mid]
        if request.url.params.get("format") == "metadata":
            self.metadata_fetches.append(mid)
            names = request.url.params.get_list("metadataHeaders")
            return httpx.Response(200, json={"id": mid, "payload": {"headers": self._headers(msg, names)}})
        self.full_fetches.append(mid)
        parts = []
        if msg["text"] is not None:
            parts.append({"mimeType": "text/plain", "body": {"data": _b64(msg["text"].encode())}})
        if msg["html"] is not None:
            parts.append({"mimeType": "text/html", "body": {"data": _b64(msg["html"].encode())}})
        for index, (filename, mime, data, size) in enumerate(msg["attachments"]):
            parts.append({"mimeType": mime, "filename": filename,
                          "body": {"attachmentId": f"{mid}-a{index}", "size": size if size is not None else len(data)}})
        payload = {"mimeType": "multipart/mixed", "headers": self._headers(msg), "parts": parts}
        internal = str(int(msg["received"].timestamp() * 1000))
        return httpx.Response(200, json={"id": mid, "internalDate": internal, "payload": payload})

    def _attachment(self, request, **_groups):
        match = ATTACHMENT_URL.match(str(request.url))
        mid, aid = match["mid"], match["aid"]
        self.downloads.append(aid)
        index = int(aid.rsplit("-a", 1)[1])
        data = self.messages[mid]["attachments"][index][2]
        return httpx.Response(200, json={"size": len(data), "data": _b64(data)})


@pytest.fixture
def mailbox(google):
    return Mailbox(google)


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


async def _sources(db):
    return [dict(r) for r in await (await db.execute(
        "SELECT * FROM import_sources WHERE kind = 'gmail' ORDER BY id"
    )).fetchall()]


# --- Query building and sender lists ---

def test_query_has_allowlist_exclusions_and_checkpoint():
    query = school_email.build_query(list(school_email.DEFAULT_SENDERS), [], 1_790_000_000)
    assert query == (
        "from:(office@greshamprimary.school OR @gresham.croydon.sch.uk) "
        "-from:sen@gresham.croydon.sch.uk -to:sen@gresham.croydon.sch.uk -cc:sen@gresham.croydon.sch.uk "
        "after:1790000000"
    )
    # The SENDCo address is excluded even when the editable list doesn't name it, and only once when it does.
    query = school_email.build_query(["office@greshamprimary.school"], ["*@spam.example", "sen@gresham.croydon.sch.uk"], 5)
    assert query.count("-from:sen@gresham.croydon.sch.uk") == 1
    assert "-from:@spam.example -to:@spam.example -cc:@spam.example" in query
    with pytest.raises(ValueError):
        school_email.build_query([], [], 5)


def test_sender_entries_are_validated():
    assert school_email.parse_entries(" Office@GreshamPrimary.school\n*@gresham.croydon.sch.uk, x@a.co ") == [
        "office@greshamprimary.school", "*@gresham.croydon.sch.uk", "x@a.co"]
    for bad in ("from:(evil)", "office", "a@b", "*@*.com", "a)@b.com", "x@y.com OR z"):
        with pytest.raises(school_email.InvalidEntry):
            school_email.parse_entries(bad)
    with pytest.raises(school_email.InvalidEntry):
        school_email.parse_entries("\n".join(f"a{i}@b.com" for i in range(school_email.MAX_ENTRIES + 1)))


def test_allowed_rechecks_from_and_every_recipient():
    senders, exclusions = list(school_email.DEFAULT_SENDERS), []
    ok = {"from": ["year4@gresham.croydon.sch.uk"], "to": ["family@example.com"]}
    assert school_email.allowed(ok, senders, exclusions)
    assert not school_email.allowed({"from": ["sen@gresham.croydon.sch.uk"]}, senders, exclusions)
    assert not school_email.allowed({**ok, "cc": ["SEN@gresham.croydon.sch.uk"]}, senders, exclusions)
    assert not school_email.allowed({**ok, "reply-to": ["sen@gresham.croydon.sch.uk"]}, senders, exclusions)
    assert not school_email.allowed({"from": ["someone@elsewhere.com"]}, senders, exclusions)
    assert not school_email.allowed({"from": ["x@notgresham.croydon.sch.uk.evil.com"]}, senders, exclusions)
    assert not school_email.allowed({"from": []}, senders, exclusions)
    assert not school_email.allowed({"from": ["office@greshamprimary.school", "a@b.com"]}, senders, exclusions)
    assert not school_email.allowed(ok, senders, ["year4@gresham.croydon.sch.uk"])


async def test_admin_saves_sender_lists(db, admin_client):
    r = await admin_client.post("/admin/school-email/senders", data={
        "senders": "office@greshamprimary.school\n*@gresham.croydon.sch.uk\nclubs@example.org",
        "exclusions": "sen@gresham.croydon.sch.uk\nhead@gresham.croydon.sch.uk",
    })
    assert r.headers["location"] == "/admin#school-email"
    assert await school_email.get_senders(db) == [
        "office@greshamprimary.school", "*@gresham.croydon.sch.uk", "clubs@example.org"]
    assert await school_email.get_exclusions(db) == ["sen@gresham.croydon.sch.uk", "head@gresham.croydon.sch.uk"]

    r = await admin_client.post("/admin/school-email/senders", data={"senders": "not an address", "exclusions": ""})
    assert r.headers["location"] == "/admin?error=school-senders#school-email"
    assert "clubs@example.org" in await school_email.get_senders(db)  # unchanged
    page = (await admin_client.get("/admin?error=school-senders")).text
    assert "Each sender must be an email address" in page


# --- The check ---

async def test_school_emails_become_inbox_items(db, gmail, claude, mailbox):
    mailbox.add("m1", text="Year 4: please read chapter 3 by Friday.")
    result = await school_email.check_now(db, NOW)

    assert (result.result, result.emails, result.items) == ("ok", 1, 1)
    (source,) = await _sources(db)
    assert (source["source_ref"], source["subject"], source["status"]) == ("m1", "Homework this week", "extracted")
    after = int((NOW - timedelta(days=school_email.BACKFILL_DAYS)).timestamp())
    assert mailbox.queries == [school_email.build_query(list(school_email.DEFAULT_SENDERS), [], after)]
    sent = claude.calls[0]["messages"][0]["content"][-1]["text"]
    assert "please read chapter 3" in sent and "Sent by: Year 4" in sent
    status = await school_email.get_status(db)
    assert status["result"] == "ok" and status["emails"] == 1 and status["items"] == 1


async def test_sendco_is_never_fetched_or_sent_even_if_the_search_returns_it(db, gmail, claude, mailbox):
    mailbox.add("sen1", sender="SENDCo <sen@gresham.croydon.sch.uk>", text="PRIVATE-SENDCO-NOTE")
    mailbox.add("cc1", sender="year4@gresham.croydon.sch.uk", cc="sen@gresham.croydon.sch.uk",
                text="PRIVATE-THREAD-NOTE")
    mailbox.add("fake", sender="Office <office@greshamprimary.school.evil.com>", text="PHISH")
    mailbox.add("ok", text="Non-uniform day on Friday")

    result = await school_email.check_now(db, NOW)

    assert result.result == "ok" and result.emails == 1
    assert mailbox.full_fetches == ["ok"]  # the others were only ever read as headers
    assert [s["source_ref"] for s in await _sources(db)] == ["ok"]
    assert len(claude.calls) == 1
    for secret in ("PRIVATE-SENDCO-NOTE", "PRIVATE-THREAD-NOTE", "PHISH", "sen@gresham"):
        assert secret not in claude.text_sent()


async def test_full_message_headers_are_rechecked_too(db, gmail, claude, mailbox, google):
    """If the full message's From differs from its metadata (it shouldn't),
    the full one is checked again before anything is ingested."""
    mailbox.add("m1", text="PRIVATE")
    real_get = mailbox._get

    def swap(request, **_groups):
        response = real_get(request)
        if request.url.params.get("format") == "full":
            body = response.json()
            body["payload"]["headers"][0]["value"] = "sen@gresham.croydon.sch.uk"
            return httpx.Response(200, json=body)
        return response

    mailbox.message_route.mock(side_effect=swap)
    await school_email.check_now(db, NOW)
    assert await _sources(db) == [] and claude.calls == []


async def test_every_page_of_the_search_is_read(db, gmail, claude, mailbox):
    mailbox.page_size = 2
    for i in range(5):
        mailbox.add(f"m{i}", received=NOW - timedelta(hours=10 - i))
    result = await school_email.check_now(db, NOW)
    assert result.emails == 5
    assert mailbox.list_route.call_count == 3
    # Oldest first, so a capped run keeps its place.
    assert [s["source_ref"] for s in await _sources(db)] == ["m0", "m1", "m2", "m3", "m4"]


async def test_attachments_pdfs_and_images_within_the_caps(db, gmail, claude, mailbox):
    photo = io.BytesIO()
    Image.effect_noise((200, 200), 80).convert("RGB").save(photo, format="PNG")
    photo = photo.getvalue()
    logo = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(logo, format="PNG")
    mailbox.add("m1", attachments=[
        ("logo.png", "image/png", logo.getvalue(), None),                          # a signature logo: skipped
        ("newsletter.pdf", "application/pdf", PDF, None),
        ("letter.docx", "application/vnd.openxmlformats", b"PK..", None),          # not a PDF or image
        ("huge.pdf", "application/pdf", b"", extraction.MAX_ATTACHMENT_BYTES + 1),  # over the cap: never downloaded
        ("page.png", "image/png", photo, None),
        ("fake.pdf", "application/pdf", b"not a pdf at all", None),              # sniffed: rejected
    ])
    await school_email.check_now(db, NOW)

    assert sorted(mailbox.downloads) == ["m1-a1", "m1-a4", "m1-a5"]
    blocks = claude.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["document", "image", "text"]
    assert base64.b64decode(blocks[0]["source"]["data"]) == PDF
    (source,) = await _sources(db)
    assert source["attachment_count"] == 2


async def test_at_most_three_attachments(db, gmail, claude, mailbox):
    mailbox.add("m1", attachments=[(f"p{i}.pdf", "application/pdf", PDF, None) for i in range(5)])
    await school_email.check_now(db, NOW)
    assert len(mailbox.downloads) == extraction.MAX_ATTACHMENTS


async def test_html_only_body_is_converted_to_text(db, gmail, claude, mailbox):
    mailbox.add("m1", text=None, html=(
        "<html><head><style>p{color:red}</style><script>alert(1)</script></head><body>"
        "<p>Dear parents,</p><p>Flu consent forms are due&nbsp;by <b>Friday 2 October</b>.</p>"
        "<table><tr><td>Year 4</td><td>Trip</td></tr></table></body></html>"
    ))
    await school_email.check_now(db, NOW)
    sent = claude.calls[0]["messages"][0]["content"][-1]["text"]
    assert "Flu consent forms are due by Friday 2 October." in sent
    assert "Dear parents," in sent and "Year 4 Trip" in sent
    assert "<p>" not in sent and "alert(1)" not in sent and "color:red" not in sent


def test_html_to_text_handles_odd_markup():
    assert google_gmail.html_to_text("a<br>b &amp; c<div>d") == "a\nb & c\nd"
    assert google_gmail.html_to_text("<p>unclosed <b>bold") == "unclosed bold"


async def test_rerun_is_idempotent(db, gmail, claude, mailbox):
    mailbox.add("m1")
    await school_email.check_now(db, NOW)
    await db.execute("DELETE FROM app_settings WHERE key = ?", (school_email.CHECKPOINT_SETTING,))
    await db.commit()
    second = await school_email.check_now(db, NOW + timedelta(hours=1))
    assert second.result == "ok" and second.emails == 0
    assert len(claude.calls) == 1 and mailbox.full_fetches == ["m1"]
    assert len(await _sources(db)) == 1


async def test_newsletter_correction_resend_is_deduped(db, gmail, claude, mailbox):
    """A same-afternoon "correction" resend is a new message: ingest reads
    it, and its dedupe marks items already waiting in the inbox."""
    await db.execute("UPDATE profiles SET school_year = 'Year 4' WHERE name = 'Riley'")
    await db.commit()
    claude.reply = _reply({"word_lists": [], "events": [], "homework": [{**READING, "due_date": "2026-10-02",
                                                                           "child": "Riley"}]})
    mailbox.add("news", sender="office@greshamprimary.school", subject="Newsletter")
    mailbox.add("news2", sender="office@greshamprimary.school", subject="Newsletter (correction)",
                received=NOW - timedelta(hours=1))
    await school_email.check_now(db, NOW)
    rows = [dict(r) for r in await (await db.execute("SELECT duplicate FROM import_candidates ORDER BY id")).fetchall()]
    assert rows == [{"duplicate": 0}, {"duplicate": 1}]


async def test_checkpoint_advances_only_after_success(db, gmail, claude, mailbox, google):
    mailbox.add("m1")
    mailbox.list_route.mock(return_value=httpx.Response(503))
    failed = await school_email.check_now(db, NOW)
    assert failed.result == "server_error"
    assert await school_email.get_checkpoint(db) is None
    assert await _sources(db) == []

    mailbox.list_route.mock(side_effect=mailbox._list)
    ok = await school_email.check_now(db, NOW + timedelta(hours=1))
    assert ok.result == "ok"
    assert await school_email.get_checkpoint(db) == int((NOW + timedelta(hours=1) - school_email.CHECKPOINT_OVERLAP).timestamp())
    # The next search starts from it.
    await school_email.check_now(db, NOW + timedelta(days=1))
    assert mailbox.queries[-1].endswith(f"after:{int((NOW + timedelta(hours=1, days=-1)).timestamp())}")


async def test_a_gmail_failure_mid_run_keeps_the_checkpoint(db, gmail, claude, mailbox):
    mailbox.add("m1", received=NOW - timedelta(hours=5))
    mailbox.add("m2", received=NOW - timedelta(hours=4))
    real_get = mailbox._get

    def flaky(request, **_groups):
        if "m2" in str(request.url):
            raise httpx.ConnectError("offline")
        return real_get(request)

    mailbox.message_route.mock(side_effect=flaky)
    result = await school_email.check_now(db, NOW)
    assert (result.result, result.emails) == ("offline", 1)
    assert await school_email.get_checkpoint(db) is None
    assert [s["source_ref"] for s in await _sources(db)] == ["m1"]  # kept; not re-read next time


async def test_a_transient_claude_failure_is_retried_next_time(db, gmail, claude, mailbox, monkeypatch):
    mailbox.add("m1")

    async def offline(*args):
        raise extraction.ExtractionFailed("offline")

    real_extract = imports.extract
    monkeypatch.setattr(imports, "extract", offline)
    first = await school_email.check_now(db, NOW)
    assert first.result == "extract_failed"
    assert await school_email.get_checkpoint(db) is None

    monkeypatch.setattr(imports, "extract", real_extract)
    second = await school_email.check_now(db, NOW + timedelta(hours=1))
    assert (second.result, second.items) == ("ok", 1)
    (source,) = await _sources(db)
    assert source["status"] == "extracted"


# --- Scopes, reconnect and offline ---

async def test_no_gmail_scope_means_reconnect_without_calling_google(db, claude, google, admin_client):
    await _store(db, scope=LEGACY)
    result = await school_email.check_now(db, NOW)
    assert result.result == "reconnect"
    assert not google.calls
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": []})
    google.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(200, json={"items": []})
    page = (await admin_client.get("/admin")).text
    assert "Reconnect Google to enable school email import" in page


async def test_a_scope_403_from_gmail_is_reconnect(db, gmail, claude, mailbox):
    mailbox.list_route.mock(return_value=httpx.Response(403, json=SCOPE_403))
    assert (await school_email.check_now(db, NOW)).result == "reconnect"
    mailbox.list_route.mock(return_value=httpx.Response(403, json=DISABLED_403))
    assert (await school_email.check_now(db, NOW)).result == "api_disabled"
    mailbox.list_route.mock(return_value=httpx.Response(429))
    assert (await school_email.check_now(db, NOW)).result == "rate_limited"


async def test_skipped_states(db, claude, monkeypatch):
    assert (await school_email.check_now(db, NOW)).result == "not_connected"
    await _store(db)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert (await school_email.check_now(db, NOW)).result == "not_configured"
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    await school_email.set_lists(db, [], [])
    assert (await school_email.check_now(db, NOW)).result == "no_senders"


async def test_offline_warns_once(db, gmail, claude, mailbox, caplog):
    mailbox.list_route.mock(side_effect=httpx.ConnectError("offline"))
    with caplog.at_level(logging.INFO, logger="app.school_email"):
        for i in range(3):
            assert (await school_email.check_now(db, NOW + timedelta(hours=i))).result == "offline"
    warnings = [r for r in caplog.records if r.name == "app.school_email" and r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_token_refresh_offline_is_offline(db, claude, google):
    await _store(db, expires_in=-10)
    google.post(google_oauth.TOKEN_ENDPOINT).mock(side_effect=httpx.ConnectError("down"))
    assert (await school_email.check_now(db, NOW)).result == "offline"
    assert await google_oauth.get_connected_account(db) == "family@example.com"  # tokens kept


async def test_nothing_sensitive_is_logged_even_at_debug(db, claude, mailbox, caplog, google):
    await _store(db, expires_in=-10)  # forces a refresh, so the refresh token is in play
    google.post(google_oauth.TOKEN_ENDPOINT).respond(200, json={
        "access_token": ACCESS, "expires_in": 3600, "scope": ALL_SCOPES})
    mailbox.add("m1", subject="SUBJECT-MARKER-7", text="BODY-MARKER-8",
                attachments=[("ATTACHMENT-NAME-9.pdf", "application/pdf", PDF, None)])
    mailbox.add("sen", sender="sen@gresham.croydon.sch.uk", subject="SENDCO-SUBJECT", text="SENDCO-BODY")
    mailbox.add("m2", subject="SUBJECT-MARKER-10", attachments=[
        ("big.pdf", "application/pdf", b"", extraction.MAX_ATTACHMENT_BYTES + 5)])
    with caplog.at_level(logging.DEBUG):
        await school_email.check_now(db, NOW)
        mailbox.list_route.mock(side_effect=httpx.ConnectError("offline"))
        await school_email.check_now(db, NOW + timedelta(hours=2))
        mailbox.list_route.mock(return_value=httpx.Response(403, json=SCOPE_403))
        await school_email.check_now(db, NOW + timedelta(hours=3))
    logged = caplog.text + json.dumps([r.args for r in caplog.records], default=str)
    assert "School email check" in caplog.text  # something was logged...
    for secret in ("SUBJECT-MARKER", "BODY-MARKER", "ATTACHMENT-NAME", "SENDCO", "sen@gresham",
                   ACCESS, REFRESH, API_KEY, "Homework this week", "year4@"):
        assert secret not in logged  # ...but none of this


# --- Scope detection ---

async def test_granted_scopes_come_from_the_token_response(db):
    assert await google_oauth.granted_scopes(db) == frozenset()
    await _store(db, scope=None)  # stored before scopes were kept: the old two
    assert await google_oauth.granted_scopes(db) == google_oauth.LEGACY_SCOPES
    assert not await google_oauth.has_scope(db, google_oauth.GMAIL_READ_SCOPE)
    await _store(db, scope=ALL_SCOPES)
    assert await google_oauth.has_scope(db, google_oauth.GMAIL_READ_SCOPE)
    assert await google_oauth.has_scope(db, google_oauth.CALENDAR_EVENTS_SCOPE)


async def test_refresh_keeps_working_and_learns_the_granted_scopes(db, google):
    await _store(db, scope=None, expires_in=-10)
    route = google.post(google_oauth.TOKEN_ENDPOINT).respond(200, json={
        "access_token": "new-access", "expires_in": 3599, "scope": LEGACY})
    assert await google_oauth.get_valid_access_token(db) == "new-access"
    assert b"scope" not in route.calls[0].request.content  # a refresh never asks for new scopes
    tokens = await google_oauth._load_stored_tokens(db)
    assert tokens["refresh_token"] == REFRESH and tokens["scope"] == LEGACY


async def test_callback_stores_the_granted_scopes(db, client, google):
    from app.routers import calendar

    google.post(google_oauth.TOKEN_ENDPOINT).respond(200, json={
        "access_token": "a", "refresh_token": "r", "expires_in": 3599, "scope": LEGACY + " " + google_oauth.GMAIL_READ_SCOPE})
    google.get(google_oauth.USERINFO_ENDPOINT).respond(200, json={"email": "family@example.com"})
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    client.cookies.set(calendar.STATE_COOKIE, "s")
    await client.get("/admin/google/callback", params={"code": "c", "state": "s"})
    assert await google_oauth.has_scope(db, google_oauth.GMAIL_READ_SCOPE)
    assert not await google_oauth.has_scope(db, google_oauth.CALENDAR_EVENTS_SCOPE)


def test_the_auth_url_asks_for_every_scope():
    url = google_oauth.build_auth_url("s", "http://localhost:8000/admin/google/callback")
    params = httpx.URL(url).params
    assert set(params["scope"].split()) == {
        google_oauth.CALENDAR_READ_SCOPE, google_oauth.TASKS_SCOPE, google_oauth.GMAIL_READ_SCOPE,
        google_oauth.CALENDAR_EVENTS_SCOPE, "openid", "email"}


# --- Schedule ---

def _schedule(mode="daily", at="18:00"):
    return school_email.parse_schedule(mode, at)


def test_daily_slots_follow_london_time_across_dst():
    daily = _schedule()
    # Before the autumn change 18:00 London is 17:00 UTC; after it, 18:00 UTC.
    assert school_email.last_slot(datetime(2026, 10, 24, 17, 30, tzinfo=UTC), daily, LONDON) == \
        datetime(2026, 10, 24, 17, 0, tzinfo=UTC)
    assert school_email.last_slot(datetime(2026, 10, 25, 17, 30, tzinfo=UTC), daily, LONDON) == \
        datetime(2026, 10, 24, 17, 0, tzinfo=UTC)
    assert school_email.last_slot(datetime(2026, 10, 25, 18, 0, tzinfo=UTC), daily, LONDON) == \
        datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
    assert school_email.next_slot(datetime(2026, 10, 24, 17, 30, tzinfo=UTC), daily, LONDON) == \
        datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
    # Spring: 01:30 doesn't exist on 29 March; there's still exactly one check that day.
    early = _schedule(at="01:30")
    slots = [school_email.last_slot(datetime(2026, 3, 29, h, 59, tzinfo=UTC), early, LONDON) for h in range(24)]
    assert len({s for s in slots if s.date() == datetime(2026, 3, 29).date()}) == 1


def test_twice_weekly_and_off():
    twice = _schedule("twice")
    assert [t.strftime("%H:%M") for t in twice.times()] == ["06:00", "18:00"]
    assert school_email.last_slot(NOW, twice, LONDON) == datetime(2026, 9, 28, 17, 0, tzinfo=UTC)
    assert school_email.next_slot(NOW, twice, LONDON) == datetime(2026, 9, 29, 5, 0, tzinfo=UTC)
    weekly = _schedule("weekly")
    assert school_email.last_slot(NOW, weekly, LONDON) == datetime(2026, 9, 25, 17, 0, tzinfo=UTC)  # Friday
    assert school_email.next_slot(NOW, weekly, LONDON) == datetime(2026, 10, 2, 17, 0, tzinfo=UTC)
    off = _schedule("off")
    assert school_email.last_slot(NOW, off, LONDON) is None and school_email.next_slot(NOW, off, LONDON) is None
    assert not school_email.is_due({}, off, NOW, LONDON)
    with pytest.raises(ValueError):
        school_email.parse_schedule("hourly", "18:00")
    with pytest.raises(ValueError):
        school_email.parse_schedule("daily", "25:00")


def test_due_logic():
    daily, slot = _schedule(), datetime(2026, 9, 28, 17, 0, tzinfo=UTC)

    def due(**status):
        return school_email.is_due({k: v.isoformat() if isinstance(v, datetime) else v for k, v in status.items()},
                                   daily, NOW, LONDON)

    assert due()  # never checked: catch up at startup
    assert not due(last_success_at=slot + timedelta(minutes=1), last_attempt_at=slot, result="ok")
    assert due(last_success_at=slot - timedelta(minutes=1), last_attempt_at=slot - timedelta(minutes=1), result="ok")
    # A failure backs off, then retries.
    assert not due(last_attempt_at=NOW - timedelta(minutes=20), result="offline")
    assert due(last_attempt_at=NOW - timedelta(hours=2), result="offline")
    # Skipped (nothing to retry) waits for the next slot.
    assert not due(last_attempt_at=slot + timedelta(minutes=5), result="reconnect")
    assert due(last_attempt_at=slot - timedelta(hours=1), result="reconnect")


async def test_run_if_due(db, monkeypatch):
    calls = []

    async def fake_check(db, now):
        calls.append(now)
        return school_email.CheckResult("ok")

    monkeypatch.setattr(school_email, "_check", fake_check)
    assert await school_email.run_if_due(NOW) is not None  # catch-up
    assert await school_email.run_if_due(NOW + timedelta(minutes=10)) is None  # done for this slot
    assert await school_email.run_if_due(datetime(2026, 9, 29, 17, 1, tzinfo=UTC)) is not None
    await school_email.set_schedule(db, _schedule("off"))
    assert await school_email.run_if_due(datetime(2026, 9, 30, 17, 1, tzinfo=UTC)) is None
    assert len(calls) == 2


async def test_admin_schedule_setting(db, admin_client):
    r = await admin_client.post("/admin/school-email/schedule", data={"mode": "twice", "time": "07:30"})
    assert r.headers["location"] == "/admin#school-email"
    assert await school_email.get_schedule(db) == _schedule("twice", "07:30")
    r = await admin_client.post("/admin/school-email/schedule", data={"mode": "hourly", "time": "07:30"})
    assert r.headers["location"] == "/admin?error=school-schedule#school-email"
    r = await admin_client.post("/admin/school-email/schedule", data={"mode": "daily", "time": "7pm"})
    assert r.headers["location"] == "/admin?error=school-schedule#school-email"
    assert await school_email.get_schedule(db) == _schedule("twice", "07:30")


async def test_default_schedule_is_daily_at_six_pm(db):
    assert await school_email.get_schedule(db) == _schedule("daily", "18:00")


# --- Check now ---

@pytest.mark.parametrize("path", ["/admin/school-email/check", "/admin/school-email/schedule",
                                  "/admin/school-email/senders", "/admin/school-email/calendar"])
async def test_school_email_routes_require_admin(client, path, monkeypatch):
    async def never(*args):
        pytest.fail("ran without admin")

    monkeypatch.setattr(school_email, "_check", never)
    r = await client.post(path, data={})
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


async def test_check_now_and_the_lock(db, admin_client, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow_check(db, now):
        calls.append(now)
        started.set()
        await release.wait()
        return school_email.CheckResult("ok", 2, 3)

    monkeypatch.setattr(school_email, "_check", slow_check)
    scheduled = asyncio.create_task(school_email.run_if_due(NOW))
    await started.wait()
    r = await admin_client.post("/admin/school-email/check")
    assert r.headers["location"] == "/admin?school_email=busy#school-email"
    assert len(calls) == 1
    release.set()
    assert (await scheduled).result == "ok"

    r = await admin_client.post("/admin/school-email/check")
    assert r.headers["location"] == "/admin?school_email=done#school-email"
    assert len(calls) == 2
    page = (await admin_client.get("/admin?school_email=done")).text
    assert "2 new emails, 3 items found" in page and "Checked just now" in page


async def test_a_crash_is_recorded_not_raised(db, monkeypatch, caplog):
    async def boom(db, now):
        raise RuntimeError("SECRET-DETAIL")

    monkeypatch.setattr(school_email, "_check", boom)
    result = await school_email.check_now(db, NOW)
    assert result.result == "error" and not school_email.check_in_progress()
    assert "SECRET-DETAIL" not in caplog.text


# --- Approving events into Google Calendar ---

TRIP = {"title": "Trip to the museum", "date": "2026-10-09", "start_time": "09:00", "end_time": "15:00",
        "all_day": False, "notes": "Packed lunch", "whole_school": True}


async def _event_candidate(db, payload=None) -> int:
    cursor = await db.execute(
        "INSERT INTO import_sources (kind, source_ref, status) VALUES ('gmail', 'm-event', 'extracted')")
    source_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO import_candidates (source_id, kind, payload_json, evidence) VALUES (?, 'event', ?, 'x')",
        (source_id, json.dumps(payload or TRIP)))
    await db.commit()
    return cursor.lastrowid


def _form(**overrides):
    return {"title": TRIP["title"], "date": TRIP["date"], "start_time": "09:00", "end_time": "15:00",
            "notes": "Packed lunch", **overrides}


@pytest.fixture
async def school_calendar(db, gmail, google):
    await school_events.set_target_calendar(db, {"id": "family@group.calendar.google.com", "summary": "Family"})
    # The calendar refresh after an insert.
    google.get(url__regex=EVENTS_URL.pattern).respond(200, json={"items": []})


async def _candidate(db, candidate_id):
    return dict(await (await db.execute("SELECT * FROM import_candidates WHERE id = ?", (candidate_id,))).fetchone())


async def test_approving_an_event_adds_it_to_the_calendar(db, school_calendar, google):
    candidate_id = await _event_candidate(db)
    await calendar_cache.store(db, "sel", datetime(2026, 9, 28).date(), datetime(2026, 11, 9).date(), {"primary": []})
    insert = google.post(url__regex=EVENTS_URL.pattern).respond(200, json={"id": "evt123"})

    event_id = await school_events.approve_event(db, candidate_id, _form())

    assert event_id == "evt123" and insert.call_count == 1
    request = insert.calls[0].request
    assert "family%40group.calendar.google.com" in str(request.url)
    body = json.loads(request.content)
    assert body["summary"] == "Trip to the museum"
    assert body["start"] == {"dateTime": "2026-10-09T09:00:00+01:00", "timeZone": "Europe/London"}
    assert body["end"] == {"dateTime": "2026-10-09T15:00:00+01:00", "timeZone": "Europe/London"}
    assert "Packed lunch" in body["description"] and "school inbox" in body["description"]
    row = await _candidate(db, candidate_id)
    assert (row["status"], row["created_table"], row["external_id"]) == ("approved", "google_calendar", "evt123")
    # The saved month was dropped and fetched again, so the new event shows.
    assert (await (await db.execute("SELECT COUNT(*) FROM calendar_cache WHERE selection = 'sel'")).fetchone())[0] == 0


async def test_all_day_event(db, school_calendar, google):
    candidate_id = await _event_candidate(db)
    insert = google.post(url__regex=EVENTS_URL.pattern).respond(200, json={"id": "e"})
    await school_events.approve_event(db, candidate_id, _form(start_time="", end_time="12:00", date="2026-10-30"))
    body = json.loads(insert.calls[0].request.content)
    assert body["start"] == {"date": "2026-10-30"} and body["end"] == {"date": "2026-10-31"}


async def test_concurrent_approves_create_exactly_one_event(db, school_calendar, google):
    candidate_id = await _event_candidate(db)
    gate = asyncio.Event()

    async def slow(request, **_groups):
        await gate.wait()
        return httpx.Response(200, json={"id": "only-one"})

    insert = google.post(url__regex=EVENTS_URL.pattern).mock(side_effect=slow)
    from app.database import get_db

    async def approve():
        async with get_db() as conn:
            return await school_events.approve_event(conn, candidate_id, _form())

    tasks = [asyncio.create_task(approve()) for _ in range(3)]
    await asyncio.sleep(0.2)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert [r for r in results if r == "only-one"] == ["only-one"]
    assert all(isinstance(r, imports.CandidateError) and r.code == "import-missing"
               for r in results if r != "only-one")
    assert insert.call_count == 1


@pytest.mark.parametrize(("failure", "code"), [
    (httpx.Response(500), "import-calendar-failed"),
    (httpx.Response(403, json=SCOPE_403), "import-calendar-scope"),
    (httpx.Response(404), "import-calendar-missing"),
    (httpx.ConnectError("down"), "import-calendar-offline"),
])
async def test_a_calendar_failure_leaves_the_event_pending(db, school_calendar, google, failure, code):
    candidate_id = await _event_candidate(db)
    route = google.post(url__regex=EVENTS_URL.pattern)
    route.mock(side_effect=failure) if isinstance(failure, Exception) else route.mock(return_value=failure)
    with pytest.raises(imports.CandidateError) as raised:
        await school_events.approve_event(db, candidate_id, _form())
    assert raised.value.code == code
    row = await _candidate(db, candidate_id)
    assert (row["status"], row["external_id"], row["created_table"]) == ("pending", None, None)
    assert json.loads(row["payload_json"]) == TRIP  # untouched

    # And it can then be approved.
    route.mock(side_effect=None, return_value=httpx.Response(200, json={"id": "second-go"}))
    assert await school_events.approve_event(db, candidate_id, _form()) == "second-go"


async def test_a_cancelled_insert_releases_the_claim(db, school_calendar, google):
    candidate_id = await _event_candidate(db)
    def cancelled(request, **_groups):
        raise asyncio.CancelledError()

    google.post(url__regex=EVENTS_URL.pattern).mock(side_effect=cancelled)
    with pytest.raises(asyncio.CancelledError):
        await school_events.approve_event(db, candidate_id, _form())
    assert (await _candidate(db, candidate_id))["status"] == "pending"


async def test_retry_after_a_crash_does_not_duplicate(db, school_calendar, google):
    """A crash after Google created the event but before it was recorded:
    startup puts the candidate back, and the retry's fixed event id gets 409."""
    candidate_id = await _event_candidate(db)
    await db.execute("UPDATE import_candidates SET status = 'approving' WHERE id = ?", (candidate_id,))
    await db.commit()
    await imports.fail_interrupted(db)
    assert (await _candidate(db, candidate_id))["status"] == "pending"
    insert = google.post(url__regex=EVENTS_URL.pattern).respond(409, json={"error": {"code": 409}})
    event_id = await school_events.approve_event(db, candidate_id, _form())
    first = json.loads(insert.calls[0].request.content)["id"]
    assert event_id == first and re.fullmatch(r"[0-9a-v]{5,1024}", first)
    assert (await _candidate(db, candidate_id))["status"] == "approved"


async def test_event_approval_is_gated_on_scope_and_calendar(db, google):
    candidate_id = await _event_candidate(db)
    await _store(db, scope=LEGACY)
    with pytest.raises(imports.CandidateError) as raised:
        await school_events.approve_event(db, candidate_id, _form())
    assert raised.value.code == "import-event"  # no calendar picked yet
    await school_events.set_target_calendar(db, {"id": "primary", "summary": "Family"})
    with pytest.raises(imports.CandidateError) as raised:
        await school_events.approve_event(db, candidate_id, _form())
    assert raised.value.code == "import-calendar-scope"
    assert not google.calls
    assert (await _candidate(db, candidate_id))["status"] == "pending"


async def test_event_form_validation(db, school_calendar, google):
    candidate_id = await _event_candidate(db)
    for bad in ({"title": ""}, {"date": ""}, {"date": "tomorrow"}, {"start_time": "9am"},
                {"start_time": "15:00", "end_time": "09:00"}):
        with pytest.raises(Exception) as raised:
            await school_events.approve_event(db, candidate_id, _form(**bad))
        assert type(raised.value).__name__ == "ValidationError"
    assert (await _candidate(db, candidate_id))["status"] == "pending"


async def test_admin_event_approval_and_errors(db, school_calendar, google, admin_client):
    candidate_id = await _event_candidate(db)
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": []})
    google.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(200, json={"items": []})
    page = (await admin_client.get("/admin")).text
    assert "Add to Family" in page

    route = google.post(url__regex=EVENTS_URL.pattern).respond(500)
    r = await admin_client.post(f"/admin/inbox/candidates/{candidate_id}/approve", data=_form(),
                                headers={"HX-Request": "true"})
    assert "Google Calendar didn&#39;t accept the event" in r.text and "Add to Family" in r.text
    r = await admin_client.post(f"/admin/inbox/candidates/{candidate_id}/approve", data=_form())
    assert r.headers["location"] == "/admin?error=import-calendar-failed#inbox"

    route.respond(200, json={"id": "e1"})
    r = await admin_client.post(f"/admin/inbox/candidates/{candidate_id}/approve", data=_form(title="Museum trip"))
    assert r.headers["location"] == "/admin#inbox"
    assert json.loads(route.calls[-1].request.content)["summary"] == "Museum trip"
    assert (await _candidate(db, candidate_id))["external_id"] == "e1"


async def test_calendar_picker_only_takes_writable_calendars(db, gmail, google, admin_client):
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": [
        {"id": "family", "summary": "Family", "accessRole": "owner", "primary": True},
        {"id": "holidays", "summary": "UK holidays", "accessRole": "reader"},
        {"id": "shared", "summary": "Shared <b>", "accessRole": "writer"},
    ]})
    google.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(200, json={"items": []})
    page = (await admin_client.get("/admin")).text
    picker = page.split('name="calendar_id" aria-label="Calendar for school events"')[1].split("</select>")[0]
    assert "Family (primary)" in picker and "Shared &lt;b&gt;" in picker and "UK holidays" not in picker

    r = await admin_client.post("/admin/school-email/calendar", data={"calendar_id": "holidays"})
    assert r.headers["location"] == "/admin?error=school-calendar#school-email"
    assert await school_events.get_target_calendar(db) is None
    r = await admin_client.post("/admin/school-email/calendar", data={"calendar_id": "shared"})
    assert r.headers["location"] == "/admin#school-email"
    assert await school_events.get_target_calendar(db) == {"id": "shared", "summary": "Shared <b>"}
    await admin_client.post("/admin/school-email/calendar", data={"calendar_id": ""})
    assert await school_events.get_target_calendar(db) is None


async def test_admin_panel_shows_status(db, gmail, google, admin_client):
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": []})
    google.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(200, json={"items": []})
    page = (await admin_client.get("/admin")).text
    assert 'id="school-email"' in page and "Not checked yet" in page and "Next check" in page
    assert "office@greshamprimary.school" in page and "sen@gresham.croydon.sch.uk is never read" in page
    await school_email._record(db, NOW, "offline")
    page = (await admin_client.get("/admin")).text
    assert "Couldn&#39;t reach Google" in page
