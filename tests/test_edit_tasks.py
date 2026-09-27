import json

import httpx
import pytest

from app import task_sync
from app.routers import admin
from app.security import create_session_token

TASKS_API = "https://tasks.googleapis.com/tasks/v1/lists"


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


async def _profile_ids(db):
    rows = await (await db.execute("SELECT id FROM profiles ORDER BY sort_order")).fetchall()
    return [r["id"] for r in rows]


async def _add_task(db, profile_id, title="Feed cat", google_task_id=None):
    cur = await db.execute(
        """INSERT INTO tasks (profile_id, title, google_task_id, updated_at)
           VALUES (?, ?, ?, '2020-01-01 00:00:00')""",
        (profile_id, title, google_task_id),
    )
    await db.commit()
    return cur.lastrowid


async def _task(db, task_id):
    return await (await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))).fetchone()


async def _queue_payloads(db):
    rows = await (await db.execute("SELECT payload_json FROM sync_queue ORDER BY id")).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


async def test_edit_title_and_days_persists_and_queues_sync(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1)

    resp = await admin_client.post(
        f"/admin/tasks/{task_id}/edit",
        data={"profile_id": p1, "title": "  Walk dog ", "days": ["Fri", "Mon"]},
    )

    assert resp.status_code == 303 and resp.headers["location"] == "/admin"
    task = await _task(db, task_id)
    assert task["title"] == "Walk dog"
    assert task["is_recurring"] == 1 and task["recurrence_rule"] == "Mon,Fri"
    assert task["updated_at"] > "2020-01-01 00:00:00"
    assert await _queue_payloads(db) == [{"task_id": task_id}]


async def test_edit_can_make_a_task_one_off_or_every_day(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1)

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "title": "X", "is_recurring": "true"})
    task = await _task(db, task_id)
    assert (task["is_recurring"], task["recurrence_rule"]) == (1, None)

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "title": "X"})
    task = await _task(db, task_id)
    assert (task["is_recurring"], task["recurrence_rule"]) == (0, None)


async def test_admin_page_prefills_edit_form(db, admin_client):
    p1, p2 = (await _profile_ids(db))[:2]
    task_id = await _add_task(db, p2, title="Bins")
    await db.execute("UPDATE tasks SET is_recurring = 1, recurrence_rule = 'weekends' WHERE id = ?", (task_id,))
    await db.commit()

    html = (await admin_client.get("/admin")).text

    assert f'action="/admin/tasks/{task_id}/edit"' in html
    assert f'<option value="{p2}" selected>' in html
    assert 'value="Sat" checked' in html and 'value="Sun" checked' in html
    assert 'value="Mon" checked' not in html


async def test_edit_validation(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1, title="Keep me")

    assert (await admin_client.post("/admin/tasks/9999/edit", data={"profile_id": p1, "title": "X"})).status_code == 404
    blank = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "title": "   "})
    assert blank.status_code == 400
    bad_profile = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": 9999, "title": "X"})
    assert bad_profile.status_code == 400

    assert (await _task(db, task_id))["title"] == "Keep me"
    assert await _queue_payloads(db) == []


async def test_edit_requires_admin(db, client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1)
    resp = await client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "title": "X"})
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"


async def test_same_person_edit_is_a_normal_update_push(db, admin_client, connected, google):
    p1 = (await _profile_ids(db))[0]
    await db.execute("UPDATE profiles SET google_tasklist_id = 'list-a' WHERE id = ?", (p1,))
    task_id = await _add_task(db, p1, google_task_id="g-1")
    patch = google.patch(f"{TASKS_API}/list-a/tasks/g-1").respond(200, json={"id": "g-1"})

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "title": "Renamed"})
    await task_sync.push_pending_changes(db, "tok")

    assert json.loads(patch.calls.last.request.content)["title"] == "Renamed"
    assert (await _task(db, task_id))["google_task_id"] == "g-1"
    assert await _queue_payloads(db) == []


async def _reassign_setup(db, admin_client):
    p1, p2 = (await _profile_ids(db))[:2]
    await db.execute("UPDATE profiles SET google_tasklist_id = 'list-a' WHERE id = ?", (p1,))
    await db.execute("UPDATE profiles SET google_tasklist_id = 'list-b' WHERE id = ?", (p2,))
    task_id = await _add_task(db, p1, google_task_id="g-old")
    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p2, "title": "Feed cat"})
    return task_id, p2


async def test_reassigning_person_moves_task_between_google_lists(db, admin_client, connected, google):
    task_id, p2 = await _reassign_setup(db, admin_client)
    delete = google.delete(f"{TASKS_API}/list-a/tasks/g-old").respond(204)
    insert = google.post(f"{TASKS_API}/list-b/tasks").respond(200, json={"id": "g-new"})

    await task_sync.push_pending_changes(db, "tok")

    assert delete.called and insert.called
    assert json.loads(insert.calls.last.request.content)["title"] == "Feed cat"
    task = await _task(db, task_id)
    assert (task["profile_id"], task["google_task_id"]) == (p2, "g-new")
    assert await _queue_payloads(db) == []


async def test_reassign_during_outage_keeps_queue_intact(db, admin_client, connected, google):
    task_id, _ = await _reassign_setup(db, admin_client)
    queued = await _queue_payloads(db)
    assert queued == [
        {"action": "delete", "tasklist_id": "list-a", "google_task_id": "g-old"},
        {"task_id": task_id},
    ]
    google.delete(f"{TASKS_API}/list-a/tasks/g-old").mock(side_effect=httpx.ConnectError("offline"))
    insert = google.post(f"{TASKS_API}/list-b/tasks").respond(200, json={"id": "g-new"})

    for _ in range(3):
        await task_sync.run_sync(db)

    assert not insert.called
    assert await _queue_payloads(db) == queued
    retries = await (await db.execute("SELECT retry_count FROM sync_queue")).fetchall()
    assert [r["retry_count"] for r in retries] == [0, 0]


async def test_reassign_to_unlinked_person_still_removes_old_copy(db, admin_client, connected, google):
    p1, p2 = (await _profile_ids(db))[:2]
    await db.execute("UPDATE profiles SET google_tasklist_id = 'list-a' WHERE id = ?", (p1,))
    task_id = await _add_task(db, p1, google_task_id="g-old")
    delete = google.delete(f"{TASKS_API}/list-a/tasks/g-old").respond(204)

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p2, "title": "Feed cat"})
    await task_sync.push_pending_changes(db, "tok")

    assert delete.called
    assert (await _task(db, task_id))["google_task_id"] is None
    assert await _queue_payloads(db) == []
