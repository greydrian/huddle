"""
Appearance: the light "day" palette, the dark "night" palette, and the
person-colour ink helper.

Auto mode is night from 19:00 to 07:00 in the family's timezone (the
connected calendar's, see database.family_timezone) — never the container's
clock, which is UTC. Pages put the result on <html data-mode="day|night">
and a small script in base.html re-asks /api/appearance when the next
switch is due, so the long-lived kiosk page flips without a reload.
"""

from datetime import datetime, time, timedelta

from fastapi import APIRouter

from app.database import family_timezone, get_db, get_setting, set_setting

APPEARANCE_SETTING = "appearance"
APPEARANCES = {
    "auto": "Auto — light by day, dark from 19:00 to 07:00",
    "light": "Always light",
    "dark": "Always dark",
}
NIGHT_STARTS = time(19, 0)
NIGHT_ENDS = time(7, 0)

router = APIRouter()


def mode_at(now: datetime, appearance: str = "auto") -> tuple[str, datetime | None]:
    """("day" | "night", when that next changes) for a tz-aware local `now`.
    The switch time is None when the appearance is pinned light or dark."""
    if appearance == "light":
        return "day", None
    if appearance == "dark":
        return "night", None
    tz, today, clock = now.tzinfo, now.date(), now.timetz().replace(tzinfo=None)
    if NIGHT_ENDS <= clock < NIGHT_STARTS:
        return "day", datetime.combine(today, NIGHT_STARTS, tzinfo=tz)
    if clock >= NIGHT_STARTS:
        return "night", datetime.combine(today + timedelta(days=1), NIGHT_ENDS, tzinfo=tz)
    return "night", datetime.combine(today, NIGHT_ENDS, tzinfo=tz)


async def get_appearance(db) -> str:
    value = await get_setting(db, APPEARANCE_SETTING, "auto")
    return value if value in APPEARANCES else "auto"


async def set_appearance(db, value: str):
    await set_setting(db, APPEARANCE_SETTING, value)
    await db.commit()


async def current_mode(db, now: datetime | None = None) -> dict:
    """Template context for base.html: the mode now and the whole seconds
    until it next changes (None = never). Seconds rather than a timestamp,
    so the tablet only measures elapsed time and its own clock/timezone
    settings can't shift the switch."""
    tz = await family_timezone(db)
    now = (now or datetime.now(tz)).astimezone(tz)
    mode, switch_at = mode_at(now, await get_appearance(db))
    switch_in = None
    if switch_at is not None:
        # Compare in UTC: across a DST change the local wall-clock gap lies.
        switch_in = max(1, int((switch_at.timestamp() - now.timestamp()) + 0.999))
    return {"mode": mode, "switch_in": switch_in}


@router.get("/api/appearance")
async def appearance_now():
    async with get_db() as db:
        return await current_mode(db)


# --- Person colours ---

LIGHT_INK = "#FFFFFF"
DARK_INK = "#1D2127"


def _luminance(hex_colour: str) -> float:
    h = hex_colour.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def person_ink(hex_colour: str) -> str:
    """ "light" or "dark": whichever ink reads better on a solid fill of this
    person's colour. Unparseable colours get light ink (the old default)."""
    try:
        return "light" if contrast(hex_colour, LIGHT_INK) >= contrast(hex_colour, DARK_INK) else "dark"
    except ValueError, IndexError, AttributeError:
        return "light"
