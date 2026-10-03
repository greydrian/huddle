"""
Saved copy of the Calendar widget's events, so an internet outage shows
the last good month (with a "Last updated" note) instead of an empty grid.

One row per (account, selection, date range, calendar): the events exactly
as google_calendar formatted them from that calendar's last successful
fetch — Google's own offsets and time labels, the calendar colour. Per
calendar, so one persistently failing calendar (e.g. unshared, 404) only
falls back to its own copy while the healthy ones stay live and keep their
copies fresh; per account (spec 12.3), so an account that's offline or
needs reconnecting is served from here while the others stay live.

`selection` is a hash of one account's part of the Admin calendar
selection (selection_keys covers every account, each with its own hash):
rows written by a fetch that started before the selection changed can
never be read under the new one, and a change to one account's calendars
never touches another account's copies. Clears are per account too: its
selection changing, or the account being removed (services/accounts). A
revoked sign-in keeps its copies, so "Reconnect needed" still shows its
calendars. Only event data lives here, never tokens. Rows from before
accounts (account_id NULL) are only for a rollback and are never read.
"""

import hashlib
import json
from datetime import date, datetime, timezone

KEEP_RANGES = 12  # most recently fetched date ranges kept per account (months someone browsed to)


def selection_key(account_id: int, calendars: list[dict]) -> str:
    """The hash of one account's selected calendars (ids and colours)."""
    payload = json.dumps([account_id, [[c.get("id"), c.get("color")] for c in calendars]])
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:16]


def selection_keys(calendars: list[dict]) -> dict[int, str]:
    """{account id: its selection_key} for a whole selection."""
    by_account: dict[int, list[dict]] = {}
    for cal in calendars:
        by_account.setdefault(cal["account_id"], []).append(cal)
    return {account_id: selection_key(account_id, cals) for account_id, cals in by_account.items()}


async def store(
    db, account_id: int, selection: str, start: date, end: date, by_calendar: dict[str, list[dict]]
) -> None:
    """Save one account's freshly fetched events for [start, end), by calendar id."""
    now = datetime.now(timezone.utc).isoformat()
    for calendar_id, events in by_calendar.items():
        await db.execute(
            """INSERT INTO calendar_cache
                 (selection, range_start, range_end, calendar_id, events_json, fetched_at, account_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(selection, range_start, range_end, calendar_id) DO UPDATE SET
                 events_json = excluded.events_json, fetched_at = excluded.fetched_at""",
            (selection, start.isoformat(), end.isoformat(), calendar_id, json.dumps(events), now, account_id),
        )
    # This account's other selections can never be read again; its old ranges age out.
    await db.execute("DELETE FROM calendar_cache WHERE account_id = ? AND selection != ?", (account_id, selection))
    await db.execute(
        """DELETE FROM calendar_cache WHERE account_id = ? AND (range_start, range_end) NOT IN (
             SELECT range_start, range_end FROM calendar_cache WHERE account_id = ?
             GROUP BY range_start, range_end ORDER BY MAX(fetched_at) DESC LIMIT ?)""",
        (account_id, account_id, KEEP_RANGES),
    )
    await db.commit()


async def load(db, selection: str, start: date, end: date, calendar_id: str) -> tuple[list[dict], datetime] | None:
    """(this calendar's events touching [start, end), when they were
    fetched) from the newest cached range that covers it — so a day view
    can be served from its month — or None if nothing covers it.
    `selection` is its account's selection_key."""
    row = await (
        await db.execute(
            """SELECT events_json, fetched_at FROM calendar_cache
           WHERE selection = ? AND calendar_id = ? AND range_start <= ? AND range_end >= ?
             AND account_id IS NOT NULL
           ORDER BY fetched_at DESC LIMIT 1""",
            (selection, calendar_id, start.isoformat(), end.isoformat()),
        )
    ).fetchone()
    if row is None:
        return None
    try:
        events = json.loads(row["events_json"])
        fetched_at = datetime.fromisoformat(row["fetched_at"])
    except ValueError, TypeError:
        return None
    first, last = start.isoformat(), end.isoformat()
    return [e for e in events if e["date"] < last and e["end_date"] >= first], fetched_at


async def clear_account(db, account_id: int) -> None:
    """Drop one account's cached events. The caller commits."""
    await db.execute("DELETE FROM calendar_cache WHERE account_id = ?", (account_id,))
