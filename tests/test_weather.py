import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import database
from app.routers import admin
from app.security import create_session_token
from app.services import weather

READING = {"name": "Reading", "country": "United Kingdom", "latitude": 51.45, "longitude": -0.97}

OFFSET = 3600


def _local_today():
    return (datetime.now(timezone.utc) + timedelta(seconds=OFFSET)).date()


def _day(n):
    return _local_today() + timedelta(days=n)


FORECAST = {
    "utc_offset_seconds": OFFSET,
    "current": {"temperature_2m": 17.6, "weather_code": 2},
    "daily": {
        "time": [_day(n).isoformat() for n in range(4)],
        "weather_code": [2, 61, 71, 95],
        "temperature_2m_max": [18.4, 15.0, 3.2, 20.1],
        "temperature_2m_min": [9.1, 8.0, -1.4, 12.0],
    },
}


async def _set_location(db, location=READING):
    await weather.set_location(db, location)


async def _set_cache(db, fetched_at, location=READING, temperature=12.0, daily=None):
    forecast = {
        "utc_offset_seconds": OFFSET,
        "current": {"temperature": temperature, "weather_code": 3},
        "daily": daily or [{"date": _day(0).isoformat(), "weather_code": 3, "max": 14.0, "min": 6.0}],
    }
    await database.set_setting(
        db,
        weather.CACHE_SETTING,
        json.dumps(
            {
                "fetched_at": fetched_at.isoformat(),
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "forecast": forecast,
            }
        ),
    )
    await db.commit()


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (0, "clear"),
        (1, "partly"),
        (2, "partly"),
        (3, "cloudy"),
        (45, "fog"),
        (48, "fog"),
        (51, "rain"),
        (65, "rain"),
        (80, "rain"),
        (71, "snow"),
        (77, "snow"),
        (86, "snow"),
        (95, "thunder"),
        (99, "thunder"),
        (None, "cloudy"),
    ],
)
def test_weather_code_categories(code, category):
    assert weather.weather_category(code) == category


async def test_no_location_shows_setup_prompt(client, google):
    resp = await client.get("/widgets/weather")

    assert resp.status_code == 200
    assert "Set your location in Admin" in resp.text
    assert 'href="/admin?tab=display#weather"' in resp.text


async def test_widget_renders_forecast_and_caches_it(db, client, google):
    await _set_location(db)
    route = google.get(weather.FORECAST_URL).respond(200, json=FORECAST)

    resp = await client.get("/widgets/weather")

    assert resp.status_code == 200
    assert "18°" in resp.text  # current temp, rounded
    assert "Partly cloudy" in resp.text
    assert "H 18° L 9°" in resp.text
    assert _day(1).strftime("%a") in resp.text and _day(3).strftime("%a") in resp.text  # 3-day strip
    assert "Last updated" not in resp.text
    assert 'hx-trigger="every 900s"' in resp.text
    assert route.call_count == 1
    assert route.calls[0].request.url.params["timezone"] == "auto"

    # A second render inside 15 minutes is served from the cache.
    await client.get("/widgets/weather")
    assert route.call_count == 1


async def test_outage_falls_back_to_cached_forecast(db, client, google):
    await _set_location(db)
    await _set_cache(db, datetime.now(timezone.utc) - timedelta(hours=2), temperature=12.0)
    google.get(weather.FORECAST_URL).mock(side_effect=httpx.ConnectError("offline"))

    resp = await client.get("/widgets/weather")

    assert resp.status_code == 200
    assert "12°" in resp.text
    assert "Last updated" in resp.text


async def test_cache_from_an_earlier_day_starts_at_today(db, client, google):
    await _set_location(db)
    daily = [
        {"date": _day(n).isoformat(), "weather_code": 0, "max": hi, "min": lo}
        for n, hi, lo in [(-1, 31.0, 21.0), (0, 14.0, 6.0), (1, 15.0, 7.0), (2, 16.0, 8.0)]
    ]
    await _set_cache(db, datetime.now(timezone.utc) - timedelta(hours=20), daily=daily)
    google.get(weather.FORECAST_URL).mock(side_effect=httpx.ConnectError("offline"))

    resp = await client.get("/widgets/weather")

    assert "H 14° L 6°" in resp.text  # not yesterday's 31/21
    assert "31°" not in resp.text
    # Served from the stale cache (backing off after the failure above).
    labels = [d["label"] for d in (await weather.get_weather(db))["upcoming"]]
    assert labels == [_day(1).strftime("%a"), _day(2).strftime("%a")]


async def test_cache_older_than_a_day_is_not_shown(db, client, google):
    await _set_location(db)
    await _set_cache(db, datetime.now(timezone.utc) - timedelta(hours=25), temperature=12.0)
    google.get(weather.FORECAST_URL).mock(side_effect=httpx.ConnectError("offline"))

    resp = await client.get("/widgets/weather")

    assert resp.status_code == 200
    assert "12°" not in resp.text
    assert "trying again soon" in resp.text


async def test_failed_fetch_backs_off_before_retrying(db, client, google):
    await _set_location(db)
    await _set_cache(db, datetime.now(timezone.utc) - timedelta(hours=2), temperature=12.0)
    route = google.get(weather.FORECAST_URL).mock(side_effect=httpx.ConnectError("offline"))

    await client.get("/widgets/weather")
    resp = await client.get("/")

    assert route.call_count == 1
    assert "12°" in resp.text and "Last updated" in resp.text

    # Once the backoff has passed, it tries again.
    await database.set_setting(
        db, weather.FAILURE_SETTING, (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    )
    await db.commit()
    await client.get("/widgets/weather")
    assert route.call_count == 2


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({"fetched_at": "yesterday", "latitude": 51.45, "longitude": -0.97, "forecast": {}}),
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "latitude": 51.45,
                "longitude": -0.97,
                "forecast": {"utc_offset_seconds": 0, "daily": "nope"},
            }
        ),
    ],
)
async def test_corrupt_cache_is_treated_as_no_cache(db, client, google, raw):
    await _set_location(db)
    await database.set_setting(db, weather.CACHE_SETTING, raw)
    await db.commit()
    google.get(weather.FORECAST_URL).respond(503)

    assert (await client.get("/")).status_code == 200
    resp = await client.get("/widgets/weather")
    assert resp.status_code == 200
    assert "trying again soon" in resp.text


async def test_outage_without_cache_shows_empty_state(db, client, google):
    await _set_location(db)
    google.get(weather.FORECAST_URL).respond(503)

    resp = await client.get("/widgets/weather")

    assert resp.status_code == 200
    assert "trying again soon" in resp.text


async def test_malformed_forecast_does_not_500(db, client, google):
    await _set_location(db)
    google.get(weather.FORECAST_URL).respond(200, json={"current": {}})

    resp = await client.get("/widgets/weather")

    assert resp.status_code == 200


async def test_cache_for_another_location_is_ignored(db, client, google):
    await _set_cache(db, datetime.now(timezone.utc), location={**READING, "latitude": 10.0})
    await _set_location(db)
    google.get(weather.FORECAST_URL).respond(200, json=FORECAST)

    resp = await client.get("/widgets/weather")

    assert "18°" in resp.text


async def test_dashboard_renders_when_open_meteo_is_down(db, client, google):
    await _set_location(db)
    google.get(weather.FORECAST_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    resp = await client.get("/")

    assert resp.status_code == 200
    assert 'id="widget-weather"' in resp.text


async def test_admin_saves_geocoded_location(db, client, google):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    google.get(weather.GEOCODING_URL).respond(
        200,
        json={
            "results": [
                {
                    "name": "Reading",
                    "country": "United States",
                    "country_code": "US",
                    "admin1": "Pennsylvania",
                    "latitude": 40.33,
                    "longitude": -75.92,
                },
                {
                    "name": "Reading",
                    "country": "United Kingdom",
                    "country_code": "GB",
                    "admin1": "England",
                    "latitude": 51.45,
                    "longitude": -0.97,
                },
            ]
        },
    )

    forecast = google.get(weather.FORECAST_URL).respond(200, json=FORECAST)

    resp = await client.post("/admin/weather-location", data={"place": "Reading, United Kingdom"})

    assert resp.status_code == 303
    stored = json.loads(await database.get_setting(db, weather.LOCATION_SETTING))
    assert stored == READING
    # The new place's forecast (and sunset) is fetched straight away.
    assert forecast.called and await database.get_setting(db, weather.CACHE_SETTING)
    page = await client.get("/admin?tab=display")
    assert "Reading, United Kingdom" in page.text


async def test_admin_location_not_found_keeps_previous(db, client, google):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    await _set_location(db)
    google.get(weather.GEOCODING_URL).respond(200, json={})

    resp = await client.post("/admin/weather-location", data={"place": "Nowhereville"})

    assert "weather_error=notfound" in resp.headers["location"]
    assert json.loads(await database.get_setting(db, weather.LOCATION_SETTING)) == READING


@pytest.mark.parametrize(
    "body",
    [
        [{"name": "Reading", "latitude": 51.45, "longitude": -0.97}],
        {"results": {"name": "Reading", "latitude": 51.45, "longitude": -0.97}},
        {"results": ["Reading"]},
    ],
)
async def test_admin_malformed_geocoder_response_is_not_500(db, client, google, body):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    google.get(weather.GEOCODING_URL).respond(200, json=body)

    resp = await client.post("/admin/weather-location", data={"place": "Reading"})

    assert resp.status_code == 303
    assert "weather_error=offline" in resp.headers["location"]
    assert await database.get_setting(db, weather.LOCATION_SETTING) is None


async def test_admin_location_requires_login(client, google):
    resp = await client.post("/admin/weather-location", data={"place": "Reading"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


async def test_saving_a_location_while_offline_still_saves_it(db, client, google):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    google.get(weather.GEOCODING_URL).respond(
        200, json={"results": [{"name": "Reading", "country": "United Kingdom", "latitude": 51.45, "longitude": -0.97}]}
    )
    google.get(weather.FORECAST_URL).mock(side_effect=httpx.ConnectError("offline"))
    resp = await client.post("/admin/weather-location", data={"place": "Reading"})
    assert resp.status_code == 303 and "weather_error" not in resp.headers["location"]
    assert json.loads(await database.get_setting(db, weather.LOCATION_SETTING))["name"] == "Reading"
