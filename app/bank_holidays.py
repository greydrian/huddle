"""
Bank holidays (spec 10.6) from the GOV.UK feed, England and Wales only,
kept in the `bank_holidays` table for app/services/term_dates.py.

A scheduler job (scheduler.py) calls run_if_due() every few hours and once
shortly after startup; it fetches when there's no data or the last good
fetch is a week old. A failed fetch keeps the old data, logs once per
outage (http_client.report_failure) and is tried again at the next check.
Admin shows when the data was last updated and warns once it's stale.
"""

import logging
from datetime import UTC, date, datetime, timedelta

import httpx

from app import http_client
from app.database import get_db, get_setting, set_setting

logger = logging.getLogger(__name__)

FEED_URL = "https://www.gov.uk/bank-holidays.json"
DIVISION = "england-and-wales"
UPDATED_SETTING = "bank_holidays_updated_at"  # UTC ISO time of the last good fetch
REFRESH_AFTER = timedelta(days=7)   # weekly
STALE_AFTER = timedelta(days=30)    # Admin warns past this
CHECK_SECONDS = 6 * 3600
STARTUP_DELAY = timedelta(seconds=30)  # after startup, out of the way of the first page loads
MAX_EVENTS = 1000  # the feed has about 80 per division; anything far bigger is not the feed
MAX_TITLE = 80
OUTAGE_KEY = "GOV.UK bank holidays"


def parse_feed(data) -> dict[str, str]:
    """{ISO date: title} for England and Wales. ValueError if the feed isn't
    shaped as expected (nothing is stored then)."""
    try:
        events = data[DIVISION]["events"]
    except (KeyError, TypeError):
        raise ValueError("no england-and-wales events") from None
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
        raise ValueError("unexpected event list")
    holidays: dict[str, str] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            day = date.fromisoformat(str(event.get("date", "")))
        except ValueError:
            continue
        title = " ".join(str(event.get("title") or "Bank holiday").split())[:MAX_TITLE] or "Bank holiday"
        holidays[day.isoformat()] = title
    if not holidays:
        raise ValueError("no usable events")
    return holidays


async def fetch() -> dict[str, str]:
    """Raises httpx.HTTPError (network, non-2xx) or ValueError (not the feed)."""
    async with http_client.client() as client:
        resp = await client.get(FEED_URL, headers={"Accept": "application/json"})
        resp.raise_for_status()
    return parse_feed(resp.json())


async def refresh(db) -> bool:
    """Fetch and store the feed. On any failure the old rows stay. True if
    stored. Idempotent by date: rows are upserted, and a date the feed no
    longer lists (within the range it covers) is removed."""
    try:
        holidays = await fetch()
    except (httpx.HTTPError, ValueError) as exc:
        http_client.report_failure(
            logger, OUTAGE_KEY, "Couldn't update bank holidays; keeping the old ones: %s",
            http_client.describe(exc),
        )
        return False
    first, last = min(holidays), max(holidays)
    try:
        await db.execute(
            f"DELETE FROM bank_holidays WHERE date BETWEEN ? AND ? AND date NOT IN ({','.join('?' * len(holidays))})",
            (first, last, *holidays),
        )
        await db.executemany(
            """INSERT INTO bank_holidays (date, title) VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET title = excluded.title, updated_at = datetime('now')
               WHERE title != excluded.title""",
            list(holidays.items()),
        )
        await set_setting(db, UPDATED_SETTING, datetime.now(UTC).isoformat())
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    http_client.report_success(logger, OUTAGE_KEY)
    logger.info("Bank holidays updated: %d dates", len(holidays))
    return True


async def updated_at(db) -> datetime | None:
    raw = await get_setting(db, UPDATED_SETTING)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


async def is_due(db, now: datetime) -> bool:
    last = await updated_at(db)
    if last is None:
        return True
    row = await (await db.execute("SELECT 1 FROM bank_holidays LIMIT 1")).fetchone()
    return row is None or now - last >= REFRESH_AFTER


async def run_if_due() -> bool:
    """Scheduler entry point (every CHECK_SECONDS, and shortly after startup)."""
    async with get_db() as db:
        if not await is_due(db, datetime.now(UTC)):
            return False
        return await refresh(db)


async def status(db) -> dict:
    """For Admin: {"updated_at": aware datetime or None, "stale": bool, "count": int}."""
    last = await updated_at(db)
    (count,) = await (await db.execute("SELECT COUNT(*) FROM bank_holidays")).fetchone()
    stale = last is None or datetime.now(UTC) - last > STALE_AFTER
    return {"updated_at": last, "stale": stale, "count": count}
