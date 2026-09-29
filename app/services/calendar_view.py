"""
The calendar widget's state and template context (spec 10.5): which view
(month, week, agenda, or one day), which period, the person filter, and the
"+" add form. Used by the dashboard's registry loader and by every
/widgets/calendar route, so both build exactly the same context.

State travels in each view's URL (see url()). The widget's root polls
/widgets/calendar, which is the Admin default view for today, unfiltered:
every re-render restarts that timer, so after IDLE_SECONDS without a tap the
widget goes back to its default view and drops the filter (calendar.html
holds the poll while the add form is open or being typed in).

The person filter (event_owners): a calendar linked to a family member in
Admin holds only their events. Any other calendar ("Everyone", or not
linked) is shared: its events belong to whoever the title names (a "Name:"
prefix or a first name as a whole word, services/people), else to
everyone. The school's term dates count for everyone.
"""

import secrets
from datetime import date, timedelta
from urllib.parse import urlencode

from app import google_calendar, google_oauth
from app.database import family_today
from app.services import calendar_add, calendar_prefs, people

IDLE_SECONDS = 180
VIEWS = ("month", "week", "agenda", "day")
ADD_DAYS = 60                  # how far ahead the add form's day list reaches
ADD_TIMES = (6, 23)            # the add form's time list: 06:00 .. 22:45
ADD_STEP = 15                  # minutes


# --- Ownership (the person filter) --------------------------------------------------------

def event_owners(event: dict, links: dict, profiles: list[dict]) -> set[int] | None:
    """The profile ids an event belongs to, or None for everyone."""
    ids = {p["id"] for p in profiles}
    owner = links.get(event.get("calendar_id"))
    if isinstance(owner, int) and owner in ids:
        return {owner}
    named = people.named_people(event.get("title") or "", profiles)
    return {p["id"] for p in named} if named else None


def person_filter(person_id: int | None, links: dict, profiles: list[dict]):
    """A google_calendar Keep for one person's view (None = everyone's)."""
    if person_id is None:
        return None

    def keep(event: dict) -> bool:
        owners = event_owners(event, links, profiles)
        return owners is None or person_id in owners
    return keep


# --- State and URLs -----------------------------------------------------------------------

def url(state: dict) -> str:
    """The route that renders `state`: {"view", and per view "year"/"month",
    "start" (a week's Monday), "date" and "back" (the day view), "person"}."""
    view = state.get("view")
    params: dict = {}
    if view == "day":
        path = f"/widgets/calendar/day/{state['date']}"
        if state.get("back") in ("week", "agenda"):
            params["back"] = state["back"]
    else:
        path = "/widgets/calendar"
        params["view"] = view
        if view == "month" and state.get("year"):
            params.update(year=state["year"], month=state["month"])
        elif view == "week" and state.get("start"):
            params["start"] = state["start"]
    if state.get("person") is not None:
        params["person"] = state["person"]
    return path + ("?" + urlencode(params) if params else "")


def parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def parse_person(value, profiles: list[dict]) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if any(p["id"] == pid for p in profiles) else None


def _day_choices(today: date) -> list[tuple[str, str]]:
    choices = []
    for offset in range(ADD_DAYS):
        day = today + timedelta(days=offset)
        label = "Today" if offset == 0 else "Tomorrow" if offset == 1 else f"{day:%a} {day.day} {day:%b}"
        choices.append((day.isoformat(), label))
    return choices


def _time_choices() -> list[str]:
    return [f"{h:02d}:{m:02d}" for h in range(*ADD_TIMES) for m in range(0, 60, ADD_STEP)]


async def _profiles(db) -> list[dict]:
    return [dict(r) for r in await (await db.execute(
        "SELECT id, name, colour_hex FROM profiles ORDER BY sort_order"
    )).fetchall()]


async def can_add(db) -> bool:
    """Whether the wall shows its "+": connected, a Family calendar chosen
    (and still shown), and the calendar.events scope granted."""
    return (await calendar_prefs.get_family_calendar(db) is not None
            and await google_oauth.has_scope(db, google_oauth.CALENDAR_EVENTS_SCOPE))


async def widget_context(
    db,
    view: str | None = None,
    *,
    year: int | None = None,
    month: int | None = None,
    start: date | None = None,
    day: date | None = None,
    back: str | None = None,
    person: int | None = None,
    add_form: dict | None = None,
    add_error: str | None = None,
    added: dict | None = None,
) -> dict:
    """Everything calendar.html needs. With no arguments: the Admin default
    view for today, unfiltered (what the dashboard and the idle poll show).
    A year/month without a view is the month view (older links)."""
    default_view = await calendar_prefs.get_default_view(db)
    if view is None:
        view = "month" if year or month else default_view
    profiles = await _profiles(db)
    person = person if any(p["id"] == person for p in profiles) else None
    keep = person_filter(person, await calendar_prefs.get_people_links(db), profiles)

    context: dict = {"view": view, "calendar_month": None, "calendar_week": None, "calendar_agenda": None,
                     "day_events": None}
    state: dict = {"view": view, "person": person}
    if view == "week":
        if start is not None:
            start = google_calendar.week_start(start)
        week = await google_calendar.get_week(db, start, keep)
        context["calendar_week"] = week
        if week:
            state["start"] = week["start"]
        connected, title = week is not None, week and week["label"]
        current = bool(week and week["is_current_week"])
    elif view == "agenda":
        agenda = await google_calendar.get_agenda(db, keep)
        context["calendar_agenda"] = agenda
        connected, title, current = agenda is not None, "Coming up", True
    elif view == "day":
        target = day or await family_today(db)
        loaded = await google_calendar.get_day_events(db, target, keep)
        back = back if back in ("month", "week", "agenda") else "month"
        state.update(date=target.isoformat(), back=back)
        context.update({
            "day_date": target.isoformat(),
            "day_year": target.year,
            "day_month": target.month,
            "day_label": target.strftime("%A, %d %B").replace(" 0", " "),  # no leading zero, cross-platform
            "day_events": loaded["events"] if loaded else None,
            "day_offline": bool(loaded and loaded["offline"]),
            "day_updated": loaded["updated_label"] if loaded else None,
        })
        # Back to the view the day was opened from, around that day.
        back_state = {"view": back, "person": person}
        if back == "month":
            back_state.update(year=target.year, month=target.month)
        elif back == "week":
            back_state["start"] = google_calendar.week_start(target).isoformat()
        context["day_back_url"] = url(back_state)
        connected, title, current = loaded is not None, context["day_label"], False
    else:
        grid = await google_calendar.get_month_grid(db, year, month, keep)
        context["calendar_month"] = grid
        if grid:
            state.update(year=grid["year"], month=grid["month"])
        connected, title = grid is not None, grid and grid["label"]
        current = bool(grid and grid["is_current_month"])

    today = await family_today(db)
    adding = connected and await can_add(db)
    family = await calendar_prefs.get_family_calendar(db) if adding else None
    person_urls = {p["id"]: url({**state, "person": None if person == p["id"] else p["id"]}) for p in profiles}
    view_urls = {v: url({"view": v, "person": person}) for v in calendar_prefs.VIEWS}
    form_day = (add_form or {}).get("day") or (context.get("day_date") if view == "day" else None)
    context["cal"] = {
        "connected": connected,
        "title": title or "Calendar",
        "state": state,
        "state_url": url(state),
        "is_default": view == default_view and person is None and current,
        "default_view": default_view,
        "views": calendar_prefs.VIEWS,
        "view_urls": view_urls,
        "profiles": profiles,
        "person": person,
        "person_urls": person_urls,
        # A day's link is "/widgets/calendar/day/<date>" + this.
        "day_suffix": url({"view": "day", "date": "", "back": view if view != "day" else back,
                           "person": person}).removeprefix("/widgets/calendar/day/"),
        "idle_seconds": IDLE_SECONDS,
        "can_add": adding,
        "family": family,
        "add_open": bool(add_error),
        "add_form": add_form or {},
        "add_error": add_error,
        "added": added,
        "request_key": (add_form or {}).get("request_key") or secrets.token_urlsafe(16),
        "fresh_key": secrets.token_urlsafe(16),  # swapped in once a refused form is edited
        "day_choices": _day_choices(today) if adding else [],
        "form_day": form_day or today.isoformat(),
        "time_choices": _time_choices() if adding else [],
        "max_title": calendar_add.MAX_TITLE,
    }
    return context
