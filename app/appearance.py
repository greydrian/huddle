"""
Appearance: the light "day" palette, the dark "night" palette, and the
person-colour ink helper.

Auto mode is night from **sunset to sunrise** at the weather location
(spec 11.4), read from the saved Open-Meteo forecast (services/weather.
sun_times: never a request from here). Without usable sun times it falls
back to 19:00–07:00, and says why: each fallback reason is logged once a
day and the latest is kept for Admin → Display → Appearance.

Times are in the family's timezone (the connected calendar's, see
database.family_timezone), never the container's clock, which is UTC.
Pages put the result on <html data-mode="day|night"> and a small script in
base.html re-asks /api/appearance when the next switch is due, so the
long-lived kiosk page flips without a reload.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter

from app.database import family_timezone, get_db, get_setting, set_setting
from app.services import weather

logger = logging.getLogger(__name__)

APPEARANCE_SETTING = "appearance"
APPEARANCES = {
    "auto": "Auto — dark from sunset to sunrise (19:00 to 07:00 until a forecast has them)",
    "light": "Always light",
    "dark": "Always dark",
}
# The fallback night, used whenever sunset and sunrise aren't available.
NIGHT_STARTS = time(19, 0)
NIGHT_ENDS = time(7, 0)

# The most recent fallback, for Admin: {"date": ISO date, "reason": code}.
FALLBACK_SETTING = "appearance_fallback"
FALLBACK_REASONS = {
    "no_location": "no weather location is set (Admin → Display → Weather)",
    "no_forecast": "there's no saved forecast with sunrise and sunset yet",
    "stale_forecast": "the saved forecast doesn't cover today (Open-Meteo unreachable for days?)",
    "bad_times": "the forecast's sunrise and sunset for today didn't make sense",
}
# (family date, reason) pairs already logged by this process: once a day each.
_logged: set[tuple[date, str]] = set()

router = APIRouter()


@dataclass(frozen=True)
class SunNight:
    """Today's sunrise and sunset, and tomorrow's sunrise if known."""

    sunrise: datetime
    sunset: datetime
    next_sunrise: datetime | None = None


def mode_at(now: datetime, appearance: str = "auto", sun: SunNight | None = None) -> tuple[str, datetime | None]:
    """("day" | "night", when that next changes) for a tz-aware local `now`.
    The switch time is None when the appearance is pinned light or dark.
    Auto follows `sun` when given, else the fixed 19:00–07:00 night."""
    if appearance == "light":
        return "day", None
    if appearance == "dark":
        return "night", None
    tz, today, clock = now.tzinfo, now.date(), now.timetz().replace(tzinfo=None)
    if sun is not None:
        if now < sun.sunrise:
            return "night", sun.sunrise
        if now < sun.sunset:
            return "day", sun.sunset
        if sun.next_sunrise is not None and sun.next_sunrise > sun.sunset:
            return "night", sun.next_sunrise
        return "night", datetime.combine(today + timedelta(days=1), NIGHT_ENDS, tzinfo=tz)
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


def _plausible(rise: datetime, set_: datetime, day: date) -> bool:
    """Both on `day` (in the forecast's own zone), sunrise first, and a day
    length a UK-ish household could see: 4 to 20 hours. Anything else is a
    bad forecast."""
    return rise.date() == day == set_.date() and timedelta(hours=4) <= set_ - rise <= timedelta(hours=20)


async def sun_night(db, now: datetime) -> tuple[SunNight | None, str | None]:
    """(today's sun times, None), or (None, fallback reason code)."""
    try:
        days = await weather.sun_times(db)
    except weather.SunUnavailable as exc:
        return None, exc.code
    # The forecast's days are the weather location's own dates, so "today"
    # is today there, not in the family's timezone (which is UTC with no
    # calendar connected: just before midnight UTC it's already tomorrow in
    # a British summer). The times are absolute, so they compare fine.
    zone = next(iter(days.values()))[0].tzinfo
    today = now.astimezone(zone).date()
    if today not in days:
        return None, "stale_forecast"
    rise, set_ = days[today]
    if not _plausible(rise, set_, today):
        return None, "bad_times"
    tomorrow = days.get(today + timedelta(days=1))
    next_rise = tomorrow[0] if tomorrow and _plausible(*tomorrow, today + timedelta(days=1)) else None
    tz = now.tzinfo
    return SunNight(rise.astimezone(tz), set_.astimezone(tz), next_rise.astimezone(tz) if next_rise else None), None


async def _note_fallback(db, today: date, reason: str) -> None:
    """Log each fallback reason once a day (no_location, a setup choice, at
    INFO; the rest at WARNING) and keep the latest for Admin."""
    if (today, reason) not in _logged:
        _logged.add((today, reason))
        level = logging.INFO if reason == "no_location" else logging.WARNING
        logger.log(level, "Night mode using the fixed 19:00-07:00 night today: %s", reason)
    record = json.dumps({"date": today.isoformat(), "reason": reason})
    if await get_setting(db, FALLBACK_SETTING) != record:
        await set_setting(db, FALLBACK_SETTING, record)
        await db.commit()


async def current_mode(db, now: datetime | None = None) -> dict:
    """Template context for base.html: the mode now and the whole seconds
    until it next changes (None = never). Seconds rather than a timestamp,
    so the tablet only measures elapsed time and its own clock/timezone
    settings can't shift the switch."""
    tz = await family_timezone(db)
    now = (now or datetime.now(tz)).astimezone(tz)
    appearance = await get_appearance(db)
    sun = None
    if appearance == "auto":
        sun, reason = await sun_night(db, now)
        if reason:
            await _note_fallback(db, now.date(), reason)
    mode, switch_at = mode_at(now, appearance, sun)
    switch_in = None
    if switch_at is not None:
        # Compare in UTC: across a DST change the local wall-clock gap lies.
        switch_in = max(1, int((switch_at.timestamp() - now.timestamp()) + 0.999))
    return {"mode": mode, "switch_in": switch_in}


async def night_source(db, now: datetime | None = None) -> dict:
    """For Admin → Display → Appearance: where tonight's night comes from,
    and the most recent fallback. {"kind": "sun", "sunset", "sunrise"} or
    {"kind": "fixed", "reason", "why"}, plus "last_fallback": {"date",
    "reason", "why"} or None."""
    tz = await family_timezone(db)
    now = (now or datetime.now(tz)).astimezone(tz)
    sun, reason = await sun_night(db, now)
    if sun is not None:
        source: dict = {"kind": "sun", "sunset": sun.sunset.strftime("%H:%M"), "sunrise": sun.sunrise.strftime("%H:%M")}
    else:
        source = {"kind": "fixed", "reason": reason, "why": FALLBACK_REASONS.get(reason or "", reason)}
    try:
        last = json.loads(await get_setting(db, FALLBACK_SETTING) or "null")
        if last and last.get("reason") in FALLBACK_REASONS:
            last["why"] = FALLBACK_REASONS[last["reason"]]
            day = date.fromisoformat(last["date"])
            last["label"] = f"{day:%a} {day.day} {day:%b}"  # no %-d: glibc-only
        else:
            last = None
    except ValueError, TypeError, AttributeError:
        last = None
    source["last_fallback"] = last
    return source


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
