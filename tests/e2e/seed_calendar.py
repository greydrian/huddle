"""Seeds a connected calendar into an e2e server's DATA_DIR (Server.seed_calendar
in conftest.py runs this with the server's environment, before it starts).

A connected account (with the calendar.events scope) showing a Family
calendar and Riley's calendar, the Family calendar chosen for the "+",
Riley's calendar linked to Riley, and a saved copy of this month and the
week ahead in calendar_cache. Events: Riley's swimming (her calendar) and
"Jamie: Dentist" and "Bins out" today, five clubs tomorrow (so the month
shows "+N more"), and a two-day trip.
"""

import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import calendar_cache, database, google_calendar, google_oauth  # noqa: E402
from app.services import calendar_prefs  # noqa: E402

ACCOUNT = "family@example.com"
CALENDARS = [
    {"account_id": 1, "id": "family@group.calendar.google.com", "summary": "Family", "color": "#4F7CAC"},
    {"account_id": 1, "id": "riley@example.com", "summary": "Riley", "color": "#C1584A"},
]


def key(calendar):
    return google_oauth.calendar_key(calendar["account_id"], calendar["id"])


def event(calendar, title, day, start=None, end=None, last_day=None):
    raw = {"id": "e2e" + str(abs(hash((title, day)))), "summary": title}
    if start is None:
        raw.update(start={"date": day.isoformat()}, end={"date": ((last_day or day) + timedelta(days=1)).isoformat()})
    else:
        raw.update(
            start={"dateTime": f"{day.isoformat()}T{start}:00+01:00"},
            end={"dateTime": f"{day.isoformat()}T{end}:00+01:00"},
        )
    formatted = google_calendar._format_event(raw)
    formatted.update(color=calendar["color"], calendar_id=calendar["id"])
    return formatted


async def main():
    async with database.get_db() as db:
        await google_oauth.store_tokens(
            db,
            {
                "access_token": "tok",
                "refresh_token": "r",
                "expires_at": time.time() + 86400,
                "scope": f"{google_oauth.CALENDAR_READ_SCOPE} {google_oauth.CALENDAR_EVENTS_SCOPE}",
            },
            ACCOUNT,
        )
        await google_oauth.set_selected_calendars(db, CALENDARS)
        await calendar_prefs.set_family_calendar(db, CALENDARS[0])
        riley = (await (await db.execute("SELECT id FROM profiles WHERE name = 'Riley'")).fetchone())[0]
        await calendar_prefs.set_people_links(
            db, {key(CALENDARS[1]): riley, key(CALENDARS[0]): calendar_prefs.EVERYONE}
        )
        now = datetime.now(await database.family_timezone(db))
        today, tomorrow = now.date(), now.date() + timedelta(days=1)
        family, riley_cal = CALENDARS
        start, end = google_calendar._refresh_span(now)
        await calendar_cache.store(
            db,
            1,
            calendar_cache.selection_keys(CALENDARS)[1],
            start,
            end,
            {
                family["id"]: [
                    event(family, "Jamie: Dentist", today, "15:00", "16:00"),
                    event(family, "Bins out", today),
                    event(family, "Trip to Gran", today + timedelta(days=2), last_day=today + timedelta(days=3)),
                    *[event(family, f"Club {h}", tomorrow, f"{h:02d}:00", f"{h:02d}:45") for h in range(9, 14)],
                ],
                riley_cal["id"]: [event(riley_cal, "Swimming", today, "17:00", "18:00")],
            },
        )


if __name__ == "__main__":
    asyncio.run(main())
