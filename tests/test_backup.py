import asyncio
import logging
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import backup, database, security
from app.routers import admin
from app.security import create_session_token

LONDON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def reset_backup_state(monkeypatch):
    monkeypatch.setattr(backup, "_last_failure", None)


def _rows(path: Path, sql: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _folder_names() -> set[str]:
    return {p.name for p in backup.backup_dir().iterdir()}


def _touch_backup(name: str, when: datetime) -> Path:
    folder = backup.backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x")
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


async def test_backup_is_a_consistent_copy_with_the_rows(db):
    await db.execute("INSERT INTO shopping_items (title) VALUES ('milk')")
    await db.commit()
    security._get_secret_key()  # make sure a key exists

    path = await backup.create_backup()

    assert path is not None and path.parent == backup.backup_dir()
    assert path.name.startswith("huddle-") and path.suffix == ".db"
    assert _rows(path, "PRAGMA integrity_check") == [("ok",)]
    assert _rows(path, "SELECT title FROM shopping_items") == [("milk",)]
    # Self-contained: rollback-journal mode, no -wal/-shm and no temp files left.
    assert _rows(path, "PRAGMA journal_mode") == [("delete",)]
    assert _folder_names() == {path.name, ".secret_key"}


async def test_backup_during_writes_is_consistent():
    """Writers commit pairs of rows in one transaction; any consistent
    snapshot therefore holds an even number of them."""
    stop = threading.Event()

    def writer():
        conn = sqlite3.connect(database.DB_PATH, timeout=30)
        n = 0
        while not stop.is_set():
            with conn:
                conn.execute("INSERT INTO shopping_items (title) VALUES (?)", (f"a{n}",))
                conn.execute("INSERT INTO shopping_items (title) VALUES (?)", (f"b{n}",))
            n += 1
        conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        await asyncio.sleep(0.05)
        paths = [await backup.create_backup() for _ in range(3)]
    finally:
        stop.set()
        thread.join()

    for path in paths:
        assert path is not None
        assert _rows(path, "PRAGMA integrity_check") == [("ok",)]
        (count,) = _rows(path, "SELECT COUNT(*) FROM shopping_items")[0]
        assert count % 2 == 0


async def test_failed_snapshot_leaves_no_partial_file(monkeypatch, caplog):
    def half_written(src, dest):
        Path(dest).write_bytes(b"SQLite format 3\x00 truncated")
        raise OSError("disk full")

    monkeypatch.setattr(backup, "_snapshot", half_written)

    with caplog.at_level(logging.WARNING, logger="app.backup"):
        assert await backup.create_backup() is None

    assert backup.list_backups() == []
    assert _folder_names() == set()
    assert "disk full" in caplog.text


async def test_leftover_temp_file_from_a_crash_is_cleaned_up():
    folder = backup.backup_dir()
    folder.mkdir(parents=True)
    (folder / ".huddle-20260101-033000.db.tmp").write_bytes(b"partial")

    path = await backup.create_backup()

    assert path is not None
    assert not any(name.endswith(".tmp") for name in _folder_names())


async def test_corrupt_copy_is_deleted_with_a_warning(monkeypatch, caplog):
    real_snapshot = backup._snapshot

    def corrupting(src, dest):
        real_snapshot(src, dest)
        data = bytearray(Path(dest).read_bytes())
        page = 4096
        data[page:] = b"\xff" * (len(data) - page)  # trash every page after the header page
        Path(dest).write_bytes(bytes(data))

    monkeypatch.setattr(backup, "_snapshot", corrupting)

    with caplog.at_level(logging.WARNING, logger="app.backup"):
        assert await backup.create_backup() is None

    assert backup.list_backups() == []
    assert _folder_names() == set()
    assert "integrity check" in caplog.text


async def test_failure_backs_off_before_retrying(monkeypatch):
    calls = []

    def failing(src, dest):
        calls.append(dest)
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(backup, "_snapshot", failing)
    assert await backup.run_backup_if_due() is None
    assert await backup.run_backup_if_due() is None
    assert len(calls) == 1  # the second check didn't hammer a failing backup


def test_retention_keeps_14_newest_plus_one_per_week_for_8_weeks():
    now = datetime.now(LONDON).replace(hour=12, minute=0, second=0, microsecond=0)
    for days_ago in range(120):
        when = now - timedelta(days=days_ago)
        _touch_backup(f"huddle-{when:%Y%m%d}-033000.db", when)
    (backup.backup_dir() / ".secret_key").write_bytes(b"k")  # never pruned

    backup.prune(LONDON)

    kept = backup.list_backups()
    kept_days = [(now.date() - datetime.fromtimestamp(p.stat().st_mtime, LONDON).date()).days for p in kept]
    assert kept_days[:14] == list(range(14))
    this_monday = now.date() - timedelta(days=now.weekday())
    oldest_week = this_monday - timedelta(weeks=7)
    weekly = [p for p, d in zip(kept, kept_days, strict=True) if d >= 14]
    assert weekly  # older weeks are represented...
    for path in weekly:
        day = datetime.fromtimestamp(path.stat().st_mtime, LONDON).date()
        assert day >= oldest_week  # ...only within the last 8 weeks...
        assert day.weekday() == 6 or day == now.date()  # ...by that week's newest (Sunday)
    assert len(kept) <= 14 + 8
    assert (backup.backup_dir() / ".secret_key").exists()


def test_retention_ignores_other_files():
    _touch_backup("notes.txt", datetime.now(timezone.utc) - timedelta(days=400))
    for i in range(20):
        _touch_backup(f"huddle-2026010{i:02d}.db", datetime.now(timezone.utc) - timedelta(days=100 + i))

    backup.prune(LONDON, keep_daily=2, keep_weekly=1)

    assert (backup.backup_dir() / "notes.txt").exists()
    assert len(backup.list_backups()) == 2


async def test_secret_key_is_copied_and_follows_changes():
    key = security._get_secret_key()

    await backup.create_backup()

    copy = backup.backup_dir() / ".secret_key"
    assert copy.read_bytes() == key
    if os.name == "posix":
        assert copy.stat().st_mode & 0o777 == 0o600
        assert backup.backup_dir().stat().st_mode & 0o777 == 0o700

    security.SECRET_KEY_PATH.write_bytes(b"n" * 32)  # the key was regenerated
    await backup.create_backup()

    assert copy.read_bytes() == b"n" * 32
    replaced = list(backup.backup_dir().glob(".secret_key.replaced-*"))
    assert [p.read_bytes() for p in replaced] == [key]  # older backups still need it


def test_backup_due_rules():
    now = datetime(2026, 9, 28, 2, 0, tzinfo=LONDON)
    assert backup.backup_due(None, now)
    assert not backup.backup_due(now - timedelta(hours=20), now)  # before 03:30, fresh
    assert backup.backup_due(now - timedelta(hours=25), now)  # stale: box was off at 03:30
    after = datetime(2026, 9, 28, 3, 35, tzinfo=LONDON)
    assert backup.backup_due(after - timedelta(hours=10), after)  # tonight's run
    assert not backup.backup_due(after - timedelta(minutes=4), after)  # already done tonight


async def test_startup_catch_up_only_when_stale(monkeypatch):
    runs = []

    async def fake_create():
        runs.append(1)
        return Path("x")

    monkeypatch.setattr(backup, "create_backup", fake_create)
    # Pin "now" to midday so only the staleness rule (not tonight's 03:30) applies.
    noon = datetime.now(LONDON).replace(hour=12, minute=0, second=0, microsecond=0)

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return noon.astimezone(tz)

    monkeypatch.setattr(backup, "datetime", FixedNow)

    fresh = _touch_backup("huddle-fresh.db", noon - timedelta(hours=2))
    assert await backup.run_backup_if_due() is None
    assert runs == []

    os.utime(fresh, ((noon - timedelta(hours=30)).timestamp(),) * 2)
    assert await backup.run_backup_if_due() is not None
    assert runs == [1]


async def test_backups_route_requires_admin(client):
    resp = await client.post("/admin/backups/run")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert not backup.backup_dir().exists()


async def test_back_up_now_and_admin_panel(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())

    page = await client.get("/admin")
    assert "No backups yet." in page.text

    resp = await client.post("/admin/backups/run")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin#backups"
    assert len(backup.list_backups()) == 1

    page = await client.get("/admin")
    assert 'id="backups"' in page.text
    assert "1 kept" in page.text


async def test_back_up_now_failure_shows_an_error(client, monkeypatch):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())

    async def failed():
        return None

    monkeypatch.setattr(backup, "create_backup", failed)
    resp = await client.post("/admin/backups/run")

    assert resp.headers["location"] == "/admin?error=backup-failed#backups"
    page = await client.get(resp.headers["location"])
    assert "The backup didn" in page.text


async def test_restore_round_trip():
    async with database.get_db() as db:
        await db.execute("INSERT INTO shopping_items (title) VALUES ('before')")
        await db.commit()
    path = await backup.create_backup()

    async with database.get_db() as db:
        await db.execute("DELETE FROM shopping_items")
        await db.execute("INSERT INTO shopping_items (title) VALUES ('after')")
        await db.commit()

    # README restore: (container stopped) copy the backup over the DB and
    # remove any -wal/-shm files.
    for suffix in ("-wal", "-shm"):
        Path(f"{database.DB_PATH}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(path, database.DB_PATH)

    await database.init_db()  # what the next startup does
    async with database.get_db() as conn:
        rows = await (await conn.execute("SELECT title FROM shopping_items")).fetchall()
    assert [r["title"] for r in rows] == ["before"]


async def test_run_backup_if_due_real():
    """End to end with nothing stubbed: no backups yet, so it's due."""
    path = await backup.run_backup_if_due()
    assert path is not None and path.exists()
    assert await backup.run_backup_if_due() is None  # fresh now, so not due again
