"""
Weather widget (Section 4.4): current conditions + a short daily forecast
from Open-Meteo (no API key). The location is geocoded once in Admin and
stored in app_settings; forecasts are cached there too so the wall keeps
showing the last good reading when Open-Meteo is down or slow.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.database import get_db, get_setting, set_setting
from app.templating import templates

router = APIRouter()

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
LOCATION_SETTING = "weather_location"
CACHE_SETTING = "weather_cache"
FAILURE_SETTING = "weather_last_failure"
CACHE_MAX_AGE = timedelta(minutes=15)
STALE_MAX_AGE = timedelta(hours=24)
RETRY_BACKOFF = timedelta(minutes=5)
# httpx timeouts are per phase, so the whole fetch is also capped: the
# dashboard render awaits it inline.
TIMEOUT = httpx.Timeout(4.0, connect=3.0)
FETCH_DEADLINE = 6.0

CONDITION_LABELS = {
    "clear": "Clear",
    "partly": "Partly cloudy",
    "cloudy": "Cloudy",
    "fog": "Fog",
    "rain": "Rain",
    "snow": "Snow",
    "thunder": "Thunderstorm",
}


def weather_category(code: int | None) -> str:
    """Collapse a WMO weather code into one of the widget's icon categories."""
    if code is None:
        return "cloudy"
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code in (45, 48):
        return "fog"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "thunder"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    return "cloudy"


async def geocode(query: str) -> dict | None:
    """Best Open-Meteo match for a place, or None. Raises httpx.HTTPError.

    Open-Meteo only matches the bare place name, so "Reading, Berkshire" is
    searched as "Reading" and the part after the comma picks among matches
    by region/country — otherwise the first (most populous) match wins."""
    name, _, qualifier = query.partition(",")
    name, qualifier = name.strip(), qualifier.strip().lower()
    if not name:
        return None
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(GEOCODING_URL, params={"name": name, "count": 5})
        resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    first = results[0]
    if qualifier:
        first = next(
            (r for r in results if any(
                qualifier in str(r.get(k, "")).lower() for k in ("country", "country_code", "admin1", "admin2")
            )),
            first,
        )
    return {
        "name": first["name"],
        "country": first.get("country", ""),
        "latitude": first["latitude"],
        "longitude": first["longitude"],
    }


async def get_location(db) -> dict | None:
    raw = await get_setting(db, LOCATION_SETTING)
    return json.loads(raw) if raw else None


async def set_location(db, location: dict):
    await set_setting(db, LOCATION_SETTING, json.dumps(location))
    # A new place deserves a fetch now, not after the old place's backoff.
    await set_setting(db, FAILURE_SETTING, "")
    await db.commit()


async def _fetch_forecast(location: dict) -> dict:
    params = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "current": "temperature_2m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "forecast_days": 4,
        "timezone": "auto",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(FORECAST_URL, params=params)
        resp.raise_for_status()
    data = resp.json()
    # Validate the shape now so a malformed response never replaces a good cache.
    daily = data["daily"]
    return {
        "utc_offset_seconds": int(data.get("utc_offset_seconds", 0)),
        "current": {
            "temperature": float(data["current"]["temperature_2m"]),
            "weather_code": int(data["current"]["weather_code"]),
        },
        "daily": [
            {
                "date": daily["time"][i],
                "weather_code": int(daily["weather_code"][i]),
                "max": float(daily["temperature_2m_max"][i]),
                "min": float(daily["temperature_2m_min"][i]),
            }
            for i in range(len(daily["time"]))
        ],
    }


def _present(location: dict, forecast: dict, fetched_at: datetime, stale: bool, now: datetime) -> dict:
    offset = timedelta(seconds=forecast["utc_offset_seconds"])
    # A cache from before midnight (location time) still lists yesterday first.
    local_today = (now + offset).date()
    days = []
    for d in forecast["daily"]:
        day = date.fromisoformat(d["date"])
        if day >= local_today:
            days.append({
                "date": day,
                "label": day.strftime("%a"),
                "category": weather_category(d["weather_code"]),
                "high": round(d["max"]),
                "low": round(d["min"]),
            })
    today = days[0] if days and days[0]["date"] == local_today else None
    category = weather_category(forecast["current"]["weather_code"])
    return {
        "location": location["name"],
        "temperature": round(forecast["current"]["temperature"]),
        "category": category,
        "condition": CONDITION_LABELS[category],
        "today": today,
        "upcoming": [d for d in days if d["date"] > local_today][:3],
        "stale": stale,
        # Shown in the forecast location's own time, which is what the family sees on their clocks.
        "updated_label": (fetched_at + offset).strftime("%H:%M"),
    }


async def get_weather(db) -> dict | None:
    """Widget data; None when no location is set, or {"unavailable": True} when
    Open-Meteo can't be reached and there's no cached forecast to fall back on."""
    location = await get_location(db)
    if not location:
        return None

    now = datetime.now(timezone.utc)
    cached = await _load_cache(db, location, now)
    if cached and now - cached["fetched_at"] < CACHE_MAX_AGE:
        return cached["weather"]

    last_failure = _parse_time(await get_setting(db, FAILURE_SETTING))
    if not (last_failure and now - last_failure < RETRY_BACKOFF):
        try:
            forecast = await asyncio.wait_for(_fetch_forecast(location), FETCH_DEADLINE)
        except (httpx.HTTPError, asyncio.TimeoutError, KeyError, IndexError, TypeError, ValueError, AttributeError):
            await set_setting(db, FAILURE_SETTING, now.isoformat())
            await db.commit()
        else:
            await set_setting(db, CACHE_SETTING, json.dumps({
                "fetched_at": now.isoformat(),
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "forecast": forecast,
            }))
            await set_setting(db, FAILURE_SETTING, "")
            await db.commit()
            return _present(location, forecast, now, stale=False, now=now)

    # Open-Meteo is down (or backing off): this morning's forecast is fine, last week's isn't.
    if cached and now - cached["fetched_at"] < STALE_MAX_AGE:
        cached["weather"]["stale"] = True
        return cached["weather"]
    return {"location": location["name"], "unavailable": True}


def _parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed and parsed.tzinfo else None


async def _load_cache(db, location: dict, now: datetime) -> dict | None:
    """The cached forecast, already presented, or None if it's missing,
    corrupt, or for a previous location (worse than nothing)."""
    raw = await get_setting(db, CACHE_SETTING)
    if not raw:
        return None
    try:
        cache = json.loads(raw)
        if (cache["latitude"], cache["longitude"]) != (location["latitude"], location["longitude"]):
            return None
        fetched_at = _parse_time(cache["fetched_at"])
        if fetched_at is None:
            return None
        weather = _present(location, cache["forecast"], fetched_at, stale=False, now=now)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    return {"fetched_at": fetched_at, "weather": weather}


@router.get("/widgets/weather", response_class=HTMLResponse)
async def weather_widget(request: Request):
    async with get_db() as db:
        weather = await get_weather(db)
    return templates.TemplateResponse(request, "widgets/weather.html", {"weather": weather})
