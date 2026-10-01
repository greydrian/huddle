"""The "next up" strip (spec 11 / review 4.3): under the banner bar, each
person's next timed event today and how long until it, readable at a
glance from across the kitchen.

Events come from calendar_cache only (google_calendar.cached_events), so
the strip polls nothing and works offline. Whose event it is follows the
calendar's person filter (calendar_view.event_owners): a calendar linked
to a person in Admin, else the person's name in the title; everything else
is Everyone's. The banner bar covers the last few minutes before an event;
this covers the rest of the day.

Countdowns ("Half term in 12 days", services/countdowns.py) share the
strip, after the day's events.

It refreshes through /api/rev as the "next_up" key (app/freshness.py). The
minutes-to text changes every minute, so the strip is re-fetched about once
a minute while anything is coming up, and never while the wall is in use
(data-busy-any, like the banner bar).
"""

import math
from datetime import datetime, timedelta

from app import avatars, google_calendar
from app.database import family_timezone, get_setting, set_setting
from app.services import calendar_prefs, countdowns, people
from app.services.calendar_view import event_owners

SETTING = "next_up_enabled"  # "1" (default) or "0": Admin → Display → Banners
EVERYONE = "everyone"


async def is_enabled(db) -> bool:
    return await get_setting(db, SETTING, "1") != "0"


async def set_enabled(db, enabled: bool) -> None:
    await set_setting(db, SETTING, "1" if enabled else "0")
    await db.commit()


def in_words(minutes: int) -> str:
    """ "in 25 min", "in 2 h", "in 1 h 5 min"."""
    if minutes < 60:
        return f"in {minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"in {hours} h" + (f" {rest} min" if rest else "")


async def _profiles(db) -> list[dict]:
    rows = await (
        await db.execute(f"SELECT id, name, colour_hex, {avatars.COLUMNS} FROM profiles ORDER BY sort_order")
    ).fetchall()
    return [dict(r) for r in rows]


def _start(event: dict) -> datetime | None:
    """A timed event's start with Google's own offset, or None (all-day,
    a local term-date bar, or unparseable)."""
    if event.get("all_day") or event.get("school"):
        return None
    try:
        start = datetime.fromisoformat(event["sort_key"])
    except KeyError, TypeError, ValueError:
        return None
    return start if start.tzinfo is not None else None


async def items(db, now: datetime) -> list[dict]:
    """One item per person with something still to come today (family
    order), then Everyone's: {"key", "person" (name, colour, avatar) or None
    for Everyone, "title", "time" ("16:30"), "in" ("in 25 min")}."""
    today = now.date()
    # The cache is filtered by each event's date in Google's own offset; one
    # day more catches an event in a zone ahead of the family's that is still
    # "today" here (00:30+02:00 tomorrow is 23:30 today in London). The
    # family-day check below decides.
    events = await google_calendar.cached_events(db, today, today + timedelta(days=2))
    if not events:
        return []
    profiles = await _profiles(db)
    links = await calendar_prefs.get_people_links(db)
    earliest: dict[int | str, tuple[datetime, dict]] = {}
    for event in events:
        start = _start(event)
        if start is None or start <= now or start.astimezone(now.tzinfo).date() != today:
            continue
        owners = event_owners(event, links, profiles)
        for owner in owners if owners is not None else {EVERYONE}:
            if owner not in earliest or start < earliest[owner][0]:
                earliest[owner] = (start, event)

    result = []
    for key in [p["id"] for p in profiles] + [EVERYONE]:
        if key not in earliest:
            continue
        start, event = earliest[key]
        profile = next((p for p in profiles if p["id"] == key), None)
        _, title = people.split_person(event.get("title") or "(untitled)", profiles)
        minutes = max(1, math.ceil((start - now).total_seconds() / 60))
        result.append(
            {
                "key": str(key),
                "person": (
                    {"name": profile["name"], "colour": profile["colour_hex"], "avatar": avatars.avatar_of(profile)}
                    if profile
                    else None
                ),
                "title": title,
                # Google's own offset for the time, never the container's clock.
                "time": start.strftime("%H:%M"),
                "in": in_words(minutes),
            }
        )
    return result


def _clock(tz) -> datetime:
    """The family-timezone wall clock (tests freeze it)."""
    return datetime.now(tz)


async def context(db, now: datetime | None = None) -> dict:
    """Template context for _next_up.html, from the dashboard, its own route
    and /api/rev alike. Countdowns (services/countdowns.py) share the strip
    and show whether or not the next-up part is switched on."""
    now = now or _clock(await family_timezone(db))
    enabled = await is_enabled(db)
    return {
        "next_up": await items(db, now) if enabled else [],
        "next_up_enabled": enabled,
        "countdowns": await countdowns.upcoming(db, now.date()),
    }
