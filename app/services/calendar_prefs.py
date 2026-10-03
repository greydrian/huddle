"""
The calendar's Admin settings (spec 10.5, 12), each JSON in app_settings:

- calendar_family: {"account", "id", "summary"} of the one shared "Family"
  calendar the wall's "+" adds events to. Chosen on the Google & Sync tab
  from the calendars that are shown on the wall and writable in the account
  doing Writing events. It only counts while it's still shown and its
  account still does Writing events.
- calendar_default_view: "month", "week" or "agenda": what the widget shows
  on load, and goes back to after CALENDAR_IDLE_SECONDS untouched.
- calendar_people: {calendar key ("<account id>:<calendar id>"): profile id,
  or "everyone"}: whose events a calendar holds, for the person filter. A
  shown calendar not listed counts as its account owner's, or everyone's
  for a Family account (see services/calendar_view.event_owners). Keys
  without an account are from before accounts (kept for a rollback) and
  ignored.
"""

import json

from app import google_accounts
from app.database import get_setting, set_setting
from app.google_oauth import calendar_key, get_selected_calendars, split_calendar_key

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


def one_calendar(value) -> dict | None:
    """{"account", "id", "summary"} from a saved one-calendar setting (the
    Family calendar, the school events calendar), or None."""
    if (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and value["id"]
        and isinstance(value.get("account"), int)
    ):
        return {"account": value["account"], "id": value["id"], "summary": str(value.get("summary") or value["id"])}
    return None


def one_calendar_value(calendar: dict | None) -> str:
    """What a one-calendar setting stores."""
    if calendar is None:
        return json.dumps(None)
    return json.dumps({"account": calendar["account"], "id": calendar["id"], "summary": calendar["summary"]})


# --- Family calendar ----------------------------------------------------------------------


async def get_family_setting(db) -> dict | None:
    """The saved Family calendar ({"account", "id", "summary"}), whether or
    not it's still in use (Admin shows it either way)."""
    return one_calendar(_load(await get_setting(db, FAMILY_KEY)))


async def get_family_calendar(db) -> dict | None:
    """The Family calendar to add events to, or None: not chosen, no longer
    shown on the wall, or its account no longer does Writing events."""
    family = await get_family_setting(db)
    if family is None:
        return None
    writer = await google_accounts.job_account(db, "write_events")
    if writer is None or writer["id"] != family["account"]:
        return None
    shown = {cal["key"] for cal in await get_selected_calendars(db)}
    return family if calendar_key(family["account"], family["id"]) in shown else None


async def set_family_calendar(db, calendar: dict | None) -> None:
    await set_setting(db, FAMILY_KEY, one_calendar_value(calendar))
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


async def get_saved_people_links(db) -> dict[str, int | str]:
    """{calendar key: profile id or EVERYONE} as saved; anything unreadable
    (or saved before accounts) is dropped."""
    value = _load(await get_setting(db, PEOPLE_KEY))
    if not isinstance(value, dict):
        return {}
    links: dict[str, int | str] = {}
    for key, owner in value.items():
        if not isinstance(key, str) or split_calendar_key(key) is None:
            continue
        if owner == EVERYONE or (isinstance(owner, int) and not isinstance(owner, bool)):
            links[key] = owner
    return links


async def get_people_links(db) -> dict[str, int | str]:
    """Whose each shown calendar is: its saved link, else its account's
    owner (spec 12.1), else nothing (everyone)."""
    links = await get_saved_people_links(db)
    owners = {a["id"]: a["owner_id"] for a in await google_accounts.list_accounts(db)}
    for cal in await get_selected_calendars(db):
        owner = owners.get(cal["account"])
        if cal["key"] not in links and owner is not None:
            links[cal["key"]] = owner
    return links


async def set_people_links(db, links: dict[str, int | str]) -> None:
    await set_setting(db, PEOPLE_KEY, json.dumps(links))
    await db.commit()
