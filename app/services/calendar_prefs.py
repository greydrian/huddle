"""
The calendar's Admin settings (spec 10.5, 12), each JSON in app_settings.
Since spec 12 each entry that names a calendar also has its "account_id":
the key was added to the existing shape (migration 13), so the previous
release still reads them.

- calendar_family: {"account_id", "id", "summary"} of the one shared
  "Family" calendar the wall's "+" adds events to. Chosen on the Google &
  Sync tab from the calendars that are shown on the wall and writable in the
  account doing Writing events. It only counts while it's still shown
  through that account and the account still does Writing events.
- calendar_default_view: "month", "week" or "agenda": what the widget shows
  on load, and goes back to after CALENDAR_IDLE_SECONDS untouched.
- calendar_people_v2: {"<account id>:<calendar id>": profile id, or
  "everyone"}: whose events a calendar holds, for the person filter. A
  shown calendar not listed counts as its account owner's, or everyone's
  for a Family account (see services/calendar_view.event_owners).
  Migration 13 copied the old account-less calendar_people into it (as
  account 1's) and leaves calendar_people itself untouched for a rollback;
  nothing writes it any more.
"""

import json

from app import google_accounts
from app.database import get_setting, set_setting
from app.google_oauth import (
    SELECTED_CALENDARS_SETTING,
    calendar_key,
    get_saved_calendars,
    get_selected_calendars,
    set_selected_calendars,
    split_calendar_key,
)

FAMILY_KEY = "calendar_family"
DEFAULT_VIEW_KEY = "calendar_default_view"
PEOPLE_KEY = "calendar_people_v2"
SCHOOL_EVENTS_KEY = "school_events_calendar"  # services/school_events

VIEWS = {"month": "Month", "week": "Week", "agenda": "Agenda"}
DEFAULT_VIEW = "month"
EVERYONE = "everyone"


def _load(raw: str | None):
    try:
        return json.loads(raw or "null")
    except ValueError:
        return None


def one_calendar(value) -> dict | None:
    """{"account_id", "id", "summary"} from a saved one-calendar setting
    (the Family calendar, the school events calendar), or None."""
    if (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and value["id"]
        and isinstance(value.get("account_id"), int)
    ):
        return {
            "account_id": value["account_id"],
            "id": value["id"],
            "summary": str(value.get("summary") or value["id"]),
        }
    return None


def one_calendar_value(calendar: dict | None) -> str:
    """What a one-calendar setting stores."""
    if calendar is None:
        return json.dumps(None)
    return json.dumps({"id": calendar["id"], "summary": calendar["summary"], "account_id": calendar["account_id"]})


# --- Family calendar ----------------------------------------------------------------------


async def get_family_setting(db) -> dict | None:
    """The saved Family calendar ({"account_id", "id", "summary"}), whether
    or not it's still in use (Admin shows it either way)."""
    return one_calendar(_load(await get_setting(db, FAMILY_KEY)))


async def get_family_calendar(db) -> dict | None:
    """The Family calendar to add events to, or None: not chosen, no longer
    shown on the wall through its account, or that account no longer does
    Writing events."""
    family = await get_family_setting(db)
    if family is None:
        return None
    writer = await google_accounts.job_account(db, "write_events")
    if writer is None or writer["id"] != family["account_id"]:
        return None
    shown = {(cal["account_id"], cal["id"]) for cal in await get_selected_calendars(db)}
    return family if (family["account_id"], family["id"]) in shown else None


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
    """{"<account id>:<calendar id>": profile id or EVERYONE} as saved;
    anything unreadable is dropped."""
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
    """{"<account id>:<calendar id>": whose it is} for each shown calendar:
    its link through the account it's shown through (else, for a real
    calendar id, through any account), else its account's owner (spec
    12.1), else nothing (everyone). Keyed by account too: two accounts'
    "primary" aliases are different calendars."""
    saved = await get_saved_people_links(db)
    by_calendar: dict[str, int | str] = {}
    for key, owner in saved.items():
        if key.partition(":")[2] != "primary":
            by_calendar.setdefault(key.partition(":")[2], owner)
    owners = {a["id"]: a["owner_id"] for a in await google_accounts.list_accounts(db)}
    links: dict[str, int | str] = {}
    for cal in await get_selected_calendars(db):
        found: int | str | None = saved.get(cal["key"], by_calendar.get(cal["id"], owners.get(cal["account_id"])))
        if found is not None:
            links[cal["key"]] = found
    return links


async def set_people_links(db, links: dict[str, int | str]) -> None:
    """{"<account id>:<calendar id>": owner}."""
    await set_setting(db, PEOPLE_KEY, json.dumps(links))
    await db.commit()


# --- The "primary" alias (spec 12.3) ------------------------------------------------------


async def resolve_primary(db, account_id: int, real_id: str) -> None:
    """Writes `real_id` wherever this account's settings say "primary": the
    selection (dropping the alias if the real id is already shown), the
    Family and school events calendars, and a "primary" person link."""
    saved = await get_saved_calendars(db)
    if saved is None and await get_setting(db, SELECTED_CALENDARS_SETTING) is None:
        saved = [{**cal} for cal in await get_selected_calendars(db)]  # the default, made explicit
    if saved:
        shown = {(cal["account_id"], cal["id"]) for cal in saved}
        resolved = []
        for cal in saved:
            if cal["account_id"] == account_id and cal["id"] == "primary":
                if (account_id, real_id) in shown:
                    continue
                cal = {**cal, "id": real_id, "primary": True}
            resolved.append(cal)
        if resolved != saved:
            await set_selected_calendars(db, resolved)
    for key in (FAMILY_KEY, SCHOOL_EVENTS_KEY):
        value = one_calendar(_load(await get_setting(db, key)))
        if value and value["account_id"] == account_id and value["id"] == "primary":
            await set_setting(db, key, one_calendar_value({**value, "id": real_id}))
    links = await get_saved_people_links(db)
    alias, real = calendar_key(account_id, "primary"), calendar_key(account_id, real_id)
    if alias in links and real not in links:
        links[real] = links.pop(alias)
        await set_setting(db, PEOPLE_KEY, json.dumps(links))
    await db.commit()
