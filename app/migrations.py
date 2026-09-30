"""
Numbered database migrations (spec 9.8).

Each migration runs once, in version order, inside its own transaction, and is
recorded in `schema_migrations` in that same transaction. A failure rolls the
whole migration back, records nothing and stops startup.

Rules for adding one:
- **Append only.** Add a new `Migration(N + 1, "name", fn)` at the end of
  MIGRATIONS. Never edit, reorder or remove one that has shipped: the family's
  database has already applied it and will never run it again.
- A migration receives a connection that is already inside a transaction.
  Its `commit()`, `rollback()` and `executescript()` raise MigrationError
  (each would end the transaction early); PRAGMAs that can't run inside a
  transaction don't belong in one either.
- Don't read live constants (database.DEFAULT_LAYOUT, ...) in a migration:
  freeze a copy, as the baseline does, so it means the same thing forever.
- Keep them idempotent where it's cheap (`IF NOT EXISTS`,
  `database._add_column_if_missing`): a restored backup from before a
  migration simply runs it again.

Checks that must hold on every boot, not once (the default PIN re-seeded if
its row is deleted, a newly registered widget getting a layout row, ...) are
in `startup_checks`, which runs after the migrations.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app import database

logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """A migration failed and was rolled back; startup must not continue."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[..., Awaitable[None]]


class _MigrationConnection:
    """What a migration is given: the runner's connection, minus the calls
    that would end its transaction early (and so commit half a migration)."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def commit(self):
        raise MigrationError("a migration must not commit: the runner commits it with its version")

    async def rollback(self):
        raise MigrationError("a migration must not roll back: raise an exception instead")

    async def executescript(self, *_args, **_kwargs):
        raise MigrationError("a migration must not use executescript (it commits): execute each statement")


# --- Every-boot checks ------------------------------------------------------------------
# Each of these ran on every start before numbered migrations and still does:
# they repair data (never schema) and are no-ops on a healthy database. The
# baseline calls them too, at the same points the old init_db() did, but with
# its own frozen constants. They are shared with the baseline, so don't change
# what they do to a database: add a new migration instead.


async def _backfill_updated_at(db):
    # Shopping items / tasks inserted without updated_at on a database whose
    # column was added by ALTER TABLE (no default) get it from created_at.
    await db.execute("UPDATE shopping_items SET updated_at = created_at WHERE updated_at IS NULL")
    await db.execute("UPDATE tasks SET updated_at = created_at WHERE updated_at IS NULL")


async def _seed_profiles_if_empty(db, profiles):
    cursor = await db.execute("SELECT COUNT(*) FROM profiles")
    (count,) = await cursor.fetchone()
    if count == 0:
        await db.executemany(
            "INSERT INTO profiles (name, colour_hex, sort_order) VALUES (?, ?, ?)",
            profiles,
        )


async def _seed_layout_if_empty(db, layout):
    cursor = await db.execute("SELECT COUNT(*) FROM layout_state")
    (count,) = await cursor.fetchone()
    if count == 0:
        await db.executemany(
            """INSERT INTO layout_state (widget_id, grid_x, grid_y, grid_w, grid_h, is_visible)
               VALUES (?, ?, ?, ?, ?, 1)""",
            layout,
        )


async def _seed_pin(db, default_pin):
    # Seed a default admin PIN (1234) on first run only — change this in Admin > Settings
    cursor = await db.execute("SELECT COUNT(*) FROM app_settings WHERE key = 'pin_hash'")
    (count,) = await cursor.fetchone()
    if count == 0:
        from app.security import hash_pin

        await db.execute(
            "INSERT INTO app_settings (key, value) VALUES ('pin_hash', ?)",
            (hash_pin(default_pin),),
        )
        # Whenever 1234 is seeded (first run, or the pin_hash row was
        # deleted), Admin must force a change away from it again.
        await database.set_setting(db, "pin_is_default", "1")

    # PIN hardening. Admin forces a change away from the default PIN,
    # tracked by a flag rather than a PBKDF2 check on every request. Set
    # once, for fresh installs and existing DBs alike, by checking whether
    # the stored hash is still "1234"; change-pin clears it.
    if await database.get_setting(db, "pin_is_default") is None:
        from app.security import verify_pin

        stored = await database.get_setting(db, "pin_hash") or ""
        await database.set_setting(db, "pin_is_default", "1" if verify_pin(default_pin, stored) else "0")


async def _seed_sync_status(db):
    await db.execute("INSERT OR IGNORE INTO sync_status (id) VALUES (1)")


async def startup_checks(db):
    """Every-boot data repairs, in the order the old init_db() ran them, with
    the live defaults."""
    from app.security import DEFAULT_PIN

    await _backfill_updated_at(db)
    await _seed_profiles_if_empty(db, database.DEFAULT_PROFILES)
    await _seed_layout_if_empty(db, database.DEFAULT_LAYOUT)
    # Widgets added after a layout shipped get a row of their own below
    # everything else, leaving every existing position alone.
    await database._add_missing_widgets(db, database.DEFAULT_LAYOUT)
    await _seed_pin(db, DEFAULT_PIN)
    await _seed_sync_status(db)


# --- 0001 baseline ----------------------------------------------------------------------
# Everything init_db() did before numbered migrations, unchanged and in the
# same order (only its intermediate commits are gone: the runner commits once).
# On a database that init_db() already built (the family's live one) every
# step is a no-op, so applying it only records version 1. Never edit it.
#
# Frozen copies of everything it seeds, as of 28 Sep 2026. The live
# database.DEFAULT_LAYOUT etc. will change; the baseline must not. A widget
# added later reaches existing databases through startup_checks'
# _add_missing_widgets, and a layout reset is a new migration, never a
# LAYOUT_VERSION bump.
BASELINE_LAYOUT_VERSION = "2"
BASELINE_LAYOUT = (
    # widget_id, x, y, w, h
    ("calendar", 0, 0, 12, 7),
    ("tasks", 0, 7, 4, 4),
    ("shopping", 4, 7, 2, 4),
    ("meals", 6, 7, 2, 4),
    ("weather", 8, 7, 2, 2),
    ("photos", 8, 9, 2, 2),
    ("homework", 10, 7, 2, 4),
    ("practice_words", 0, 11, 6, 4),
)
BASELINE_PROFILES = (
    # name, colour_hex, sort_order
    ("Mum", "#C1584A", 0),
    ("Dad", "#3D6E93", 1),
    ("Riley", "#D6A02C", 2),
    ("Jamie", "#4C8577", 3),
)
BASELINE_PIN = "1234"

BASELINE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    colour_hex TEXT NOT NULL,
    avatar_path TEXT,  -- reserved/unused: nothing reads it (kept for the live DB)
    sort_order INTEGER NOT NULL DEFAULT 0,
    google_tasklist_id TEXT
)""",
    """CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    google_task_id TEXT,
    title TEXT NOT NULL,
    is_recurring INTEGER NOT NULL DEFAULT 0,
    recurrence_rule TEXT,
    is_completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)""",
    """CREATE TABLE IF NOT EXISTS meal_plans (
    date TEXT PRIMARY KEY,
    meal_description TEXT NOT NULL DEFAULT ''
)""",
    """CREATE TABLE IF NOT EXISTS shopping_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    is_checked INTEGER NOT NULL DEFAULT 0,
    google_task_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)""",
    """CREATE TABLE IF NOT EXISTS layout_state (
    widget_id TEXT PRIMARY KEY,
    grid_x INTEGER NOT NULL,
    grid_y INTEGER NOT NULL,
    grid_w INTEGER NOT NULL,
    grid_h INTEGER NOT NULL,
    is_visible INTEGER NOT NULL DEFAULT 1,
    config_json TEXT  -- reserved/unused: nothing reads it (kept for the live DB)
)""",
    """CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)""",
    """CREATE TABLE IF NOT EXISTS auth_tokens (
    service_name TEXT PRIMARY KEY,
    account_email TEXT,
    encrypted_token_json TEXT,
    sync_token TEXT  -- reserved/unused: nothing reads it (kept for the live DB)
)""",
    # Homework and handwriting practice words (Admin-entered for now; `source`
    # leaves room for school-email / Classroom imports). Never synced to Google.
    """CREATE TABLE IF NOT EXISTS homework (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    subject TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    details TEXT,
    due_date TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    done_at TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)""",
    """CREATE TABLE IF NOT EXISTS practice_word_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    words TEXT NOT NULL DEFAULT '',
    starts_on TEXT,
    ends_on TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)""",
    """CREATE TABLE IF NOT EXISTS practice_log (
    list_id INTEGER NOT NULL REFERENCES practice_word_lists(id) ON DELETE CASCADE,
    practised_on TEXT NOT NULL,
    PRIMARY KEY (list_id, practised_on)
)""",
    # Generic key/value store: admin PIN hash, lockout state, brightness schedule, etc.
    """CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
)""",
]


async def m0001_baseline(db):
    for statement in BASELINE_SCHEMA:
        await db.execute(statement)

    add_column = database._add_column_if_missing

    # Google Tasks sync (shopping list + per-person task lists) added columns
    # to tables that already shipped without them.
    await add_column(db, "profiles", "google_tasklist_id", "TEXT")
    await add_column(db, "shopping_items", "updated_at", "TEXT")
    await add_column(db, "tasks", "updated_at", "TEXT")
    await _backfill_updated_at(db)

    await _seed_profiles_if_empty(db, BASELINE_PROFILES)
    await _seed_layout_if_empty(db, BASELINE_LAYOUT)

    # Calendar became the dashboard's hero widget (Google-style month grid
    # with event bars + a day view) and needs far more room, so every
    # widget's default position changed shape. One-time, keyed on a version
    # flag: wipes the layout and re-inserts the layout of the time. This
    # intentionally resets any custom drag/resize positions. Future layout
    # changes are new migrations, not a LAYOUT_VERSION bump.
    if await database.get_setting(db, "layout_version") != BASELINE_LAYOUT_VERSION:
        await db.execute("DELETE FROM layout_state")
        await db.executemany(
            """INSERT INTO layout_state (widget_id, grid_x, grid_y, grid_w, grid_h, is_visible)
               VALUES (?, ?, ?, ?, ?, 1)""",
            BASELINE_LAYOUT,
        )
        await database.set_setting(db, "layout_version", BASELINE_LAYOUT_VERSION)

    # The Calendar widget absorbed Upcoming Events (now a month grid instead
    # of two agenda lists) — widen calendar's slot by upcoming_events' old
    # width and drop it, preserving wherever the widget's been dragged to.
    # No-op once this has run (upcoming_events row no longer exists).
    cursor = await db.execute("SELECT grid_w FROM layout_state WHERE widget_id = 'upcoming_events'")
    row = await cursor.fetchone()
    if row is not None:
        await db.execute(
            "UPDATE layout_state SET grid_w = grid_w + ? WHERE widget_id = 'calendar'",
            (row[0],),
        )
        await db.execute("DELETE FROM layout_state WHERE widget_id = 'upcoming_events'")

    await database._add_missing_widgets(db, BASELINE_LAYOUT)
    await _seed_pin(db, BASELINE_PIN)

    # Calendar outage cache (app/calendar_cache.py). Each selected calendar's
    # last successfully fetched events per displayed [range_start, range_end)
    # date range, shown with a "Last updated" note when Google is down or
    # slow. `selection` hashes the account + calendar selection. Event data
    # only — never tokens.
    await db.execute(
        """CREATE TABLE IF NOT EXISTS calendar_cache (
                selection TEXT NOT NULL,
                range_start TEXT NOT NULL,
                range_end TEXT NOT NULL,
                calendar_id TEXT NOT NULL,
                events_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,  -- UTC ISO timestamp
                PRIMARY KEY (selection, range_start, range_end, calendar_id)
            )"""
    )

    # Sync health (app/sync_status.py). One row, written by
    # task_sync.run_sync every cycle; read by Admin's Sync panel, the
    # dashboard's status dot and /health. Log-safe codes only
    # ("offline", "HTTP 403", "not connected") — never tokens or URLs.
    await db.execute(
        """CREATE TABLE IF NOT EXISTS sync_status (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   connected INTEGER NOT NULL DEFAULT 0,
                   last_cycle_at TEXT,           -- *_at: tz-aware UTC ISO 8601
                   last_success_at TEXT,
                   last_failure_at TEXT,
                   failing_since TEXT,           -- first failure of the current streak
                   last_error TEXT,
                   consecutive_failures INTEGER NOT NULL DEFAULT 0,
                   auth_failures INTEGER NOT NULL DEFAULT 0,  -- same 401/403 cycles in a row
                   queue_depth INTEGER NOT NULL DEFAULT 0
               )"""
    )
    await _seed_sync_status(db)

    # School inbox (app/services/imports.py). A child's year group ("Year 4")
    # lets the extractor assign school items to them. import_sources has one
    # row per ingested document (a screenshot, pasted text, later a Gmail
    # message) — metadata and a short excerpt only, never attachment bytes or
    # whole email bodies. Each candidate the extractor found waits in
    # import_candidates until a parent approves (creating the row named by
    # created_table/created_id) or discards it.
    await add_column(db, "profiles", "school_year", "TEXT")
    await db.execute(
        """CREATE TABLE IF NOT EXISTS import_sources (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   kind TEXT NOT NULL,              -- upload / paste / gmail
                   source_ref TEXT NOT NULL,        -- content hash, later the Gmail message id
                   received_at TEXT,
                   subject TEXT,
                   excerpt TEXT,                    -- first few hundred characters of any text
                   attachment_count INTEGER NOT NULL DEFAULT 0,
                   status TEXT NOT NULL DEFAULT 'pending',  -- pending/extracted/failed/not_configured
                   error_code TEXT,                 -- log-safe code, see imports.ERROR_MESSAGES
                   created_at TEXT NOT NULL DEFAULT (datetime('now')),
                   updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                   UNIQUE (kind, source_ref)
               )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS import_candidates (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   source_id INTEGER NOT NULL REFERENCES import_sources(id) ON DELETE CASCADE,
                   kind TEXT NOT NULL,              -- word_list / homework / event
                   profile_id INTEGER REFERENCES profiles(id) ON DELETE SET NULL,
                   payload_json TEXT NOT NULL,
                   evidence TEXT NOT NULL DEFAULT '',
                   duplicate INTEGER NOT NULL DEFAULT 0,   -- already on the wall when found
                   status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/discarded
                   created_table TEXT,
                   created_id INTEGER,
                   created_at TEXT NOT NULL DEFAULT (datetime('now')),
                   updated_at TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS import_candidates_source ON import_candidates (source_id)")

    # School email import (app/school_email.py). An approved event goes to
    # Google Calendar rather than a local table, and its Calendar event id is
    # kept here (created_table = 'google_calendar').
    await add_column(db, "import_candidates", "external_id", "TEXT")
    # The calendar an event approval was claimed for: a retry after a crash
    # goes to the same one (services/school_events.py).
    await add_column(db, "import_candidates", "claim_calendar_id", "TEXT")
    # Claude calls made for a source (automatic retries give up after
    # imports.MAX_ATTEMPTS), and whether a Gmail sender went unverified.
    await add_column(db, "import_sources", "attempts", "INTEGER NOT NULL DEFAULT 0")
    await add_column(db, "import_sources", "sender_unverified", "INTEGER NOT NULL DEFAULT 0")
    # Gmail messages the school email check decided not to read (not from an
    # allowed sender, mentions an excluded address, sender failed
    # verification): ids, a neutral code and Gmail's timestamp only, never
    # content, so they aren't fetched again.
    await db.execute(
        """CREATE TABLE IF NOT EXISTS gmail_skipped (
                   message_id TEXT PRIMARY KEY,
                   code TEXT NOT NULL,
                   internal_date INTEGER,            -- Gmail internalDate, epoch ms
                   created_at TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
    )


async def m0002_profile_parents(db):
    """Spec 10.0: which family members are parents, and their email
    addresses (for assistant A1's parent tasks and A4's digest)."""
    await database._add_column_if_missing(db, "profiles", "is_parent", "INTEGER NOT NULL DEFAULT 0")
    await database._add_column_if_missing(db, "profiles", "email", "TEXT")


async def m0003_term_dates(db):
    """Spec 10.6: the school's term dates (one set per household) and the
    GOV.UK bank holidays (England and Wales). Bank holidays get their own
    table rather than a kind in school_periods: they're a feed, refreshed
    wholesale and keyed by date, so a refresh can never touch a parent's
    own entries, and Admin's term-date list, edits and inbox dedupe never
    see feed rows. See app/services/term_dates.py."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS school_periods (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   kind TEXT NOT NULL,          -- term / holiday / half_term / inset / closure
                   start_date TEXT NOT NULL,    -- ISO date, inclusive
                   end_date TEXT NOT NULL,      -- ISO date, inclusive
                   label TEXT NOT NULL DEFAULT '',
                   source TEXT NOT NULL DEFAULT 'manual',  -- manual / import
                   created_at TEXT NOT NULL DEFAULT (datetime('now')),
                   updated_at TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS school_periods_dates ON school_periods (start_date, end_date)")
    await db.execute(
        """CREATE TABLE IF NOT EXISTS bank_holidays (
                   date TEXT PRIMARY KEY,       -- ISO date
                   title TEXT NOT NULL,
                   updated_at TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
    )


async def m0004_task_groups(db):
    """Spec 10.4: two local-only task columns, never pushed to Google (like
    recurrence_rule). time_of_day is 'morning' / 'after_school' / 'evening'
    or NULL (no group). due_on is the family-local ISO date a one-off was
    due, for "from yesterday" carry-over labels; Google's own `due` is a
    date with no time and is often unset, so it can't be relied on. Existing
    rows keep NULL in both: no group, and never marked late."""
    await database._add_column_if_missing(db, "tasks", "time_of_day", "TEXT")
    await database._add_column_if_missing(db, "tasks", "due_on", "TEXT")


async def m0005_widget_visibility(db):
    """Spec 10.3: a "school days only" switch per widget, and spec 10.2: the
    Photos placeholder widget leaves the dashboard.

    school_days_only is a column on layout_state rather than a table of its
    own: like is_visible it's one flag per widget, it belongs to the same row
    (added for a new widget by startup_checks, deleted with a removed one),
    and every reader already loads that row. hide_moves (JSON) records, on a
    hidden widget's row, which widgets moved up when it was hidden, so
    showing it again can put them back.

    The Photos row is deleted, and if it was showing, its gap is closed once
    with the same logic as hiding a widget (only the widgets below it in its
    columns move up). A row already hidden by hand never took up space on the
    wall, so nothing moves for it. Its registry entry is gone, so
    startup_checks won't add it back."""
    from app.services.layout import close_gap

    await database._add_column_if_missing(db, "layout_state", "school_days_only", "INTEGER NOT NULL DEFAULT 0")
    await database._add_column_if_missing(db, "layout_state", "hide_moves", "TEXT")
    rows = [
        dict(r)
        for r in await (
            await db.execute("SELECT widget_id, grid_x, grid_y, grid_w, grid_h, is_visible FROM layout_state")
        ).fetchall()
    ]
    photos = next((r for r in rows if r["widget_id"] == "photos"), None)
    if photos is None:
        return
    await db.execute("DELETE FROM layout_state WHERE widget_id = 'photos'")
    if not photos["is_visible"]:
        return
    visible = [r for r in rows if r["is_visible"]]
    before = {r["widget_id"]: r["grid_y"] for r in visible}
    for row in close_gap(visible, photos):
        if row["grid_y"] != before[row["widget_id"]]:
            await db.execute(
                "UPDATE layout_state SET grid_y = ? WHERE widget_id = ?", (row["grid_y"], row["widget_id"])
            )


async def m0006_photos(db):
    """Spec 10.2: the idle slideshow's photos, picked with the Google Photos
    Picker and copied into DATA_DIR/photos/ (app/google_photos.py). One row
    per file on disk: `filename` and `thumb` are names we generated (never
    Google's), so the serving route can only ever open these. The files
    aren't in backups (they can be re-picked); a restored database whose
    files are missing just shows fewer photos. The Photos account's token
    goes in auth_tokens under its own service_name, so it needs no schema."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS photos (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: /photos/{id} can be cached for good
                   filename TEXT NOT NULL,
                   thumb TEXT NOT NULL,
                   width INTEGER,
                   height INTEGER,
                   bytes INTEGER,
                   created_at TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
    )


# Frozen copy of services/homework.subject_key_for's table as of this
# migration (see "Don't read live constants" above).
_M7_LANGUAGE_WORDS = ("french", "spanish", "german", "mfl", "languages", "modern foreign languages")
_M7_SYNONYMS = {
    "maths": (
        "maths",
        "math",
        "mathematics",
        "numeracy",
        "number",
        "numbers",
        "number bonds",
        "times tables",
        "times table",
        "arithmetic",
        "mental maths",
        "fractions",
        "sumdog",
        "mathletics",
        "numbots",
        "tt rockstars",
        "ttrockstars",
        "ttrs",
        "times tables rock stars",
    ),
    "english": (
        "english",
        "spelling",
        "spellings",
        "phonics",
        "grammar",
        "spag",
        "gps",
        "punctuation",
        "writing",
        "handwriting",
        "literacy",
        "vocabulary",
        "creative writing",
        "read write inc",
        "rwi",
    ),
    "reading": (
        "reading",
        "reader",
        "library",
        "reading book",
        "reading record",
        "guided reading",
        "comprehension",
        "reading comprehension",
        "bug club",
        "oxford owl",
    ),
    "science": ("science", "biology", "chemistry", "physics", "experiment", "investigation"),
    "topic": ("topic", "project", "history", "geography", "humanities", "topic work"),
}
_M7_WEAK_SYNONYMS = {"reading": ("read",)}


def _m7_first_match(text: str, table: dict) -> str | None:
    best = None
    for key, phrases in table.items():
        for phrase in phrases:
            at = text.find(f" {phrase} ")
            if at >= 0 and (best is None or (at, -len(phrase), key) < best):
                best = (at, -len(phrase), key)
    return best[2] if best else None


def _m7_subject_key(subject: str | None) -> str:
    text = " " + " ".join(re.sub(r"[^0-9a-z]+", " ", (subject or "").casefold()).split()) + " "
    if _m7_first_match(text, {"other": _M7_LANGUAGE_WORDS}):
        return "other"
    return _m7_first_match(text, _M7_SYNONYMS) or _m7_first_match(text, _M7_WEAK_SYNONYMS) or "other"


async def m0007_homework_extras(db):
    """Spec 10.10: homework subjects, the reading log and the done history.

    - homework.subject_key: one of the fixed subjects (maths / english /
      reading / science / topic / other) that picks the icon and colour. The
      free-text subject is kept as typed; existing rows are backfilled from
      it with a frozen copy of the synonym table.
    - reading_log: one row per child per family date they read.
    - homework_events: each tick done / undone with its family-tz time (who
      is unknown on a kiosk). Existing done rows get one event at their
      done_at, so Admin's history starts with what's already known."""
    await database._add_column_if_missing(db, "homework", "subject_key", "TEXT NOT NULL DEFAULT 'other'")
    # Only rows still on the default: a re-run (a restored older backup) never
    # overwrites a subject Admin picked since.
    for row in await (
        await db.execute("SELECT id, subject FROM homework WHERE subject_key IS NULL OR subject_key = 'other'")
    ).fetchall():
        await db.execute(
            "UPDATE homework SET subject_key = ? WHERE id = ?", (_m7_subject_key(row["subject"]), row["id"])
        )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS reading_log (
                   profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                   read_on TEXT NOT NULL,       -- family-local ISO date
                   created_at TEXT NOT NULL DEFAULT (datetime('now')),
                   PRIMARY KEY (profile_id, read_on)
               )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS homework_events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   homework_id INTEGER NOT NULL REFERENCES homework(id) ON DELETE CASCADE,
                   done INTEGER NOT NULL,       -- 1 ticked done, 0 unticked
                   at TEXT NOT NULL             -- ISO timestamp with the family's offset
               )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS homework_events_at ON homework_events (at)")
    await db.execute(
        """INSERT INTO homework_events (homework_id, done, at)
           SELECT id, 1, done_at FROM homework h
           WHERE done = 1 AND done_at IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM homework_events e WHERE e.homework_id = h.id)"""
    )


async def m0008_avatars(db):
    """Spec 10.9: each person's avatar is their coloured initial (as before),
    an emoji, or a photo. The photo is a 256x256 WebP kept in the database
    itself, so the nightly backup (the database and the key only) includes
    it. avatar_hash is the first characters of its SHA-256: the photo's URL
    carries it, so the URL changes whenever the photo does and can be cached
    for good. Existing rows get 'initial' and NULLs: nothing changes."""
    await database._add_column_if_missing(db, "profiles", "avatar_kind", "TEXT NOT NULL DEFAULT 'initial'")
    await database._add_column_if_missing(db, "profiles", "avatar_emoji", "TEXT")
    await database._add_column_if_missing(db, "profiles", "avatar_photo", "BLOB")
    await database._add_column_if_missing(db, "profiles", "avatar_hash", "TEXT")


# Append only: see the module docstring.
MIGRATIONS: list[Migration] = [
    Migration(1, "baseline", m0001_baseline),
    Migration(2, "profile_parents", m0002_profile_parents),
    Migration(3, "term_dates", m0003_term_dates),
    Migration(4, "task_groups", m0004_task_groups),
    Migration(5, "widget_visibility", m0005_widget_visibility),
    Migration(6, "photos", m0006_photos),
    Migration(7, "homework_extras", m0007_homework_extras),
    Migration(8, "avatars", m0008_avatars),
]


# --- Runner -----------------------------------------------------------------------------

SCHEMA_MIGRATIONS = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT,
    applied_at TEXT
)"""


def check_versions(migrations: list[Migration]) -> None:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be 1..N, unique and in order, got {versions}")


async def applied_versions(db) -> set[int]:
    rows = await (await db.execute("SELECT version FROM schema_migrations")).fetchall()
    return {row[0] for row in rows}


async def _has_table(db, name: str) -> bool:
    row = await (await db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))).fetchone()
    return row is not None


async def is_empty(db) -> bool:
    """A database with no tables at all: a first start, nothing to snapshot."""
    row = await (await db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1")).fetchone()
    return row is None


async def pending_versions(db, migrations: list[Migration] | None = None) -> list[int]:
    """The versions run_migrations() would apply now; empty when up to date.
    A database from before numbered migrations has no schema_migrations
    table, so everything is pending (the baseline then records itself)."""
    migrations = MIGRATIONS if migrations is None else migrations
    if not await _has_table(db, "schema_migrations"):
        return [m.version for m in migrations]
    latest = max(await applied_versions(db), default=0)
    return [m.version for m in migrations if m.version > latest]


async def run_migrations(db, migrations: list[Migration] | None = None) -> list[int]:
    """Apply every migration newer than the latest recorded one, in order.

    `db` must be opened with isolation_level=None so the runner owns the
    transactions. Returns the versions applied by this call."""
    migrations = MIGRATIONS if migrations is None else migrations
    check_versions(migrations)
    await db.execute(SCHEMA_MIGRATIONS)

    latest = max(await applied_versions(db), default=0)
    applied = []
    for migration in migrations:
        if migration.version <= latest:
            continue
        # IMMEDIATE takes the write lock up front; recheck inside it so a
        # second runner that got here first is never applied over.
        await db.execute("BEGIN IMMEDIATE")
        try:
            if migration.version in await applied_versions(db):
                await db.execute("ROLLBACK")
                continue
            await migration.apply(_MigrationConnection(db))
            if not db.in_transaction:
                raise MigrationError("it ended its own transaction (commit or executescript)")
            await db.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, datetime.now(UTC).isoformat()),
            )
            await db.execute("COMMIT")
        except BaseException as exc:
            if db.in_transaction:
                await db.execute("ROLLBACK")
            if not isinstance(exc, Exception):
                raise  # cancellation / interpreter exit: still rolled back
            logger.error(
                "Database migration %04d %r failed and was rolled back; startup stopped: %s",
                migration.version,
                migration.name,
                exc,
            )
            raise MigrationError(f"migration {migration.version:04d} {migration.name!r} failed: {exc}") from exc
        logger.info("Applied database migration %04d %r", migration.version, migration.name)
        applied.append(migration.version)
    return applied


# One run at a time per process: the lifespan, reset_pin and tests all call
# init_db(). asyncio.Lock belongs to one event loop, so keep one per loop.
_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def run_lock() -> asyncio.Lock:
    global _lock
    loop = asyncio.get_running_loop()
    if _lock is None or _lock[0] is not loop:
        _lock = (loop, asyncio.Lock())
    return _lock[1]


async def run_startup_checks(db) -> None:
    await db.execute("BEGIN IMMEDIATE")
    try:
        await startup_checks(db)
        await db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            await db.execute("ROLLBACK")
        raise
