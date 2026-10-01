"""Day/night appearance (decided in the family's timezone, never the
container's UTC clock) and the person-colour ink helper."""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import appearance, database
from app.services import weather

LONDON = ZoneInfo("Europe/London")
SYDNEY = ZoneInfo("Australia/Sydney")


def _at(hour, minute=0, tz=LONDON, day=(2026, 6, 15)):
    return datetime(*day, hour, minute, tzinfo=tz)


@pytest.mark.parametrize(
    ("hour", "minute", "mode", "switch"),
    [
        (7, 0, "day", (6, 15, 19)),  # morning edge: day starts at 07:00 sharp
        (12, 30, "day", (6, 15, 19)),
        (18, 59, "day", (6, 15, 19)),
        (19, 0, "night", (6, 16, 7)),  # evening edge: night starts at 19:00 sharp
        (23, 30, "night", (6, 16, 7)),
        (0, 0, "night", (6, 15, 7)),  # after midnight: switches back the same morning
        (6, 59, "night", (6, 15, 7)),
    ],
)
def test_auto_mode_boundaries(hour, minute, mode, switch):
    got_mode, switch_at = appearance.mode_at(_at(hour, minute), "auto")
    assert got_mode == mode
    month, day, switch_hour = switch
    assert switch_at == datetime(2026, month, day, switch_hour, tzinfo=LONDON)


def test_pinned_appearance_never_switches():
    assert appearance.mode_at(_at(12), "dark") == ("night", None)
    assert appearance.mode_at(_at(22), "light") == ("day", None)


async def test_mode_follows_family_timezone_not_utc(db):
    # 10:00 UTC is 20:00 in Sydney: night there, day in London.
    utc_now = datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("UTC"))
    assert (await appearance.current_mode(db, utc_now))["mode"] == "day"  # conftest: Europe/London

    await database.set_setting(db, "calendar_timezone", "Australia/Sydney")
    await db.commit()
    result = await appearance.current_mode(db, utc_now)
    assert result["mode"] == "night"
    assert result["switch_in"] == 11 * 3600  # until 07:00 Sydney time


async def test_switch_in_counts_real_seconds_across_dst(db):
    # Night of the UK clocks going forward (29 Mar 2026): 19:00 -> 07:00 is 11 real hours.
    now = datetime(2026, 3, 28, 19, 0, tzinfo=LONDON)
    result = await appearance.current_mode(db, now)
    assert result == {"mode": "night", "switch_in": 11 * 3600}


async def test_setting_is_used_and_validated(db):
    assert await appearance.get_appearance(db) == "auto"
    await appearance.set_appearance(db, "dark")
    assert (await appearance.current_mode(db, _at(12))) == {"mode": "night", "switch_in": None}
    await database.set_setting(db, appearance.APPEARANCE_SETTING, "purple")
    assert await appearance.get_appearance(db) == "auto"


async def test_pages_carry_the_mode(db, client):
    await appearance.set_appearance(db, "dark")
    for path in ("/", "/admin/login"):
        html = (await client.get(path)).text
        assert '<html lang="en" data-mode="night">' in html, path
        assert "/api/appearance" in html, path
    assert (await client.get("/api/appearance")).json() == {"mode": "night", "switch_in": None}


async def test_admin_saves_appearance(db, client):
    from app.routers.admin import SESSION_COOKIE
    from app.security import create_session_token

    client.cookies.set(SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/appearance", data={"value": "light"})
    assert resp.status_code == 303
    assert await appearance.get_appearance(db) == "light"
    assert 'name="value" value="light" checked' in (await client.get("/admin?tab=display")).text
    bad = await client.post("/admin/appearance", data={"value": "sepia"})
    assert bad.headers["location"] == "/admin?tab=display&error=appearance#display"
    assert await appearance.get_appearance(db) == "light"


@pytest.mark.parametrize(
    ("colour", "ink"),
    [
        ("#FFFFFF", "dark"),
        ("#000000", "light"),
        ("#D6A02C", "dark"),  # ochre: white text would be ~2.3:1
        ("#F2C94C", "dark"),
        ("#3D6E93", "light"),
        ("#C1584A", "light"),
        ("#8E5BB5", "light"),
        ("#abc", "dark"),  # shorthand hex
        ("not-a-colour", "light"),
    ],
)
def test_person_ink(colour, ink):
    assert appearance.person_ink(colour) == ink


def test_person_ink_always_clears_large_text_contrast():
    # Pills are large bold text (AA needs 3:1); the better ink always clears it.
    for r in range(0, 256, 15):
        for g in range(0, 256, 15):
            for b in range(0, 256, 15):
                c = f"#{r:02X}{g:02X}{b:02X}"
                ink = appearance.LIGHT_INK if appearance.person_ink(c) == "light" else appearance.DARK_INK
                assert appearance.contrast(c, ink) >= 3.0, c


async def test_widget_pills_use_person_ink(db, client):
    await db.execute("UPDATE profiles SET colour_hex = '#F2C94C' WHERE sort_order = 0")
    await db.commit()
    html = (await client.get("/widgets/tasks")).text
    assert 'class="person-pill ink-dark" style="--person: #F2C94C;"' in html


async def test_switch_in_counts_real_seconds_across_autumn_dst(db):
    # UK clocks go back on 25 Oct 2026: 19:00 -> 07:00 is 13 real hours.
    now = datetime(2026, 10, 24, 19, 0, tzinfo=LONDON)
    assert await appearance.current_mode(db, now) == {"mode": "night", "switch_in": 13 * 3600}


@pytest.mark.parametrize("tz_setting", ["Not/AZone", None])
async def test_bad_or_missing_timezone_falls_back_to_utc(db, tz_setting):
    if tz_setting is None:
        await db.execute("DELETE FROM app_settings WHERE key = 'calendar_timezone'")
    else:
        await database.set_setting(db, "calendar_timezone", tz_setting)
    await db.commit()
    # 06:30 UTC is still night in UTC (it's 07:30, day, in London).
    now = datetime(2026, 6, 15, 6, 30, tzinfo=ZoneInfo("UTC"))
    assert await appearance.current_mode(db, now) == {"mode": "night", "switch_in": 30 * 60}


def test_person_ink_clears_large_text_contrast_with_night_inks():
    # Night swaps the pill inks for softer ones (no pure white); still >= 3:1.
    night_light, night_dark = "#F4F1EA", "#14171C"  # --pill-light / --pill-dark
    for r in range(0, 256, 15):
        for g in range(0, 256, 15):
            for b in range(0, 256, 15):
                c = f"#{r:02X}{g:02X}{b:02X}"
                ink = night_light if appearance.person_ink(c) == "light" else night_dark
                assert appearance.contrast(c, ink) >= 3.0, c


# --- Sunset-based night (spec 11.4) ---

READING = {"name": "Reading", "country": "United Kingdom", "latitude": 51.45, "longitude": -0.97}
DAY = date(2026, 10, 15)


@pytest.fixture(autouse=True)
def fresh_fallback_log(monkeypatch):
    monkeypatch.setattr(appearance, "_logged", set())


async def _sun_forecast(db, days, zone="Europe/London", location=READING):
    """Saves a forecast as the weather service would: `days` maps a date to
    (sunrise, sunset) local strings, or None for a day without them."""
    await weather.set_location(db, location)
    daily = []
    for day, sun in days.items():
        entry = {"date": day.isoformat(), "weather_code": 3, "max": 14.0, "min": 6.0}
        if sun:
            entry["sunrise"], entry["sunset"] = f"{day.isoformat()}T{sun[0]}", f"{day.isoformat()}T{sun[1]}"
        daily.append(entry)
    forecast = {"utc_offset_seconds": 3600, "timezone": zone, "current": {"temperature": 12.0, "weather_code": 3}}
    forecast["daily"] = daily
    cache = {"fetched_at": "2026-10-15T05:00:00+00:00", "latitude": location["latitude"]}
    cache |= {"longitude": location["longitude"], "forecast": forecast}
    await database.set_setting(db, weather.CACHE_SETTING, json.dumps(cache))
    await db.commit()


def _local(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LONDON)


@pytest.mark.parametrize(
    ("hour", "minute", "mode", "switch"),
    [
        (5, 0, "night", _local(7, 21)),  # before sunrise: morning comes at sunrise, not 07:00
        (7, 21, "day", _local(18, 5)),  # sunrise edge
        (12, 0, "day", _local(18, 5)),
        (18, 4, "day", _local(18, 5)),  # 18:04 is still day: an October evening, not 19:00
        (18, 5, "night", _local(7, 23, DAY + timedelta(days=1))),  # sunset edge -> tomorrow's sunrise
        (23, 0, "night", _local(7, 23, DAY + timedelta(days=1))),
    ],
)
async def test_auto_night_runs_from_sunset_to_sunrise(db, hour, minute, mode, switch):
    await _sun_forecast(db, {DAY: ("07:21", "18:05"), DAY + timedelta(days=1): ("07:23", "18:03")})
    result = await appearance.current_mode(db, _local(hour, minute))
    assert result["mode"] == mode
    assert result["switch_in"] == int((switch - _local(hour, minute)).total_seconds())
    assert await database.get_setting(db, appearance.FALLBACK_SETTING) is None  # nothing fell back


async def test_after_sunset_without_tomorrows_sunrise_morning_is_07_00(db):
    await _sun_forecast(db, {DAY: ("07:21", "18:05")})
    result = await appearance.current_mode(db, _local(20))
    assert result == {"mode": "night", "switch_in": 11 * 3600}


async def test_sun_times_convert_from_the_forecast_zone_to_the_family_zone(db):
    """Forecast in Paris time (UTC+2 in October), family in London (UTC+1):
    a 19:05 Paris sunset is 18:05 London."""
    await _sun_forecast(db, {DAY: ("08:21", "19:05")}, zone="Europe/Paris")
    assert (await appearance.current_mode(db, _local(18, 4)))["mode"] == "day"
    assert (await appearance.current_mode(db, _local(18, 5)))["mode"] == "night"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("nothing", "no_location"),
        ("location only", "no_forecast"),
        ("forecast without sun times", "no_forecast"),
        ("forecast for other days", "stale_forecast"),
        ("sunset before sunrise", "bad_times"),
        ("a two-hour day", "bad_times"),
    ],
)
async def test_fallback_to_19_to_07_says_why(db, caplog, setup, reason):
    if setup == "location only":
        await weather.set_location(db, READING)
    elif setup == "forecast without sun times":
        await _sun_forecast(db, {DAY: None})
    elif setup == "forecast for other days":
        await _sun_forecast(db, {DAY - timedelta(days=5): ("07:10", "18:20")})
    elif setup == "sunset before sunrise":
        await _sun_forecast(db, {DAY: ("18:05", "07:21")})
    elif setup == "a two-hour day":
        await _sun_forecast(db, {DAY: ("11:00", "13:00")})

    with caplog.at_level(logging.INFO, logger="app.appearance"):
        at_1830 = await appearance.current_mode(db, _local(18, 30))
        await appearance.current_mode(db, _local(18, 45))  # same day, same reason: logged once

    assert at_1830 == {"mode": "day", "switch_in": 30 * 60}  # the fixed night starts at 19:00
    logged = [r for r in caplog.records if "fixed 19:00-07:00" in r.getMessage()]
    assert len(logged) == 1 and reason in logged[0].getMessage()
    assert logged[0].levelno == (logging.INFO if reason == "no_location" else logging.WARNING)
    assert json.loads(await database.get_setting(db, appearance.FALLBACK_SETTING)) == {
        "date": DAY.isoformat(),
        "reason": reason,
    }


async def test_fallback_is_logged_again_the_next_day(db, caplog):
    with caplog.at_level(logging.INFO, logger="app.appearance"):
        await appearance.current_mode(db, _local(12))
        await appearance.current_mode(db, _local(12, day=DAY + timedelta(days=1)))
    assert sum("fixed 19:00-07:00" in r.getMessage() for r in caplog.records) == 2


async def test_pinned_appearance_never_reads_or_logs_the_sun(db, caplog):
    await appearance.set_appearance(db, "light")
    with caplog.at_level(logging.INFO, logger="app.appearance"):
        assert await appearance.current_mode(db, _local(23)) == {"mode": "day", "switch_in": None}
    assert not caplog.records
    assert await database.get_setting(db, appearance.FALLBACK_SETTING) is None


async def test_admin_shows_where_tonights_night_comes_from(db, client, monkeypatch):
    from app.routers.admin import SESSION_COOKIE
    from app.security import create_session_token

    client.cookies.set(SESSION_COOKIE, create_session_token())
    source = await appearance.night_source(db)
    assert source["kind"] == "fixed" and source["reason"] == "no_location" and source["last_fallback"] is None
    html = (await client.get("/admin?tab=display")).text
    assert "the fixed 19:00–07:00 night, because no weather location is set" in html

    await _sun_forecast(db, {DAY: ("07:21", "18:05")})
    await appearance.current_mode(db, _local(12, day=DAY - timedelta(days=1)))  # yesterday fell back
    monkeypatch.setattr(appearance, "datetime", _FrozenDatetime)
    html = (await client.get("/admin?tab=display")).text
    assert "dark from sunset 18:05 to sunrise 07:21, from the weather forecast" in html
    assert "Last fell back on Wed 14 Oct: the saved forecast doesn&#39;t cover today" in html


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _local(12).astimezone(tz)


async def test_forecast_stores_sun_times_and_zone(db, google):
    await weather.set_location(db, READING)
    route = google.get(weather.FORECAST_URL).respond(
        200,
        json={
            "utc_offset_seconds": 3600,
            "timezone": "Europe/London",
            "current": {"temperature_2m": 12.0, "weather_code": 2},
            "daily": {
                "time": ["2026-10-15"],
                "weather_code": [2],
                "temperature_2m_max": [15.0],
                "temperature_2m_min": [7.0],
                "sunrise": ["2026-10-15T07:21"],
                "sunset": ["2026-10-15T18:05"],
            },
        },
    )
    await weather.refresh(db)
    assert "sunrise" in route.calls.last.request.url.params["daily"]
    days = await weather.sun_times(db)
    assert days[DAY] == (_local(7, 21), _local(18, 5))


async def test_late_evening_in_utc_reads_the_forecasts_own_day(db):
    """No calendar connected, so the family's timezone is UTC; the weather is
    in a British summer. At 23:10 UTC it's already 00:10 tomorrow in Reading,
    and a forecast fetched since starts on that day: that's still night until
    Reading's sunrise, not a "stale forecast" fallback."""
    summer = date(2026, 6, 16)
    await _sun_forecast(db, {summer: ("04:43", "21:21"), summer + timedelta(days=1): ("04:43", "21:21")})
    now = datetime(2026, 6, 15, 23, 10, tzinfo=UTC)
    result = await appearance.current_mode(db, now)
    sunrise = datetime(2026, 6, 16, 4, 43, tzinfo=LONDON)
    assert result == {"mode": "night", "switch_in": int((sunrise - now).total_seconds())}
    assert await database.get_setting(db, appearance.FALLBACK_SETTING) is None
