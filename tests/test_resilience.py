import time

import httpx
import pytest

from app import google_calendar, google_oauth

TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL_PATTERN = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"


async def _expire_token(db):
    await google_oauth.store_tokens(
        db, {"access_token": "old", "refresh_token": "refresh", "expires_at": time.time() - 10}
    )


async def test_refresh_outage_keeps_the_stored_login(db, google):
    await _expire_token(db)
    google.post(TOKEN_URL).respond(503)

    with pytest.raises(httpx.HTTPError):
        await google_oauth.get_valid_access_token(db)

    assert await google_oauth._load_stored_tokens(db) is not None


async def test_revoked_refresh_token_disconnects(db, google):
    await _expire_token(db)
    google.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})

    assert await google_oauth.get_valid_access_token(db) is None
    assert await google_oauth._load_stored_tokens(db) is None


async def test_dashboard_still_renders_when_google_is_unreachable(connected, client, google):
    google.get(url__regex=EVENTS_URL_PATTERN).mock(side_effect=httpx.ConnectError("offline"))

    resp = await client.get("/")

    assert resp.status_code == 200
    assert "Couldn&#39;t reach Google" in resp.text or "Couldn't reach Google" in resp.text
    assert "Today's Tasks" in resp.text or "Today&#39;s Tasks" in resp.text


async def test_invalid_calendar_inputs_are_400_not_500(connected, client):
    assert (await client.get("/widgets/calendar/day/not-a-date")).status_code == 400
    assert (await client.get("/widgets/calendar?year=2026&month=13")).status_code == 422


async def test_multi_day_event_renders_as_one_spanning_bar(db, connected, google):
    google.get(url__regex=EVENTS_URL_PATTERN).respond(200, json={"items": [{
        "summary": "Half term",
        # Google's all-day end date is exclusive: this covers Wed 12 - Fri 14.
        "start": {"date": "2026-08-12"},
        "end": {"date": "2026-08-15"},
    }]})

    grid = await google_calendar.get_month_grid(db, 2026, 8)

    bars = [b for week in grid["weeks"] for b in week["bars"]]
    assert len(bars) == 1
    assert (bars[0]["col_start"], bars[0]["col_end"]) == (3, 5)  # Wed..Fri, Monday-start week
    assert grid["offline"] is False


async def test_cross_site_writes_are_refused(client):
    evil = await client.post("/api/shopping", data={"title": "x"}, headers={"Origin": "http://evil.example"})
    sandboxed = await client.post("/api/shopping", data={"title": "x"}, headers={"Origin": "null"})
    same_site = await client.post("/api/shopping", data={"title": "x"}, headers={"Origin": "http://testserver"})

    assert evil.status_code == 403
    assert sandboxed.status_code == 403
    assert same_site.status_code == 200


async def test_deleting_a_family_member_removes_their_tasks(db, client):
    await db.execute("INSERT INTO tasks (profile_id, title) VALUES (1, 'Feed cat')")
    await db.commit()
    from app.routers import admin
    from app.security import create_session_token

    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/profiles/1/delete")

    assert resp.status_code == 303
    count = await (await db.execute("SELECT COUNT(*) FROM tasks WHERE profile_id = 1")).fetchone()
    assert count[0] == 0
