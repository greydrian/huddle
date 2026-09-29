"""
Keeping the long-lived kiosk page fresh without reloading it.

- /api/rev: a revision per self-refreshing widget. app/static/js/fresh.js
  polls it and re-fetches only the widgets whose revision changed, so
  changes pulled in by the Google Tasks sync, Admin edits made on a phone
  and the midnight reset reach the wall. (Calendar and Weather poll
  themselves: their data is Google's/Open-Meteo's, not ours.) Only the
  widgets the wall shows are loaded (spec 10.3); `shown` lists them, and
  fresh.js reloads the page when the layout generation changes.
- /api/today: the family's date and the seconds until it next changes, for
  the top bar — the same "server says how long, tablet counts elapsed time"
  approach as the day/night switch in app/appearance.py, so the tablet's own
  clock and timezone never decide the date.

A revision is a short hash of the widget's own template context (its
registry loader's output): anything that changes what the widget shows —
a row added, edited or deleted, a new family day, a profile's colour —
changes it, and nothing else does. The loaders are small SQLite reads, so
this stays cheap at household scale.
"""

import hashlib
import json
from datetime import datetime, time, timedelta

from fastapi import APIRouter

from app import database
from app.database import family_timezone, get_db
from app.services import banners, layout
from app.widgets import WIDGETS

# Widgets refreshed through /api/rev. Each template's root carries
# data-refresh="<its /widgets/... route>" (tests/test_freshness.py).
REFRESHED = ("tasks", "shopping", "meals", "homework", "practice_words")
# Not a widget, refreshed the same way: the notification banner bar
# (templates/_banners.html, data-refresh="/banners").
BANNERS = "banners"

router = APIRouter()


def revision(context: dict) -> str:
    # The on-screen keyboard toggle changes the inputs' markup too.
    payload = json.dumps([context, database.onscreen_keyboard_enabled], sort_keys=True, default=str)
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:12]


def widget_revisions(contexts: dict[str, dict]) -> dict[str, str]:
    """{widget_id: revision} for the REFRESHED widgets (and BANNERS) in
    `contexts`: the dashboard passes the contexts it just rendered (only the
    widgets it shows), so the page's starting revisions describe exactly
    what it shows."""
    return {key: revision(contexts[key]) for key in (*REFRESHED, BANNERS) if key in contexts}


async def shown_revisions(db, shown: list[str]) -> dict[str, str]:
    """Revisions for the REFRESHED widgets the wall shows today, plus the
    banner bar (always there). A hidden widget (or a "school days only" one
    on a day off) isn't loaded at all."""
    revs = {widget_id: revision(await WIDGETS[widget_id].load(db)) for widget_id in REFRESHED if widget_id in shown}
    revs[BANNERS] = revision(await banners.context(db))
    return revs


def seconds_until_tomorrow(now: datetime) -> int:
    """Whole seconds from a tz-aware local `now` to the next local midnight,
    measured in real (UTC) time so a DST change in between is counted."""
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo)
    return max(1, int((midnight.timestamp() - now.timestamp()) + 0.999))


async def today_info(db, now: datetime | None = None) -> dict:
    tz = await family_timezone(db)
    now = (now or datetime.now(tz)).astimezone(tz)
    return {"date": now.date().isoformat(), "next_change_in": seconds_until_tomorrow(now)}


@router.get("/api/rev")
async def revisions():
    """`shown`: the widgets the wall should show now; `layout`: the layout
    generation (services/layout.generation). fresh.js reloads the page when
    that differs from the page's (a widget hidden or shown in Admin, or a
    new day)."""
    async with get_db() as db:
        shown = await layout.shown_ids(db)
        return {
            "today": (await today_info(db))["date"],
            "shown": shown,
            "layout": await layout.generation(db, shown),
            "widgets": await shown_revisions(db, shown),
        }


@router.get("/api/today")
async def today():
    async with get_db() as db:
        return await today_info(db)
