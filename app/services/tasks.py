"""
Family member tasks (Section 4.3, spec 10.4): today's tasks per profile,
time-of-day groups, carry-over of missed one-offs, the kiosk quick-add,
ticking, and the end-of-day reset. Purely functional — no points/rewards.

Every mutation writes SQLite immediately (optimistic, offline-first) and
queues a sync_queue row; app/task_sync.py pushes it to the family member's
linked Google Tasks list on its next scheduled cycle. Only the title and
done state ever reach Google: the schedule (recurrence_rule), the group
(time_of_day) and the carry-over date (due_on) are local-only.
"""

import time as _time
from collections import deque
from datetime import date, datetime, time, timezone

from app import avatars, recurrence
from app.database import family_timezone, family_today, get_setting, set_setting
from app.services import term_dates
from app.task_sync import queue_sync

# --- Time-of-day groups ---------------------------------------------------------------

MORNING, AFTER_SCHOOL, EVENING = "morning", "after_school", "evening"
GROUPS = (MORNING, AFTER_SCHOOL, EVENING)  # in the day's order
GROUP_LABELS = {MORNING: "Morning", AFTER_SCHOOL: "After school", EVENING: "Evening", None: "Any time"}
AFTER_SCHOOL_SETTING = "task_group_after_school_start"
EVENING_SETTING = "task_group_evening_start"
DEFAULT_AFTER_SCHOOL = "12:00"
DEFAULT_EVENING = "18:00"


def parse_hhmm(value) -> time | None:
    """'HH:MM' (24-hour) -> time, or None when it isn't one."""
    try:
        hours, minutes = str(value).strip().split(":")
        if len(hours) not in (1, 2) or len(minutes) != 2:
            return None
        return time(int(hours), int(minutes))
    except ValueError, TypeError:
        return None


def clean_boundaries(after_school_start, evening_start) -> tuple[str, str]:
    """Validated 'HH:MM' pair for the Admin form. Raises ValueError unless
    both are times and Morning < After school < Evening (Morning starts at
    midnight, so After school can't start at 00:00)."""
    a, e = parse_hhmm(after_school_start), parse_hhmm(evening_start)
    if a is None or e is None:
        raise ValueError("format")
    if not (time(0, 0) < a < e):
        raise ValueError("order")
    return a.strftime("%H:%M"), e.strftime("%H:%M")


async def get_group_boundaries(db) -> tuple[str, str]:
    """(After school starts, Evening starts) as 'HH:MM'. A stored pair that
    no longer validates falls back to the defaults rather than breaking the
    widget."""
    raw = (
        await get_setting(db, AFTER_SCHOOL_SETTING, DEFAULT_AFTER_SCHOOL),
        await get_setting(db, EVENING_SETTING, DEFAULT_EVENING),
    )
    try:
        return clean_boundaries(*raw)
    except ValueError:
        return DEFAULT_AFTER_SCHOOL, DEFAULT_EVENING


async def set_group_boundaries(db, after_school_start, evening_start) -> None:
    """Raises ValueError (nothing saved) for a bad pair."""
    a, e = clean_boundaries(after_school_start, evening_start)
    await set_setting(db, AFTER_SCHOOL_SETTING, a)
    await set_setting(db, EVENING_SETTING, e)
    await db.commit()


def group_at(now: time, boundaries: tuple[str, str]) -> str:
    after_school, evening = parse_hhmm(boundaries[0]), parse_hhmm(boundaries[1])
    if after_school is None or evening is None:
        raise ValueError("boundaries")
    if now < after_school:
        return MORNING
    return AFTER_SCHOOL if now < evening else EVENING


def _clock(tz) -> datetime:
    """The family-timezone wall clock (tests freeze it)."""
    return datetime.now(tz)


async def current_group(db) -> str:
    now = _clock(await family_timezone(db))
    return group_at(now.time(), await get_group_boundaries(db))


def clean_group(value) -> str | None:
    """A form's group value -> stored value. '' / 'any' / missing = no
    group; anything else unknown raises ValueError."""
    if value in (None, "", "any"):
        return None
    if value not in GROUPS:
        raise ValueError("group")
    return value


# --- Carry-over -------------------------------------------------------------------------


def late_label(due_on: str | None, today: date) -> str | None:
    """'from yesterday' / 'from Mon' / 'from 3 Sep' for a one-off whose day
    has passed; None when it's due today (or later, or has no date)."""
    if not due_on:
        return None
    try:
        due = date.fromisoformat(due_on)
    except ValueError:
        return None
    days = (today - due).days
    if days <= 0:
        return None
    if days == 1:
        return "from yesterday"
    if days < 7:
        return "from " + recurrence.WEEKDAYS[due.weekday()]
    return f"from {due.day} {due.strftime('%b')}"


# --- The widget -------------------------------------------------------------------------


async def get_profiles_with_tasks(db, today: date | None = None) -> list[dict]:
    """Return profiles, each with today's non-archived tasks attached — a
    recurring task set to specific days only shows on those days, and a
    school-days one only on school days (in the household's timezone, not
    the container's). Each task carries `local_only` (its person has no
    Google list, so it's on this display only) and `late` (a missed one-off
    carried over: "from yesterday")."""
    profiles = [
        avatars.attach(dict(row))
        for row in await (
            await db.execute(f"SELECT {avatars.PROFILE_COLUMNS} FROM profiles ORDER BY sort_order")
        ).fetchall()
    ]

    today = today or await family_today(db)
    weekday = today.weekday()
    rows = await (
        await db.execute("SELECT * FROM tasks WHERE archived = 0 ORDER BY is_completed, created_at")
    ).fetchall()
    # "School days" follows the task owner's school, else the family's (spec 11.2).
    school_days: dict[int | None, bool] = {}
    for row in rows:
        if row["is_recurring"] and recurrence.is_school_days(row["recurrence_rule"]):
            owner = row["profile_id"]
            if owner not in school_days:
                school_days[owner] = await term_dates.person_school_day(db, today, owner)
    tasks = [
        dict(row)
        for row in rows
        if not row["is_recurring"]
        or recurrence.is_due(row["recurrence_rule"], weekday, school_days.get(row["profile_id"]))
    ]

    for profile in profiles:
        profile["linked"] = bool(profile["google_tasklist_id"])
        profile["tasks"] = [t for t in tasks if t["profile_id"] == profile["id"]]
        for task in profile["tasks"]:
            task["local_only"] = not profile["linked"]
            task["late"] = None if task["is_recurring"] or task["is_completed"] else late_label(task["due_on"], today)

    return profiles


def _group_of(task: dict) -> str | None:
    return task["time_of_day"] if task["time_of_day"] in GROUPS else None


def build_sections(profiles: list[dict], current: str) -> list[dict]:
    """The widget's sections. With no task in any group it's one headless
    section (the plain per-person list). Otherwise: the current group first
    (always shown, highlighted), then earlier groups (their unfinished tasks
    still to do), then "Any time" (doable now too), then later groups. Only
    groups with a task today appear, apart from the current one."""
    if not any(_group_of(t) for p in profiles for t in p["tasks"]):
        return [
            {"key": None, "label": None, "current": False, "earlier": False, "headless": True, "profiles": profiles}
        ]

    now = GROUPS.index(current)
    order = [current, *GROUPS[:now], None, *GROUPS[now + 1 :]]
    sections = []
    for key in order:
        members = []
        for p in profiles:
            tasks = [t for t in p["tasks"] if _group_of(t) == key]
            if tasks:
                members.append({**p, "tasks": tasks})
        if members or key == current:
            sections.append(
                {
                    "key": key,
                    "label": GROUP_LABELS[key],
                    "current": key == current,
                    "earlier": key in GROUPS and GROUPS.index(key) < now,
                    "headless": False,
                    "profiles": members,
                }
            )
    return sections


async def widget_context(db) -> dict:
    """Everything widgets/tasks.html needs, for the dashboard (widgets.py)
    and the widget's own routes alike. `current_group` is part of it, so the
    /api/rev hash (and with it the highlight) moves at each boundary."""
    profiles = await get_profiles_with_tasks(db)
    group = await current_group(db)
    return {
        "profiles": profiles,
        "current_group": group,
        "task_sections": build_sections(profiles, group),
        "task_groups": [(g, GROUP_LABELS[g]) for g in GROUPS],
    }


# --- Quick add (PIN-free, from the widget) -----------------------------------------------

MAX_TITLE = 200
QUICK_ADD_LIMIT = 30  # adds per QUICK_ADD_WINDOW, for the whole kiosk
QUICK_ADD_WINDOW = 3600  # seconds
_recent_adds: deque[float] = deque()  # monotonic times of recent adds (one process)

QUICK_ADD_ERRORS = {
    "title": "Give the task a name (up to 200 characters).",
    "person": "Choose who the task is for.",
    "group": "Choose a time of day, or Any time.",
    "rate": "That's a lot of new tasks in one go. Try again in a little while.",
}


class QuickAddError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.message = QUICK_ADD_ERRORS[code]


def reset_rate_limit() -> None:
    _recent_adds.clear()


def _rate_limited(now: float) -> bool:
    while _recent_adds and now - _recent_adds[0] >= QUICK_ADD_WINDOW:
        _recent_adds.popleft()
    return len(_recent_adds) >= QUICK_ADD_LIMIT


async def quick_add(db, profile_id, title: str, time_of_day=None) -> int:
    """Add a one-off task for today. It's queued for Google when its person
    has a linked list (the push inserts it once and stores its Google id);
    otherwise it stays on this display only, and relink_profile backfills it
    if a list is linked later. Raises QuickAddError."""
    title = (title or "").strip()
    if not title or len(title) > MAX_TITLE:
        raise QuickAddError("title")
    try:
        pid = int(profile_id)
    except TypeError, ValueError:
        raise QuickAddError("person") from None
    profile = await (await db.execute("SELECT id, google_tasklist_id FROM profiles WHERE id = ?", (pid,))).fetchone()
    if profile is None:
        raise QuickAddError("person")
    try:
        group = clean_group(time_of_day)
    except ValueError:
        raise QuickAddError("group") from None
    # Check and reserve the slot with no await in between, so concurrent
    # adds can't all pass the check before any of them is counted.
    now = _time.monotonic()
    if _rate_limited(now):
        raise QuickAddError("rate")
    _recent_adds.append(now)
    try:
        cursor = await db.execute(
            """INSERT INTO tasks (profile_id, title, is_recurring, time_of_day, due_on, updated_at)
               VALUES (?, ?, 0, ?, ?, datetime('now'))""",
            (pid, title, group, (await family_today(db)).isoformat()),
        )
        task_id = cursor.lastrowid
        if task_id is None:
            raise RuntimeError("insert returned no row id")
        if profile["google_tasklist_id"]:
            await queue_sync(db, "tasks", {"task_id": task_id})
        await db.commit()
    except BaseException:
        try:
            _recent_adds.remove(now)  # nothing was added: give the slot back
        except ValueError:
            pass  # already aged out of the window
        raise
    return task_id


# --- Admin ------------------------------------------------------------------------------


async def get_admin_tasks(db) -> list[dict]:
    """Every live task for Admin's schedule panel, with a readable schedule
    and the day chips to pre-tick in the edit form (empty = every day, a
    one-off, or school days)."""
    tasks = [
        dict(r)
        for r in await (
            await db.execute(
                "SELECT tasks.* FROM tasks "
                "JOIN profiles ON profiles.id = tasks.profile_id "
                "WHERE archived = 0 ORDER BY profiles.sort_order, tasks.created_at"
            )
        ).fetchall()
    ]
    for task in tasks:
        task["schedule"] = recurrence.describe(task["recurrence_rule"]) if task["is_recurring"] else None
        task["school_days"] = bool(task["is_recurring"]) and recurrence.is_school_days(task["recurrence_rule"])
        days = (
            recurrence.parse_rule(task["recurrence_rule"]) if task["is_recurring"] and not task["school_days"] else None
        )
        task["days"] = [recurrence.WEEKDAYS[i] for i in sorted(days)] if days else []
        task["group_label"] = GROUP_LABELS[_group_of(task)] if _group_of(task) else None
    return tasks


async def toggle_task(db, task_id: int) -> bool | None:
    """Tick or untick a task and queue it for sync. Returns the new
    completed state, or None if there's no such task."""
    cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    task = await cursor.fetchone()
    if task is None:
        return None

    now_completed = not bool(task["is_completed"])
    completed_at = datetime.now(timezone.utc).isoformat() if now_completed else None

    await db.execute(
        "UPDATE tasks SET is_completed = ?, completed_at = ?, updated_at = datetime('now') WHERE id = ?",
        (int(now_completed), completed_at, task_id),
    )

    await queue_sync(db, "tasks", {"task_id": task_id, "is_completed": now_completed})
    await db.commit()
    return now_completed


async def _update_and_queue(db, where: str, set_clause: str) -> int:
    """Apply a bulk change and queue each affected task for Google sync —
    a bare UPDATE would leave Google out of step until someone touched each
    task again (and last-write-wins could then undo the reset)."""
    rows = await (await db.execute(f"SELECT id FROM tasks WHERE {where}")).fetchall()
    for row in rows:
        await db.execute(f"UPDATE tasks SET {set_clause}, updated_at = datetime('now') WHERE id = ?", (row["id"],))
        await queue_sync(db, "tasks", {"task_id": row["id"]})
    return len(rows)


async def archive_completed_one_off_tasks(db) -> int:
    """End-of-day cleanup (spec 4.3): ticked one-off tasks are removed from
    view — archived locally, deleted from Google on the next sync. Unticked
    one-offs are left alone: they carry over, labelled late from their
    due_on (spec 10.4), keeping the same row and Google id."""
    return await _update_and_queue(db, "is_recurring = 0 AND is_completed = 1 AND archived = 0", "archived = 1")


async def reset_recurring_tasks(db) -> int:
    """Start of a new day: untick every recurring task."""
    return await _update_and_queue(
        db, "is_recurring = 1 AND is_completed = 1 AND archived = 0", "is_completed = 0, completed_at = NULL"
    )


async def run_daily_reset_if_due(db) -> bool:
    """Runs the end-of-day reset once per family-local calendar day.

    Called often by the scheduler rather than as a midnight cron job, so a
    reset missed while the server was off still happens on the next check,
    and "midnight" follows the household's timezone, not the container's
    UTC. The very first check only records today — resetting then would
    wipe ticks made earlier on the day the feature was switched on."""
    today = (await family_today(db)).isoformat()
    last = await get_setting(db, "last_daily_reset")
    if last == today:
        return False
    if last is not None:
        await archive_completed_one_off_tasks(db)
        await reset_recurring_tasks(db)
    await set_setting(db, "last_daily_reset", today)
    await db.commit()
    return last is not None
