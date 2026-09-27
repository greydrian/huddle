"""Outage logging (once per outage, never secrets) and the calendar's
request-path deadline."""

import asyncio
import json
import logging
import time

import httpx

from app import google_calendar, google_oauth, task_sync
from app.main import RedactOAuthQuery

TASKS_API = "https://tasks.googleapis.com/tasks/v1/lists"
EVENTS_URL_PATTERN = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"
REFRESH = "SECRET-REFRESH-7f3a"
ACCESS = "SECRET-ACCESS-91bc"


def _records(caplog, name, level):
    return [r for r in caplog.records if r.name == name and r.levelno == level]


async def test_an_outage_warns_once_and_logs_recovery_once(db, connected, google, caplog):
    await task_sync.set_shopping_tasklist(db, {"id": "shop", "title": "Shopping"})
    await db.execute(
        "INSERT INTO sync_queue (service, payload_json) VALUES ('shopping', ?)",
        (json.dumps({"action": "delete", "google_task_id": "g1"}),),
    )
    await db.commit()
    offline = httpx.ConnectError("offline")
    google.delete(f"{TASKS_API}/shop/tasks/g1").mock(
        side_effect=[offline, offline, offline, httpx.Response(204)]
    )
    google.get(f"{TASKS_API}/shop/tasks").respond(200, json={"items": []})

    with caplog.at_level(logging.INFO, logger="app.task_sync"):
        for _ in range(3):
            await task_sync.run_sync(db)
        assert len(_records(caplog, "app.task_sync", logging.WARNING)) == 1

        await task_sync.run_sync(db)  # Google is back
        await task_sync.run_sync(db)  # ...and stays back: nothing more to say

    assert len(_records(caplog, "app.task_sync", logging.WARNING)) == 1
    infos = _records(caplog, "app.task_sync", logging.INFO)
    assert len(infos) == 1
    assert "recovered" in infos[0].getMessage()


async def test_failure_logs_never_contain_tokens(db, google, caplog):
    await google_oauth.store_tokens(
        db, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": time.time() - 10}
    )
    google.post(google_oauth.TOKEN_ENDPOINT).respond(500, json={"error": "backend", "refresh_token": REFRESH})
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(400, json={"error": "invalid_token"})
    google.get(url__regex=EVENTS_URL_PATTERN).respond(403, json={"error": {"message": ACCESS}})

    with caplog.at_level(logging.DEBUG):
        assert (await google_calendar.get_month_grid(db, 2026, 8))["offline"] is True  # refresh 500
        await task_sync.run_sync(db)  # refresh 500 again, via sync

        await google_oauth.store_tokens(
            db, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": time.time() + 3600}
        )
        assert (await google_calendar.get_month_grid(db, 2026, 8))["offline"] is True  # events 403
        await google_oauth.revoke_and_clear(db)  # revoke 400

    assert caplog.records, "expected the failures above to be logged"
    for record in caplog.records:
        text = f"{record.getMessage()} {record.exc_text or ''}"
        assert REFRESH not in text
        assert ACCESS not in text
        assert "token=" not in text


async def test_slow_google_cannot_stall_the_dashboard(connected, client, google, monkeypatch):
    monkeypatch.setattr(google_calendar, "CALENDAR_DEADLINE", 0.3)

    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"items": []})

    google.get(url__regex=EVENTS_URL_PATTERN).mock(side_effect=hang)

    started = time.monotonic()
    resp = await client.get("/")
    elapsed = time.monotonic() - started

    assert resp.status_code == 200
    assert elapsed < 2
    assert "Couldn&#39;t reach Google" in resp.text or "Couldn't reach Google" in resp.text


def test_oauth_callback_query_is_stripped_from_the_access_log():
    def access_record(path):
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0,
            '%s - "%s %s HTTP/%s" %d', ("10.0.0.2:5000", "GET", path, "1.1", 303), None,
        )
        RedactOAuthQuery().filter(record)
        return record.getMessage()

    assert access_record("/admin/google/callback?code=4/abc&state=xyz") == (
        '10.0.0.2:5000 - "GET /admin/google/callback HTTP/1.1" 303'
    )
    assert "?year=2026" in access_record("/widgets/calendar?year=2026")
