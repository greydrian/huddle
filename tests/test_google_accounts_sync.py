"""Dormant task lists (spec 12.2, 12.4, 12.6): unticking Tasks & shopping
or removing the account keeps its list links and Google ids; local changes
keep queueing and wait; re-ticking or adding the same account back pushes
them, then reconciles, with nothing duplicated or resurrected on either side."""

import itertools
import json
import re
import time
from urllib.parse import unquote

import httpx
import pytest

from app import google_accounts, google_oauth, task_sync
from app.routers import admin
from app.security import create_session_token
from app.services import accounts, shopping, tasks

RILEY = 3
OLD = "2026-01-01T00:00:00.000Z"
TASKS_URL = re.compile(r"https://tasks\.googleapis\.com/tasks/v1/lists/([^/]+)/tasks(?:/([^/?]+))?")
ALL_JOBS = ["calendars", "tasks", "school_email", "write_events"]


class FakeTasks:
    """Google Tasks for a few lists: {list id: {task id: task}}, with every
    insert counted (a duplicate would be a second insert)."""

    def __init__(self, lists: dict[str, list[tuple[str, str]]]):
        self.lists = {
            list_id: {gid: {"id": gid, "title": title, "status": "needsAction", "updated": OLD} for gid, title in items}
            for list_id, items in lists.items()
        }
        self.inserts: list[tuple[str, str]] = []
        self._ids = itertools.count(100)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        match = TASKS_URL.match(str(request.url))
        assert match, request.url
        list_id, task_id = unquote(match.group(1)), match.group(2)
        tasklist = self.lists.setdefault(list_id, {})
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 5))
        if request.method == "GET":
            return httpx.Response(200, json={"items": list(tasklist.values())})
        if request.method == "POST":
            body = json.loads(request.content)
            gid = f"g{next(self._ids)}"
            tasklist[gid] = {"id": gid, "title": body["title"], "status": body["status"], "updated": now}
            self.inserts.append((list_id, body["title"]))
            return httpx.Response(200, json=tasklist[gid])
        if request.method == "PATCH":
            tasklist[task_id].update(json.loads(request.content), updated=now)
            return httpx.Response(200, json=tasklist[task_id])
        if request.method == "DELETE":
            tasklist.pop(task_id, None)
            return httpx.Response(204)
        raise AssertionError(request.method)

    def titles(self, list_id: str) -> list[tuple[str, str]]:
        return sorted((t["title"], t["status"]) for t in self.lists[list_id].values())


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


@pytest.fixture
async def synced(db, google):
    """The family account does every job; Riley's list and the shopping list
    are linked in it and in step with Google."""
    tokens = {
        "access_token": "tok",
        "refresh_token": "r",
        "expires_at": time.time() + 3600,
        "scope": google_oauth.SCOPES,
    }
    await google_oauth.store_tokens(db, tokens, "family@example.com")
    fake = FakeTasks({"list-riley": [("g1", "Feed cat")], "shop": [("s1", "Milk"), ("s2", "Eggs")]})
    google.route(url__regex=TASKS_URL.pattern).mock(side_effect=fake)
    await db.execute(
        "UPDATE profiles SET google_tasklist_id = 'list-riley', google_account_id = 1 WHERE id = ?", (RILEY,)
    )
    await db.execute(
        "INSERT INTO tasks (profile_id, title, google_task_id, updated_at) VALUES (?, 'Feed cat', 'g1', '2026-01-01 00:00:00')",
        (RILEY,),
    )
    await db.execute(
        "INSERT INTO shopping_items (title, google_task_id, updated_at) VALUES "
        "('Milk', 's1', '2026-01-01 00:00:00'), ('Eggs', 's2', '2026-01-01 00:00:00')"
    )
    await db.commit()
    await task_sync.set_shopping_tasklist(db, {"id": "shop", "title": "Shopping", "account_id": 1})
    assert await task_sync.run_sync(db)
    assert fake.inserts == []
    return fake


async def _local(db):
    task_rows = await (
        await db.execute("SELECT title, google_task_id, is_completed FROM tasks WHERE archived = 0 ORDER BY id")
    ).fetchall()
    item_rows = await (await db.execute("SELECT title, google_task_id FROM shopping_items ORDER BY id")).fetchall()
    return [tuple(r) for r in task_rows], [tuple(r) for r in item_rows]


async def _queued(db) -> int:
    return (await (await db.execute("SELECT COUNT(*) FROM sync_queue")).fetchone())[0]


async def test_remove_then_add_the_same_account_back_duplicates_nothing(admin_client, db, google, synced):
    before = await _local(db)
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)
    await admin_client.post("/admin/google/accounts/1/remove")
    assert await _local(db) == before  # every link and Google id kept

    # Added back through Add account: the same `sub`, so the same row comes back.
    start = await admin_client.post("/admin/google/accounts", data={"owner": "family", "job": ALL_JOBS})
    google.post(google_oauth.TOKEN_ENDPOINT).respond(
        200, json={"access_token": "tok", "refresh_token": "r2", "expires_in": 3600, "scope": google_oauth.SCOPES}
    )
    google.get(google_oauth.USERINFO_ENDPOINT).respond(
        200, json={"email": "family@example.com", "sub": "local:family@example.com"}
    )
    state = start.cookies["google_oauth_state"]
    await admin_client.get("/admin/google/callback", params={"code": "c", "state": state})
    assert [a["id"] for a in await google_accounts.list_accounts(db)] == [1]

    await task_sync.run_sync(db)
    await task_sync.run_sync(db)

    assert synced.inserts == []  # nothing pushed again...
    assert await _local(db) == before  # ...or pulled back in
    assert synced.titles("list-riley") == [("Feed cat", "needsAction")]
    assert synced.titles("shop") == [("Eggs", "needsAction"), ("Milk", "needsAction")]


async def test_untick_then_re_tick_tasks_duplicates_nothing(admin_client, db, synced):
    before = await _local(db)
    await admin_client.post("/admin/google/accounts/1/edit", data={"owner": "family", "job": ["calendars"]})
    await task_sync.run_sync(db)  # no account does Tasks: nothing touches Google
    await admin_client.post("/admin/google/accounts/1/edit", data={"owner": "family", "job": ALL_JOBS})
    await task_sync.run_sync(db)
    assert synced.inserts == []
    assert await _local(db) == before


async def test_relinking_the_same_list_clears_no_google_ids(admin_client, db, google, synced):
    google.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").respond(
        200, json={"items": [{"id": "list-riley", "title": "Riley"}, {"id": "shop", "title": "Shopping"}]}
    )
    before = await _local(db)
    await admin_client.post("/admin/google/task-lists", data={f"tasklist_{RILEY}": "list-riley"})
    await admin_client.post("/admin/google/shopping-list", data={"tasklist_id": "shop"})
    assert await _local(db) == before and await _queued(db) == 0
    await task_sync.run_sync(db)
    assert synced.inserts == []


async def test_changes_made_while_dormant_wait_and_reach_google_once(admin_client, db, synced):
    """Correction A: untick, add a one-off, tick a task and delete a shopping
    item, re-tick, sync: Google gets all three, nothing doubled or revived."""
    await admin_client.post("/admin/google/accounts/1/edit", data={"owner": "family", "job": ["calendars"]})
    tasks.reset_rate_limit()
    await tasks.quick_add(db, RILEY, "Pack bag")  # queued: a dormant link still counts as linked
    feed_cat = (await (await db.execute("SELECT id FROM tasks WHERE title = 'Feed cat'")).fetchone())["id"]
    await tasks.toggle_task(db, feed_cat)
    milk = (await (await db.execute("SELECT id FROM shopping_items WHERE title = 'Milk'")).fetchone())["id"]
    await shopping.delete_item(db, milk)
    assert await _queued(db) == 3
    await task_sync.run_sync(db)  # nothing is dropped while dormant
    assert await _queued(db) == 3

    await admin_client.post("/admin/google/accounts/1/edit", data={"owner": "family", "job": ALL_JOBS})
    await task_sync.run_sync(db)
    await task_sync.run_sync(db)

    assert await _queued(db) == 0
    assert synced.inserts == [("list-riley", "Pack bag")]  # the one-off, once
    assert synced.titles("list-riley") == [("Feed cat", "completed"), ("Pack bag", "needsAction")]
    assert synced.titles("shop") == [("Eggs", "needsAction")]  # deleted, not revived
    task_rows, item_rows = await _local(db)
    assert sorted((t[0], t[2]) for t in task_rows) == [("Feed cat", 1), ("Pack bag", 0)]
    assert [i[0] for i in item_rows] == ["Eggs"]


async def test_a_dormant_lists_changes_wait_while_another_account_does_tasks(db, synced):
    await google_accounts.update(db, 1, None, ["calendars"])
    other = {"access_token": "tok-2", "refresh_token": "r-2", "expires_at": time.time() + 3600}
    other["scope"] = f"{google_oauth.TASKS_SCOPE} openid email"
    await google_oauth.store_tokens(db, other, "other@example.com")
    assert (await google_accounts.job_account(db, "tasks"))["id"] == 2
    feed_cat = (await (await db.execute("SELECT id FROM tasks WHERE title = 'Feed cat'")).fetchone())["id"]
    await tasks.toggle_task(db, feed_cat)

    await task_sync.run_sync(db)

    assert await _queued(db) == 1  # kept for account 1's list, not pushed through account 2
    assert synced.titles("list-riley") == [("Feed cat", "needsAction")]


async def test_removing_keeps_the_dormant_links_on_the_kept_row(db, google, synced):
    google.post(url__startswith=google_oauth.REVOKE_ENDPOINT).respond(200)
    await accounts.remove(db, 1)
    row = await (
        await db.execute("SELECT google_account_id, google_tasklist_id FROM profiles WHERE id = ?", (RILEY,))
    ).fetchone()
    assert tuple(row) == (1, "list-riley")
    assert (await task_sync.get_shopping_tasklist(db))["account_id"] == 1
