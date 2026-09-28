"""
Nightly SQLite backups into <DATA_DIR>/backups/ (restore steps: README).

Why VACUUM INTO rather than the sqlite3 online-backup API: both give a
consistent snapshot of a live WAL database (WAL content included) without
blocking writers. VACUUM INTO does it as one read transaction in a single
statement, and writes a compact, self-contained file in rollback-journal mode
(no -wal/-shm companions to forget when copying it). The backup API copies
page by page, and in one step it holds the same read lock anyway. Copying the
.db file directly (cp/rsync) can miss or tear committed data still in the WAL.

The snapshot is written to a hidden ".tmp" name, fsynced, checked with
PRAGMA integrity_check and only then renamed into place, so a file matching
huddle-*.db is always complete and verified.

The Google tokens in the DB are encrypted with data/.secret_key, so a copy of
that key is kept alongside (owner-only permissions). That makes the backups
folder exactly as sensitive as data/ itself.
"""

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database, security

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "huddle-"
BACKUP_SUFFIX = ".db"
TMP_SUFFIX = ".tmp"
KEEP_DAILY = 14  # the newest 14 backups...
KEEP_WEEKLY = 8  # ...plus the newest backup of each of the last 8 weeks
NIGHTLY_AT = (3, 30)  # family-local time
STALE_AFTER = timedelta(hours=24)
RETRY_AFTER_FAILURE_SECONDS = 3600  # don't retry a failing backup every check

_lock = asyncio.Lock()
_last_failure: float | None = None  # time.monotonic() of the last failed attempt


def backup_dir() -> Path:
    # Derived from DB_PATH at call time (DATA_DIR/backups in production).
    return database.DB_PATH.parent / "backups"


def list_backups() -> list[Path]:
    """Completed backups, newest first (by modification time)."""
    folder = backup_dir()
    if not folder.is_dir():
        return []
    files = [p for p in folder.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def newest_backup_time() -> datetime | None:
    backups = list_backups()
    if not backups:
        return None
    return datetime.fromtimestamp(backups[0].stat().st_mtime, timezone.utc)


def _snapshot(src: Path, dest: Path) -> None:
    """Consistent copy of the live DB at `dest`, flushed to disk."""
    conn = sqlite3.connect(src, timeout=30)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    # VACUUM INTO doesn't fsync its output; do it before the rename.
    with open(dest, "rb+") as f:
        os.fsync(f.fileno())


def _integrity_ok(path: Path) -> bool:
    try:
        # Read-only URI: checking must not create -wal/-shm files next to it.
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return rows == [("ok",)]


def _restrict(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:  # e.g. filesystems without POSIX permissions
        pass


def _copy_secret_key(folder: Path, stamp: str) -> None:
    """Keep a copy of the token-encryption key next to the backups. If it has
    changed, the old copy is kept too (older backups still need it)."""
    key_path = security.SECRET_KEY_PATH
    if not key_path.exists():
        return  # nothing encrypted with it yet
    key = key_path.read_bytes()
    dest = folder / ".secret_key"
    if dest.exists():
        if dest.read_bytes() == key:
            return
        dest.replace(folder / f".secret_key.replaced-{stamp}")
    tmp = folder / f".secret_key{TMP_SUFFIX}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
        f.flush()
        os.fsync(f.fileno())
    _restrict(tmp, 0o600)
    tmp.replace(dest)


def _unique_name(folder: Path, stamp: str) -> Path:
    path = folder / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    n = 2
    while path.exists():
        path = folder / f"{BACKUP_PREFIX}{stamp}-{n}{BACKUP_SUFFIX}"
        n += 1
    return path


def _create_backup_sync(stamp: str) -> Path | None:
    folder = backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _restrict(folder, 0o700)
    for leftover in folder.glob(f"*{TMP_SUFFIX}"):  # from a crash mid-backup
        leftover.unlink(missing_ok=True)

    final = _unique_name(folder, stamp)
    tmp = folder / f".{final.name}{TMP_SUFFIX}"
    try:
        _snapshot(database.DB_PATH, tmp)
        if not _integrity_ok(tmp):
            logger.warning("Backup %s failed its integrity check and was deleted", final.name)
            tmp.unlink(missing_ok=True)
            return None
        _restrict(tmp, 0o600)
        tmp.replace(final)  # atomic: the complete name only ever holds a verified file
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Backup failed: %s", exc)
        tmp.unlink(missing_ok=True)
        return None
    try:
        _copy_secret_key(folder, stamp)
    except OSError as exc:
        logger.warning("Backup %s written, but copying the secret key failed: %s", final.name, exc)
    return final


def prune(tz, keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY) -> list[Path]:
    """Delete backups outside the retention policy; returns what was deleted."""
    backups = list_backups()
    keep = set(backups[:keep_daily])
    today = datetime.now(tz).date()
    this_monday = today - timedelta(days=today.weekday())
    oldest_week = this_monday - timedelta(weeks=keep_weekly - 1)
    seen_weeks = set()
    for path in backups:  # newest first, so the first one per week is its newest
        day = datetime.fromtimestamp(path.stat().st_mtime, tz).date()
        monday = day - timedelta(days=day.weekday())
        if monday >= oldest_week and monday not in seen_weeks:
            seen_weeks.add(monday)
            keep.add(path)
    deleted = []
    for path in backups:
        if path not in keep:
            path.unlink(missing_ok=True)
            deleted.append(path)
    return deleted


async def create_backup() -> Path | None:
    """Back up now. Returns the new file, or None if it failed (logged)."""
    global _last_failure
    async with _lock:
        async with database.get_db() as db:
            tz = await database.family_timezone(db)
        stamp = datetime.now(tz).strftime("%Y%m%d-%H%M%S")
        path = await asyncio.to_thread(_create_backup_sync, stamp)
        if path is None:
            _last_failure = time.monotonic()
            return None
        _last_failure = None
        try:
            await asyncio.to_thread(prune, tz)
        except OSError as exc:
            logger.warning("Pruning old backups failed: %s", exc)
        logger.info("Backup written: %s", path.name)
        return path


def backup_due(newest: datetime | None, now: datetime) -> bool:
    """`now` is family-local. Due when there's no backup, the newest is over
    24h old (e.g. the box was off at 03:30), or tonight's 03:30 has passed
    since the newest one."""
    if newest is None or now - newest > STALE_AFTER:
        return True
    hour, minute = NIGHTLY_AT
    nightly = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= nightly and newest < nightly


async def run_backup_if_due() -> Path | None:
    """Scheduler entry point: checked every few minutes and once at startup."""
    if _last_failure is not None and time.monotonic() - _last_failure < RETRY_AFTER_FAILURE_SECONDS:
        return None
    async with database.get_db() as db:
        tz = await database.family_timezone(db)
    if not backup_due(newest_backup_time(), datetime.now(tz)):
        return None
    return await create_backup()


def status(tz) -> dict:
    """For the Admin page: newest backup's local time and size, and the count."""
    backups = list_backups()
    if not backups:
        return {"count": 0, "newest_at": None, "newest_size": None}
    stat = backups[0].stat()
    return {
        "count": len(backups),
        "newest_at": datetime.fromtimestamp(stat.st_mtime, tz),
        "newest_size": stat.st_size,
    }

