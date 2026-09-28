"""
School inbox pipeline: source documents in, candidates out, and nothing
reaches the wall until a parent approves it in Admin.

    SourceDocument ──ingest()──▶ import_sources row ──extraction──▶ import_candidates
                                                        (Claude)        │
                                           Admin: Approve ─▶ practice_word_lists / homework
                                                  Discard ─▶ (kept, marked discarded)

Entry points:
- `ingest(db, doc)` — THE entry point for automated sources (stage 2: the
  Gmail importer builds a `SourceDocument` per school email and awaits this
  from a scheduler job). Idempotent by (doc.kind, doc.source_ref): a
  document already read, or being read, is not sent to Claude again; one
  that failed or arrived while no API key was set is retried.
- `start_ingest(doc)` — the Admin upload/paste path: records the source,
  then runs the extraction as a background task so the request returns at
  once. Admin shows "Reading…" and polls the source's fragment. Chosen over
  awaiting with a deadline because extraction can take a minute (PDFs,
  thinking), and a parent's phone browser or a reverse proxy may give up on
  a request that long; a background task also never ties up a worker the
  kiosk needs. (Single uvicorn process, like the scheduler.)

Attachment bytes and full text live only in memory for the extraction; the
database keeps metadata and a short excerpt.
"""

import asyncio
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.database import family_today, get_db
from app.services import homework
from app.services.extraction import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_PDF_PAGES,
    MAX_TOTAL_BYTES,
    PDF_TYPE,
    Attachment,
    Candidate,
    Child,
    ExtractionFailed,
    NotConfigured,
    SourceDocument,
    extract,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Attachment", "SourceDocument", "IngestResult", "ingest", "start_ingest",
    "prepare_attachment", "content_ref",
]

KINDS = ("upload", "paste", "gmail")
EXCERPT_CHARS = 300
MAX_SUBJECT_CHARS = 200
PROCESSED_SHOWN = 15
# A source "Reading…" longer than this can be removed even if its task is
# somehow still running (it has long outlived the API timeout).
STUCK_AFTER = timedelta(minutes=10)

# Log-safe error codes stored on import_sources.error_code -> Admin text.
ERROR_MESSAGES = {
    "not_configured": "The school inbox isn't set up yet, so this wasn't read. Add an Anthropic API key "
                      "(see below), then add it again.",
    "offline": "Couldn't reach Claude just now. Try adding it again in a minute.",
    "rate_limited": "Claude is busy right now. Try adding it again in a few minutes.",
    "server_error": "Claude had a problem reading this. Try adding it again in a few minutes.",
    "auth": "Claude refused the API key. Check ANTHROPIC_API_KEY, then add it again.",
    "too_large": "That was too big for Claude to read. Try a smaller screenshot or fewer pages.",
    "pdf_too_long": "That PDF has too many pages for Claude to read (100 at most). Try just the pages you need.",
    "rejected": "Claude couldn't read this document. Try a clearer screenshot, or paste the text instead.",
    "no_result": "Claude didn't return anything for this. Try adding it again.",
    "interrupted": "Reading was interrupted (the display restarted). Add it again.",
    "error": "Something went wrong reading this. Try adding it again.",
}

_RETRYABLE = ("failed", "not_configured")


class UploadRejected(ValueError):
    """An upload the inbox won't take; `code` is an admin.ADMIN_ERRORS key."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CandidateError(ValueError):
    """Approve/discard refused; `code` is an admin.ADMIN_ERRORS key."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class IngestResult:
    source_id: int
    status: str            # pending / extracted / failed / not_configured
    already: bool = False  # this (kind, source_ref) was already in the inbox
    candidate_count: int = 0


# --- Preparing uploads ---

_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")


def _sniff(data: bytes) -> str | None:
    """The real type from magic bytes — the browser's claimed type is ignored."""
    if data.startswith(b"%PDF-"):
        return PDF_TYPE
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "image/heic"
    return None


# Claude takes images up to 5 MB *base64* (about 3.75 MB raw) and 8000 px;
# bigger ones are re-encoded smaller.
_IMAGE_MAX_BYTES = 3_700_000
_IMAGE_MAX_SIDE = 7_900
_IMAGE_SHRINK_TO = 3_000
# Pixel-bomb guard: a small PNG can declare a huge canvas. Checked from the
# header before anything is decoded (a phone photo is 12-50 MP).
MAX_IMAGE_PIXELS = 60_000_000


def _normalise_image(data: bytes, mime_type: str) -> tuple[bytes, str]:
    """Verify the image decodes; convert HEIC to JPEG; shrink if too big."""
    from PIL import Image

    # Pillow's own limit (it errors at twice this) backs up the check below.
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    if mime_type == "image/heic":
        import pillow_heif

        pillow_heif.register_heif_opener()
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size  # from the header: nothing decoded yet
            if width * height > MAX_IMAGE_PIXELS:
                raise UploadRejected("import-too-big")
            image.load()
            too_big = len(data) > _IMAGE_MAX_BYTES or max(image.size) > _IMAGE_MAX_SIDE
            if mime_type != "image/heic" and not too_big:
                return data, mime_type
            converted = image.convert("RGB")
            limit = _IMAGE_SHRINK_TO
            while True:
                if max(converted.size) > limit:
                    converted.thumbnail((limit, limit))
                out = io.BytesIO()
                converted.save(out, format="JPEG", quality=85)
                if out.tell() <= _IMAGE_MAX_BYTES or limit < 500:
                    return out.getvalue(), "image/jpeg"
                limit = int(limit * 0.75)
    except UploadRejected:
        raise
    except Image.DecompressionBombError:  # Pillow's own check, at twice the limit
        raise UploadRejected("import-too-big") from None
    except (OSError, ValueError, SyntaxError):
        raise UploadRejected("import-bad-type") from None


# A page object: "/Type /Page" but not "/Type /Pages". Cheap and approximate
# (object streams can hide pages); the API's own 400 is mapped too.
_PDF_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def _pdf_page_count(data: bytes) -> int:
    return len(_PDF_PAGE.findall(data))


def prepare_attachment(filename: str, data: bytes) -> Attachment:
    """Checks one file (size, real type) and returns it ready for Claude.
    Blocking (Pillow): call via asyncio.to_thread. Raises UploadRejected."""
    if not data:
        raise UploadRejected("import-empty")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise UploadRejected("import-too-big")
    mime_type = _sniff(data)
    if mime_type is None:
        raise UploadRejected("import-bad-type")
    if mime_type == PDF_TYPE:
        if _pdf_page_count(data) > MAX_PDF_PAGES:
            raise UploadRejected("import-pdf-pages")
    else:
        data, mime_type = _normalise_image(data, mime_type)
    return Attachment(filename=(filename or "file")[:120], mime_type=mime_type, data=data)


def check_attachment_limits(count: int, total_bytes: int) -> None:
    if count > MAX_ATTACHMENTS:
        raise UploadRejected("import-too-many")
    if total_bytes > MAX_TOTAL_BYTES:
        raise UploadRejected("import-too-big")


def content_ref(text: str, blobs: list[bytes]) -> str:
    """A stable source_ref for an upload or paste: a hash of its content,
    so adding the same screenshot twice finds the first one."""
    digest = hashlib.sha256()
    digest.update(" ".join(text.split()).encode())
    for blob in blobs:
        digest.update(b"\0" + hashlib.sha256(blob).digest())
    return digest.hexdigest()


# --- Ingest ---

async def get_children(db) -> list[Child]:
    """Who the extractor may assign items to: family members with a year
    group, or everyone if no year groups are set yet."""
    rows = [dict(r) for r in await (await db.execute(
        "SELECT id, name, school_year FROM profiles ORDER BY sort_order"
    )).fetchall()]
    with_year = [r for r in rows if (r["school_year"] or "").strip()]
    return [Child(r["id"], r["name"], (r["school_year"] or "").strip() or None) for r in (with_year or rows)]


def _excerpt(text: str) -> str | None:
    return " ".join((text or "").split())[:EXCERPT_CHARS] or None


async def claim_source(db, doc: SourceDocument) -> tuple[int, bool, str]:
    """Records the source. Returns (source_id, should_extract, status): a new
    source, or one that failed / wasn't configured last time, is claimed
    (set to pending) and should be extracted; anything else is left alone."""
    if doc.kind not in KINDS:
        raise ValueError(f"unknown source kind {doc.kind!r}")
    received = (doc.received_at or datetime.now(UTC)).isoformat()
    cursor = await db.execute(
        """INSERT OR IGNORE INTO import_sources
               (kind, source_ref, received_at, subject, excerpt, attachment_count, status)
           VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
        (doc.kind, doc.source_ref, received, (doc.subject or "")[:MAX_SUBJECT_CHARS] or None,
         _excerpt(doc.text), len(doc.attachments)),
    )
    if cursor.rowcount:
        await db.commit()
        return cursor.lastrowid, True, "pending"
    row = await (await db.execute(
        "SELECT id, status FROM import_sources WHERE kind = ? AND source_ref = ?", (doc.kind, doc.source_ref)
    )).fetchone()
    # Conditional update, so two retries racing can't both claim it.
    retried = await db.execute(
        f"""UPDATE import_sources SET status = 'pending', error_code = NULL, updated_at = datetime('now')
            WHERE id = ? AND status IN ({",".join("?" * len(_RETRYABLE))})""",
        (row["id"], *_RETRYABLE),
    )
    await db.commit()
    if retried.rowcount:
        return row["id"], True, "pending"
    return row["id"], False, row["status"]


async def _set_status(db, source_id: int, status: str, error_code: str | None = None):
    await db.execute(
        "UPDATE import_sources SET status = ?, error_code = ?, updated_at = datetime('now') WHERE id = ?",
        (status, error_code, source_id),
    )
    await db.commit()


async def _extract_into(db, source_id: int, doc: SourceDocument) -> IngestResult:
    """Runs the extractor for a claimed source and stores its candidates."""
    try:
        try:
            candidates = await extract(doc, await get_children(db), await family_today(db))
        except NotConfigured:
            await _set_status(db, source_id, "not_configured", "not_configured")
            return IngestResult(source_id, "not_configured")
        except ExtractionFailed as exc:
            await _set_status(db, source_id, "failed", exc.code)
            return IngestResult(source_id, "failed")
        for candidate in candidates:
            await _store_candidate(db, source_id, candidate)
        await _set_status(db, source_id, "extracted")
    except Exception as exc:
        # Never leave a source stuck on "Reading…" (a crash, or the database
        # busy while storing). Only the exception type is logged: a
        # traceback could carry document text.
        logger.error("School inbox extraction crashed: %s", type(exc).__name__)
        await db.rollback()
        try:
            await _set_status(db, source_id, "failed", "error")
        except Exception:  # still busy: fail_interrupted / Remove will tidy it
            logger.error("Couldn't mark school inbox source %s failed", source_id)
        return IngestResult(source_id, "failed")
    return IngestResult(source_id, "extracted", candidate_count=len(candidates))


async def ingest(db, doc: SourceDocument) -> IngestResult:
    """Stage-2 entry point: record `doc` and extract its candidates, awaited.

    Idempotent by (doc.kind, doc.source_ref): calling it again for a
    document already extracted (or being extracted) returns that source with
    already=True and makes no API call. A source that failed or found no API
    key is retried. The only writes are import_sources/import_candidates
    rows — nothing appears on the wall until a parent approves it."""
    source_id, should_extract, status = await claim_source(db, doc)
    if not should_extract:
        return IngestResult(source_id, status, already=True)
    _running.add(source_id)
    try:
        return await _extract_into(db, source_id, doc)
    finally:
        _running.discard(source_id)


# Background extraction (the Admin path). Tasks are kept referenced so they
# aren't garbage-collected mid-flight.
_tasks: set[asyncio.Task] = set()
_running: set[int] = set()


async def _background(source_id: int, doc: SourceDocument):
    try:
        async with get_db() as db:
            await _extract_into(db, source_id, doc)
    finally:
        _running.discard(source_id)


async def start_ingest(doc: SourceDocument) -> IngestResult:
    """Admin path: record the source now, extract in the background."""
    async with get_db() as db:
        source_id, should_extract, status = await claim_source(db, doc)
    if not should_extract:
        return IngestResult(source_id, status, already=True)
    _running.add(source_id)
    task = asyncio.create_task(_background(source_id, doc))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return IngestResult(source_id, "pending")


async def wait_for_background():
    """Waits for any running background extractions (tests, demo scripts)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def fail_interrupted(db):
    """At startup: a source still "pending" was being read when the app
    stopped, and its bytes are gone — mark it failed so it can be re-added
    (a Gmail one is read again by the next school email check)."""
    await db.execute(
        """UPDATE import_sources SET status = 'failed', error_code = 'interrupted', updated_at = datetime('now')
           WHERE status = 'pending'"""
    )
    # An event mid-approval when the app stopped goes back to pending; its
    # Calendar event id is deterministic, so approving it again can't make
    # a second event (see services/school_events.py).
    await db.execute(
        "UPDATE import_candidates SET status = 'pending' WHERE status = 'approving'"
    )
    await db.commit()


# --- Dedupe ---

def _word_key(words) -> frozenset:
    return frozenset(w.casefold() for w in words if w)


async def _is_duplicate(db, source_id: int, candidate: Candidate) -> bool:
    """Whether this candidate is already on the wall (or already waiting in
    the inbox from another source): the same words for the same child and
    week, or the same homework title for the same child when the due dates
    match or the existing one is still open on the wall. Recurring homework
    ("Read 20 minutes", no due date) isn't a duplicate of last week's done
    or archived copy."""
    if candidate.profile_id is None or candidate.kind == "event":
        return False
    payload = candidate.payload
    if candidate.kind == "word_list":
        key = _word_key(payload["words"])
        rows = await (await db.execute(
            "SELECT words, starts_on FROM practice_word_lists WHERE profile_id = ? AND archived = 0",
            (candidate.profile_id,),
        )).fetchall()
        for row in rows:
            same_week = not payload["starts_on"] or not row["starts_on"] or payload["starts_on"] == row["starts_on"]
            if same_week and _word_key(row["words"].split("\n")) == key:
                return True
        pending = await _pending_payloads(db, source_id, "word_list", candidate.profile_id)
        return any(
            _word_key(p["words"]) == key and (not payload["starts_on"] or not p.get("starts_on")
                                              or p.get("starts_on") == payload["starts_on"])
            for p in pending
        )
    title, due = payload["title"].casefold(), payload["due_date"]
    rows = await (await db.execute(
        "SELECT title, due_date, done, archived FROM homework WHERE profile_id = ?", (candidate.profile_id,)
    )).fetchall()
    for row in rows:
        if row["title"].casefold() != title:
            continue
        if due and row["due_date"] == due:
            return True
        if not row["done"] and not row["archived"] and row["due_date"] == due:
            return True
    pending = await _pending_payloads(db, source_id, "homework", candidate.profile_id)
    return any(p["title"].casefold() == title and p.get("due_date") == payload["due_date"] for p in pending)


async def _pending_payloads(db, source_id: int, kind: str, profile_id: int) -> list[dict]:
    rows = await (await db.execute(
        """SELECT payload_json FROM import_candidates
           WHERE kind = ? AND profile_id = ? AND status = 'pending' AND source_id != ?""",
        (kind, profile_id, source_id),
    )).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


async def _store_candidate(db, source_id: int, candidate: Candidate):
    duplicate = await _is_duplicate(db, source_id, candidate)
    await db.execute(
        """INSERT INTO import_candidates (source_id, kind, profile_id, payload_json, evidence, duplicate)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (source_id, candidate.kind, candidate.profile_id, json.dumps(candidate.payload),
         candidate.evidence, int(duplicate)),
    )


# --- The Admin inbox ---

async def get_inbox(db) -> dict:
    """Admin's School inbox: `active` sources (reading, failed, or with
    candidates still to decide) and recently `processed` ones, newest first."""
    sources = [dict(r) for r in await (await db.execute(
        "SELECT * FROM import_sources ORDER BY created_at DESC, id DESC"
    )).fetchall()]
    rows = await (await db.execute(
        """SELECT c.*, p.name AS profile_name FROM import_candidates c
           LEFT JOIN profiles p ON p.id = c.profile_id ORDER BY c.id"""
    )).fetchall()
    by_source: dict[int, list[dict]] = {}
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        by_source.setdefault(item["source_id"], []).append(item)
    active, processed = [], []
    for source in sources:
        _decorate(source, by_source.get(source["id"], []))
        (processed if source["processed"] else active).append(source)
    return {"active": active, "processed": processed[:PROCESSED_SHOWN]}


async def get_source(db, source_id: int) -> dict | None:
    row = await (await db.execute("SELECT * FROM import_sources WHERE id = ?", (source_id,))).fetchone()
    if row is None:
        return None
    source = dict(row)
    rows = await (await db.execute(
        """SELECT c.*, p.name AS profile_name FROM import_candidates c
           LEFT JOIN profiles p ON p.id = c.profile_id WHERE c.source_id = ? ORDER BY c.id""",
        (source_id,),
    )).fetchall()
    candidates = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        candidates.append(item)
    _decorate(source, candidates)
    return source


def _decorate(source: dict, candidates: list[dict]):
    source["candidates"] = [c for c in candidates if c["status"] == "pending"]
    source["approved_count"] = sum(c["status"] == "approved" for c in candidates)
    source["discarded_count"] = sum(c["status"] == "discarded" for c in candidates)
    source["error_message"] = ERROR_MESSAGES.get(source["error_code"] or "", ERROR_MESSAGES["error"])
    source["processed"] = source["status"] == "extracted" and not source["candidates"]
    source["removable"] = source["status"] != "pending" or _is_stuck(source)
    source["approvable"] = sum(
        1 for c in source["candidates"] if c["kind"] != "event" and c["profile_id"] and not c["duplicate"]
    )
    if source["subject"]:
        source["label"] = source["subject"]
    else:
        source["label"] = {"paste": "Pasted text", "gmail": "School email"}.get(source["kind"], "Upload")


def _is_stuck(source: dict) -> bool:
    """Pending but not being read by this process, or "Reading…" for far
    longer than any API call can take."""
    if source["id"] not in _running:
        return True
    try:
        updated = datetime.fromisoformat(source["updated_at"]).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return True
    return datetime.now(UTC) - updated > STUCK_AFTER


async def _pending_candidate(db, candidate_id: int) -> dict:
    row = await (await db.execute(
        """SELECT c.*, s.kind AS source_kind FROM import_candidates c
           JOIN import_sources s ON s.id = c.source_id WHERE c.id = ?""",
        (candidate_id,),
    )).fetchone()
    if row is None or row["status"] != "pending":
        raise CandidateError("import-missing")
    return dict(row)


async def approve_candidate(db, candidate_id: int, form: dict) -> tuple[str, int]:
    """Creates the word list or homework from the (possibly edited) form,
    validated exactly like Admin's own forms. Returns (table, row id).
    Raises CandidateError or homework.ValidationError. Events go to Google
    Calendar instead (services/school_events.approve_event)."""
    candidate = await _pending_candidate(db, candidate_id)
    source = f"import:{candidate['source_kind']}"
    # Validate first (reads only), then claim + insert in one transaction.
    if candidate["kind"] == "word_list":
        fields = await homework.word_list_fields(
            db, form.get("profile_id"), form.get("title"), form.get("words"),
            form.get("starts_on"), form.get("ends_on"),
        )
        table = "practice_word_lists"
        insert = """INSERT INTO practice_word_lists (profile_id, title, words, starts_on, ends_on, source)
                    VALUES (?, ?, ?, ?, ?, ?)"""
        payload = {"title": fields[1], "words": fields[2].split("\n"), "starts_on": fields[3], "ends_on": fields[4]}
    elif candidate["kind"] == "homework":
        fields = await homework.homework_fields(
            db, form.get("profile_id"), form.get("subject"), form.get("title"),
            form.get("details"), form.get("due_date"),
        )
        table = "homework"
        insert = """INSERT INTO homework (profile_id, subject, title, details, due_date, source)
                    VALUES (?, ?, ?, ?, ?, ?)"""
        payload = {"subject": fields[1], "title": fields[2], "details": fields[3] or "", "due_date": fields[4]}
    else:
        raise CandidateError("import-event")
    try:
        # The conditional claim is the lock: of two approvals racing (two
        # taps, or Approve all against a single Approve), only one gets
        # rowcount 1; the other waits on SQLite's write lock, then sees 0.
        claimed = await db.execute(
            """UPDATE import_candidates SET status = 'approved', profile_id = ?, payload_json = ?,
                   updated_at = datetime('now') WHERE id = ? AND status = 'pending'""",
            (fields[0], json.dumps(payload), candidate_id),
        )
        if claimed.rowcount != 1:
            raise CandidateError("import-missing")
        cursor = await db.execute(insert, (*fields, source))
        await db.execute(
            "UPDATE import_candidates SET created_table = ?, created_id = ? WHERE id = ?",
            (table, cursor.lastrowid, candidate_id),
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return table, cursor.lastrowid


def form_from_payload(candidate: dict) -> dict:
    """The approve form's values for a stored candidate, as the Admin form
    would submit them."""
    payload = candidate["payload"]
    form = {key: payload.get(key) or "" for key in ("title", "subject", "details", "due_date", "starts_on", "ends_on")}
    form["words"] = "\n".join(payload.get("words") or [])
    form["profile_id"] = candidate["profile_id"] or ""
    return form


async def approve_all(db, source_id: int) -> int:
    """Approves every pending word list and homework in a source that has a
    child and isn't already on the wall. Anything that fails validation is
    left for the parent. Returns how many were approved."""
    source = await get_source(db, source_id)
    if source is None:
        raise CandidateError("import-missing")
    approved = 0
    for candidate in source["candidates"]:
        if candidate["kind"] == "event" or not candidate["profile_id"] or candidate["duplicate"]:
            continue
        try:
            await approve_candidate(db, candidate["id"], form_from_payload(candidate))
            approved += 1
        except (homework.ValidationError, CandidateError):
            continue
    return approved


async def discard_candidate(db, candidate_id: int):
    cursor = await db.execute(
        """UPDATE import_candidates SET status = 'discarded', updated_at = datetime('now')
           WHERE id = ? AND status = 'pending'""",
        (candidate_id,),
    )
    await db.commit()
    if cursor.rowcount != 1:
        raise CandidateError("import-missing")


async def delete_source(db, source_id: int):
    """Removes a source and its candidates from the inbox. Rows already
    approved onto the wall stay. A source still being read can't be removed
    unless it's stuck."""
    source = await get_source(db, source_id)
    if source is None:
        return
    if not source["removable"]:
        raise CandidateError("import-busy")
    await db.execute("DELETE FROM import_sources WHERE id = ?", (source_id,))
    await db.commit()


def is_reading(source_id: int) -> bool:
    return source_id in _running
