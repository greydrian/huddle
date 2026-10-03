"""Several Google accounts, each with its own jobs (spec 12), stage 1:
migration 13, per-account tokens and states, the Admin Google accounts panel
(add, edit, reconnect, remove) and calendars from every account."""

import asyncio
import json
import logging
import re
import sqlite3
import time
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from app import (
    database,
    google_accounts,
    google_calendar,
    google_oauth,
    migrations,
    school_email,
    security,
    sync_status,
    task_sync,
)
from app.routers import admin
from app.routers.admin import google as admin_google
from app.security import create_session_token
from app.services import accounts, calendar_prefs, calendar_view, school_events

EVENTS = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"
TASKS_API = "https://tasks.googleapis.com/tasks/v1/lists"
ALL_SCOPES = google_oauth.SCOPES
FAMILY_CAL = "family@group.calendar.google.com"
SHARED_CAL = "shared@group.calendar.google.com"
MUM, DAD, RILEY, JAMIE = 1, 2, 3, 4


def timed(title, day="2026-08-11", start="09:00"):
    moment = f"{day}T{start}:00+01:00"
    return {"id": title.replace(" ", ""), "summary": title, "start": {"dateTime": moment}, "end": {"dateTime": moment}}


def calendar_api(answers: dict):
    """events.list answered per (access token, calendar id): a list of raw
    events, an exception to raise, or a coroutine function (a slow Google).
    Anything not listed 404s."""

    async def answer(request):
        token = request.headers["authorization"].removeprefix("Bearer ")
        cal = unquote(request.url.path.split("/calendars/")[1].split("/")[0])
        found = answers.get((token, cal))
        if isinstance(found, Exception):
            raise found
        if callable(found):
            return await found(request)
        if found is None:
            return httpx.Response(404)
        return httpx.Response(200, json={"items": found})

    return answer


def refresh_api(answers: dict):
    """The token endpoint answered per refresh token."""

    def answer(request):
        refresh = parse_qs(request.content.decode())["refresh_token"][0]
        found = answers[refresh]
        if isinstance(found, Exception):
            raise found
        return found

    return answer


def _titles(grid) -> dict[str, str]:
    """{title: colour} of every bar in a month grid."""
    return {bar["event"]["title"]: bar["event"]["color"] for week in grid["weeks"] for bar in week["bars"]}


def _tokens(n: int, scope: str, expires_in: float = 3600) -> dict:
    return {
        "access_token": f"tok-{n}",
        "refresh_token": f"r-{n}",
        "expires_at": time.time() + expires_in,
        "scope": scope,
    }


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


@pytest.fixture
async def two_accounts(db):
    """Account 1: the family's, every job (Family). Account 2: Riley's,
    Calendars only. Shown: the Family calendar (account 1) and Riley's
    primary (account 2)."""
    await google_oauth.store_tokens(db, _tokens(1, ALL_SCOPES), "family@example.com")
    await google_oauth.store_tokens(
        db, _tokens(2, f"{google_oauth.CALENDAR_READ_SCOPE} openid email"), "riley@example.com", owner_id=RILEY
    )
    await google_oauth.set_selected_calendars(
        db,
        [
            {"account": 1, "id": FAMILY_CAL, "summary": "Family", "color": "#123456"},
            {"account": 2, "id": "riley@example.com", "summary": "Riley", "color": "#654321"},
        ],
    )
    return await google_accounts.list_accounts(db)


# --- Migration 13 -------------------------------------------------------------------------


OLD_SETTINGS = {
    "google_selected_calendars": [
        {"id": FAMILY_CAL, "summary": "Family", "color": "#123456", "primary": False},
        {"id": "riley@example.com", "summary": "Riley", "color": "#654321", "primary": False},
    ],
    "calendar_people": {"riley@example.com": RILEY, FAMILY_CAL: "everyone"},
    "calendar_family": {"id": FAMILY_CAL, "summary": "Family"},
    "school_events_calendar": {"id": FAMILY_CAL, "summary": "Family"},
    "google_shopping_tasklist": {"id": "shop", "title": "Shopping"},
}


async def _upgrade(tmp_path, monkeypatch, tokens: dict | None, encrypted: str | None = None, settings=OLD_SETTINGS):
    """A database as the family's is before migration 13 (one connection in
    auth_tokens, the old settings), then upgraded. Returns the old google row."""
    path = tmp_path / "upgrade.db"
    every = list(migrations.MIGRATIONS)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(migrations, "MIGRATIONS", [m for m in every if m.version < 13])
    await database.init_db()
    with sqlite3.connect(path) as conn:
        if tokens is not None or encrypted is not None:
            conn.execute(
                "INSERT INTO auth_tokens (service_name, account_email, encrypted_token_json) VALUES (?, ?, ?)",
                ("google", "family@example.com", encrypted or security.encrypt_token_json(tokens)),
            )
        conn.execute(
            "INSERT INTO auth_tokens (service_name, account_email, encrypted_token_json) VALUES "
            "('google_photos', NULL, 'photos-encrypted')"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in settings.items()]
            + [("calendar_timezone", "Europe/London"), ("pin_is_default", "0")],
        )
        conn.execute("UPDATE profiles SET google_tasklist_id = 'list-riley' WHERE id = ?", (RILEY,))
        old = conn.execute("SELECT * FROM auth_tokens ORDER BY service_name").fetchall()
    monkeypatch.setattr(migrations, "MIGRATIONS", every)
    await database.init_db()
    await database.init_db()  # and again: nothing more happens
    return old


async def test_migration_makes_the_connection_account_1_and_the_wall_looks_the_same(
    tmp_path, monkeypatch, google, admin_client
):
    old = await _upgrade(tmp_path, monkeypatch, _tokens(0, ALL_SCOPES) | {"access_token": "tok"})
    with sqlite3.connect(database.DB_PATH) as conn:
        assert conn.execute("SELECT * FROM auth_tokens ORDER BY service_name").fetchall() == old  # rollback
        [row] = conn.execute("SELECT id, email, owner_profile_id, jobs, encrypted_token_json FROM google_accounts")
        assert row[:4] == (1, "family@example.com", None, "calendars tasks school_email write_events")
        assert row[4] == old[0][2]  # the very same encrypted token
        assert conn.execute("SELECT google_account_id FROM profiles WHERE id = ?", (RILEY,)).fetchone() == (1,)

    google.get(url__regex=EVENTS).mock(
        side_effect=calendar_api(
            {("tok", FAMILY_CAL): [timed("Bins out", start="19:00")], ("tok", "riley@example.com"): [timed("Swimming")]}
        )
    )
    async with database.get_db() as db:
        grid = await google_calendar.get_month_grid(db, 2026, 8)
        assert _titles(grid) == {"Bins out": "#123456", "Swimming": "#654321"}
        assert not grid["offline"] and grid["updated_label"] is None
        # Whose calendar is whose, the wall's "+" and the school events calendar are as before.
        links = await calendar_prefs.get_people_links(db)
        events = {e["title"]: e for w in grid["weeks"] for e in (b["event"] for b in w["bars"])}
        profiles = [{"id": i, "name": n} for i, n in ((RILEY, "Riley"), (JAMIE, "Jamie"))]
        assert calendar_view.event_owners(events["Swimming"], links, profiles) == {RILEY}
        assert calendar_view.event_owners(events["Bins out"], links, profiles) is None
        assert await calendar_view.can_add(db)
        target = await school_events.get_target_calendar(db)
        assert target == {"account": 1, "id": FAMILY_CAL, "summary": "Family"}
        assert await school_events._can_write(db, target)
        assert await google_oauth.connect_job(db, "school_email") == ("tok", False)
        assert (await sync_status.summary(db))["dot"]["level"] is None
        # The old person links are still there for a rollback, next to the new ones.
        raw = json.loads(await database.get_setting(db, "calendar_people"))
        assert raw["riley@example.com"] == RILEY and raw["1:riley@example.com"] == RILEY

        # Tasks and the shopping list still sync with the same lists.
        shop = google.get(f"{TASKS_API}/shop/tasks").respond(200, json={"items": []})
        riley = google.get(f"{TASKS_API}/list-riley/tasks").respond(200, json={"items": []})
        await task_sync.run_sync(db)
        assert shop.called and riley.called
        assert riley.calls.last.request.headers["authorization"] == "Bearer tok"
        assert (await sync_status.summary(db))["state"] == "ok"

    html = (await admin_client.get("/widgets/calendar?year=2026&month=8")).text
    assert "Bins out" in html and "Swimming" in html and "cal-add-toggle" in html


async def test_migration_of_a_grant_from_before_scopes_were_kept(tmp_path, monkeypatch):
    await _upgrade(tmp_path, monkeypatch, {"access_token": "tok", "refresh_token": "r", "expires_at": 0})
    async with database.get_db() as db:
        [account] = await google_accounts.list_accounts(db)
    assert account["jobs"] == ["calendars", "tasks"] and account["state"] == google_accounts.CONNECTED


async def test_migration_copes_with_an_unreadable_token_and_odd_settings(tmp_path, monkeypatch):
    odd = {"google_selected_calendars": ["primary"], "calendar_people": [1], "calendar_family": "x"}
    old = await _upgrade(tmp_path, monkeypatch, None, encrypted="gAAAA-not-ours", settings=odd)
    with sqlite3.connect(database.DB_PATH) as conn:
        assert conn.execute("SELECT jobs, encrypted_token_json FROM google_accounts").fetchall() == [
            ("calendars tasks", "gAAAA-not-ours")
        ]
        assert conn.execute("SELECT * FROM auth_tokens ORDER BY service_name").fetchall() == old
        assert json.loads(
            conn.execute("SELECT value FROM app_settings WHERE key = 'google_selected_calendars'").fetchone()[0]
        ) == ["primary"]
    async with database.get_db() as db:
        assert (await google_accounts.get(db, 1))["state"] == google_accounts.RECONNECT
        assert await google_oauth.get_selected_calendars(db) == [
            {**google_oauth.DEFAULT_CALENDAR, "account": 1, "key": "1:primary"}
        ]


async def test_migration_without_a_connection_adds_no_account(tmp_path, monkeypatch):
    await _upgrade(tmp_path, monkeypatch, None)
    with sqlite3.connect(database.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM google_accounts").fetchone() == (0,)
        family = conn.execute("SELECT value FROM app_settings WHERE key = 'calendar_family'").fetchone()[0]
        assert json.loads(family) == OLD_SETTINGS["calendar_family"]  # untouched


# --- Calendars from every account (spec 12.3) ---------------------------------------------


async def test_calendars_from_every_account_show_together(db, google, two_accounts):
    route = google.get(url__regex=EVENTS).mock(
        side_effect=calendar_api(
            {("tok-1", FAMILY_CAL): [timed("Bins out")], ("tok-2", "riley@example.com"): [timed("Swimming")]}
        )
    )
    grid = await google_calendar.get_month_grid(db, 2026, 8)
    assert _titles(grid) == {"Bins out": "#123456", "Swimming": "#654321"}
    assert route.call_count == 2  # each with its own account's token
    # A calendar's person defaults to its account's owner (Riley); Family = everyone.
    assert await calendar_prefs.get_people_links(db) == {"2:riley@example.com": RILEY}


async def test_one_account_offline_while_the_other_loads_live(db, google, two_accounts):
    answers = {("tok-1", FAMILY_CAL): [timed("Bins out")], ("tok-2", "riley@example.com"): [timed("Swimming")]}
    google.get(url__regex=EVENTS).mock(side_effect=calendar_api(answers))
    await google_calendar.get_month_grid(db, 2026, 8)  # both live: the cache is filled

    # Riley's account now needs a refresh, and Google can't be reached for it.
    await google_accounts.save_tokens(db, 2, _tokens(2, google_oauth.CALENDAR_READ_SCOPE, expires_in=-10))
    google.post(google_oauth.TOKEN_ENDPOINT).mock(side_effect=refresh_api({"r-2": httpx.ConnectError("down")}))
    answers[("tok-1", FAMILY_CAL)] = [timed("Bins out"), timed("Film night", start="19:30")]

    grid = await google_calendar.get_month_grid(db, 2026, 8)

    assert set(_titles(grid)) == {"Bins out", "Film night", "Swimming"}  # live + Riley's saved copy
    assert grid["updated_label"] and not grid["offline"]
    states = {a["id"]: a["state"] for a in await google_accounts.list_accounts(db)}
    assert states == {1: google_accounts.CONNECTED, 2: google_accounts.OFFLINE}
    assert await google_accounts.load_tokens(db, 2) is not None  # offline is never disconnected


async def test_a_slow_account_does_not_slow_or_blank_the_others(db, google, two_accounts, monkeypatch):
    monkeypatch.setattr(google_calendar, "CALENDAR_DEADLINE", 0.3)

    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"items": []})

    google.get(url__regex=EVENTS).mock(
        side_effect=calendar_api({("tok-1", FAMILY_CAL): [timed("Bins out")], ("tok-2", "riley@example.com"): hang})
    )
    started = time.monotonic()
    grid = await google_calendar.get_month_grid(db, 2026, 8)

    assert time.monotonic() - started < 2
    assert set(_titles(grid)) == {"Bins out"} and grid["offline"]  # Riley's: nothing saved yet
    assert (await google_accounts.get(db, 2))["state"] == google_accounts.OFFLINE


async def test_invalid_grant_on_one_account_only(db, google, two_accounts, client):
    answers = {("tok-1", FAMILY_CAL): [timed("Bins out")], ("tok-2", "riley@example.com"): [timed("Swimming")]}
    google.get(url__regex=EVENTS).mock(side_effect=calendar_api(answers))
    await google_calendar.get_month_grid(db, 2026, 8)
    await google_accounts.save_tokens(db, 2, _tokens(2, google_oauth.CALENDAR_READ_SCOPE, expires_in=-10))
    google.post(google_oauth.TOKEN_ENDPOINT).mock(
        side_effect=refresh_api({"r-2": httpx.Response(400, json={"error": "invalid_grant"})})
    )

    grid = await google_calendar.get_month_grid(db, 2026, 8)

    assert set(_titles(grid)) == {"Bins out", "Swimming"} and grid["updated_label"]  # Riley's from the cache
    riley = await google_accounts.get(db, 2)
    assert riley["state"] == google_accounts.RECONNECT and riley["email"] == "riley@example.com"
    assert riley["owner_id"] == RILEY and riley["jobs"] == ["calendars"]  # the row and links stay
    assert [c["key"] for c in await google_oauth.get_selected_calendars(db)] == [
        f"1:{FAMILY_CAL}",
        "2:riley@example.com",
    ]
    assert (await google_accounts.get(db, 1))["state"] == google_accounts.CONNECTED
    assert await google_accounts.load_tokens(db, 1) is not None
    # The top-bar dot shows the worst account.
    dot = (await client.get("/sync-status")).text
    assert 'data-state="account-reconnect"' in dot and "sync-dot-red" in dot and "riley@" not in dot


async def test_a_calendar_shared_into_two_accounts_is_shown_once(db, google, two_accounts, admin_client):
    await google_oauth.set_selected_calendars(
        db,
        [
            {"account": 1, "id": SHARED_CAL, "summary": "Shared", "color": "#111111"},
            {"account": 2, "id": SHARED_CAL, "summary": "Shared", "color": "#222222"},
            {"account": 2, "id": "primary", "summary": "Riley", "color": "#654321"},
            {"account": 1, "id": "primary", "summary": "Family", "color": "#123456"},
        ],
    )
    shown = await google_oauth.get_selected_calendars(db)
    assert [c["key"] for c in shown] == [f"1:{SHARED_CAL}", "2:primary", "1:primary"]  # primary is per account
    route = google.get(url__regex=EVENTS).mock(
        side_effect=calendar_api(
            {("tok-1", SHARED_CAL): [timed("Party")], ("tok-1", "primary"): [], ("tok-2", "primary"): []}
        )
    )
    grid = await google_calendar.get_month_grid(db, 2026, 8)
    assert _titles(grid) == {"Party": "#111111"} and route.call_count == 3

    lists = {
        "family@example.com": [{"id": SHARED_CAL, "summary": "Shared"}, {"id": "family@example.com", "primary": True}],
        "riley@example.com": [{"id": SHARED_CAL, "summary": "Shared"}, {"id": "riley@example.com", "primary": True}],
    }
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "items": lists[
                    "family@example.com" if "tok-1" in request.headers["authorization"] else "riley@example.com"
                ]
            },
        )
    )
    google.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(200, json={"items": []})
    page = (await admin_client.get("/admin?tab=google")).text
    assert re.search(r'value="2:shared@group\.calendar\.google\.com"\s+disabled', page)
    assert "Shown through family@example.com" in page

    # Ticking both copies keeps one, through the first account.
    await admin_client.post(
        "/admin/google/calendars", data={"calendar_id": [f"1:{SHARED_CAL}", f"2:{SHARED_CAL}", "2:riley@example.com"]}
    )
    assert [c["key"] for c in await google_oauth.get_selected_calendars(db)] == [
        f"1:{SHARED_CAL}",
        "2:riley@example.com",
    ]


async def test_picker_save_keeps_an_unreachable_accounts_calendars(db, google, two_accounts, admin_client):
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).mock(
        side_effect=lambda request: (
            httpx.Response(200, json={"items": [{"id": "work", "summary": "Work"}]})
            if "tok-1" in request.headers["authorization"]
            else httpx.Response(503)
        )
    )
    await admin_client.post("/admin/google/calendars", data={"calendar_id": ["1:work"]})
    assert [c["key"] for c in await google_oauth.get_selected_calendars(db)] == ["1:work", "2:riley@example.com"]


async def test_unticking_calendars_takes_an_accounts_calendars_off_the_wall(db, two_accounts):
    await google_accounts.update(db, 2, RILEY, ["write_events"])  # normalised: implies Calendars
    assert (await google_accounts.get(db, 2))["jobs"] == ["calendars", "write_events"]
    await google_accounts.update(db, 2, RILEY, [])
    assert [c["key"] for c in await google_oauth.get_selected_calendars(db)] == [f"1:{FAMILY_CAL}"]


# --- Add account, Reconnect (spec 12.2) ---------------------------------------------------


@pytest.mark.parametrize(
    ("jobs", "scopes"),
    [
        (["calendars"], {google_oauth.CALENDAR_READ_SCOPE}),
        (["tasks"], {google_oauth.TASKS_SCOPE}),
        (["write_events"], {google_oauth.CALENDAR_READ_SCOPE, google_oauth.CALENDAR_EVENTS_SCOPE}),
        (["school_email", "nonsense"], {google_oauth.GMAIL_READ_SCOPE}),
    ],
)
async def test_add_asks_google_only_for_the_ticked_jobs(admin_client, jobs, scopes):
    resp = await admin_client.post("/admin/google/accounts", data={"owner": "family", "job": jobs})
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert set(query["scope"][0].split()) == scopes | {"openid", "email"}
    assert "login_hint" not in query


async def test_add_needs_a_job_and_a_real_owner(admin_client, db):
    resp = await admin_client.post("/admin/google/accounts", data={"owner": "family"})
    assert resp.headers["location"] == "/admin?tab=google&error=google-jobs#google"
    resp = await admin_client.post("/admin/google/accounts", data={"owner": "999", "job": "calendars"})
    assert resp.headers["location"] == "/admin?tab=google&error=google-owner#google"


async def test_school_email_is_only_for_a_parent_or_family_account(admin_client, db):
    resp = await admin_client.post("/admin/google/accounts", data={"owner": str(RILEY), "job": "school_email"})
    assert resp.headers["location"] == "/admin?tab=google&error=google-job-parent#google"
    await db.execute("UPDATE profiles SET is_parent = 1 WHERE id = ?", (MUM,))
    await db.commit()
    for owner in (str(MUM), "family"):
        resp = await admin_client.post("/admin/google/accounts", data={"owner": owner, "job": "school_email"})
        assert resp.headers["location"].startswith(google_oauth.AUTH_ENDPOINT)


async def test_stage_1_jobs_are_one_account_at_a_time(admin_client, db, two_accounts):
    for job in ("tasks", "school_email", "write_events"):
        resp = await admin_client.post("/admin/google/accounts", data={"owner": "family", "job": job})
        assert resp.headers["location"] == "/admin?tab=google&error=google-job-taken#google"
    resp = await admin_client.post(
        "/admin/google/accounts/2/edit", data={"owner": str(RILEY), "job": ["calendars", "tasks"]}
    )
    assert resp.headers["location"] == "/admin?tab=google&error=google-job-taken#google"
    assert (await google_accounts.get(db, 2))["jobs"] == ["calendars"]


async def _callback(admin_client, google, start_response, email, scope=ALL_SCOPES):
    state = start_response.cookies[admin_google.STATE_COOKIE]
    google.post(google_oauth.TOKEN_ENDPOINT).respond(
        200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3599, "scope": scope}
    )
    google.get(google_oauth.USERINFO_ENDPOINT).respond(200, json={"email": email})
    return await admin_client.get("/admin/google/callback", params={"code": "c", "state": state})


async def test_adding_an_address_already_connected_merges_into_its_row(admin_client, db, google):
    await google_oauth.store_tokens(db, _tokens(1, google_oauth.CALENDAR_READ_SCOPE), "family@example.com")
    start = await admin_client.post("/admin/google/accounts", data={"owner": str(DAD), "job": ["tasks"]})
    scope = f"{google_oauth.CALENDAR_READ_SCOPE} {google_oauth.TASKS_SCOPE} openid email"
    resp = await _callback(admin_client, google, start, "Family@Example.com", scope)

    assert resp.headers["location"] == "/admin?tab=google#google"
    [account] = await google_accounts.list_accounts(db)
    assert account["jobs"] == ["calendars", "tasks"] and account["owner_id"] is None  # owner kept
    assert (await google_accounts.load_tokens(db, 1))["access_token"] == "new-access"


async def test_a_new_address_is_a_new_account_with_its_owner(admin_client, db, google, two_accounts):
    start = await admin_client.post("/admin/google/accounts", data={"owner": str(JAMIE), "job": ["calendars"]})
    await _callback(
        admin_client, google, start, "jamie@example.com", f"{google_oauth.CALENDAR_READ_SCOPE} openid email"
    )
    jamie = await google_accounts.get(db, 3)
    assert (jamie["email"], jamie["owner_id"], jamie["jobs"], jamie["state"]) == (
        "jamie@example.com",
        JAMIE,
        ["calendars"],
        google_accounts.CONNECTED,
    )


async def test_reconnect_asks_for_the_same_address_and_refuses_another(admin_client, db, google, two_accounts):
    start = await admin_client.get("/admin/google/accounts/2/reconnect")
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["login_hint"] == ["riley@example.com"]
    assert set(query["scope"][0].split()) == {google_oauth.CALENDAR_READ_SCOPE, "openid", "email"}
    revoke = google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)

    resp = await _callback(admin_client, google, start, "someone.else@example.com")

    assert resp.headers["location"] == "/admin?tab=google&error=google-wrong-account#google"
    assert revoke.called and "new-refresh" in str(revoke.calls.last.request.url)  # the stray grant is withdrawn
    assert (await google_accounts.load_tokens(db, 2))["access_token"] == "tok-2"
    assert len(await google_accounts.list_accounts(db)) == 2
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": []})
    google.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(200, json={"items": []})
    page = (await admin_client.get(resp.headers["location"].split("#")[0])).text
    assert "different account. Use Add account for it." in page


async def test_reconnect_restores_a_revoked_account_with_everything_linked(admin_client, db, google, two_accounts):
    await google_accounts.drop_token(db, 2)
    await calendar_prefs.set_people_links(db, {"2:riley@example.com": JAMIE})
    start = await admin_client.get("/admin/google/accounts/2/reconnect")

    await _callback(
        admin_client, google, start, "riley@example.com", f"{google_oauth.CALENDAR_READ_SCOPE} openid email"
    )

    riley = await google_accounts.get(db, 2)
    assert riley["state"] == google_accounts.CONNECTED and riley["owner_id"] == RILEY
    assert "2:riley@example.com" in {c["key"] for c in await google_oauth.get_selected_calendars(db)}
    assert (await calendar_prefs.get_people_links(db))["2:riley@example.com"] == JAMIE


async def test_reconnecting_a_missing_account(admin_client):
    resp = await admin_client.get("/admin/google/accounts/99/reconnect")
    assert resp.headers["location"] == "/admin?tab=google&error=google-missing#google"


# --- Edit (spec 12.2) ---------------------------------------------------------------------


async def test_a_ticked_job_google_hasnt_allowed_needs_a_permission(admin_client, db, google, two_accounts):
    await google_accounts.update(db, 1, None, ["calendars", "tasks", "school_email"])  # frees Writing events
    resp = await admin_client.post(
        "/admin/google/accounts/2/edit", data={"owner": str(RILEY), "job": ["calendars", "write_events"]}
    )
    # Off to Google for it (login_hint: this account)...
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["login_hint"] == ["riley@example.com"] and google_oauth.CALENDAR_EVENTS_SCOPE in query["scope"][0]
    # ...and until it's allowed, the row says so.
    riley = await google_accounts.get(db, 2)
    assert riley["missing_jobs"] == ["write_events"] and riley["state"] == google_accounts.PERMISSION
    assert not await google_oauth.has_scope(db, google_oauth.CALENDAR_EVENTS_SCOPE)
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": []})
    google.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(200, json={"items": []})
    page = (await admin_client.get("/admin?tab=google")).text
    assert "Needs a permission" in page and "Google hasn't allowed Writing events" in page
    assert 'data-state="account-permission"' in (await admin_client.get("/sync-status")).text


async def test_unticking_tasks_unlinks_its_lists_and_keeps_every_local_item(admin_client, db, google, two_accounts):
    await db.execute(
        "UPDATE profiles SET google_tasklist_id = 'list-riley', google_account_id = 1 WHERE id = ?", (RILEY,)
    )
    await db.execute("INSERT INTO tasks (profile_id, title, google_task_id) VALUES (?, 'Feed cat', 'g1')", (RILEY,))
    await db.execute("INSERT INTO shopping_items (title, google_task_id) VALUES ('Milk', 's1')")
    await db.commit()
    await task_sync.set_shopping_tasklist(db, {"account": 1, "id": "shop", "title": "Shopping"})

    resp = await admin_client.post(
        "/admin/google/accounts/1/edit", data={"owner": "family", "job": ["calendars", "school_email", "write_events"]}
    )

    assert resp.headers["location"] == "/admin?tab=google&unticked=tasks#google"
    assert await task_sync.get_shopping_tasklist(db) is None
    profile = await (
        await db.execute("SELECT google_tasklist_id, google_account_id FROM profiles WHERE id = ?", (RILEY,))
    ).fetchone()
    assert tuple(profile) == (None, None)
    assert [tuple(r) for r in await (await db.execute("SELECT title, google_task_id FROM tasks")).fetchall()] == [
        ("Feed cat", None)
    ]
    assert [
        tuple(r) for r in await (await db.execute("SELECT title, google_task_id FROM shopping_items")).fetchall()
    ] == [("Milk", None)]
    await task_sync.run_sync(db)  # nothing is deleted on Google: no account does Tasks now
    assert not [c for c in google.calls if "tasks.googleapis" in str(c.request.url)]
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": []})
    page = (await admin_client.get(resp.headers["location"].split("#")[0])).text
    assert "Huddle no longer uses Google Tasks for this account" in page
    assert "Remove the account to withdraw the permission." in page


async def test_unticking_school_email_forgets_its_checkpoint(admin_client, db, two_accounts):
    await database.set_setting(db, school_email.CHECKPOINT_SETTING, "1790000000")
    await db.commit()
    await admin_client.post("/admin/google/accounts/1/edit", data={"owner": "family", "job": ["calendars", "tasks"]})
    assert await school_email.get_checkpoint(db) is None
    assert await google_oauth.connect_job(db, "school_email") == (None, False)


async def test_deleting_the_owner_makes_it_a_family_account(admin_client, db, two_accounts):
    await admin_client.post(f"/admin/profiles/{RILEY}/delete")
    assert (await google_accounts.get(db, 2))["owner_id"] is None


# --- Remove (spec 12.2, 12.6) -------------------------------------------------------------


async def _linked_family_account(db):
    await db.execute(
        "UPDATE profiles SET google_tasklist_id = 'list-riley', google_account_id = 1 WHERE id = ?", (RILEY,)
    )
    await db.execute("INSERT INTO tasks (profile_id, title, google_task_id) VALUES (?, 'Feed cat', 'g1')", (RILEY,))
    await db.execute("INSERT INTO tasks (profile_id, title) VALUES (?, 'Bag')", (JAMIE,))
    await db.execute("INSERT INTO shopping_items (title, google_task_id) VALUES ('Milk', 's1'), ('Eggs', NULL)")
    await db.commit()
    await task_sync.set_shopping_tasklist(db, {"account": 1, "id": "shop", "title": "Shopping"})
    await calendar_prefs.set_family_calendar(db, {"account": 1, "id": FAMILY_CAL, "summary": "Family"})
    await school_events.set_target_calendar(db, {"account": 1, "id": FAMILY_CAL, "summary": "Family"})
    await database.set_setting(db, school_email.CHECKPOINT_SETTING, "1790000000")
    await db.commit()


async def test_remove_shows_what_changes_first(admin_client, db, google, two_accounts):
    await _linked_family_account(db)
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": []})
    google.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(200, json={"items": []})
    page = (await admin_client.get("/admin?tab=google&remove=1")).text
    confirm = page.split('class="admin-note google-account-remove"')[1].split("</div>\n    </div>")[0]
    assert "Remove family@example.com?" in confirm
    assert "Its calendars leave the wall: Family." in confirm
    assert "Riley&#39;s task list becomes" in confirm or "Riley's task list becomes" in confirm
    assert "The shopping list becomes" in confirm
    assert "The Family calendar (Family) is cleared" in confirm
    assert "The calendar for school events (Family) is cleared." in confirm
    assert "Its school email stops being read." in confirm
    assert 'action="/admin/google/accounts/1/remove"' in confirm
    assert await google_accounts.get(db, 1) is not None  # nothing done yet


async def test_removing_an_account_keeps_every_local_task_and_shopping_item(admin_client, db, google, two_accounts):
    await _linked_family_account(db)
    tasks_before = [r["title"] for r in await (await db.execute("SELECT title FROM tasks ORDER BY id")).fetchall()]
    revoke = google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)

    resp = await admin_client.post("/admin/google/accounts/1/remove")

    assert resp.headers["location"] == "/admin?tab=google&removed=1#google"
    assert revoke.called and "r-1" in str(revoke.calls.last.request.url)
    assert await google_accounts.get(db, 1) is None
    assert [
        r["title"] for r in await (await db.execute("SELECT title FROM tasks ORDER BY id")).fetchall()
    ] == tasks_before
    items = [
        tuple(r)
        for r in await (await db.execute("SELECT title, google_task_id FROM shopping_items ORDER BY id")).fetchall()
    ]
    assert items == [("Milk", None), ("Eggs", None)]
    assert await task_sync.get_shopping_tasklist(db) is None
    assert [c["key"] for c in await google_oauth.get_selected_calendars(db)] == ["2:riley@example.com"]
    assert await calendar_prefs.get_family_setting(db) is None
    assert await school_events.get_target_calendar(db) is None
    assert await school_email.get_checkpoint(db) is None
    assert (await google_accounts.removed_notice(db))["family"] == "Family"
    # The other account carries on.
    assert (await google_accounts.get(db, 2))["state"] == google_accounts.CONNECTED

    await task_sync.run_sync(db)  # nothing deleted on Google either
    assert not [c for c in google.calls if "tasks.googleapis" in str(c.request.url)]
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(
        200, json={"items": [{"id": FAMILY_CAL, "summary": "Family"}]}
    )
    page = (await admin_client.get("/admin?tab=google&removed=1")).text
    assert "The Family calendar (Family) was in a removed account" in page
    # Admin offers to show the calendar through the account that still sees it.
    assert "Was shown through a removed account" in page


async def test_removing_a_missing_account(admin_client):
    resp = await admin_client.post("/admin/google/accounts/99/remove")
    assert resp.headers["location"] == "/admin?tab=google&error=google-missing#google"


async def test_removing_with_google_unreachable_still_removes(db, google, two_accounts):
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).mock(side_effect=httpx.ConnectError("down"))
    assert await accounts.remove(db, 2)
    assert [a["id"] for a in await google_accounts.list_accounts(db)] == [1]


# --- State, the dot, /health and logs ------------------------------------------------------


async def test_an_account_offline_for_a_while_turns_the_dot_amber(db, two_accounts, client):
    await google_accounts.note_check(db, 2, ok=False)
    assert (await sync_status.summary(db))["dot"]["level"] is None  # a blip
    await db.execute("UPDATE google_accounts SET offline_since = '2020-01-01T00:00:00+00:00' WHERE id = 2")
    await db.commit()
    dot = (await client.get("/sync-status")).text
    assert "sync-dot-amber" in dot and 'data-state="account-offline"' in dot
    await google_accounts.note_check(db, 2, ok=True)
    assert (await sync_status.summary(db))["dot"]["level"] is None


async def test_health_reports_each_account_by_number_never_address(db, two_accounts, client):
    await google_accounts.drop_token(db, 2)
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["sync"]["accounts"] == [{"id": 1, "state": "connected"}, {"id": 2, "state": "reconnect"}]
    assert "example.com" not in json.dumps(body)


async def test_logs_name_accounts_by_id_never_by_address(db, google, two_accounts, caplog):
    await google_accounts.save_tokens(db, 2, _tokens(2, google_oauth.CALENDAR_READ_SCOPE, expires_in=-10))
    google.post(google_oauth.TOKEN_ENDPOINT).mock(
        side_effect=refresh_api({"r-2": httpx.Response(400, json={"error": "invalid_grant"})})
    )
    google.get(url__regex=EVENTS).mock(side_effect=calendar_api({}))
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)
    with caplog.at_level(logging.DEBUG):
        await google_calendar.get_month_grid(db, 2026, 8)
        await accounts.remove(db, 2)
    text = caplog.text + json.dumps([r.args for r in caplog.records], default=str)
    assert "Google account 2" in caplog.text
    assert "example.com" not in text and "r-2" not in text and "tok-2" not in text
