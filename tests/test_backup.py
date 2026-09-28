import asyncio
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
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
    monkeypatch.setattr(backup, "_warned_future", set())


def _rows(path: Path, sql: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _folder_names() -> set[str]:
    return {p.name for p in backup.backup_dir().iterdir()}


def _touch_backup(when: datetime, name: str | None = None, mtime: datetime | None = None) -> Path:
    """A fake backup taken at `when` (named by its London-local stamp)."""
    folder = backup.backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or f"huddle-{when.astimezone(LONDON):%Y%m%d-%H%M%S}.db")
    path.write_bytes(b"x")
    ts = (mtime or when).timestamp()
    os.utime(path, (ts, ts))
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

    assert backup.list_backups(LONDON) == []
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

    assert backup.list_backups(LONDON) == []
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
    now = datetime.now(LONDON).replace(hour=3, minute=30, second=0, microsecond=0)
    for days_ago in range(120):
        _touch_backup(now - timedelta(days=days_ago))
    (backup.backup_dir() / ".secret_key").write_bytes(b"k")  # never pruned

    backup.prune(LONDON)

    kept = backup.list_backups(LONDON)
    kept_dates = [backup.backup_time(p, LONDON).astimezone(LONDON).date() for p in kept]
    kept_days = [(now.date() - d).days for d in kept_dates]
    assert kept_days[:14] == list(range(14))
    this_monday = now.date() - timedelta(days=now.weekday())
    oldest_week = this_monday - timedelta(weeks=7)
    weekly = [d for d, n in zip(kept_dates, kept_days, strict=True) if n >= 14]
    assert weekly  # older weeks are represented...
    for day in weekly:
        assert day >= oldest_week  # ...only within the last 8 weeks...
        assert day.weekday() == 6  # ...by that week's newest (Sunday)
    assert len(kept) <= 14 + 8
    assert (backup.backup_dir() / ".secret_key").exists()


def test_retention_ignores_other_files():
    now = datetime.now(timezone.utc)
    _touch_backup(now - timedelta(days=400), name="notes.txt")
    for i in range(20):
        _touch_backup(now - timedelta(days=100 + i))

    backup.prune(LONDON, keep_daily=2, keep_weekly=1)

    assert (backup.backup_dir() / "notes.txt").exists()
    assert len(backup.list_backups(LONDON)) == 2


def test_order_comes_from_the_name_not_the_mtime():
    """Old backups copied back in from a NAS get a fresh mtime; they must
    not become "newest" and push out the real recent ones."""
    now = datetime.now(timezone.utc)
    recent = [_touch_backup(now - timedelta(days=d), mtime=now - timedelta(days=d)) for d in range(3)]
    old = [_touch_backup(now - timedelta(days=200 + d), mtime=now) for d in range(3)]  # cp'd back today

    assert backup.list_backups(LONDON)[:3] == recent
    backup.prune(LONDON, keep_daily=3, keep_weekly=1)
    assert all(p.exists() for p in recent)
    assert not any(p.exists() for p in old)


def test_same_second_suffixes_sort_after_the_first():
    when = datetime(2026, 9, 28, 12, 0, tzinfo=LONDON)
    first = _touch_backup(when)
    second = _touch_backup(when, name=f"{first.stem}-2.db", mtime=when - timedelta(days=5))
    unparsable = _touch_backup(when - timedelta(days=1), name="huddle-manual.db")  # falls back to mtime

    assert backup.list_backups(LONDON) == [second, first, unparsable]


async def test_future_stamped_backup_does_not_suppress_backups(caplog):
    _touch_backup(datetime.now(timezone.utc) + timedelta(days=2))

    with caplog.at_level(logging.WARNING, logger="app.backup"):
        assert await backup.run_backup_if_due() is not None  # due despite the "newer" one
        assert await backup.run_backup_if_due() is None  # the real one now counts

    assert caplog.text.count("stamped in the future") == 1  # logged once


def test_backup_due_rules():
    before = datetime(2026, 9, 28, 2, 0, tzinfo=LONDON)
    assert backup.backup_due(None, before, LONDON)
    assert not backup.backup_due(before - timedelta(hours=20), before, LONDON)  # after last night's run
    assert backup.backup_due(before - timedelta(hours=25), before, LONDON)  # the box was off at 03:30
    after = datetime(2026, 9, 28, 3, 35, tzinfo=LONDON)
    assert backup.backup_due(after - timedelta(hours=10), after, LONDON)  # tonight's run
    assert not backup.backup_due(after - timedelta(minutes=4), after, LONDON)  # already done tonight


# (zone, transition date) for 2026: spring forward and fall back.
DST_DAYS = [
    ("Europe/London", date(2026, 3, 29)),
    ("Europe/London", date(2026, 10, 25)),
    ("America/New_York", date(2026, 3, 8)),
    ("America/New_York", date(2026, 11, 1)),
    ("Europe/Helsinki", date(2026, 3, 29)),  # clocks jump 03:00 -> 04:00: no 03:30 at all
    ("Europe/Helsinki", date(2026, 10, 25)),  # 04:00 -> 03:00: 03:30 happens twice
]


@pytest.mark.parametrize(("zone", "day"), DST_DAYS)
def test_exactly_one_backup_per_night_across_dst(zone, day):
    """Walk the 10-minute scheduler over the nights around a DST change,
    stamping each backup the way create_backup does and reading it back."""
    tz = ZoneInfo(zone)
    start = datetime.combine(day - timedelta(days=1), dtime(12, 0), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=2), dtime(12, 0), tzinfo=tz).astimezone(timezone.utc)
    newest = backup.last_nightly(start, tz)  # last night's backup already exists
    taken = []
    now = start
    while now < end:
        if backup.backup_due(newest, now, tz):
            stamp = now.astimezone(tz).strftime(backup.STAMP_FORMAT)
            newest = backup.backup_time(Path(f"huddle-{stamp}.db"), tz)
            assert newest <= now
            taken.append(now.astimezone(tz))
        now += timedelta(minutes=10)

    nights = [d.date() for d in taken]
    assert nights == [day, day + timedelta(days=1), day + timedelta(days=2)]
    for when in taken:
        wall = when.hour * 60 + when.minute
        assert 3 * 60 + 30 <= wall < 4 * 60 + 40, when  # ~03:30 (04:30 when 03:30 doesn't exist)


async def test_startup_catch_up_only_when_stale(monkeypatch):
    runs = []

    async def fake_create():
        runs.append(1)
        return Path("x")

    monkeypatch.setattr(backup, "create_backup", fake_create)
    now = datetime.now(timezone.utc)

    fresh = _touch_backup(backup.last_nightly(now, LONDON) + timedelta(minutes=1))
    assert await backup.run_backup_if_due() is None
    assert runs == []

    fresh.unlink()  # the newest is now from before the last 03:30 (box was off)
    _touch_backup(backup.last_nightly(now, LONDON) - timedelta(hours=1))
    assert await backup.run_backup_if_due() is not None
    assert runs == [1]


async def test_setup_failure_is_logged_and_backs_off(monkeypatch, caplog):
    """E.g. a root-owned leftover .tmp that can't be removed: no traceback
    every 10 minutes, just a warning and the usual retry backoff."""
    calls = []

    def unwritable(folder, stamp):
        calls.append(stamp)
        raise PermissionError("permission denied")

    monkeypatch.setattr(backup, "_unique_name", unwritable)

    with caplog.at_level(logging.WARNING, logger="app.backup"):
        assert await backup.run_backup_if_due() is None
        assert await backup.run_backup_if_due() is None

    assert len(calls) == 1
    assert "permission denied" in caplog.text
    assert backup._last_failure is not None


async def test_overlapping_backups_are_serialised(monkeypatch):
    real_snapshot = backup._snapshot
    active = 0
    peak = 0
    guard = threading.Lock()

    def slow_snapshot(src, dest):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.2)
        real_snapshot(src, dest)
        with guard:
            active -= 1

    monkeypatch.setattr(backup, "_snapshot", slow_snapshot)

    paths = await asyncio.gather(backup.create_backup(), backup.create_backup(), backup.create_backup())

    assert peak == 1
    assert all(p is not None for p in paths)
    assert len(set(paths)) == 3  # same-second runs get -2/-3 names, nothing overwritten
    assert len(backup.list_backups(LONDON)) == 3


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
    assert len(backup.list_backups(LONDON)) == 1

    page = await client.get("/admin")
    assert 'id="backups"' in page.text
    assert "1 kept" in page.text


async def test_back_up_now_failure_shows_an_error(client, monkeypatch):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())

    def unwritable(folder, stamp):
        raise PermissionError("permission denied")

    monkeypatch.setattr(backup, "_unique_name", unwritable)  # not a 500
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
