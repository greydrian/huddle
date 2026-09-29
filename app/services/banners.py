"""
Notification banner (spec 10.1): a slim bar under the top bar saying what's
coming up, without anyone opening a widget.

Four triggers, each switched on or off (and its chime) in Admin → Display →
Banners:
- events: a timed event on a selected calendar, from its lead time before it
  starts until it starts. The lead time is the event's smallest popup
  reminder (or its calendar's defaultReminders, with useDefault), else the
  Admin default, capped at MAX_LEAD. Read from calendar_cache only (kept
  fresh by the month grid and a 5-minute job), so it works the same while
  Google is unreachable, and polling it never calls Google.
- homework: "due tomorrow" from 16:00 the day before, "due today" from 07:00,
  until it's ticked (the widget's own visibility rules).
- school: today's approved school-inbox events, INSET days and closures,
  07:00–18:00.
- chores: once the Evening task group has started, "N chores still to do"
  per person with unfinished tasks on today's list.

Quiet hours (default 21:00–07:00, separate from night mode) show nothing.
Tapping a banner dismisses that occurrence: its key goes into app_settings
(JSON, pruned after DISMISS_KEEP), so it stays dismissed across reloads.
Every time is the family's (a tz-aware `now`), never the container's UTC.
"""

import json
import math
import re
from datetime import UTC, datetime, time, timedelta

from app import google_calendar
from app.database import family_timezone, get_setting, set_setting
from app.services import homework, term_dates
from app.services import tasks as task_service

SETTINGS_KEY = "banner_settings"
DISMISSED_KEY = "banner_dismissed"

TRIGGERS = {
    "events": "Events starting soon",
    "homework": "Homework due",
    "school": "Today's school events",
    "chores": "Chores left late in the day",
}
DEFAULT_LEAD = 30          # minutes, for an event with no popup reminder
MIN_LEAD, MAX_LEAD = 5, 120  # the Admin setting's range; MAX_LEAD also caps an event's own reminder
DEFAULT_QUIET = ("21:00", "07:00")
SHOWN = 2                  # banners on show before "+N more"

HOMEWORK_TOMORROW_FROM = time(16, 0)
DAY_STARTS = time(7, 0)    # "due today" and school events show from here
SCHOOL_UNTIL = time(18, 0)
SCHOOL_PERIOD_KINDS = ("inset", "closure")  # term-date periods that make a banner

DISMISS_KEEP = timedelta(days=2)
MAX_DISMISSED = 300        # the kiosk route is PIN-free: never let the list grow unbounded
MAX_KEY = 200
KEY_PATTERN = re.compile(r"(event|homework|school|term|chores):[^\s]{1,190}")

# Most urgent first: a club in 20 minutes beats tomorrow's spelling.
_RANK = {"events": 0, "school": 1, "homework": 2, "chores": 3}


class SettingsError(ValueError):
    """`code` is an admin.ADMIN_ERRORS key."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- Settings -----------------------------------------------------------------------------

def default_settings() -> dict:
    return {
        "triggers": {kind: {"on": True, "sound": False} for kind in TRIGGERS},
        "lead_minutes": DEFAULT_LEAD,
        "quiet_start": DEFAULT_QUIET[0],
        "quiet_end": DEFAULT_QUIET[1],
    }


def clean_settings(form: dict) -> dict:
    """Admin's form -> settings. Checkboxes are present when ticked. Raises
    SettingsError("banner-lead" | "banner-quiet"); nothing is saved then."""
    try:
        lead = int(str(form.get("lead_minutes", "")).strip())
    except ValueError:
        raise SettingsError("banner-lead") from None
    if not MIN_LEAD <= lead <= MAX_LEAD:
        raise SettingsError("banner-lead")
    start = task_service.parse_hhmm(form.get("quiet_start"))
    end = task_service.parse_hhmm(form.get("quiet_end"))
    if start is None or end is None or start == end:
        raise SettingsError("banner-quiet")
    return {
        "triggers": {kind: {"on": bool(form.get(f"on_{kind}")), "sound": bool(form.get(f"sound_{kind}"))}
                     for kind in TRIGGERS},
        "lead_minutes": lead,
        "quiet_start": start.strftime("%H:%M"),
        "quiet_end": end.strftime("%H:%M"),
    }


async def get_settings(db) -> dict:
    """The saved settings over the defaults; anything unreadable falls back
    to its default rather than breaking the wall."""
    settings = default_settings()
    try:
        saved = json.loads(await get_setting(db, SETTINGS_KEY) or "{}")
    except ValueError:
        saved = {}
    if not isinstance(saved, dict):
        return settings
    for kind, value in (saved.get("triggers") or {}).items():
        if kind in TRIGGERS and isinstance(value, dict):
            settings["triggers"][kind] = {"on": bool(value.get("on", True)), "sound": bool(value.get("sound"))}
    lead = saved.get("lead_minutes")
    if isinstance(lead, int) and MIN_LEAD <= lead <= MAX_LEAD:
        settings["lead_minutes"] = lead
    start, end = task_service.parse_hhmm(saved.get("quiet_start")), task_service.parse_hhmm(saved.get("quiet_end"))
    if start is not None and end is not None and start != end:
        settings["quiet_start"], settings["quiet_end"] = start.strftime("%H:%M"), end.strftime("%H:%M")
    return settings


async def save_settings(db, settings: dict) -> None:
    await set_setting(db, SETTINGS_KEY, json.dumps(settings))
    await db.commit()


def in_quiet_hours(clock: time, start: str, end: str) -> bool:
    """Whether a wall-clock time falls in [start, end), which may run past
    midnight (21:00–07:00)."""
    s, e = task_service.parse_hhmm(start), task_service.parse_hhmm(end)
    if s is None or e is None or s == e:
        return False
    clock = clock.replace(tzinfo=None)
    return s <= clock < e if s < e else (clock >= s or clock < e)


# --- Dismissals ---------------------------------------------------------------------------

def _read_dismissed(raw: str | None) -> dict[str, str]:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(value, dict) else {}


def _prune(dismissed: dict[str, str], now: datetime) -> dict[str, str]:
    """Drop entries older than DISMISS_KEEP (or unreadable), then keep the
    newest MAX_DISMISSED."""
    cutoff = now - DISMISS_KEEP
    kept = {}
    for key, when in dismissed.items():
        try:
            at = datetime.fromisoformat(when)
        except ValueError:
            continue
        if at.tzinfo is not None and at >= cutoff:
            kept[key] = when
    newest = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:MAX_DISMISSED]
    return dict(newest)


async def dismissed_keys(db) -> set[str]:
    return set(_read_dismissed(await get_setting(db, DISMISSED_KEY)))


async def dismiss(db, key: str, now: datetime | None = None) -> bool:
    """Hide one banner occurrence. False (nothing stored) for a key that
    isn't one of ours."""
    key = (key or "").strip()
    if len(key) > MAX_KEY or not KEY_PATTERN.fullmatch(key):
        return False
    now = (now or datetime.now(UTC)).astimezone(UTC)
    dismissed = _read_dismissed(await get_setting(db, DISMISSED_KEY))
    dismissed[key] = now.isoformat()
    await set_setting(db, DISMISSED_KEY, json.dumps(_prune(dismissed, now)))
    await db.commit()
    return True


# --- Triggers -----------------------------------------------------------------------------

async def _profiles(db) -> list[dict]:
    return [dict(r) for r in await (await db.execute(
        "SELECT id, name, colour_hex FROM profiles ORDER BY sort_order"
    )).fetchall()]


def _person(profile: dict | None) -> dict | None:
    return {"name": profile["name"], "colour": profile["colour_hex"]} if profile else None


def _split_person(title: str, profiles: list[dict]) -> tuple[dict | None, str]:
    """"Alanna: Dentist" -> (Alanna's profile, "Dentist") when the part
    before the first colon is a family member's name (quick-add titles, spec
    10.5); otherwise (None, title), i.e. Everyone."""
    name, sep, rest = title.partition(":")
    if sep and rest.strip():
        for profile in profiles:
            if profile["name"].strip().casefold() == name.strip().casefold():
                return profile, rest.strip()
    return None, title


def _in_words(minutes: int) -> str:
    if minutes < 60:
        return f"in {minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"in {hours} h" + (f" {rest} min" if rest else "")


def _banner(kind: str, key: str, text: str, person: dict | None, sort_time: datetime) -> dict:
    return {"kind": kind, "key": key, "text": text, "person": person, "sort_time": sort_time}


async def _event_banners(db, now: datetime, lead_default: int, profiles: list[dict]) -> list[dict]:
    today = now.date()
    events = await google_calendar.cached_events(db, today, today + timedelta(days=2))
    if not events:  # e.g. the cached range ends today
        events = await google_calendar.cached_events(db, today, today + timedelta(days=1))
    banners = {}
    for event in events:
        if event.get("all_day") or event.get("school"):
            continue
        try:
            start = datetime.fromisoformat(event["sort_key"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is None:
            continue
        reminder = event.get("reminder_minutes")
        lead = reminder if isinstance(reminder, int) and reminder >= 0 else lead_default
        lead = min(lead, MAX_LEAD)
        if not start - timedelta(minutes=lead) <= now < start:
            continue
        person, title = _split_person(event.get("title") or "(untitled)", profiles)
        minutes = max(1, math.ceil((start - now).total_seconds() / 60))
        # Google's own offset for the time, never the container's clock.
        text = f"{title} at {start:%H:%M} ({_in_words(minutes)})"
        key = f"event:{event.get('id') or _slug(event.get('title'))}:{event['sort_key']}"
        banners[key] = _banner("events", key, text, _person(person), start)
    return list(banners.values())


def _slug(title: str | None) -> str:
    return re.sub(r"\s+", "-", (title or "untitled").strip())[:60] or "untitled"


async def _homework_banners(db, now: datetime, profiles: list[dict]) -> list[dict]:
    today, clock = now.date(), now.time().replace(tzinfo=None)
    tomorrow = today + timedelta(days=1)
    wanted = []
    if clock >= DAY_STARTS:
        wanted.append((today, "today"))
    if clock >= HOMEWORK_TOMORROW_FROM:
        wanted.append((tomorrow, "tomorrow"))
    if not wanted:
        return []
    by_id = {p["id"]: p for p in profiles}
    rows = await (await db.execute(
        "SELECT * FROM homework WHERE archived = 0 AND done = 0 AND due_date IN (?, ?)",
        (today.isoformat(), tomorrow.isoformat()),
    )).fetchall()
    banners = []
    for row in rows:
        item = dict(row)
        if not homework.is_visible(item, today):
            continue
        for due, when in wanted:
            if item["due_date"] != due.isoformat():
                continue
            subject = f" ({item['subject']})" if item["subject"] else ""
            key = f"homework:{item['id']}:{item['due_date']}:{when}"
            banners.append(_banner(
                "homework", key, f"Homework due {when}: {item['title']}{subject}",
                _person(by_id.get(item["profile_id"])),
                datetime.combine(due, time.min, tzinfo=now.tzinfo),
            ))
    return banners


async def _school_banners(db, now: datetime, profiles: list[dict]) -> list[dict]:
    today, clock = now.date(), now.time().replace(tzinfo=None)
    if not DAY_STARTS <= clock < SCHOOL_UNTIL:
        return []
    by_id = {p["id"]: p for p in profiles}
    day_start = datetime.combine(today, time.min, tzinfo=now.tzinfo)
    banners = []
    rows = await (await db.execute(
        """SELECT id, profile_id, payload_json FROM import_candidates
           WHERE kind = 'event' AND status = 'approved' AND json_valid(payload_json)
             AND json_extract(payload_json, '$.date') = ?""",
        (today.isoformat(),),
    )).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        title = str(payload.get("title") or "School event")
        start = task_service.parse_hhmm(payload.get("start_time")) if not payload.get("all_day") else None
        text = f"Today: {title}" + (f" at {start:%H:%M}" if start else "")
        sort_time = datetime.combine(today, start, tzinfo=now.tzinfo) if start else day_start
        banners.append(_banner("school", f"school:{row['id']}:{today.isoformat()}", text,
                               _person(by_id.get(row["profile_id"])), sort_time))
    for period in await term_dates.periods_between(db, today, today):
        if period["kind"] in SCHOOL_PERIOD_KINDS and period["id"] is not None:
            label = period["label"] or term_dates.KINDS[period["kind"]]
            banners.append(_banner("school", f"term:{period['id']}:{today.isoformat()}",
                                   f"Today: {label} (no school)", None, day_start))
    return banners


async def _chore_banners(db, now: datetime) -> list[dict]:
    evening = task_service.parse_hhmm((await task_service.get_group_boundaries(db))[1])
    if evening is None or now.time().replace(tzinfo=None) < evening:
        return []
    today = now.date()
    banners = []
    for profile in await task_service.get_profiles_with_tasks(db, today):
        left = sum(1 for t in profile["tasks"] if not t["is_completed"])
        if left:
            banners.append(_banner(
                "chores", f"chores:{profile['id']}:{today.isoformat()}",
                f"{left} chore{'' if left == 1 else 's'} still to do", _person(profile),
                datetime.combine(today, evening, tzinfo=now.tzinfo),
            ))
    return banners


async def active_banners(db, now: datetime, settings: dict | None = None) -> list[dict]:
    """Every banner to show at `now` (tz-aware, the family's time), most
    urgent first: events by start, then school events, homework, chores.
    Each is {"kind", "key", "text", "person" ({"name", "colour"} or None for
    Everyone), "sort_time", "sound"}. Empty in quiet hours."""
    settings = settings or await get_settings(db)
    if in_quiet_hours(now.time(), settings["quiet_start"], settings["quiet_end"]):
        return []
    on = {kind for kind, t in settings["triggers"].items() if t["on"]}
    profiles = await _profiles(db)
    banners: list[dict] = []
    if "events" in on:
        banners += await _event_banners(db, now, settings["lead_minutes"], profiles)
    if "school" in on:
        banners += await _school_banners(db, now, profiles)
    if "homework" in on:
        banners += await _homework_banners(db, now, profiles)
    if "chores" in on:
        banners += await _chore_banners(db, now)
    hidden = await dismissed_keys(db)
    banners = [b for b in banners if b["key"] not in hidden]
    for b in banners:
        b["sound"] = settings["triggers"][b["kind"]]["sound"]
    banners.sort(key=lambda b: (_RANK[b["kind"]], b["sort_time"], b["text"]))
    return banners


def _clock(tz) -> datetime:
    """The family-timezone wall clock (tests freeze it)."""
    return datetime.now(tz)


async def context(db, now: datetime | None = None) -> dict:
    """Template context for _banners.html, from the dashboard, its own
    route and /api/rev alike."""
    now = now or _clock(await family_timezone(db))
    return {"banners": await active_banners(db, now), "banners_shown": SHOWN}
