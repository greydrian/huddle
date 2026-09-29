"""
The idle screen (spec 10.2): what the wall does when nobody has touched it
for a while. Huddle owns this, not Fully Kiosk: Fully's own screensaver and
screen-off timer stay OFF, and only overnight screen-off is left to Fully's
wake/sleep schedule (a screen Fully has turned off doesn't wake on a tap).

The browser side is app/static/js/idle.js. This module holds the Admin
settings and what the idle screen shows:
- /api/idle: the settings, the family's time now (the overlay's clock runs
  from it, counting elapsed time on the tablet like fresh.js's date, so the
  tablet's own clock and timezone never show), the next event today (from
  calendar_cache), the current weather (from the weather cache) and the
  slideshow's photos in this week's order. Nothing here calls Google or
  Open-Meteo, so it answers the same offline.
- /photos/{id} and /photos/{id}/thumb: the local copies, by row id only.
"""

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from app import google_calendar, google_photos
from app.database import family_timezone, get_db, get_setting, set_setting
from app.services import tasks as task_service
from app.services import weather

SETTINGS_KEY = "idle_settings"

MODES = {
    "slideshow": "Photo slideshow",
    "dashboard": "Stay on the dashboard",
    "dim": "Dim the screen",
}
DEFAULTS = {
    "mode": "slideshow",
    "night_mode": "dim",
    "delay_minutes": 5,
    "night_start": "",   # blank: night follows Appearance (night mode)
    "night_end": "",
    "dim_percent": 8,
    "interval_seconds": 20,
}
RANGES = {"delay_minutes": (1, 120), "dim_percent": (1, 50), "interval_seconds": (5, 300)}

router = APIRouter()


class SettingsError(ValueError):
    """`code` is an admin.ADMIN_ERRORS key."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _int_in_range(value, key: str) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    low, high = RANGES[key]
    return number if low <= number <= high else None


def clean_settings(form: dict) -> dict:
    """Admin's form -> settings. Raises SettingsError; nothing is saved then."""
    if form.get("mode") not in MODES or form.get("night_mode") not in MODES:
        raise SettingsError("idle-mode")
    numbers = {key: _int_in_range(form.get(key), key) for key in RANGES}
    if None in numbers.values():
        raise SettingsError("idle-numbers")
    start_raw, end_raw = str(form.get("night_start") or "").strip(), str(form.get("night_end") or "").strip()
    if start_raw or end_raw:
        start, end = task_service.parse_hhmm(start_raw), task_service.parse_hhmm(end_raw)
        if start is None or end is None or start == end:
            raise SettingsError("idle-night")
        start_raw, end_raw = start.strftime("%H:%M"), end.strftime("%H:%M")
    return {"mode": form["mode"], "night_mode": form["night_mode"], **numbers,
            "night_start": start_raw, "night_end": end_raw}


async def get_settings(db) -> dict:
    """Saved settings over the defaults; anything unreadable is its default."""
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(await get_setting(db, SETTINGS_KEY) or "{}")
    except ValueError:
        saved = {}
    if not isinstance(saved, dict):
        return settings
    for key in ("mode", "night_mode"):
        if saved.get(key) in MODES:
            settings[key] = saved[key]
    for key in RANGES:
        value = _int_in_range(saved.get(key), key)
        if value is not None:
            settings[key] = value
    start, end = task_service.parse_hhmm(saved.get("night_start")), task_service.parse_hhmm(saved.get("night_end"))
    if start is not None and end is not None and start != end:
        settings["night_start"], settings["night_end"] = start.strftime("%H:%M"), end.strftime("%H:%M")
    return settings


async def save_settings(db, settings: dict) -> None:
    await set_setting(db, SETTINGS_KEY, json.dumps(settings))
    await db.commit()


async def next_event(db, now: datetime) -> dict | None:
    """The next timed event still to start today (family time), from
    calendar_cache only. Shown with Google's own offset, like the banners."""
    today = now.date()
    upcoming = []
    for event in await google_calendar.cached_events(db, today, today + timedelta(days=1)):
        if event.get("all_day") or event.get("school") or event.get("date") != today.isoformat():
            continue
        try:
            start = datetime.fromisoformat(event["sort_key"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is not None and start >= now:
            upcoming.append((start, event))
    if not upcoming:
        return None
    start, event = min(upcoming, key=lambda pair: pair[0])
    return {"title": event.get("title") or "(untitled)", "time": f"{start:%H:%M}"}


async def context(db, now: datetime | None = None) -> dict:
    """Everything the idle screen needs, as JSON-ready data."""
    tz = await family_timezone(db)
    now = (now or datetime.now(tz)).astimezone(tz)
    settings = await get_settings(db)
    return {
        **settings,
        # Wall-clock family time, no offset: idle.js only adds elapsed time to it.
        "now": now.replace(tzinfo=None, microsecond=0).isoformat(),
        "next_event": await next_event(db, now),
        "weather": await weather.cached_now(db),
        "photos": await google_photos.slideshow(db, now.date()),
    }


@router.get("/api/idle")
async def idle_now():
    async with get_db() as db:
        return await context(db)


async def _photo_response(photo_id: int, thumb: bool) -> FileResponse:
    async with get_db() as db:
        path = await google_photos.photo_file(db, photo_id, thumb=thumb)
    if path is None:
        raise HTTPException(status_code=404)
    # A row id is never reused, so its file never changes.
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


PHOTO_ID = PathParam(ge=1, le=2**53)  # SQLite integers stop at 2**63; ids never get near either


@router.get("/photos/{photo_id}")
async def photo(photo_id: int = PHOTO_ID):
    return await _photo_response(photo_id, thumb=False)


@router.get("/photos/{photo_id}/thumb")
async def photo_thumb(photo_id: int = PHOTO_ID):
    return await _photo_response(photo_id, thumb=True)
