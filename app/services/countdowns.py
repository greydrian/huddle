"""Countdowns (spec 11.8): "Half term in 12 days", "Trip to Gran in 3 days",
shown in the "next up" strip under the banners.

Two sources:
- each family school's next break (a half term or holiday) from its term
  dates (services/term_dates.py), within BREAK_HORIZON days, unless
  switched off in Admin;
- family dates added in Admin → Display → Countdowns (the `countdowns`
  table), each optionally one person's.

The nearest SHOWN are shown; a date that has passed simply stops showing
(Admin lists it greyed until someone deletes it).
"""

from datetime import date, timedelta

from app import avatars
from app.database import get_setting, set_setting
from app.services import term_dates

SHOWN = 3
BREAK_HORIZON = 60  # days: further off, a school break isn't worth the space
MAX_AHEAD = 2 * 366  # days: a date further off is almost certainly a typo
MAX_TITLE = 60
MAX_COUNTDOWNS = 50  # the table is Admin-only, but keep it bounded
BREAKS_SETTING = "countdown_school_breaks"  # "1" (default) or "0"
BREAK_KINDS = ("half_term", "holiday")


class CountdownError(ValueError):
    """A countdown that can't be saved; str() is its ADMIN_ERRORS code."""


def in_days(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


async def breaks_enabled(db) -> bool:
    return await get_setting(db, BREAKS_SETTING, "1") != "0"


async def set_breaks_enabled(db, enabled: bool) -> None:
    await set_setting(db, BREAKS_SETTING, "1" if enabled else "0")
    await db.commit()


async def list_all(db) -> list[dict]:
    """Every saved countdown, soonest first, with its person's name (for Admin)."""
    rows = await (
        await db.execute(
            """SELECT c.id, c.title, c.target_date, c.profile_id, p.name AS person
               FROM countdowns c LEFT JOIN profiles p ON p.id = c.profile_id
               ORDER BY c.target_date, c.id"""
        )
    ).fetchall()
    return [dict(r) for r in rows]


async def add(db, title: str, target: str, profile_id: str, today: date) -> int:
    """Saves a countdown; raises CountdownError(code) for a bad form."""
    clean_title = (title or "").strip()
    if not clean_title or len(clean_title) > MAX_TITLE:
        raise CountdownError("countdown-title")
    try:
        when = date.fromisoformat((target or "").strip())
    except ValueError as exc:
        raise CountdownError("countdown-date") from exc
    if not today <= when <= today + timedelta(days=MAX_AHEAD):
        raise CountdownError("countdown-date")
    person = None
    if (profile_id or "").strip():
        row = None
        if profile_id.strip().isdigit():
            row = await (await db.execute("SELECT id FROM profiles WHERE id = ?", (int(profile_id),))).fetchone()
        if row is None:
            raise CountdownError("countdown-person")
        person = row["id"]
    (count,) = await (await db.execute("SELECT COUNT(*) FROM countdowns")).fetchone()
    if count >= MAX_COUNTDOWNS:
        raise CountdownError("countdown-full")
    cursor = await db.execute(
        "INSERT INTO countdowns (title, target_date, profile_id) VALUES (?, ?, ?)",
        (clean_title, when.isoformat(), person),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def delete(db, countdown_id: int) -> None:
    await db.execute("DELETE FROM countdowns WHERE id = ?", (countdown_id,))
    await db.commit()


async def _next_breaks(db, today: date) -> list[dict]:
    """Each family school's next half term or holiday starting after today,
    within the horizon (spec 11.2: one per school, named when there's more
    than one)."""
    horizon = today + timedelta(days=BREAK_HORIZON)
    school_ids = await term_dates.family_ids(db)
    names = {r["id"]: r["name"] for r in await (await db.execute("SELECT id, name FROM schools")).fetchall()}
    found = []
    for school_id in school_ids:
        for period in await term_dates.list_periods(db, school_id):  # by start date
            if period["kind"] not in BREAK_KINDS:
                continue
            start = date.fromisoformat(period["start_date"])
            if today < start <= horizon:
                title = (period.get("label") or "").strip() or term_dates.KINDS[period["kind"]]
                if len(names) > 1:
                    title = f"{names[school_id]}: {title}"
                found.append({"key": f"break{school_id}", "title": title, "date": start, "person": None})
                break
    return found


async def upcoming(db, today: date) -> list[dict]:
    """The nearest SHOWN countdowns from today: {"key", "title", "when" ("in 12
    days"), "days", "person" (name, colour, avatar) or None, "school"}."""
    found = []
    if await breaks_enabled(db):
        found += await _next_breaks(db, today)
    rows = await (
        await db.execute(
            f"""SELECT c.id, c.title, c.target_date, p.id AS pid, p.name, p.colour_hex, {avatars.columns("p")}
                FROM countdowns c LEFT JOIN profiles p ON p.id = c.profile_id
                WHERE c.target_date >= ? ORDER BY c.target_date, c.id""",
            (today.isoformat(),),
        )
    ).fetchall()
    for row in rows:
        person = None
        if row["pid"] is not None:
            profile = dict(row) | {"id": row["pid"]}
            person = {"name": row["name"], "colour": row["colour_hex"], "avatar": avatars.avatar_of(profile)}
        found.append(
            {
                "key": f"c{row['id']}",
                "title": row["title"],
                "date": date.fromisoformat(row["target_date"]),
                "person": person,
            }
        )
    found.sort(key=lambda c: c["date"])
    result = []
    for c in found[:SHOWN]:
        days = (c["date"] - today).days
        result.append(
            {
                "key": c["key"],
                "title": c["title"],
                "days": days,
                "when": in_days(days),
                "person": c["person"],
                "school": c["key"].startswith("break"),
            }
        )
    return result
