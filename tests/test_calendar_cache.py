"""The calendar outage cache (app/calendar_cache.py): a failed or slow Google
fetch renders the last good copy with a "Last updated" note; the offline
state only shows when nothing is cached for that range."""

import asyncio
import json
import time
from datetime import date, datetime

import httpx
import pytest

from app import calendar_cache, database, google_calendar, google_oauth

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL_PATTERN = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"
# August 2026's Monday-start grid: Mon 27 Jul .. Sun 6 Sep.
AUG_GRID = (date(2026, 7, 27), date(2026, 9, 7))

DENTIST = {
    "summary": "Dentist",
    # Google's own offset (the calendar owner's zone), which must be kept as-is.
    "start": {"dateTime": "2026-08-11T09:00:00+01:00"},
    "end": {"dateTime": "2026-08-11T10:00:00+01:00"},
}
CAMPING = {"summary": "Camping", "start": {"date": "2026-08-07"}, "end": {"date": "2026-08-10"}}


def _titles(grid):
    return sorted({bar["event"]["title"] for week in grid["weeks"] for bar in week["bars"]})


@pytest.fixture
def events_route(google, connected):
    return google.get(url__regex=EVENTS_URL_PATTERN)


async def _prime(db, events_route):
    """One good fetch of August, which fills the cache."""
    events_route.respond(200, json={"items": [DENTIST, CAMPING]})
    grid = await google_calendar.get_month_grid(db, 2026, 8)
    assert grid["offline"] is False and grid["updated_label"] is None
    return grid


async def _cached_row_count(db):
    return (await (await db.execute("SELECT COUNT(*) FROM calendar_cache")).fetchone())[0]


async def test_successful_fetch_is_saved_for_its_range(db, events_route):
    await _prime(db, events_route)

    cached = await calendar_cache.load(db, *AUG_GRID)
    assert cached is not None
    events, fetched_at = cached
    assert sorted(e["title"] for e in events) == ["Camping", "Dentist"]
    assert fetched_at.tzinfo is not None


async def test_google_down_serves_the_cache_with_a_last_updated_note(db, events_route):
    live = await _prime(db, events_route)
    events_route.mock(side_effect=httpx.ConnectError("offline"))

    grid = await google_calendar.get_month_grid(db, 2026, 8)

    assert grid["offline"] is False
    assert grid["updated_label"] and len(grid["updated_label"]) == 5  # "HH:MM"
    assert _titles(grid) == ["Camping", "Dentist"]
    assert grid["weeks"] == live["weeks"]


async def test_cached_events_keep_googles_own_offsets(db, events_route):
    await _prime(db, events_route)
    events_route.respond(503)

    grid = await google_calendar.get_month_grid(db, 2026, 8)
    dentist = next(b["event"] for w in grid["weeks"] for b in w["bars"] if b["event"]["title"] == "Dentist")

    # 09:00 at +01:00 — not shifted to UTC (08:00) or any other zone.
    assert dentist["time_label"] == "9:00 AM"
    assert dentist["sort_key"] == "2026-08-11T09:00:00+01:00"


async def test_slow_google_serves_the_cache(db, events_route, monkeypatch):
    await _prime(db, events_route)
    monkeypatch.setattr(google_calendar, "CALENDAR_DEADLINE", 0.3)

    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"items": []})

    events_route.mock(side_effect=hang)
    started = time.monotonic()
    grid = await google_calendar.get_month_grid(db, 2026, 8)

    assert time.monotonic() - started < 2
    assert grid["updated_label"] and not grid["offline"]
    assert _titles(grid) == ["Camping", "Dentist"]


async def test_token_refresh_outage_serves_the_cache(db, events_route, google):
    await _prime(db, events_route)
    await google_oauth.store_tokens(
        db, {"access_token": "old", "refresh_token": "refresh", "expires_at": time.time() - 10}
    )
    google.post(TOKEN_URL).respond(503)

    grid = await google_calendar.get_month_grid(db, 2026, 8)

    assert grid["updated_label"] and _titles(grid) == ["Camping", "Dentist"]


async def test_partial_fetch_prefers_the_complete_cached_copy(db, events_route):
    await google_oauth.set_selected_calendars(db, [
        {"id": "family", "summary": "Family", "color": "#123456"},
        {"id": "school", "summary": "School", "color": "#654321"},
    ])
    await _prime(db, events_route)
    events_route.mock(side_effect=lambda request: (
        httpx.Response(200, json={"items": [DENTIST]}) if "family" in str(request.url)
        else httpx.Response(503)
    ))

    grid = await google_calendar.get_month_grid(db, 2026, 8)

    assert grid["updated_label"] and not grid["offline"]
    assert _titles(grid) == ["Camping", "Dentist"]


async def test_no_cache_shows_the_offline_state(db, events_route):
    events_route.mock(side_effect=httpx.ConnectError("offline"))

    grid = await google_calendar.get_month_grid(db, 2026, 8)

    assert grid["offline"] is True
    assert grid["updated_label"] is None
    assert _titles(grid) == []


async def test_other_months_cache_does_not_cover_this_one(db, events_route):
    await _prime(db, events_route)
    events_route.mock(side_effect=httpx.ConnectError("offline"))

    grid = await google_calendar.get_month_grid(db, 2026, 10)

    assert grid["offline"] is True and grid["updated_label"] is None


async def test_failed_fetch_never_overwrites_the_cache(db, events_route):
    await _prime(db, events_route)
    events_route.mock(side_effect=httpx.ConnectError("offline"))
    await google_calendar.get_month_grid(db, 2026, 8)

    events, _ = await calendar_cache.load(db, *AUG_GRID)
    assert len(events) == 2


async def test_widget_renders_the_note_instead_of_the_offline_banner(db, events_route, client):
    await _prime(db, events_route)
    events_route.mock(side_effect=httpx.ConnectError("offline"))

    html = (await client.get("/widgets/calendar?year=2026&month=8")).text

    assert "Last updated" in html
    assert "reach Google" not in html
    assert "Dentist" in html


async def test_widget_without_cache_still_shows_the_offline_banner(db, events_route, client):
    events_route.mock(side_effect=httpx.ConnectError("offline"))

    html = (await client.get("/widgets/calendar?year=2026&month=8")).text

    assert "reach Google" in html
    assert "Last updated" not in html


async def test_day_view_is_served_from_its_months_cache(db, events_route, client):
    await _prime(db, events_route)
    events_route.mock(side_effect=httpx.ConnectError("offline"))

    html = (await client.get("/widgets/calendar/day/2026-08-11")).text
    assert "Last updated" in html and "Dentist" in html and "9:00 AM" in html
    assert "Camping" not in html  # 7-9 Aug only

    multi_day = (await client.get("/widgets/calendar/day/2026-08-08")).text
    assert "Camping" in multi_day and "Dentist" not in multi_day


async def test_day_view_fetch_does_not_add_cache_rows(db, events_route, client):
    events_route.respond(200, json={"items": [DENTIST]})
    await client.get("/widgets/calendar/day/2026-08-11")
    assert await _cached_row_count(db) == 0


async def test_scheduler_refresh_fills_the_cache(db, events_route):
    events_route.respond(200, json={"items": [DENTIST]})

    assert await google_calendar.refresh_cache(db) is True
    tz = await database.family_timezone(db)
    today = await database.family_today(db)
    grid_range = google_calendar._grid_span(datetime.now(tz))
    assert grid_range[0] <= today < grid_range[1]
    assert await calendar_cache.load(db, *grid_range) is not None


async def test_scheduler_refresh_during_an_outage_changes_nothing(db, events_route):
    events_route.mock(side_effect=httpx.ConnectError("offline"))
    assert await google_calendar.refresh_cache(db) is False
    assert await _cached_row_count(db) == 0


async def test_scheduler_refresh_when_not_connected_does_nothing(db, google):
    assert await google_calendar.refresh_cache(db) is False


async def test_old_ranges_are_pruned(db):
    for month in range(1, 13 + calendar_cache.KEEP_RANGES):
        start = date(2020 + month // 12, month % 12 + 1, 1)
        await calendar_cache.store(db, start, start.replace(day=28), [])
    assert await _cached_row_count(db) == calendar_cache.KEEP_RANGES


async def test_cache_holds_event_data_only_never_tokens(db, events_route):
    await _prime(db, events_route)
    rows = await (await db.execute("SELECT * FROM calendar_cache")).fetchall()
    blob = json.dumps([dict(r) for r in rows])
    # The connected fixture's access token is "tok", refresh token "refresh".
    assert '"tok"' not in blob and "refresh" not in blob and "access_token" not in blob


# --- Invalidation ---

async def test_selection_change_clears_the_cache(db, events_route):
    await _prime(db, events_route)
    await google_oauth.set_selected_calendars(db, [{"id": "other", "summary": "Other", "color": "#000000"}])
    assert await _cached_row_count(db) == 0


async def test_disconnect_clears_the_cache(db, events_route, google):
    await _prime(db, events_route)
    google.post(REVOKE_URL).respond(200)
    await google_oauth.revoke_and_clear(db)
    assert await _cached_row_count(db) == 0


async def test_disconnect_clears_the_cache_even_if_revoke_fails(db, events_route, google):
    await _prime(db, events_route)
    google.post(REVOKE_URL).mock(side_effect=httpx.ConnectError("offline"))
    await google_oauth.revoke_and_clear(db)
    assert await _cached_row_count(db) == 0


async def test_revoked_grant_clears_the_cache(db, events_route, google):
    await _prime(db, events_route)
    await google_oauth.store_tokens(
        db, {"access_token": "old", "refresh_token": "refresh", "expires_at": time.time() - 10}
    )
    google.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})

    assert await google_calendar.get_month_grid(db, 2026, 8) is None  # disconnected: stub, not cache
    assert await _cached_row_count(db) == 0


async def test_reconnect_clears_but_token_refresh_keeps_the_cache(db, events_route):
    await _prime(db, events_route)
    await google_oauth.store_tokens(db, {"access_token": "new", "expires_at": time.time() + 3600})
    assert await _cached_row_count(db) == 1

    await google_oauth.store_tokens(db, {"access_token": "new", "expires_at": time.time() + 3600}, "b@example.com")
    assert await _cached_row_count(db) == 0


# --- Migration ---

async def test_migration_is_idempotent_and_keeps_cached_rows(db):
    await calendar_cache.store(db, *AUG_GRID, [{"title": "Kept", "date": "2026-08-01", "end_date": "2026-08-01"}])

    await database.init_db()
    await database.init_db()

    events, _ = await calendar_cache.load(db, *AUG_GRID)
    assert [e["title"] for e in events] == ["Kept"]
    columns = [r["name"] for r in await (await db.execute("PRAGMA table_info(calendar_cache)")).fetchall()]
    assert columns == ["range_start", "range_end", "events_json", "fetched_at"]
