"""
Saved copy of the Calendar widget's events, so an internet outage shows
the last good month (with a "Last updated" note) instead of an empty grid.

One row per (selection, date range, calendar): the events exactly as
google_calendar formatted them from that calendar's last successful fetch
— Google's own offsets and time labels, the calendar colour. Per calendar,
so one persistently failing calendar (e.g. unshared, 404) only falls back
to its own copy while the healthy ones stay live and keep their copies
fresh. A calendar is its account and its id together
(google_oauth.calendar_key, "2:primary"), so an account that's offline or
needs reconnecting is served from here while the others stay live.
`selection` is a hash of the Admin calendar selection: rows written by a
fetch that started before the selection changed can never be read under
the new one. Only event data lives here, never tokens. The cache is also
cleared outright whenever the selection changes (google_oauth), and an
account's rows go when it's removed (services/accounts).
"""

import hashlib
import json
from datetime import date, datetime, timezone

KEEP_RANGES = 12  # most recently fetched date ranges kept (months someone browsed to)


def selection_key(calendars: list[dict]) -> str:
    payload = json.dumps([[c.get("account"), c.get("id"), c.get("color")] for c in calendars])
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:16]


async def store(db, selection: str, start: date, end: date, by_calendar: dict[str, list[dict]]) -> None:
    """Save each calendar's freshly fetched events for [start, end)."""
    now = datetime.now(timezone.utc).isoformat()
    for calendar_id, events in by_calendar.items():
        await db.execute(
            """INSERT INTO calendar_cache
                 (selection, range_start, range_end, calendar_id, events_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(selection, range_start, range_end, calendar_id) DO UPDATE SET
                 events_json = excluded.events_json, fetched_at = excluded.fetched_at""",
            (selection, start.isoformat(), end.isoformat(), calendar_id, json.dumps(events), now),
        )
    # Another selection's rows can never be read again; old ranges age out.
    await db.execute("DELETE FROM calendar_cache WHERE selection != ?", (selection,))
    await db.execute(
        """DELETE FROM calendar_cache WHERE (range_start, range_end) NOT IN (
             SELECT range_start, range_end FROM calendar_cache
             GROUP BY range_start, range_end ORDER BY MAX(fetched_at) DESC LIMIT ?)""",
        (KEEP_RANGES,),
    )
    await db.commit()


async def load(db, selection: str, start: date, end: date, calendar_id: str) -> tuple[list[dict], datetime] | None:
    """(this calendar's events touching [start, end), when they were
    fetched) from the newest cached range that covers it — so a day view
    can be served from its month — or None if nothing covers it."""
    row = await (
        await db.execute(
            """SELECT events_json, fetched_at FROM calendar_cache
           WHERE selection = ? AND calendar_id = ? AND range_start <= ? AND range_end >= ?
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


async def clear(db) -> None:
    """Drop every cached range. The caller commits."""
    await db.execute("DELETE FROM calendar_cache")


async def clear_account(db, account_id: int) -> None:
    """Drop one account's calendars (keys "<account id>:..."). The caller commits."""
    await db.execute("DELETE FROM calendar_cache WHERE calendar_id LIKE ?", (f"{account_id}:%",))
