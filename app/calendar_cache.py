"""
Saved copy of the Calendar widget's events, so an internet outage shows
the last good month (with a "Last updated HH:MM" note) instead of an empty
grid.

Rows hold the events exactly as google_calendar formatted them from a fully
successful fetch — Google's own offsets and time labels, the calendar
colours — keyed by the [start, end) date range that was fetched. Only
event data lives here, never tokens. The cache is cleared whenever the
calendar selection changes or Google is disconnected (google_oauth), since
it would otherwise show another account's or an unselected calendar's
events.
"""

import json
from datetime import date, datetime, timezone

KEEP_RANGES = 12  # most recently fetched ranges kept (months someone browsed to)


async def store(db, start: date, end: date, events: list[dict]) -> None:
    await db.execute(
        """INSERT INTO calendar_cache (range_start, range_end, events_json, fetched_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(range_start, range_end) DO UPDATE SET
             events_json = excluded.events_json, fetched_at = excluded.fetched_at""",
        (start.isoformat(), end.isoformat(), json.dumps(events), datetime.now(timezone.utc).isoformat()),
    )
    await db.execute(
        """DELETE FROM calendar_cache WHERE rowid NOT IN (
             SELECT rowid FROM calendar_cache ORDER BY fetched_at DESC LIMIT ?)""",
        (KEEP_RANGES,),
    )
    await db.commit()


async def load(db, start: date, end: date) -> tuple[list[dict], datetime] | None:
    """(events touching [start, end), when they were fetched) from the most
    recent cached range that covers it — so a day view can be served from
    its month — or None if nothing covers it."""
    row = await (await db.execute(
        """SELECT events_json, fetched_at FROM calendar_cache
           WHERE range_start <= ? AND range_end >= ?
           ORDER BY fetched_at DESC LIMIT 1""",
        (start.isoformat(), end.isoformat()),
    )).fetchone()
    if row is None:
        return None
    try:
        events = json.loads(row["events_json"])
        fetched_at = datetime.fromisoformat(row["fetched_at"])
    except (ValueError, TypeError):
        return None
    first, last = start.isoformat(), end.isoformat()
    events = [e for e in events if e["date"] < last and e["end_date"] >= first]
    return events, fetched_at


async def clear(db) -> None:
    """Drop every cached range. The caller commits."""
    await db.execute("DELETE FROM calendar_cache")
