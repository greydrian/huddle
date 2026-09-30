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
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone, tzinfo
from datetime import time as dtime
from pathlib import Path

from app import database, security
from app.migrations import MigrationError

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "huddle-"
BACKUP_SUFFIX = ".db"
TMP_SUFFIX = ".tmp"
KEEP_DAILY = 14  # the newest 14 backups...
KEEP_WEEKLY = 8  # ...plus the newest backup of each of the last 8 weeks
NIGHTLY_AT = dtime(3, 30)  # family-local time
# The time in a backup's name (family-local) orders backups, not the file's
# mtime: copying old backups back in from a NAS must not make them "newest".
STAMP_FORMAT = "%Y%m%d-%H%M%S"
NAME_PATTERN = re.compile(r"huddle-(\d{8}-\d{6})(?:-(\d+))?\.db")
RETRY_AFTER_FAILURE_SECONDS = 3600  # don't retry a failing backup every check

_lock = asyncio.Lock()
_last_failure: float | None = None  # time.monotonic() of the last failed attempt
_warned_future: set[str] = set()  # future-stamped backups already logged


def backup_dir() -> Path:
    # Derived from DB_PATH at call time (DATA_DIR/backups in production).
    return database.DB_PATH.parent / "backups"


def backup_time(path: Path, tz) -> datetime:
    """When a backup was taken (aware, UTC), from the family-local time in its
    name; the file's mtime only if the name doesn't parse. An ambiguous
    autumn-hour stamp reads as its first occurrence (fold=0)."""
    match = NAME_PATTERN.fullmatch(path.name)
    if match:
        try:
            local = datetime.strptime(match.group(1), STAMP_FORMAT).replace(tzinfo=tz)
            return local.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def _sort_key(path: Path, tz) -> tuple[datetime, int]:
    match = NAME_PATTERN.fullmatch(path.name)
    suffix = int(match.group(2)) if match and match.group(2) else 1
    return backup_time(path, tz), suffix


def list_backups(tz) -> list[Path]:
    """Completed backups, newest first."""
    folder = backup_dir()
    if not folder.is_dir():
        return []
    files = [p for p in folder.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p.is_file()]
    return sorted(files, key=lambda p: _sort_key(p, tz), reverse=True)


def newest_backup_time(tz, now: datetime) -> datetime | None:
    """The newest backup taken no later than `now`. A backup stamped in the
    future (clock was wrong, or the family timezone changed) is logged once
    and ignored, so it can't suppress backups."""
    for path in list_backups(tz):
        taken = backup_time(path, tz)
        if taken <= now:
            return taken
        if path.name not in _warned_future:
            _warned_future.add(path.name)
            logger.warning("Backup %s is stamped in the future; ignoring it for scheduling", path.name)
    return None


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
    tmp = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _restrict(folder, 0o700)
        for leftover in folder.glob(f"*{TMP_SUFFIX}"):  # from a crash mid-backup
            leftover.unlink(missing_ok=True)
        final = _unique_name(folder, stamp)
        tmp = folder / f".{final.name}{TMP_SUFFIX}"
        _snapshot(database.DB_PATH, tmp)
        if not _integrity_ok(tmp):
            logger.warning("Backup %s failed its integrity check and was deleted", final.name)
            tmp.unlink(missing_ok=True)
            return None
        _restrict(tmp, 0o600)
        tmp.replace(final)  # atomic: the complete name only ever holds a verified file
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Backup failed: %s", exc)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return None
    try:
        _copy_secret_key(folder, stamp)
    except OSError as exc:
        logger.warning("Backup %s written, but copying the secret key failed: %s", final.name, exc)
    return final


def prune(tz, keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY) -> list[Path]:
    """Delete backups outside the retention policy; returns what was deleted."""
    backups = list_backups(tz)
    keep = set(backups[:keep_daily])
    today = datetime.now(tz).date()
    this_monday = today - timedelta(days=today.weekday())
    oldest_week = this_monday - timedelta(weeks=keep_weekly - 1)
    seen_weeks = set()
    for path in backups:  # newest first, so the first one per week is its newest
        day = backup_time(path, tz).astimezone(tz).date()
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


class SnapshotError(MigrationError):
    """The snapshot init_db() takes before pending migrations could not be
    taken. A MigrationError so startup stops the documented way (README, "If
    the app won't start after an update"): nothing has been changed."""


async def snapshot_before_migrations(db, pending: list[int]) -> Path:
    """A verified backup of the database as it is *before* `pending` runs.

    init_db() calls this with its own connection, open but with nothing
    changed yet, so a migration that succeeds and is wrong can still be
    undone by restoring this file. Unlike the nightly job it never swallows
    a failure: no verified snapshot, no migration."""
    tz: tzinfo
    try:
        tz = await database.family_timezone(db)
    except sqlite3.Error:  # a database with no app_settings yet
        tz = timezone.utc
    stamp = datetime.now(tz).strftime(STAMP_FORMAT)
    async with _lock:
        path = await asyncio.to_thread(_create_backup_sync, stamp)
    if path is None:
        raise SnapshotError(
            "no snapshot could be taken before migration(s) "
            f"{', '.join(f'{v:04d}' for v in pending)} (see the warning above); nothing was changed"
        )
    logger.info("Snapshot %s taken before migration(s) %s", path.name, ", ".join(f"{v:04d}" for v in pending))
    return path


async def create_backup() -> Path | None:
    """Back up now. Returns the new file, or None if it failed (logged)."""
    global _last_failure
    async with _lock:
        async with database.get_db() as db:
            tz = await database.family_timezone(db)
        stamp = datetime.now(tz).strftime(STAMP_FORMAT)
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


def last_nightly(now: datetime, tz) -> datetime:
    """The most recent 03:30 family-local instant at or before `now`, in UTC.
    Worked in UTC because Python compares same-tz datetimes by wall time,
    ignoring DST. An ambiguous 03:30 (autumn) means its first occurrence
    (fold=0); a non-existent one (spring, where clocks jump over 03:30) maps
    to the matching instant after the jump (e.g. 04:30)."""
    now_utc = now.astimezone(timezone.utc)
    day = now_utc.astimezone(tz).date()
    for _ in range(3):
        nightly = datetime.combine(day, NIGHTLY_AT, tzinfo=tz).astimezone(timezone.utc)
        if nightly <= now_utc:
            return nightly
        day -= timedelta(days=1)
    raise AssertionError("unreachable: there is a 03:30 within any three days")


def backup_due(newest: datetime | None, now: datetime, tz) -> bool:
    """Due when there's no backup yet, or none since the most recent 03:30
    that has passed (which also covers a box that was off at 03:30)."""
    return newest is None or newest < last_nightly(now, tz)


async def run_backup_if_due() -> Path | None:
    """Scheduler entry point: checked every few minutes and once at startup."""
    if _last_failure is not None and time.monotonic() - _last_failure < RETRY_AFTER_FAILURE_SECONDS:
        return None
    async with database.get_db() as db:
        tz = await database.family_timezone(db)
    now = datetime.now(timezone.utc)
    if not backup_due(newest_backup_time(tz, now), now, tz):
        return None
    return await create_backup()


def status(tz) -> dict:
    """For the Admin page: newest backup's local time and size, and the count."""
    backups = list_backups(tz)
    if not backups:
        return {"count": 0, "newest_at": None, "newest_size": None}
    return {
        "count": len(backups),
        "newest_at": backup_time(backups[0], tz).astimezone(tz),
        "newest_size": backups[0].stat().st_size,
    }

