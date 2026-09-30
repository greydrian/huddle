"""
The calendar's Admin settings (spec 10.5), each JSON in app_settings (no
migration needed):

- calendar_family: {"id", "summary"} of the one shared "Family" calendar the
  wall's "+" adds events to. Chosen on the Google & Sync tab from calendars
  that are both shown on the wall and writable. It only counts while it's
  still one of the selected calendars.
- calendar_default_view: "month", "week" or "agenda": what the widget shows
  on load, and goes back to after CALENDAR_IDLE_SECONDS untouched.
- calendar_people: {calendar id: profile id, or "everyone"}: whose events a
  calendar holds, for the person filter. A calendar not listed counts as
  "everyone" (see services/calendar_view.event_owners).
"""

import json

from app.database import get_setting, set_setting
from app.google_oauth import get_selected_calendars

FAMILY_KEY = "calendar_family"
DEFAULT_VIEW_KEY = "calendar_default_view"
PEOPLE_KEY = "calendar_people"

VIEWS = {"month": "Month", "week": "Week", "agenda": "Agenda"}
DEFAULT_VIEW = "month"
EVERYONE = "everyone"


def _load(raw: str | None):
    try:
        return json.loads(raw or "null")
    except ValueError:
        return None


# --- Family calendar ----------------------------------------------------------------------


async def get_family_setting(db) -> dict | None:
    """The saved Family calendar ({"id", "summary"}), whether or not it's
    still selected (Admin shows it either way)."""
    value = _load(await get_setting(db, FAMILY_KEY))
    if isinstance(value, dict) and isinstance(value.get("id"), str) and value["id"]:
        return {"id": value["id"], "summary": str(value.get("summary") or value["id"])}
    return None


async def get_family_calendar(db) -> dict | None:
    """The Family calendar to add events to, or None: not chosen, or no
    longer one of the calendars shown on the wall."""
    family = await get_family_setting(db)
    if family is None:
        return None
    selected = {cal.get("id") for cal in await get_selected_calendars(db)}
    return family if family["id"] in selected else None


async def set_family_calendar(db, calendar: dict | None) -> None:
    value = {"id": calendar["id"], "summary": calendar["summary"]} if calendar else None
    await set_setting(db, FAMILY_KEY, json.dumps(value))
    await db.commit()


# --- Default view -------------------------------------------------------------------------


async def get_default_view(db) -> str:
    value = _load(await get_setting(db, DEFAULT_VIEW_KEY))
    return value if value in VIEWS else DEFAULT_VIEW


async def set_default_view(db, view: str) -> None:
    """Raises ValueError for a view that isn't one of VIEWS."""
    if view not in VIEWS:
        raise ValueError(view)
    await set_setting(db, DEFAULT_VIEW_KEY, json.dumps(view))
    await db.commit()


# --- Calendar -> person links -------------------------------------------------------------


async def get_people_links(db) -> dict[str, int | str]:
    """{calendar id: profile id or EVERYONE}; anything unreadable is dropped."""
    value = _load(await get_setting(db, PEOPLE_KEY))
    if not isinstance(value, dict):
        return {}
    links: dict[str, int | str] = {}
    for cal_id, owner in value.items():
        if not isinstance(cal_id, str):
            continue
        if owner == EVERYONE or (isinstance(owner, int) and not isinstance(owner, bool)):
            links[cal_id] = owner
    return links


async def set_people_links(db, links: dict[str, int | str]) -> None:
    await set_setting(db, PEOPLE_KEY, json.dumps(links))
    await db.commit()
