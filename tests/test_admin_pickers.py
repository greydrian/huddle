"""Admin's Google calendar picker and task-list pickers (rendered on /admin,
saved via /admin/google/calendars, /shopping-list and /task-lists)."""

import json
import re

import httpx
import pytest

from app import google_oauth, task_sync
from app.routers import admin
from app.security import create_session_token

CALENDAR_LIST_URL = google_oauth.CALENDAR_LIST_ENDPOINT
TASKLISTS_URL = "https://tasks.googleapis.com/tasks/v1/users/@me/lists"

CALENDARS = [
    {"id": "family@example.com", "summary": "Family", "backgroundColor": "#123456", "primary": True},
    {"id": "school#holidays", "summary": "raw name", "summaryOverride": "School holidays"},
]
TASKLISTS = [{"id": "list-shop", "title": "Groceries"}, {"id": "list-kid", "title": "Kid chores"}]


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


@pytest.fixture
def google_lists(google, connected):
    google.get(CALENDAR_LIST_URL).respond(200, json={"items": CALENDARS})
    google.get(TASKLISTS_URL).respond(200, json={"items": TASKLISTS})
    return google


async def _profiles(db):
    rows = await (await db.execute("SELECT id, google_tasklist_id FROM profiles ORDER BY sort_order")).fetchall()
    return [dict(r) for r in rows]


async def _queued(db):
    rows = await (await db.execute("SELECT service, payload_json FROM sync_queue ORDER BY id")).fetchall()
    return [(r["service"], json.loads(r["payload_json"])) for r in rows]


# --- Listing ---

async def test_admin_lists_calendars_and_tasklists_from_google(admin_client, google_lists):
    resp = await admin_client.get("/admin")

    assert resp.status_code == 200
    html = resp.text
    assert 'name="calendar_id" value="family@example.com"' in html
    assert "Family (primary)" in html
    assert 'value="school#holidays"' in html
    assert "School holidays" in html  # summaryOverride wins
    assert '<option value="list-shop"' in html and "Groceries" in html
    assert '<option value="list-kid"' in html and "Kid chores" in html
    assert "reach Google just now" not in html


async def test_calendar_list_follows_every_page(admin_client, google, connected):
    google.get(CALENDAR_LIST_URL, params={"pageToken": "p2"}).respond(200, json={"items": [CALENDARS[1]]})
    google.get(CALENDAR_LIST_URL).respond(200, json={"items": [CALENDARS[0]], "nextPageToken": "p2"})
    google.get(TASKLISTS_URL).respond(200, json={"items": TASKLISTS})

    html = (await admin_client.get("/admin")).text

    assert 'value="family@example.com"' in html
    assert 'value="school#holidays"' in html


# --- Saving ---

async def test_saving_calendars_persists_googles_details_not_the_forms(admin_client, db, google_lists):
    resp = await admin_client.post(
        "/admin/google/calendars", data={"calendar_id": ["school#holidays", "not-on-this-account"]}
    )

    assert resp.status_code == 303
    assert await google_oauth.get_selected_calendars(db) == [
        {"id": "school#holidays", "summary": "School holidays", "color": "#D6A02C", "primary": False},
    ]
    html = (await admin_client.get("/admin")).text
    assert re.search(r'value="school#holidays"\s+checked', html)
    assert not re.search(r'value="family@example.com"\s+checked', html)


async def test_saving_no_known_calendars_keeps_the_previous_selection(admin_client, db, google_lists):
    before = await google_oauth.get_selected_calendars(db)

    await admin_client.post("/admin/google/calendars", data={"calendar_id": ["bogus"]})

    assert await google_oauth.get_selected_calendars(db) == before


async def test_saving_shopping_list_persists_and_relinks(admin_client, db, google_lists):
    await db.execute("INSERT INTO shopping_items (title, google_task_id) VALUES ('Milk', 'old-g-id')")
    await db.commit()

    resp = await admin_client.post("/admin/google/shopping-list", data={"tasklist_id": "list-shop"})

    assert resp.status_code == 303
    assert await task_sync.get_shopping_tasklist(db) == {"id": "list-shop", "title": "Groceries"}
    item = await (await db.execute("SELECT id, google_task_id FROM shopping_items")).fetchone()
    assert item["google_task_id"] is None
    assert await _queued(db) == [("shopping", {"item_id": item["id"]})]


async def test_saving_an_unknown_shopping_list_is_ignored(admin_client, db, google_lists):
    await admin_client.post("/admin/google/shopping-list", data={"tasklist_id": "forged"})

    assert await task_sync.get_shopping_tasklist(db) is None


async def test_saving_profile_task_lists_persists_and_relinks_only_changes(admin_client, db, google_lists):
    first, second = (p["id"] for p in (await _profiles(db))[:2])
    await db.execute("UPDATE profiles SET google_tasklist_id = 'list-kid' WHERE id = ?", (second,))
    await db.execute(
        "INSERT INTO tasks (profile_id, title, google_task_id) VALUES (?, 'Feed cat', 'g1')", (first,)
    )
    await db.commit()

    resp = await admin_client.post(
        "/admin/google/task-lists", data={f"tasklist_{first}": "list-shop", f"tasklist_{second}": "list-kid"}
    )

    assert resp.status_code == 303
    by_id = {p["id"]: p["google_tasklist_id"] for p in await _profiles(db)}
    assert by_id[first] == "list-shop"
    assert by_id[second] == "list-kid"
    task = await (await db.execute("SELECT id, google_task_id FROM tasks")).fetchone()
    assert task["google_task_id"] is None
    assert await _queued(db) == [("tasks", {"task_id": task["id"]})]  # unchanged profile not re-queued


async def test_blank_task_list_unlinks_a_profile(admin_client, db, google_lists):
    first = (await _profiles(db))[0]["id"]
    await db.execute("UPDATE profiles SET google_tasklist_id = 'list-kid' WHERE id = ?", (first,))
    await db.commit()

    await admin_client.post("/admin/google/task-lists", data={f"tasklist_{first}": ""})

    assert (await _profiles(db))[0]["google_tasklist_id"] is None


# --- Auth ---

@pytest.mark.parametrize("path, data", [
    ("/admin/google/calendars", {"calendar_id": "family@example.com"}),
    ("/admin/google/shopping-list", {"tasklist_id": "list-shop"}),
    ("/admin/google/task-lists", {"tasklist_1": "list-shop"}),
])
async def test_picker_saves_require_admin(client, db, google, connected, path, data):
    before = (await google_oauth.get_selected_calendars(db), await _profiles(db))

    resp = await client.post(path, data=data)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert not google.calls
    assert (await google_oauth.get_selected_calendars(db), await _profiles(db)) == before
    assert await task_sync.get_shopping_tasklist(db) is None


async def test_admin_page_requires_admin(client, google, connected):
    resp = await client.get("/admin")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert not google.calls


# --- Offline ---

async def test_admin_renders_when_google_is_unreachable(admin_client, google, connected):
    google.get(CALENDAR_LIST_URL).mock(side_effect=httpx.ConnectError("offline"))
    google.get(TASKLISTS_URL).mock(side_effect=httpx.ConnectError("offline"))

    resp = await admin_client.get("/admin")

    assert resp.status_code == 200
    assert "reach Google just now" in resp.text
    assert 'name="calendar_id"' not in resp.text


async def test_admin_renders_when_token_refresh_is_unreachable(admin_client, db, google):
    await google_oauth.store_tokens(
        db, {"access_token": "old", "refresh_token": "r", "expires_at": 0}, "family@example.com"
    )
    google.post(google_oauth.TOKEN_ENDPOINT).respond(503)

    resp = await admin_client.get("/admin")

    assert resp.status_code == 200
    assert "reach Google just now" in resp.text
    assert "family@example.com" in resp.text  # still connected, not wiped


async def test_tasklists_403_prompts_a_reconnect(admin_client, google, connected):
    google.get(CALENDAR_LIST_URL).respond(200, json={"items": CALENDARS})
    google.get(TASKLISTS_URL).respond(403)

    resp = await admin_client.get("/admin")

    assert resp.status_code == 200
    assert "Disconnect and reconnect above" in resp.text
    assert "reach Google just now" not in resp.text


@pytest.mark.parametrize("path, data", [
    ("/admin/google/calendars", {"calendar_id": "family@example.com"}),
    ("/admin/google/shopping-list", {"tasklist_id": "list-shop"}),
])
async def test_picker_saves_while_offline_change_nothing(admin_client, db, google, connected, path, data):
    google.get(CALENDAR_LIST_URL).mock(side_effect=httpx.ConnectError("offline"))
    google.get(TASKLISTS_URL).mock(side_effect=httpx.ConnectError("offline"))
    before = await google_oauth.get_selected_calendars(db)

    resp = await admin_client.post(path, data=data)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    assert await google_oauth.get_selected_calendars(db) == before
    assert await task_sync.get_shopping_tasklist(db) is None
