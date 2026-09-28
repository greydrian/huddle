"""Sync health: the persisted sync_status row, the needs-attention transition
for a persistent 401/403, the dashboard dot, Admin's Sync panel and
"Sync now", and /health."""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import database, google_oauth, google_tasks, sync_status, task_sync
from app.routers import calendar

TASKS_API = "https://tasks.googleapis.com/tasks/v1/lists"
SHOP_LIST = {"id": "shop", "title": "Shopping"}
ACCESS = "SECRET-ACCESS-5d2e"
REFRESH = "SECRET-REFRESH-8c1b"


async def _status(db):
    return await (await db.execute("SELECT * FROM sync_status WHERE id = 1")).fetchone()


async def _queue_delete(db, gid="g1"):
    """Link the shopping list and queue one push that will hit Google."""
    await task_sync.set_shopping_tasklist(db, SHOP_LIST)
    await db.execute(
        "INSERT INTO sync_queue (service, payload_json) VALUES ('shopping', ?)",
        (json.dumps({"action": "delete", "google_task_id": gid}),),
    )
    await db.commit()


async def _login(client):
    resp = await client.post("/admin/login", data={"pin": "1234"})
    assert resp.status_code == 303


def _mock_admin_pickers(google):
    """Admin's page loads the calendar and task-list pickers when connected."""
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": []})
    google.get(google_tasks.TASKLISTS_ENDPOINT).respond(200, json={"items": []})


def _warnings(caplog):
    return [r for r in caplog.records if r.name == "app.task_sync" and r.levelno == logging.WARNING]


# --- Recording ---------------------------------------------------------------

async def test_a_clean_cycle_records_success(db, connected, google):
    await _queue_delete(db)
    google.delete(f"{TASKS_API}/shop/tasks/g1").respond(204)
    google.get(f"{TASKS_API}/shop/tasks").respond(200, json={"items": []})

    assert await task_sync.run_sync(db) is True

    row = await _status(db)
    assert row["connected"] == 1
    assert row["last_success_at"] and row["last_cycle_at"] == row["last_success_at"]
    assert row["consecutive_failures"] == 0 and row["failing_since"] is None
    assert row["queue_depth"] == 0
    assert (await sync_status.summary(db))["state"] == "ok"


async def test_an_offline_cycle_records_the_failure_and_keeps_the_queue(db, connected, google):
    await _queue_delete(db)
    google.delete(f"{TASKS_API}/shop/tasks/g1").mock(side_effect=httpx.ConnectError("offline"))

    await task_sync.run_sync(db)
    await task_sync.run_sync(db)

    row = await _status(db)
    assert row["last_error"] == "offline"
    assert row["consecutive_failures"] == 2
    assert row["last_failure_at"] and row["failing_since"]
    assert row["last_success_at"] is None
    assert row["queue_depth"] == 1
    assert row["auth_failures"] == 0
    # A couple of minutes of failure is a blip: Admin says "Retrying", no dot.
    summary = await sync_status.summary(db)
    assert summary["state"] == "retrying" and summary["level"] is None


async def test_never_connected_is_recorded_but_not_alarming(db):
    await task_sync.run_sync(db)

    row = await _status(db)
    assert row["connected"] == 0
    assert row["last_error"] == "not connected"
    assert row["consecutive_failures"] == 0
    summary = await sync_status.summary(db)
    assert summary["state"] == "unlinked" and summary["level"] is None
    assert "Connect a Google account" in summary["detail"]


async def test_a_revoked_grant_shows_as_disconnected(db, google):
    await google_oauth.store_tokens(
        db, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": time.time() - 10}, "f@example.com"
    )
    await sync_status.record_success(db)  # it was working before
    google.post(google_oauth.TOKEN_ENDPOINT).respond(400, json={"error": "invalid_grant"})

    await task_sync.run_sync(db)

    summary = await sync_status.summary(db)
    assert summary["state"] == "disconnected" and summary["level"] == "red"


# --- Failures that aren't Google's ---------------------------------------------

async def test_an_unexpected_error_in_a_cycle_is_recorded_not_raised(db, connected, monkeypatch, caplog):
    async def broken_reconcile(db, token):
        raise KeyError("updated")
    monkeypatch.setattr(task_sync, "reconcile_shopping", broken_reconcile)

    with caplog.at_level(logging.WARNING, logger="app.task_sync"):
        for _ in range(sync_status.ATTENTION_AFTER + 2):
            assert await task_sync.run_sync(db) is True

    row = await _status(db)
    assert row["last_error"] == "error"
    assert row["consecutive_failures"] == sync_status.ATTENTION_AFTER + 2
    assert row["auth_failures"] == 0  # never "needs attention"
    assert (await sync_status.summary(db))["state"] != "attention"
    warnings = _warnings(caplog)
    assert len(warnings) == 1 and "unexpected error: KeyError" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None  # the traceback, once
    assert not task_sync.sync_in_progress()


async def test_a_failing_status_write_does_not_fail_the_cycle(client, db, connected, google, monkeypatch, caplog):
    async def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(sync_status, "record_success", locked)
    monkeypatch.setattr(sync_status, "record_failure", locked)
    _mock_admin_pickers(google)
    await _login(client)

    with caplog.at_level(logging.WARNING, logger="app.task_sync"):
        assert await task_sync.run_sync(db) is True  # success path
        resp = await client.post("/admin/sync")
        assert resp.status_code == 303 and resp.headers["location"] == "/admin?sync=done#sync"
        await _queue_delete(db)
        google.delete(f"{TASKS_API}/shop/tasks/g1").mock(side_effect=httpx.ConnectError("offline"))
        assert await task_sync.run_sync(db) is True  # failure path

    assert any("Couldn't save the sync status: OperationalError" in r.getMessage() for r in _warnings(caplog))
    assert not task_sync.sync_in_progress()


async def test_the_lock_is_released_when_a_cycle_is_cancelled(db, connected, monkeypatch):
    async def cancelled(db):
        raise asyncio.CancelledError
    monkeypatch.setattr(google_oauth, "get_valid_access_token", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await task_sync.run_sync(db)

    assert not task_sync.sync_in_progress()


# --- 403 reasons ------------------------------------------------------------------

RATE_LIMIT_403 = {"error": {
    "code": 403, "status": "PERMISSION_DENIED", "message": "Rate Limit Exceeded",
    "errors": [{"reason": "userRateLimitExceeded", "domain": "usageLimits"}],
}}
SCOPE_403 = {"error": {
    "code": 403, "status": "PERMISSION_DENIED", "message": "Request had insufficient authentication scopes.",
    "errors": [{"reason": "insufficientPermissions"}],
    "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
}}


def _status_error(status, body=None):
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(status, json=body) if body is not None else httpx.Response(status)
    return httpx.HTTPStatusError("x", request=request, response=response)


def test_403_reasons_map_to_short_codes():
    assert sync_status.error_code(_status_error(403, SCOPE_403)) == "HTTP 403"
    assert sync_status.error_code(_status_error(403)) == "HTTP 403"  # no reason given: assume access
    assert sync_status.error_code(_status_error(403, RATE_LIMIT_403)) == "rate limited"
    rate = {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
    assert sync_status.error_code(_status_error(403, rate)) == "rate limited"
    other = {"error": {"errors": [{"reason": "somethingElse"}]}}
    assert sync_status.error_code(_status_error(403, other)) == "HTTP 403 other"
    assert sync_status.error_code(KeyError("x")) == "error"


async def test_rate_limit_403s_never_need_attention(db, connected, google):
    await _queue_delete(db)
    google.delete(f"{TASKS_API}/shop/tasks/g1").respond(403, json=RATE_LIMIT_403)

    for _ in range(sync_status.ATTENTION_AFTER + 2):
        await task_sync.run_sync(db)

    row = await _status(db)
    assert row["last_error"] == "rate limited" and row["auth_failures"] == 0
    assert (await sync_status.summary(db))["state"] != "attention"
    assert "Rate Limit" not in json.dumps(dict(row))  # the code only, never the body


async def test_scope_403s_need_attention(db, connected, google):
    await _queue_delete(db)
    google.delete(f"{TASKS_API}/shop/tasks/g1").respond(403, json=SCOPE_403)

    for _ in range(sync_status.ATTENTION_AFTER):
        await task_sync.run_sync(db)

    assert (await sync_status.summary(db))["state"] == "attention"


# --- Reconnect / disconnect ---------------------------------------------------------

async def _needs_attention(db):
    for _ in range(sync_status.ATTENTION_AFTER):
        await sync_status.record_failure(db, _status_error(403))
    assert (await sync_status.summary(db))["state"] == "attention"


async def test_reconnecting_clears_needs_attention(client, db, connected, google):
    await _needs_attention(db)
    google.post(google_oauth.TOKEN_ENDPOINT).respond(200, json={
        "access_token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 3599,
    })
    google.get(google_oauth.USERINFO_ENDPOINT).respond(200, json={"email": "family@example.com"})
    await _login(client)
    client.cookies.set(calendar.STATE_COOKIE, "the-state")

    resp = await client.get("/admin/google/callback", params={"code": "c", "state": "the-state"})

    assert resp.status_code == 303
    row = await _status(db)
    assert row["auth_failures"] == 0 and row["consecutive_failures"] == 0 and row["last_error"] is None
    assert (await sync_status.summary(db))["state"] == "waiting"


async def test_a_failed_reconnect_leaves_the_status_alone(client, db, connected, google):
    await _needs_attention(db)
    google.post(google_oauth.TOKEN_ENDPOINT).respond(400, json={"error": "invalid_grant"})
    await _login(client)
    client.cookies.set(calendar.STATE_COOKIE, "the-state")

    await client.get("/admin/google/callback", params={"code": "c", "state": "the-state"})

    assert (await sync_status.summary(db))["state"] == "attention"


async def test_disconnecting_clears_the_status_and_hides_the_dot(client, db, connected, google):
    await _needs_attention(db)
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)
    await _login(client)

    resp = await client.post("/admin/google/disconnect")

    assert resp.status_code == 303
    assert (await sync_status.summary(db))["state"] == "unlinked"
    assert "<a" not in (await client.get("/sync-status")).text


async def test_stored_status_never_holds_tokens_or_urls(db, google):
    await google_oauth.store_tokens(
        db, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": time.time() + 3600}, "f@example.com"
    )
    await _queue_delete(db)
    google.delete(f"{TASKS_API}/shop/tasks/g1").respond(
        403, json={"error": {"message": f"bad token {ACCESS}"}}
    )

    await task_sync.run_sync(db)

    stored = json.dumps(dict(await _status(db)))
    assert "HTTP 403" in stored
    for secret in (ACCESS, REFRESH, "googleapis", "http://", "https://", "f@example.com"):
        assert secret not in stored
    health = json.dumps(sync_status.health_summary(await sync_status.summary(db)))
    for secret in (ACCESS, REFRESH, "googleapis", "f@example.com"):
        assert secret not in health


# --- Persistent 401/403 -> needs attention -------------------------------------

async def test_a_persistent_403_needs_attention_after_n_cycles_and_recovers(db, connected, google, caplog):
    await _queue_delete(db)
    delete = google.delete(f"{TASKS_API}/shop/tasks/g1")
    delete.respond(403)
    google.get(f"{TASKS_API}/shop/tasks").respond(200, json={"items": []})

    with caplog.at_level(logging.INFO, logger="app.task_sync"):
        for _ in range(sync_status.ATTENTION_AFTER - 1):
            await task_sync.run_sync(db)
        assert (await sync_status.summary(db))["state"] != "attention"
        assert len(_warnings(caplog)) == 1  # just the ordinary once-per-outage one

        await task_sync.run_sync(db)
        summary = await sync_status.summary(db)
        assert summary["state"] == "attention" and summary["level"] == "red"
        assert "reconnect in Admin" in summary["detail"]
        warnings = _warnings(caplog)
        assert len(warnings) == 2 and "needs attention" in warnings[1].getMessage()

        for _ in range(5):  # stays needing attention; no more WARNINGs
            await task_sync.run_sync(db)
        assert len(_warnings(caplog)) == 2

        # Nothing was dropped or burned while Google refused access.
        rows = await (await db.execute("SELECT retry_count FROM sync_queue")).fetchall()
        assert [r["retry_count"] for r in rows] == [0]

        delete.respond(204)  # reconnected / access restored
        await task_sync.run_sync(db)

    summary = await sync_status.summary(db)
    assert summary["state"] == "ok" and summary["queue_depth"] == 0
    assert (await _status(db))["auth_failures"] == 0
    recovered = [r.getMessage() for r in caplog.records if r.name == "app.task_sync" and r.levelno == logging.INFO]
    assert any("sync access recovered" in m for m in recovered)


async def test_a_401_counts_towards_attention_too(db, connected, google):
    await _queue_delete(db)
    google.delete(f"{TASKS_API}/shop/tasks/g1").respond(401)

    for _ in range(sync_status.ATTENTION_AFTER):
        await task_sync.run_sync(db)

    assert (await sync_status.summary(db))["state"] == "attention"


async def test_a_different_error_restarts_the_403_count(db, connected, google):
    await _queue_delete(db)
    side_effects = [httpx.Response(403)] * (sync_status.ATTENTION_AFTER - 1)
    side_effects += [httpx.ConnectError("offline")] + [httpx.Response(403)] * (sync_status.ATTENTION_AFTER - 1)
    google.delete(f"{TASKS_API}/shop/tasks/g1").mock(side_effect=side_effects)

    for _ in range(len(side_effects)):
        await task_sync.run_sync(db)

    assert (await _status(db))["auth_failures"] == sync_status.ATTENTION_AFTER - 1
    assert (await sync_status.summary(db))["state"] != "attention"


async def test_a_429_is_never_needs_attention(db, connected, google):
    await _queue_delete(db)
    google.delete(f"{TASKS_API}/shop/tasks/g1").respond(429)

    for _ in range(sync_status.ATTENTION_AFTER + 2):
        await task_sync.run_sync(db)

    row = await _status(db)
    assert row["last_error"] == "HTTP 429" and row["auth_failures"] == 0


# --- Summary states --------------------------------------------------------------

async def test_long_failure_turns_amber_after_the_grace_period(db, connected):
    start = datetime.now(timezone.utc) - timedelta(minutes=7)
    await sync_status.record_success(db, now=start - timedelta(minutes=1))
    await sync_status.record_failure(db, httpx.ConnectError("x"), now=start)
    await sync_status.record_failure(db, httpx.ConnectError("x"))

    summary = await sync_status.summary(db)
    assert summary["state"] == "failing" and summary["level"] == "amber"
    assert "Can't reach Google" in summary["detail"] and "7 minutes" in summary["detail"]
    assert summary["last_success_ago"] == "8 minutes ago"


async def test_no_cycle_for_a_long_time_is_stalled(db, connected, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(sync_status, "PROCESS_STARTED_AT", now - timedelta(hours=1))
    await sync_status.record_success(db, now=now - timedelta(minutes=20))
    summary = await sync_status.summary(db)
    assert summary["state"] == "stalled" and summary["level"] == "amber"


async def test_just_after_a_restart_an_old_last_cycle_is_not_stalled(db, connected, monkeypatch):
    # Down for two hours; the first cycle only runs a minute after start.
    now = datetime.now(timezone.utc)
    await sync_status.record_success(db, now=now - timedelta(hours=2))
    monkeypatch.setattr(sync_status, "PROCESS_STARTED_AT", now - timedelta(seconds=30))
    assert (await sync_status.summary(db))["state"] == "ok"
    # ...but if the scheduler never gets going, it is stalled 15 minutes after start.
    monkeypatch.setattr(sync_status, "PROCESS_STARTED_AT", now - timedelta(minutes=16))
    assert (await sync_status.summary(db))["state"] == "stalled"


async def test_unconfigured_google_shows_nothing(db, monkeypatch):
    monkeypatch.setattr(google_oauth, "is_configured", lambda: False)
    summary = await sync_status.summary(db)
    assert summary["state"] == "off" and summary["level"] is None


async def test_ago_wording():
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    assert sync_status.ago(now - timedelta(seconds=20), now) == "just now"
    assert sync_status.ago(now - timedelta(minutes=1), now) == "1 minute ago"
    assert sync_status.ago(now - timedelta(minutes=3, seconds=50), now) == "3 minutes ago"
    assert sync_status.ago(now - timedelta(hours=2), now) == "2 hours ago"
    assert sync_status.ago(now - timedelta(days=1), now) == "1 day ago"
    assert sync_status.ago(None, now) is None


# --- Dashboard dot -----------------------------------------------------------------

async def test_dashboard_loads_the_dot_in_the_top_bar_outside_any_widget(client):
    html = (await client.get("/")).text
    topbar = html[html.index('class="topbar"'):html.index('id="dashboard-scroll"')]
    actions = topbar[topbar.index('class="topbar-actions"'):]
    assert 'id="sync-dot"' in actions and 'hx-get="/sync-status"' in actions and 'href="/admin"' in actions
    assert html.count('id="sync-dot"') == 1


async def test_dot_is_hidden_while_healthy(client, db, connected):
    await sync_status.record_success(db)
    html = (await client.get("/sync-status")).text
    assert 'id="sync-dot"' in html and 'hx-trigger="every 60s"' in html and 'hx-swap="outerHTML"' in html
    assert 'data-state="ok"' in html
    assert "<a" not in html and "sync-dot-amber" not in html and "sync-dot-red" not in html


async def test_dot_is_hidden_during_a_short_blip(client, db, connected):
    await sync_status.record_failure(db, httpx.ConnectError("x"))
    html = (await client.get("/sync-status")).text
    assert 'data-state="retrying"' in html and "<a" not in html


async def test_dot_is_amber_when_failing_for_a_while(client, db, connected):
    await sync_status.record_failure(db, httpx.ConnectError("x"), now=datetime.now(timezone.utc) - timedelta(minutes=6))
    await sync_status.record_failure(db, httpx.ConnectError("x"))
    html = (await client.get("/sync-status")).text
    assert "sync-dot-amber" in html and 'href="/admin#sync"' in html and "Sync delayed" in html
    assert 'hx-trigger="every 60s"' in html  # keeps polling in every state


async def test_dot_is_red_when_needing_attention(client, db, connected):
    for _ in range(sync_status.ATTENTION_AFTER):
        await sync_status.record_failure(db, httpx.HTTPStatusError(
            "x", request=httpx.Request("GET", "https://example.test"), response=httpx.Response(403)))
    html = (await client.get("/sync-status")).text
    assert "sync-dot-red" in html and "Needs attention" in html and 'href="/admin#sync"' in html


async def test_dot_is_red_when_google_drops_the_connection(client, db):
    await sync_status.record_success(db)
    await sync_status.record_not_connected(db)  # e.g. the grant was revoked
    html = (await client.get("/sync-status")).text
    assert "sync-dot-red" in html and "Google disconnected" in html


async def test_dot_is_hidden_on_a_fresh_install(client):
    html = (await client.get("/sync-status")).text
    assert 'data-state="unlinked"' in html and "<a" not in html


async def test_dot_is_hidden_when_google_is_not_set_up(client, monkeypatch):
    monkeypatch.setattr(google_oauth, "is_configured", lambda: False)
    html = (await client.get("/sync-status")).text
    assert 'data-state="off"' in html and "<a" not in html


# --- Admin panel + Sync now ---------------------------------------------------------

async def test_admin_sync_panel_renders_in_family_time(client, db, connected, google):
    _mock_admin_pickers(google)
    # 11:05 UTC is 12:05 in Europe/London (BST), the test family's timezone.
    await sync_status.record_success(db, now=datetime(2026, 9, 28, 11, 5, tzinfo=timezone.utc))
    await _login(client)
    html = (await client.get("/admin")).text
    panel = html[html.index('id="sync"'):html.index("<!-- Weather location -->")]
    assert "<h2>Sync</h2>" in panel
    assert "Last successful sync" in panel and "12:05" in panel
    assert "Waiting to sync" in panel and "0 changes" in panel
    assert 'action="/admin/sync"' in panel and "Sync now" in panel


async def test_sync_now_requires_admin(client, db, connected):
    resp = await client.post("/admin/sync")
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"
    assert (await _status(db))["last_cycle_at"] is None  # nothing ran


async def test_sync_now_runs_one_cycle(client, db, connected, google):
    await _queue_delete(db)
    delete = google.delete(f"{TASKS_API}/shop/tasks/g1").respond(204)
    google.get(f"{TASKS_API}/shop/tasks").respond(200, json={"items": []})
    _mock_admin_pickers(google)
    await _login(client)

    resp = await client.post("/admin/sync")

    assert resp.status_code == 303 and resp.headers["location"] == "/admin?sync=done#sync"
    assert delete.call_count == 1
    assert (await _status(db))["last_success_at"] is not None
    html = (await client.get("/admin?sync=done")).text
    assert "Sync ran just now" in html


async def test_sync_now_does_not_overlap_a_running_cycle(client, db, connected, google):
    _mock_admin_pickers(google)
    await _login(client)
    async with task_sync._sync_lock:  # the scheduler's cycle is mid-run
        assert task_sync.sync_in_progress()
        resp = await client.post("/admin/sync")
        assert await task_sync.run_sync(db) is False
    assert resp.headers["location"] == "/admin?sync=busy#sync"
    assert (await _status(db))["last_cycle_at"] is None
    assert "A sync was already running" in (await client.get("/admin?sync=busy")).text


async def test_admin_ignores_unknown_sync_messages(client, db):
    await _login(client)
    html = (await client.get("/admin?sync=<script>")).text
    assert "<script>" not in html.split('id="sync"')[1].split("<!-- Weather")[0]


# --- /health ------------------------------------------------------------------------

async def test_health_stays_200_with_google_down(client, db, connected, google):
    await _queue_delete(db)
    google.delete(f"{TASKS_API}/shop/tasks/g1").mock(side_effect=httpx.ConnectError("offline"))
    for _ in range(sync_status.ATTENTION_AFTER + 1):
        await task_sync.run_sync(db)

    resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["db"] == "ok"
    assert body["scheduler"] == "stopped"  # tests skip the lifespan
    assert body["sync"]["last_error"] == "offline"
    assert body["sync"]["queue_depth"] == 1
    assert body["sync"]["consecutive_failures"] == sync_status.ATTENTION_AFTER + 1


async def test_health_stays_200_when_sync_needs_attention(client, db, connected):
    for _ in range(sync_status.ATTENTION_AFTER):
        await sync_status.record_failure(db, httpx.HTTPStatusError(
            "x", request=httpx.Request("GET", "https://example.test"), response=httpx.Response(403)))
    resp = await client.get("/health")
    assert resp.status_code == 200 and resp.json()["sync"]["state"] == "attention"


async def test_health_is_503_when_the_database_cannot_be_opened(client, monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path)  # a directory: SQLite can't open it
    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["db"] == "error"


# --- Migration ------------------------------------------------------------------------

async def test_migration_is_idempotent_and_keeps_status(db, connected):
    await sync_status.record_success(db)
    before = dict(await _status(db))

    await database.init_db()
    await database.init_db()

    (count,) = await (await db.execute("SELECT COUNT(*) FROM sync_status")).fetchone()
    assert count == 1
    assert dict(await _status(db)) == before
