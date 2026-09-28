"""
Database layer for the family display.

SQLite in WAL mode, per the spec's embedded-reliability decisions:
- journal_mode=WAL and synchronous=NORMAL for crash resistance
- a sync_queue table backs the offline-first mutation queue for Google Tasks/Calendar
- schema mirrors the "Draft Database Schema" section of the spec
"""

import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

# DATA_DIR defaults to a local ./data folder for bare-metal/dev use, but is
# overridden to /data in the Docker image, pointed at a mounted volume so
# the database survives container recreation (see docker-compose.yml).
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent / "data"))
DB_PATH = DATA_DIR / "family_display.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    colour_hex TEXT NOT NULL,
    avatar_path TEXT,  -- reserved/unused: nothing reads it (kept for the live DB)
    sort_order INTEGER NOT NULL DEFAULT 0,
    google_tasklist_id TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
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
);

CREATE TABLE IF NOT EXISTS meal_plans (
    date TEXT PRIMARY KEY,
    meal_description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shopping_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    is_checked INTEGER NOT NULL DEFAULT 0,
    google_task_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS layout_state (
    widget_id TEXT PRIMARY KEY,
    grid_x INTEGER NOT NULL,
    grid_y INTEGER NOT NULL,
    grid_w INTEGER NOT NULL,
    grid_h INTEGER NOT NULL,
    is_visible INTEGER NOT NULL DEFAULT 1,
    config_json TEXT  -- reserved/unused: nothing reads it (kept for the live DB)
);

CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    service_name TEXT PRIMARY KEY,
    account_email TEXT,
    encrypted_token_json TEXT,
    sync_token TEXT  -- reserved/unused: nothing reads it (kept for the live DB)
);

-- Homework and handwriting practice words (Admin-entered for now; `source`
-- leaves room for school-email / Classroom imports). Never synced to Google.
CREATE TABLE IF NOT EXISTS homework (
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
);

CREATE TABLE IF NOT EXISTS practice_word_lists (
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
);

CREATE TABLE IF NOT EXISTS practice_log (
    list_id INTEGER NOT NULL REFERENCES practice_word_lists(id) ON DELETE CASCADE,
    practised_on TEXT NOT NULL,
    PRIMARY KEY (list_id, practised_on)
);

-- Generic key/value store: admin PIN hash, lockout state, brightness schedule, etc.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

LAYOUT_VERSION = "2"  # bump + branch init_db's migration when DEFAULT_LAYOUT changes shape

DEFAULT_LAYOUT = [
    # widget_id, x, y, w, h
    ("calendar", 0, 0, 12, 7),
    ("tasks", 0, 7, 4, 4),
    ("shopping", 4, 7, 2, 4),
    ("meals", 6, 7, 2, 4),
    ("weather", 8, 7, 2, 2),
    ("photos", 8, 9, 2, 2),
    ("homework", 10, 7, 2, 4),
    ("practice_words", 0, 11, 6, 4),
]

DEFAULT_PROFILES = [
    # name, colour_hex, sort_order
    ("Mum", "#C1584A", 0),
    ("Dad", "#3D6E93", 1),
    ("Riley", "#D6A02C", 2),
    ("Jamie", "#4C8577", 3),
]


async def _add_column_if_missing(db, table: str, column: str, coltype: str):
    """CREATE TABLE IF NOT EXISTS never retroactively alters an existing
    table, so columns added after a table already shipped need an explicit,
    idempotent ALTER TABLE — PRAGMA table_info first since SQLite errors on
    adding a column that's already there."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    existing = [row["name"] for row in await cursor.fetchall()]
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


async def _add_missing_widgets(db):
    rows = await (await db.execute("SELECT widget_id, grid_y + grid_h AS bottom FROM layout_state")).fetchall()
    present = {row["widget_id"] for row in rows}
    bottom = max((row["bottom"] for row in rows), default=0)
    for widget_id, x, _y, w, h in DEFAULT_LAYOUT:
        if widget_id not in present:
            await db.execute(
                """INSERT OR IGNORE INTO layout_state (widget_id, grid_x, grid_y, grid_w, grid_h, is_visible)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (widget_id, x, bottom, w, h),
            )
            bottom += h


async def init_db():
    """Create tables (if needed) and seed default data on first run."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.executescript(SCHEMA)
        await db.commit()

        # Migration: Google Tasks sync (shopping list + per-person task
        # lists) added columns to tables that already shipped without them.
        await _add_column_if_missing(db, "profiles", "google_tasklist_id", "TEXT")
        await _add_column_if_missing(db, "shopping_items", "updated_at", "TEXT")
        await _add_column_if_missing(db, "tasks", "updated_at", "TEXT")
        await db.execute("UPDATE shopping_items SET updated_at = created_at WHERE updated_at IS NULL")
        await db.execute("UPDATE tasks SET updated_at = created_at WHERE updated_at IS NULL")
        await db.commit()

        # Seed profiles only if the table is empty (first run)
        cursor = await db.execute("SELECT COUNT(*) FROM profiles")
        (count,) = await cursor.fetchone()
        if count == 0:
            await db.executemany(
                "INSERT INTO profiles (name, colour_hex, sort_order) VALUES (?, ?, ?)",
                DEFAULT_PROFILES,
            )

        # Seed default widget layout only if empty
        cursor = await db.execute("SELECT COUNT(*) FROM layout_state")
        (count,) = await cursor.fetchone()
        if count == 0:
            await db.executemany(
                """INSERT INTO layout_state (widget_id, grid_x, grid_y, grid_w, grid_h, is_visible)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                DEFAULT_LAYOUT,
            )

        # Migration: Calendar became the dashboard's hero widget (Google-style
        # month grid with event bars + a day view) and needs far more room,
        # so every widget's default position changed shape. One-time, keyed
        # on a version flag rather than re-running every boot: wipes the
        # layout and lets the "seed if empty" block above re-insert the new
        # DEFAULT_LAYOUT. This intentionally resets any custom drag/resize
        # positions — unavoidable given how much bigger Calendar needs to be.
        current_layout_version = await get_setting(db, "layout_version")
        if current_layout_version != LAYOUT_VERSION:
            await db.execute("DELETE FROM layout_state")
            await db.executemany(
                """INSERT INTO layout_state (widget_id, grid_x, grid_y, grid_w, grid_h, is_visible)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                DEFAULT_LAYOUT,
            )
            await set_setting(db, "layout_version", LAYOUT_VERSION)

        # Migration: the Calendar widget absorbed Upcoming Events (now a
        # month grid instead of two agenda lists) — widen calendar's slot by
        # upcoming_events' old width and drop it, preserving wherever the
        # widget's actually been dragged to rather than resetting positions.
        # No-op once this has run (upcoming_events row no longer exists).
        cursor = await db.execute(
            "SELECT grid_w FROM layout_state WHERE widget_id = 'upcoming_events'"
        )
        row = await cursor.fetchone()
        if row is not None:
            await db.execute(
                "UPDATE layout_state SET grid_w = grid_w + ? WHERE widget_id = 'calendar'",
                (row[0],),
            )
            await db.execute("DELETE FROM layout_state WHERE widget_id = 'upcoming_events'")

        # Migration: widgets added after a layout shipped get a row of their
        # own below everything else, leaving every existing position alone
        # (bumping LAYOUT_VERSION would wipe the family's arrangement).
        await _add_missing_widgets(db)

        # Seed a default admin PIN (1234) on first run only — change this in Admin > Settings
        cursor = await db.execute("SELECT COUNT(*) FROM app_settings WHERE key = 'pin_hash'")
        (count,) = await cursor.fetchone()
        if count == 0:
            from app.security import hash_pin

            await db.execute(
                "INSERT INTO app_settings (key, value) VALUES ('pin_hash', ?)",
                (hash_pin("1234"),),
            )
            # Whenever 1234 is seeded (first run, or the pin_hash row was
            # deleted), Admin must force a change away from it again.
            await set_setting(db, "pin_is_default", "1")

        # Migration: PIN hardening. Admin forces a change away from the
        # default PIN, tracked by a flag rather than a PBKDF2 check on every
        # request. Set once, for fresh installs and existing DBs alike, by
        # checking whether the stored hash is still "1234"; change-pin clears it.
        if await get_setting(db, "pin_is_default") is None:
            from app.security import DEFAULT_PIN, verify_pin

            stored = await get_setting(db, "pin_hash") or ""
            await set_setting(db, "pin_is_default", "1" if verify_pin(DEFAULT_PIN, stored) else "0")

        await db.commit()

        # Migration: sync health (app/sync_status.py). One row, written by
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
        await db.execute("INSERT OR IGNORE INTO sync_status (id) VALUES (1)")
        await db.commit()

        await load_onscreen_keyboard(db)


# Cached in-process so templates can read it without a DB round-trip on
# every render (single uvicorn process, see scheduler.py). Loaded by
# init_db() at startup; changed only through set_onscreen_keyboard().
ONSCREEN_KEYBOARD_SETTING = "onscreen_keyboard"
onscreen_keyboard_enabled = False


async def load_onscreen_keyboard(db):
    global onscreen_keyboard_enabled
    onscreen_keyboard_enabled = await get_setting(db, ONSCREEN_KEYBOARD_SETTING) == "1"


async def set_onscreen_keyboard(db, enabled: bool):
    global onscreen_keyboard_enabled
    await set_setting(db, ONSCREEN_KEYBOARD_SETTING, "1" if enabled else "0")
    await db.commit()
    onscreen_keyboard_enabled = enabled


CALENDAR_TIMEZONE_SETTING = "calendar_timezone"


async def family_timezone(db) -> ZoneInfo:
    """The household's timezone — the connected Google Calendar's own
    (cached by google_calendar), else UTC. The container itself always runs in
    UTC, so naive date.today()/datetime.now() are wrong for "today" here."""
    name = await get_setting(db, CALENDAR_TIMEZONE_SETTING)
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # OSError: a tzdata directory name such as "Europe" raises
        # IsADirectoryError/PermissionError rather than NotFound.
        return ZoneInfo("UTC")


async def family_today(db) -> date:
    return datetime.now(await family_timezone(db)).date()


async def get_setting(db, key: str, default=None):
    cursor = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else default


async def set_setting(db, key: str, value: str):
    await db.execute(
        """INSERT INTO app_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )


@asynccontextmanager
async def get_db():
    """Async context manager yielding a connection with row access by column name."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    # SQLite's foreign_keys pragma is per-connection, not per-database —
    # without this, ON DELETE CASCADE (profiles -> tasks) silently does nothing.
    await db.execute("PRAGMA foreign_keys=ON;")
    # Also per-connection: WAL + NORMAL is the durability the spec intends
    # (init_db sets it too, but only on its own connection).
    await db.execute("PRAGMA synchronous=NORMAL;")
    try:
        yield db
    finally:
        await db.close()
