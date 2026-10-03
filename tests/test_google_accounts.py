"""Several Google accounts, each with its own jobs (spec 12), stage 1:
migration 13, per-account tokens, identity and states, the Admin Google
accounts panel (add, edit, reconnect, remove) and calendars from every
account. Dormant task lists (untick/re-tick, remove/re-add) are
test_google_accounts_sync.py."""

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
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
TASKLISTS_URL = "https://tasks.googleapis.com/tasks/v1/users/@me/lists"
ALL_SCOPES = google_oauth.SCOPES
READ_ONLY = f"{google_oauth.CALENDAR_READ_SCOPE} openid email"
FAMILY_CAL = "family@group.calendar.google.com"
SHARED_CAL = "shared@group.calendar.google.com"
MUM, DAD, RILEY, JAMIE = 1, 2, 3, 4
FAMILY_SUB, RILEY_SUB = "local:family@example.com", "local:riley@example.com"  # store_tokens' stand-ins


def timed(title, day="2026-08-11", start="09:00"):
    moment = f"{day}T{start}:00+01:00"
    return {"id": title.replace(" ", ""), "summary": title, "start": {"dateTime": moment}, "end": {"dateTime": moment}}


def _token_of(request) -> str:
    return request.headers["authorization"].removeprefix("Bearer ")


def calendar_api(answers: dict):
    """events.list answered per (access token, calendar id): a list of raw
    events, an exception to raise, or a coroutine function (a slow Google).
    Anything not listed 404s."""

    async def answer(request):
        cal = unquote(request.url.path.split("/calendars/")[1].split("/")[0])
        found = answers.get((_token_of(request), cal))
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


def calendar_lists(by_token: dict):
    """calendarList per access token: [(id, accessRole), ...]."""

    def answer(request):
        items = [
            {"id": cal_id, "summary": cal_id.split("@")[0].title(), "accessRole": role}
            for cal_id, role in by_token.get(_token_of(request), [])
        ]
        return httpx.Response(200, json={"items": items})

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
    Calendars only. Shown: the Family calendar (account 1) and Riley's own
    calendar (account 2)."""
    await google_oauth.store_tokens(db, _tokens(1, ALL_SCOPES), "family@example.com")
    await google_oauth.store_tokens(db, _tokens(2, READ_ONLY), "riley@example.com", owner_id=RILEY)
    await google_oauth.set_selected_calendars(
        db,
        [
            {"account_id": 1, "id": FAMILY_CAL, "summary": "Family", "color": "#123456"},
            {"account_id": 2, "id": "riley@example.com", "summary": "Riley", "color": "#654321"},
        ],
    )
    return await google_accounts.list_accounts(db)


def _no_lists(google):
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(200, json={"items": []})
    google.get(TASKLISTS_URL).respond(200, json={"items": []})


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
    auth_tokens, the old settings), then upgraded. Returns the old
    auth_tokens and app_settings rows."""
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
        if "google_shopping_tasklist" in settings:
            conn.execute("UPDATE profiles SET google_tasklist_id = 'list-riley' WHERE id = ?", (RILEY,))
        old_tokens = conn.execute("SELECT * FROM auth_tokens ORDER BY service_name").fetchall()
        old_settings = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
    monkeypatch.setattr(migrations, "MIGRATIONS", every)
    await database.init_db()
    await database.init_db()  # and again: nothing more happens
    return old_tokens, old_settings


async def test_migration_makes_the_connection_account_1_and_the_wall_looks_the_same(
    tmp_path, monkeypatch, google, admin_client
):
    old_tokens, old_settings = await _upgrade(tmp_path, monkeypatch, _tokens(0, ALL_SCOPES) | {"access_token": "tok"})
    with sqlite3.connect(database.DB_PATH) as conn:
        assert conn.execute("SELECT * FROM auth_tokens ORDER BY service_name").fetchall() == old_tokens  # rollback
        [row] = conn.execute(
            "SELECT id, google_sub, email, owner_profile_id, jobs, encrypted_token_json FROM google_accounts"
        )
        assert row[:5] == (1, None, "family@example.com", None, "calendars tasks school_email write_events")
        assert row[5] == old_tokens[0][2]  # the very same encrypted token
        assert conn.execute("SELECT google_account_id FROM profiles WHERE id = ?", (RILEY,)).fetchone() == (1,)
        new_settings = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
    # Settings change additively only: each entry gains "account_id", nothing else moves.
    for key, old in OLD_SETTINGS.items():
        if key == "calendar_people":
            assert new_settings[key] == old_settings[key]  # untouched, for a rollback
        elif isinstance(old, list):
            assert json.loads(new_settings[key]) == [{**cal, "account_id": 1} for cal in old]
        else:
            assert json.loads(new_settings[key]) == {**old, "account_id": 1}
    assert json.loads(new_settings["calendar_people_v2"]) == {
        "1:riley@example.com": RILEY,
        f"1:{FAMILY_CAL}": "everyone",
    }

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
        assert target == {"account_id": 1, "id": FAMILY_CAL, "summary": "Family"}
        assert await school_events._can_write(db, target)
        assert await google_oauth.connect_job(db, "school_email") == ("tok", False)
        assert (await sync_status.summary(db))["dot"]["level"] is None

        # Tasks and the shopping list still sync with the same lists.
        shop = google.get(f"{TASKS_API}/shop/tasks").respond(200, json={"items": []})
        riley = google.get(f"{TASKS_API}/list-riley/tasks").respond(200, json={"items": []})
        await task_sync.run_sync(db)
        assert shop.called and riley.called
        assert riley.calls.last.request.headers["authorization"] == "Bearer tok"
        assert (await sync_status.summary(db))["state"] == "ok"

    html = (await admin_client.get("/widgets/calendar?year=2026&month=8")).text
    assert "Bins out" in html and "Swimming" in html and "cal-add-toggle" in html


async def test_migration_of_a_grant_without_scopes_takes_the_configured_jobs(tmp_path, monkeypatch):
    """No scope kept: account 1 gets the legacy scopes' jobs (Calendars,
    Tasks) and every job the family has set up (here Writing events, for the
    Family calendar), so the wall loses nothing."""
    await _upgrade(tmp_path, monkeypatch, {"access_token": "tok", "refresh_token": "r", "expires_at": 0})
    async with database.get_db() as db:
        [account] = await google_accounts.list_accounts(db)
    assert account["jobs"] == ["calendars", "tasks", "write_events"]


async def test_migration_fallback_with_nothing_set_up_is_the_legacy_jobs(tmp_path, monkeypatch):
    await _upgrade(tmp_path, monkeypatch, {"access_token": "tok", "expires_at": 0}, settings={})
    async with database.get_db() as db:
        assert (await google_accounts.get(db, 1))["jobs"] == ["calendars", "tasks"]


async def test_migration_fallback_counts_school_email_once_a_check_ran(tmp_path, monkeypatch):
    await _upgrade(tmp_path, monkeypatch, None, encrypted="gAAAA-not-ours", settings={"school_email_checkpoint": 1})
    async with database.get_db() as db:
        assert (await google_accounts.get(db, 1))["jobs"] == ["calendars", "tasks", "school_email"]


async def test_migration_copes_with_an_unreadable_token_and_odd_settings(tmp_path, monkeypatch):
    odd = {"google_selected_calendars": ["primary"], "calendar_people": [1], "calendar_family": "x"}
    old_tokens, _ = await _upgrade(tmp_path, monkeypatch, None, encrypted="gAAAA-not-ours", settings=odd)
    with sqlite3.connect(database.DB_PATH) as conn:
        assert conn.execute("SELECT jobs, encrypted_token_json FROM google_accounts").fetchall() == [
            ("calendars tasks", "gAAAA-not-ours")
        ]
        assert conn.execute("SELECT * FROM auth_tokens ORDER BY service_name").fetchall() == old_tokens
        assert json.loads(
            conn.execute("SELECT value FROM app_settings WHERE key = 'google_selected_calendars'").fetchone()[0]
        ) == ["primary"]
    async with database.get_db() as db:
        assert (await google_accounts.get(db, 1))["state"] == google_accounts.RECONNECT
        assert await google_oauth.get_selected_calendars(db) == [
            {**google_oauth.DEFAULT_CALENDAR, "account_id": 1, "key": "1:primary"}
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
    assert await calendar_prefs.get_people_links(db) == {"riley@example.com": RILEY}
    rows = await (await db.execute("SELECT account_id, calendar_id FROM calendar_cache ORDER BY 1")).fetchall()
    assert [tuple(r) for r in rows] == [(1, FAMILY_CAL), (2, "riley@example.com")]


async def test_one_account_offline_while_the_other_loads_live(db, google, two_accounts):
    answers = {("tok-1", FAMILY_CAL): [timed("Bins out")], ("tok-2", "riley@example.com"): [timed("Swimming")]}
    google.get(url__regex=EVENTS).mock(side_effect=calendar_api(answers))
    await google_calendar.get_month_grid(db, 2026, 8)  # both live: the cache is filled

    # Riley's account now needs a refresh, and Google can't be reached for it.
    await google_accounts.save_tokens(db, 2, _tokens(2, READ_ONLY, expires_in=-10))
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
    await google_accounts.save_tokens(db, 2, _tokens(2, READ_ONLY, expires_in=-10))
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


async def test_removing_an_account_leaves_the_others_cache(db, google, two_accounts):
    """Account 1 offline, account 2 removed: account 1 still renders from
    its cache (each account's cache rows and selection hash are its own)."""
    answers = {("tok-1", FAMILY_CAL): [timed("Bins out")], ("tok-2", "riley@example.com"): [timed("Swimming")]}
    google.get(url__regex=EVENTS).mock(side_effect=calendar_api(answers))
    await google_calendar.get_month_grid(db, 2026, 8)
    await google_accounts.save_tokens(db, 1, _tokens(1, ALL_SCOPES, expires_in=-10))
    google.post(google_oauth.TOKEN_ENDPOINT).mock(side_effect=refresh_api({"r-1": httpx.ConnectError("down")}))
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)

    assert await accounts.remove(db, 2)
    grid = await google_calendar.get_month_grid(db, 2026, 8)

    assert set(_titles(grid)) == {"Bins out"} and grid["updated_label"] and not grid["offline"]
    rows = await (await db.execute("SELECT DISTINCT account_id FROM calendar_cache")).fetchall()
    assert [r[0] for r in rows] == [1]


async def test_changing_one_accounts_calendars_keeps_the_others_cache(db, google, two_accounts):
    answers = {("tok-1", FAMILY_CAL): [timed("Bins out")], ("tok-2", "riley@example.com"): [timed("Swimming")]}
    google.get(url__regex=EVENTS).mock(side_effect=calendar_api(answers))
    await google_calendar.get_month_grid(db, 2026, 8)
    saved = await google_oauth.get_saved_calendars(db)
    await google_oauth.set_selected_calendars(db, [*saved, {"account_id": 2, "id": "work", "color": "#000000"}])
    rows = await (await db.execute("SELECT DISTINCT account_id FROM calendar_cache")).fetchall()
    assert [r[0] for r in rows] == [1]


async def test_the_timezone_comes_from_the_oldest_calendar_account_and_is_kept(db, google, two_accounts):
    await database.set_setting(db, database.CALENDAR_TIMEZONE_SETTING, "")
    await db.commit()
    metadata = google.get(google_calendar.CALENDAR_METADATA_ENDPOINT).mock(
        side_effect=lambda request: httpx.Response(
            200, json={"id": _token_of(request), "timeZone": "Europe/Paris" if _token_of(request) == "tok-1" else "X"}
        )
    )
    google.get(url__regex=EVENTS).mock(side_effect=calendar_api({}))
    await google_calendar.get_month_grid(db, 2026, 8)
    assert await database.get_setting(db, database.CALENDAR_TIMEZONE_SETTING) == "Europe/Paris"
    assert all(c.request.headers["authorization"] == "Bearer tok-1" for c in metadata.calls)
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)
    await accounts.remove(db, 1)
    assert await database.get_setting(db, database.CALENDAR_TIMEZONE_SETTING) == "Europe/Paris"


async def test_primary_is_resolved_once_a_second_account_shows_calendars(db, google, two_accounts):
    """With two accounts, "primary" (a different calendar in each) becomes
    the real id, so one shared into the other account is seen as the same."""
    await google_oauth.set_selected_calendars(
        db,
        [
            {"account_id": 1, "id": "primary", "summary": "Family", "color": "#123456"},
            {"account_id": 2, "id": "family@example.com", "summary": "Family", "color": "#222222"},
            {"account_id": 2, "id": "primary", "summary": "Riley", "color": "#654321"},
        ],
    )
    await calendar_prefs.set_people_links(db, {"2:primary": JAMIE})
    await calendar_prefs.set_family_calendar(db, {"account_id": 1, "id": "primary", "summary": "Family"})
    google.get(google_calendar.CALENDAR_METADATA_ENDPOINT).mock(
        side_effect=lambda request: httpx.Response(
            200, json={"id": {"tok-1": "family@example.com", "tok-2": "riley@example.com"}[_token_of(request)]}
        )
    )
    google.get(url__regex=EVENTS).mock(
        side_effect=calendar_api(
            {("tok-1", "family@example.com"): [timed("Bins out")], ("tok-2", "riley@example.com"): [timed("Swim")]}
        )
    )

    grid = await google_calendar.get_month_grid(db, 2026, 8)

    shown = await google_oauth.get_selected_calendars(db)
    assert [c["key"] for c in shown] == ["1:family@example.com", "2:riley@example.com"]  # shown once
    assert set(_titles(grid)) == {"Bins out", "Swim"}
    assert await calendar_prefs.get_saved_people_links(db) == {"2:riley@example.com": JAMIE}
    assert (await calendar_prefs.get_family_setting(db))["id"] == "family@example.com"


async def test_a_single_account_keeps_its_primary_alias_without_a_call(db, google, connected):
    google.get(url__regex=EVENTS).mock(side_effect=calendar_api({("tok", "primary"): [timed("Bins out")]}))
    grid = await google_calendar.get_month_grid(db, 2026, 8)
    assert set(_titles(grid)) == {"Bins out"}
    assert await google_oauth.get_saved_calendars(db) is None


async def test_a_calendar_shared_into_two_accounts_is_shown_once(db, google, two_accounts, admin_client):
    await google_oauth.set_selected_calendars(
        db,
        [
            {"account_id": 1, "id": SHARED_CAL, "summary": "Shared", "color": "#111111"},
            {"account_id": 2, "id": SHARED_CAL, "summary": "Shared", "color": "#222222"},
            {"account_id": 2, "id": "riley@example.com", "summary": "Riley", "color": "#654321"},
        ],
    )
    shown = await google_oauth.get_selected_calendars(db)
    assert [c["key"] for c in shown] == [f"1:{SHARED_CAL}", "2:riley@example.com"]
    route = google.get(url__regex=EVENTS).mock(
        side_effect=calendar_api({("tok-1", SHARED_CAL): [timed("Party")], ("tok-2", "riley@example.com"): []})
    )
    grid = await google_calendar.get_month_grid(db, 2026, 8)
    assert _titles(grid) == {"Party": "#111111"} and route.call_count == 2

    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).mock(
        side_effect=calendar_lists(
            {"tok-1": [(SHARED_CAL, "reader")], "tok-2": [(SHARED_CAL, "reader"), ("riley@example.com", "owner")]}
        )
    )
    google.get(TASKLISTS_URL).respond(200, json={"items": []})
    page = (await admin_client.get("/admin?tab=google")).text
    assert re.search(r'value="2:shared@group\.calendar\.google\.com"\s+disabled', page)
    assert "Shown through family@example.com" in page

    # Ticking both copies keeps one.
    await admin_client.post(
        "/admin/google/calendars", data={"calendar_id": [f"1:{SHARED_CAL}", f"2:{SHARED_CAL}", "2:riley@example.com"]}
    )
    assert [c["key"] for c in await google_oauth.get_selected_calendars(db)] == [
        f"1:{SHARED_CAL}",
        "2:riley@example.com",
    ]


async def test_a_shared_family_calendar_goes_through_the_account_that_can_write_it(db, google, admin_client):
    """Shared into account 1 (read-only) and account 2 (writer, Writing
    events): shown through account 2, so it can still be the Family calendar."""
    readonly_tasks = f"{google_oauth.CALENDAR_READ_SCOPE} {google_oauth.TASKS_SCOPE}"
    await google_oauth.store_tokens(db, _tokens(1, readonly_tasks), "family@example.com")
    writer = f"{google_oauth.CALENDAR_READ_SCOPE} {google_oauth.CALENDAR_EVENTS_SCOPE}"
    await google_oauth.store_tokens(db, _tokens(2, writer), "mum@example.com", owner_id=MUM)
    assert (await google_accounts.job_account(db, "write_events"))["id"] == 2
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).mock(
        side_effect=calendar_lists({"tok-1": [(FAMILY_CAL, "reader")], "tok-2": [(FAMILY_CAL, "writer")]})
    )
    google.get(TASKLISTS_URL).respond(200, json={"items": []})

    await admin_client.post("/admin/google/calendars", data={"calendar_id": [f"1:{FAMILY_CAL}"]})

    assert [c["key"] for c in await google_oauth.get_selected_calendars(db)] == [f"2:{FAMILY_CAL}"]
    page = (await admin_client.get("/admin?tab=google")).text
    assert f'<option value="2:{FAMILY_CAL}"' in page.split('aria-label="Family calendar"')[1]
    resp = await admin_client.post("/admin/google/family-calendar", data={"calendar_id": f"2:{FAMILY_CAL}"})
    assert resp.headers["location"] == "/admin?tab=google#calendar-options"
    assert (await calendar_prefs.get_family_calendar(db))["account_id"] == 2


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


async def test_a_ticked_calendars_job_without_its_scope_is_never_fetched(db, google):
    await google_oauth.store_tokens(db, _tokens(1, f"{google_oauth.TASKS_SCOPE} openid email"), "family@example.com")
    await google_accounts.update(db, 1, None, ["calendars", "tasks"])  # ticked, not granted
    route = google.get(url__regex=EVENTS).respond(200, json={"items": []})
    grid = await google_calendar.get_month_grid(db, 2026, 8)
    assert grid["offline"] and not route.called


async def test_unticking_calendars_takes_an_accounts_calendars_off_the_wall(db, two_accounts):
    await google_accounts.update(db, 2, RILEY, ["write_events"])  # normalised: implies Calendars
    assert (await google_accounts.get(db, 2))["jobs"] == ["calendars", "write_events"]
    await google_accounts.update(db, 2, RILEY, [])
    assert [c["key"] for c in await google_oauth.get_selected_calendars(db)] == [f"1:{FAMILY_CAL}"]


# --- Add account, Reconnect, identity (spec 12.2) -----------------------------------------


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
    assert query["include_granted_scopes"] == ["true"]
    assert query["prompt"] == ["select_account consent"]  # always the account chooser
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


async def _callback(admin_client, google, start_response, email, sub, scope=ALL_SCOPES, revoke=True):
    state = start_response.cookies[admin_google.STATE_COOKIE]
    google.post(google_oauth.TOKEN_ENDPOINT).respond(
        200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3599, "scope": scope}
    )
    google.get(google_oauth.USERINFO_ENDPOINT).respond(200, json={"email": email, "sub": sub})
    route = google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)
    resp = await admin_client.get("/admin/google/callback", params={"code": "c", "state": state})
    return resp, route


async def test_adding_an_account_already_connected_says_so_and_merges(admin_client, db, google):
    await google_oauth.store_tokens(db, _tokens(1, READ_ONLY), "family@example.com")
    start = await admin_client.post("/admin/google/accounts", data={"owner": str(DAD), "job": ["tasks"]})
    scope = f"{google_oauth.CALENDAR_READ_SCOPE} {google_oauth.TASKS_SCOPE} openid email"
    resp, _ = await _callback(admin_client, google, start, "family@example.com", FAMILY_SUB, scope)

    assert resp.headers["location"] == "/admin?tab=google&merged=1#google"
    [account] = await google_accounts.list_accounts(db)
    assert account["jobs"] == ["calendars", "tasks"] and account["owner_id"] is None  # owner kept
    assert (await google_accounts.load_tokens(db, 1))["access_token"] == "new-access"
    _no_lists(google)
    page = (await admin_client.get("/admin?tab=google&merged=1")).text
    assert "family@example.com is already connected; its jobs were updated." in page


async def test_the_same_account_under_a_new_address_is_matched_by_sub(admin_client, db, google):
    await google_oauth.store_tokens(db, _tokens(1, READ_ONLY), "old@example.com", sub="sub-1")
    start = await admin_client.post("/admin/google/accounts", data={"owner": "family", "job": ["calendars"]})
    resp, _ = await _callback(admin_client, google, start, "new@example.com", "sub-1", READ_ONLY)
    assert "merged=1" in resp.headers["location"]
    [account] = await google_accounts.list_accounts(db)
    assert account["email"] == "new@example.com"


async def test_a_new_account_is_a_new_row_with_its_owner(admin_client, db, google, two_accounts):
    start = await admin_client.post("/admin/google/accounts", data={"owner": str(JAMIE), "job": ["calendars"]})
    resp, _ = await _callback(admin_client, google, start, "jamie@example.com", "sub-jamie", READ_ONLY)
    assert resp.headers["location"] == "/admin?tab=google#google"
    jamie = await google_accounts.get(db, 3)
    assert (jamie["sub"], jamie["email"], jamie["owner_id"], jamie["jobs"], jamie["state"]) == (
        "sub-jamie",
        "jamie@example.com",
        JAMIE,
        ["calendars"],
        google_accounts.CONNECTED,
    )


async def test_two_accounts_can_share_an_address(admin_client, db, google, two_accounts):
    """The address is for display: only `sub` says who an account is."""
    start = await admin_client.post("/admin/google/accounts", data={"owner": "family", "job": ["calendars"]})
    await _callback(admin_client, google, start, "riley@example.com", "another-sub", READ_ONLY)
    assert [a["email"] for a in await google_accounts.list_accounts(db)].count("riley@example.com") == 2


async def test_reconnect_asks_for_the_same_account_and_refuses_another(admin_client, db, google, two_accounts):
    start = await admin_client.get("/admin/google/accounts/2/reconnect")
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["login_hint"] == ["riley@example.com"] and query["prompt"] == ["consent"]
    assert set(query["scope"][0].split()) == {google_oauth.CALENDAR_READ_SCOPE, "openid", "email"}

    resp, revoke = await _callback(admin_client, google, start, "riley@example.com", "someone-else")

    assert resp.headers["location"] == "/admin?tab=google&error=google-wrong-account#google"
    assert revoke.called and "new-refresh" in str(revoke.calls.last.request.url)  # the stray grant is withdrawn
    assert (await google_accounts.load_tokens(db, 2))["access_token"] == "tok-2"
    assert len(await google_accounts.list_accounts(db)) == 2
    _no_lists(google)
    page = (await admin_client.get(resp.headers["location"].split("#")[0])).text
    assert "different account. Use Add account for it." in page


async def test_a_refused_reconnect_as_another_connected_account_keeps_its_grant(admin_client, db, google, two_accounts):
    """Signing in as account 1 on account 2's Reconnect: refused, and the
    token isn't revoked, since that would end account 1's grant too."""
    start = await admin_client.get("/admin/google/accounts/2/reconnect")
    resp, revoke = await _callback(admin_client, google, start, "family@example.com", FAMILY_SUB)
    assert "error=google-wrong-account" in resp.headers["location"]
    assert not revoke.called


async def _migrated_account(db, email: str | None):
    """A row as migration 13 makes it: no `sub` yet."""
    await db.execute(
        "INSERT INTO google_accounts (id, email, jobs, encrypted_token_json) VALUES (1, ?, 'calendars', ?)",
        (email, security.encrypt_token_json(_tokens(1, READ_ONLY))),
    )
    await db.commit()


async def test_a_migrated_account_reconnects_by_address_and_learns_its_sub(admin_client, db, google):
    await _migrated_account(db, "family@example.com")
    start = await admin_client.get("/admin/google/accounts/1/reconnect")
    resp, _ = await _callback(admin_client, google, start, "Family@Example.com", "real-sub", READ_ONLY)
    assert resp.headers["location"] == "/admin?tab=google#google"
    assert (await google_accounts.get(db, 1))["sub"] == "real-sub"
    # From now on only that sub is this account.
    again = await admin_client.get("/admin/google/accounts/1/reconnect")
    resp, _ = await _callback(admin_client, google, again, "family@example.com", "other-sub", READ_ONLY)
    assert "error=google-wrong-account" in resp.headers["location"]


async def test_a_row_without_address_or_sub_adopts_no_account(admin_client, db, google):
    await _migrated_account(db, None)
    start = await admin_client.get("/admin/google/accounts/1/reconnect")
    resp, _ = await _callback(admin_client, google, start, "anyone@example.com", "any-sub", READ_ONLY)
    assert "error=google-wrong-account" in resp.headers["location"]
    assert (await google_accounts.get(db, 1))["sub"] is None


async def test_a_migrated_account_learns_its_sub_on_refresh(db, google):
    await _migrated_account(db, "family@example.com")
    await google_accounts.save_tokens(db, 1, _tokens(1, READ_ONLY, expires_in=-10))
    google.post(google_oauth.TOKEN_ENDPOINT).respond(200, json={"access_token": "fresh", "expires_in": 3600})
    google.get(google_oauth.USERINFO_ENDPOINT).respond(200, json={"sub": "real-sub", "email": "family@example.com"})
    assert await google_oauth.get_valid_access_token(db, 1) == "fresh"
    assert (await google_accounts.get(db, 1))["sub"] == "real-sub"


async def test_reconnect_restores_a_revoked_account_with_everything_linked(admin_client, db, google, two_accounts):
    await google_accounts.drop_token(db, 2)
    await calendar_prefs.set_people_links(db, {"2:riley@example.com": JAMIE})
    start = await admin_client.get("/admin/google/accounts/2/reconnect")

    await _callback(admin_client, google, start, "riley@example.com", RILEY_SUB, READ_ONLY)

    riley = await google_accounts.get(db, 2)
    assert riley["state"] == google_accounts.CONNECTED and riley["owner_id"] == RILEY
    assert "2:riley@example.com" in {c["key"] for c in await google_oauth.get_selected_calendars(db)}
    assert (await calendar_prefs.get_people_links(db))["riley@example.com"] == JAMIE


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
    _no_lists(google)
    page = (await admin_client.get("/admin?tab=google")).text
    assert "Needs a permission" in page and "Google hasn't allowed Writing events" in page
    assert 'data-state="account-permission"' in (await admin_client.get("/sync-status")).text


async def test_a_granted_scope_without_its_job_ticked_does_nothing(db, two_accounts):
    """include_granted_scopes keeps old grants: ticked AND granted decides."""
    await google_accounts.update(db, 1, None, ["calendars", "tasks"])  # Gmail still granted
    assert not await google_oauth.has_scope(db, google_oauth.GMAIL_READ_SCOPE)
    assert await google_oauth.connect_job(db, "school_email") == (None, False)


async def test_school_email_untick_keeps_its_checkpoint_and_re_tick_reads_14_days_at_most(
    admin_client, db, two_accounts
):
    old = int((datetime.now(UTC) - timedelta(days=90)).timestamp())
    await database.set_setting(db, school_email.CHECKPOINT_SETTING, str(old))
    await db.commit()
    await admin_client.post("/admin/google/accounts/1/edit", data={"owner": "family", "job": ["calendars", "tasks"]})
    assert await school_email.get_checkpoint(db) == old  # kept
    assert await google_oauth.connect_job(db, "school_email") == (None, False)  # not read

    await admin_client.post(
        "/admin/google/accounts/1/edit", data={"owner": "family", "job": ["calendars", "tasks", "school_email"]}
    )
    fourteen_days_ago = (datetime.now(UTC) - timedelta(days=school_email.BACKFILL_DAYS)).timestamp()
    assert abs(await school_email.get_checkpoint(db) - fourteen_days_ago) < 60


async def test_school_email_re_tick_keeps_a_recent_checkpoint(admin_client, db, two_accounts):
    recent = int((datetime.now(UTC) - timedelta(days=2)).timestamp())
    await database.set_setting(db, school_email.CHECKPOINT_SETTING, str(recent))
    await db.commit()
    await admin_client.post("/admin/google/accounts/1/edit", data={"owner": "family", "job": ["calendars", "tasks"]})
    await admin_client.post(
        "/admin/google/accounts/1/edit", data={"owner": "family", "job": ["calendars", "tasks", "school_email"]}
    )
    assert await school_email.get_checkpoint(db) == recent


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
    await task_sync.set_shopping_tasklist(db, {"account_id": 1, "id": "shop", "title": "Shopping"})
    await calendar_prefs.set_family_calendar(db, {"account_id": 1, "id": FAMILY_CAL, "summary": "Family"})
    await school_events.set_target_calendar(db, {"account_id": 1, "id": FAMILY_CAL, "summary": "Family"})
    await database.set_setting(db, school_email.CHECKPOINT_SETTING, "1790000000")
    await db.commit()


async def test_remove_shows_what_changes_first(admin_client, db, google, two_accounts):
    await _linked_family_account(db)
    _no_lists(google)
    page = (await admin_client.get("/admin?tab=google&remove=1")).text
    confirm = page.split('class="admin-note google-account-remove"')[1].split("</div>\n    </div>")[0]
    assert "Remove family@example.com?" in confirm
    assert "Its calendars leave the wall: Family." in confirm
    assert "task list stops syncing and stays on this display only" in confirm and "Riley" in confirm
    assert "adding this account back links it again" in confirm
    assert "The shopping list stops syncing" in confirm
    assert "The Family calendar (Family) is cleared" in confirm
    assert "The calendar for school events (Family) is cleared." in confirm
    assert "Its school email stops being read." in confirm
    assert 'action="/admin/google/accounts/1/remove"' in confirm
    assert await google_accounts.get(db, 1) is not None  # nothing done yet


async def test_removing_an_account_keeps_every_local_item_and_its_row(admin_client, db, google, two_accounts):
    await _linked_family_account(db)
    tasks_before = [tuple(r) for r in await (await db.execute("SELECT title, google_task_id FROM tasks")).fetchall()]
    revoke = google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)

    resp = await admin_client.post("/admin/google/accounts/1/remove")

    assert resp.headers["location"] == "/admin?tab=google&removed=1#google"
    assert revoke.called and "r-1" in str(revoke.calls.last.request.url)
    assert await google_accounts.get(db, 1) is None  # gone from Admin and every feature...
    row = await (
        await db.execute("SELECT removed_at, encrypted_token_json, google_sub FROM google_accounts WHERE id = 1")
    ).fetchone()
    assert row["removed_at"] and row["encrypted_token_json"] is None and row["google_sub"] == FAMILY_SUB  # ...kept
    # Lists stay linked (dormant), with every Google id: nothing is deleted or re-keyed.
    assert [
        tuple(r) for r in await (await db.execute("SELECT title, google_task_id FROM tasks")).fetchall()
    ] == tasks_before
    items = await (await db.execute("SELECT title, google_task_id FROM shopping_items ORDER BY id")).fetchall()
    assert [tuple(r) for r in items] == [("Milk", "s1"), ("Eggs", None)]
    assert (await task_sync.get_shopping_tasklist(db))["account_id"] == 1
    profile = await (
        await db.execute("SELECT google_tasklist_id, google_account_id FROM profiles WHERE id = ?", (RILEY,))
    ).fetchone()
    assert tuple(profile) == ("list-riley", 1)
    assert [c["key"] for c in await google_oauth.get_selected_calendars(db)] == ["2:riley@example.com"]
    assert await calendar_prefs.get_family_setting(db) is None
    assert await school_events.get_target_calendar(db) is None
    assert await school_email.get_checkpoint(db) is None  # Remove deletes it
    assert (await google_accounts.removed_notice(db))["family"] == "Family"
    assert (await google_accounts.get(db, 2))["state"] == google_accounts.CONNECTED

    await task_sync.run_sync(db)  # nothing touches Google: no account does Tasks
    assert not [c for c in google.calls if "tasks.googleapis" in str(c.request.url)]
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(
        200, json={"items": [{"id": FAMILY_CAL, "summary": "Family"}]}
    )
    page = (await admin_client.get("/admin?tab=google&removed=1")).text
    assert "The Family calendar (Family) was in a removed account" in page
    assert "Was shown through a removed account" in page  # offered through the account that still sees it
    assert "family@example.com" not in page.split('id="calendars"')[0].split("Google accounts")[1]


async def test_removing_a_missing_account(admin_client):
    resp = await admin_client.post("/admin/google/accounts/99/remove")
    assert resp.headers["location"] == "/admin?tab=google&error=google-missing#google"


async def test_removing_with_google_unreachable_still_removes(db, google, two_accounts):
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).mock(side_effect=httpx.ConnectError("down"))
    assert await accounts.remove(db, 2)
    assert [a["id"] for a in await google_accounts.list_accounts(db)] == [1]


async def test_adding_a_removed_account_back_revives_its_row(admin_client, db, google, two_accounts):
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)
    await accounts.remove(db, 2)
    start = await admin_client.post("/admin/google/accounts", data={"owner": str(RILEY), "job": ["calendars"]})
    resp, _ = await _callback(admin_client, google, start, "riley@example.com", RILEY_SUB, READ_ONLY)
    assert resp.headers["location"] == "/admin?tab=google#google"  # not "already connected"
    assert [a["id"] for a in await google_accounts.list_accounts(db)] == [1, 2]
    assert (await google_accounts.get(db, 2))["state"] == google_accounts.CONNECTED


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
    await google_accounts.save_tokens(db, 2, _tokens(2, READ_ONLY, expires_in=-10))
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
