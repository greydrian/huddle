"""
Database layer for the family display.

SQLite in WAL mode, per the spec's embedded-reliability decisions:
- journal_mode=WAL and synchronous=NORMAL for crash resistance
- a sync_queue table backs the offline-first mutation queue for Google Tasks/Calendar
- the schema is built and upgraded by the numbered migrations in app/migrations.py
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

LAYOUT_VERSION = "2"  # frozen in migration 0001; a future layout reset is a new migration, not a bump

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


async def _add_missing_widgets(db, layout=None):
    """Give every widget in `layout` (default: the live DEFAULT_LAYOUT) that
    has no layout_state row one below everything else."""
    rows = await (await db.execute("SELECT widget_id, grid_y + grid_h AS bottom FROM layout_state")).fetchall()
    present = {row["widget_id"] for row in rows}
    bottom = max((row["bottom"] for row in rows), default=0)
    for widget_id, x, _y, w, h in DEFAULT_LAYOUT if layout is None else layout:
        if widget_id not in present:
            await db.execute(
                """INSERT OR IGNORE INTO layout_state (widget_id, grid_x, grid_y, grid_w, grid_h, is_visible)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (widget_id, x, bottom, w, h),
            )
            bottom += h


async def init_db():
    """Open the database, set its pragmas, bring its schema up to date with
    the numbered migrations (app/migrations.py), then run the every-boot data
    checks. Safe to call repeatedly: the lifespan, reset_pin and tests do. A
    failed migration raises migrations.MigrationError and nothing of it is
    kept."""
    from app import migrations

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with migrations.run_lock():
        # isolation_level=None: the migration runner issues BEGIN/COMMIT itself.
        async with aiosqlite.connect(DB_PATH, isolation_level=None) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA foreign_keys=ON;")
            await migrations.run_migrations(db)
            await migrations.run_startup_checks(db)
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
