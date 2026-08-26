"""
Database layer for the family display.

SQLite in WAL mode, per the spec's embedded-reliability decisions:
- journal_mode=WAL and synchronous=NORMAL for crash resistance
- a sync_queue table backs the offline-first mutation queue for Google Tasks/Calendar
- schema mirrors the "Draft Database Schema" section of the spec
"""

import os
import aiosqlite
from pathlib import Path
from contextlib import asynccontextmanager

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
    avatar_path TEXT,
    points_balance INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    google_task_id TEXT,
    title TEXT NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    is_recurring INTEGER NOT NULL DEFAULT 0,
    recurrence_rule TEXT,
    is_completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    points_cost INTEGER NOT NULL,
    icon TEXT
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
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS layout_state (
    widget_id TEXT PRIMARY KEY,
    grid_x INTEGER NOT NULL,
    grid_y INTEGER NOT NULL,
    grid_w INTEGER NOT NULL,
    grid_h INTEGER NOT NULL,
    is_visible INTEGER NOT NULL DEFAULT 1,
    config_json TEXT
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
    sync_token TEXT
);

-- Generic key/value store: admin PIN hash, lockout state, brightness schedule, etc.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_LAYOUT = [
    # widget_id, x, y, w, h
    ("calendar", 0, 0, 5, 4),
    ("upcoming_events", 5, 0, 3, 4),
    ("tasks", 0, 4, 4, 4),
    ("shopping", 4, 4, 2, 4),
    ("meals", 6, 4, 2, 4),
    ("weather", 8, 0, 2, 2),
    ("photos", 8, 2, 2, 2),
    ("homework", 8, 4, 2, 2),
]

DEFAULT_PROFILES = [
    # name, colour_hex, sort_order
    ("Mum", "#C1584A", 0),
    ("Dad", "#3D6E93", 1),
    ("Riley", "#D6A02C", 2),
    ("Jamie", "#4C8577", 3),
]


async def init_db():
    """Create tables (if needed) and seed default data on first run."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.executescript(SCHEMA)
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

        # Seed a default admin PIN (1234) on first run only — change this in Admin > Settings
        cursor = await db.execute("SELECT COUNT(*) FROM app_settings WHERE key = 'pin_hash'")
        (count,) = await cursor.fetchone()
        if count == 0:
            from app.security import hash_pin

            await db.execute(
                "INSERT INTO app_settings (key, value) VALUES ('pin_hash', ?)",
                (hash_pin("1234"),),
            )

        await db.commit()


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
    try:
        yield db
    finally:
        await db.close()
