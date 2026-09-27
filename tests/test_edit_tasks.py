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


async def test_schedule_days_persist_locally_without_a_push(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1)

    resp = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "days": ["Fri", "Mon"]})

    assert resp.status_code == 303 and resp.headers["location"] == "/admin#tasks"
    task = await _task(db, task_id)
    assert task["title"] == "Feed cat"
    assert task["is_recurring"] == 1 and task["recurrence_rule"] == "Mon,Fri"
    # The schedule never goes to Google, so nothing to push and no LWW bump.
    assert task["updated_at"] == "2020-01-01 00:00:00"
    assert await _queue_payloads(db) == []


async def test_schedule_can_make_a_task_one_off_or_every_day(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1)

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "is_recurring": "true"})
    task = await _task(db, task_id)
    assert (task["is_recurring"], task["recurrence_rule"]) == (1, None)

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "days": ["Sun"]})
    task = await _task(db, task_id)
    assert (task["is_recurring"], task["recurrence_rule"]) == (1, "Sun")

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1})
    task = await _task(db, task_id)
    assert (task["is_recurring"], task["recurrence_rule"]) == (0, None)


async def test_schedule_all_seven_days_is_every_day(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1)

    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "days": days})

    task = await _task(db, task_id)
    assert (task["is_recurring"], task["recurrence_rule"]) == (1, None)


async def test_schedule_save_ignores_a_posted_title(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1, title="Keep me")

    resp = await admin_client.post(
        f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "title": "Hijacked", "days": ["Mon"]}
    )

    assert resp.status_code == 303
    task = await _task(db, task_id)
    assert task["title"] == "Keep me" and task["recurrence_rule"] == "Mon"


async def test_admin_page_prefills_schedule_form(db, admin_client):
    p1, p2 = (await _profile_ids(db))[:2]
    task_id = await _add_task(db, p2, title="Bins")
    await db.execute("UPDATE tasks SET is_recurring = 1, recurrence_rule = 'weekends' WHERE id = ?", (task_id,))
    await db.commit()

    html = (await admin_client.get("/admin")).text

    assert f'action="/admin/tasks/{task_id}/edit"' in html
    assert f'<option value="{p2}" selected>' in html
    assert 'value="Sat" checked' in html and 'value="Sun" checked' in html
    assert 'value="Mon" checked' not in html
    assert 'name="title"' not in html.split('id="tasks"')[1].split("<!-- Homework")[0]


async def test_admin_page_shows_hint_and_unlinked_note(db, admin_client):
    p1, p2 = (await _profile_ids(db))[:2]
    await db.execute("UPDATE profiles SET google_tasklist_id = 'list-a' WHERE id = ?", (p1,))
    await db.commit()

    html = (await admin_client.get("/admin")).text
    panel = html.split('id="tasks"')[1].split("<!-- Homework")[0]

    assert "Add, rename or delete tasks in Google Tasks — they sync here within a minute." in panel
    assert 'action="/admin/tasks"' not in html and "/delete\"" not in panel
    # Only the unlinked person gets the note.
    assert panel.count("Link a Google list under Google Account → Task Sync") == len(await _profile_ids(db)) - 1


async def test_removed_task_routes_are_gone(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1)

    add = await admin_client.post("/admin/tasks", data={"profile_id": p1, "title": "New"})
    delete = await admin_client.post(f"/admin/tasks/{task_id}/delete")

    assert add.status_code in (404, 405) and delete.status_code in (404, 405)
    assert (await _task(db, task_id))["archived"] == 0
    count = await (await db.execute("SELECT COUNT(*) FROM tasks")).fetchone()
    assert count[0] == 1
    assert await _queue_payloads(db) == []


async def test_edit_validation(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1, title="Keep me")

    assert (await admin_client.post("/admin/tasks/9999/edit", data={"profile_id": p1})).status_code == 404
    bad_profile = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": 9999, "days": ["Mon"]})
    assert bad_profile.status_code == 400
    missing_profile = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"days": ["Mon"]})
    assert missing_profile.status_code == 422

    task = await _task(db, task_id)
    assert (task["profile_id"], task["is_recurring"], task["recurrence_rule"]) == (p1, 0, None)
    assert await _queue_payloads(db) == []


async def test_edit_of_archived_task_is_404(db, admin_client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1)
    await db.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (task_id,))
    await db.commit()

    resp = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "days": ["Mon"]})

    assert resp.status_code == 404
    assert await _queue_payloads(db) == []


async def test_edit_requires_admin(db, client):
    p1 = (await _profile_ids(db))[0]
    task_id = await _add_task(db, p1)
    resp = await client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "days": ["Mon"]})
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"
    assert (await _task(db, task_id))["recurrence_rule"] is None
    assert await _queue_payloads(db) == []


async def test_same_person_edit_pushes_nothing(db, admin_client, connected, google):
    p1 = (await _profile_ids(db))[0]
    await db.execute("UPDATE profiles SET google_tasklist_id = 'list-a' WHERE id = ?", (p1,))
    task_id = await _add_task(db, p1, google_task_id="g-1")
    patch = google.patch(f"{TASKS_API}/list-a/tasks/g-1").respond(200, json={"id": "g-1"})

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "days": ["Mon"]})
    await task_sync.push_pending_changes(db, "tok")

    assert not patch.called
    task = await _task(db, task_id)
    assert (task["google_task_id"], task["updated_at"]) == ("g-1", "2020-01-01 00:00:00")
    assert await _queue_payloads(db) == []


async def test_google_rename_survives_a_schedule_save(db, admin_client, connected, google):
    """Admin page loaded, then the task renamed in Google, then its days saved
    here before the next sync: the rename must win."""
    p1 = (await _profile_ids(db))[0]
    await db.execute("UPDATE profiles SET google_tasklist_id = 'list-a' WHERE id = ?", (p1,))
    task_id = await _add_task(db, p1, google_task_id="g-1")
    await db.execute("UPDATE tasks SET updated_at = '2026-09-27 10:00:00' WHERE id = ?", (task_id,))
    await db.commit()
    google.get(url__regex=rf"{TASKS_API}/list-a/tasks(\?.*)?$").respond(200, json={"items": [
        {"id": "g-1", "title": "Feed the cat", "status": "needsAction", "updated": "2026-09-27T10:00:30Z"},
    ]})
    patch = google.patch(f"{TASKS_API}/list-a/tasks/g-1").respond(200, json={"id": "g-1"})

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": p1, "days": ["Sat"]})
    await task_sync.run_sync(db)

    assert not patch.called
    task = await _task(db, task_id)
    assert (task["title"], task["recurrence_rule"]) == ("Feed the cat", "Sat")


async def test_reassign_to_person_without_google_list_is_rejected(db, admin_client):
    pa, pb = await _link_lists(db, ["A"]) + [(await _profile_ids(db))[1]]
    task_id = await _add_task(db, pa, google_task_id="g-old")

    resp = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": pb, "days": ["Mon"]})

    assert resp.status_code == 400
    task = await _task(db, task_id)
    assert (task["profile_id"], task["google_task_id"], task["recurrence_rule"]) == (pa, "g-old", None)
    assert await _live_tasks(db) == [(pa, "Feed cat", "g-old")]
    tombstones = await (await db.execute("SELECT COUNT(*) FROM tasks WHERE archived = 1")).fetchone()
    assert tombstones[0] == 0
    assert await _queue_payloads(db) == []


async def test_person_select_disables_unlinked_people(db, admin_client):
    pa, pb = await _link_lists(db, ["A", "B"])
    others = (await _profile_ids(db))[2:]
    await _add_task(db, pa)
    await _add_task(db, others[0], title="Legacy chore")

    panel = (await admin_client.get("/admin")).text.split('id="tasks"')[1].split("<!-- Homework")[0]

    assert f'<option value="{pb}">' in panel
    assert f'<option value="{others[-1]}" disabled>' in panel
    # A task already under an unlinked person keeps that person selectable.
    assert f'<option value="{others[0]}" selected>' in panel
    assert "These stay on this display only until a Google list is linked below" in panel


class FakeTasks:
    """Just enough of the Google Tasks API, keeping per-list state, to run
    whole sync cycles. Deletes on lists in `fail_delete` answer 400."""

    def __init__(self, google, lists):
        self.lists = {name: {} for name in lists}
        self.fail_delete = set()
        self.calls = []
        self._next = 0
        base = r"https://tasks\.googleapis\.com/tasks/v1/lists/(?P<tl>[^/]+)/tasks"
        google.route(method="GET", url__regex=base + r"(\?.*)?$").mock(side_effect=self._list)
        google.route(method="POST", url__regex=base + "$").mock(side_effect=self._insert)
        google.route(method="PATCH", url__regex=base + r"/(?P<gid>[^/?]+)$").mock(side_effect=self._patch)
        google.route(method="DELETE", url__regex=base + r"/(?P<gid>[^/?]+)$").mock(side_effect=self._delete)

    def _list(self, request, tl):
        items = [{"id": gid, "title": t, "updated": "2020-01-01T00:00:00Z"} for gid, t in self.lists[tl].items()]
        return httpx.Response(200, json={"items": items})

    def _insert(self, request, tl):
        self._next += 1
        gid = f"{tl}-{self._next}"
        self.lists[tl][gid] = json.loads(request.content)["title"]
        self.calls.append(("insert", tl, gid))
        return httpx.Response(200, json={"id": gid})

    def _patch(self, request, tl, gid):
        self.calls.append(("patch", tl, gid))
        self.lists[tl][gid] = json.loads(request.content).get("title", self.lists[tl][gid])
        return httpx.Response(200, json={"id": gid})

    def _delete(self, request, tl, gid):
        self.calls.append(("delete", tl, gid))
        if tl in self.fail_delete:
            return httpx.Response(400)
        self.lists[tl].pop(gid, None)
        return httpx.Response(204)


async def _link_lists(db, names):
    ids = (await _profile_ids(db))[:len(names)]
    for pid, name in zip(ids, names, strict=True):
        await db.execute("UPDATE profiles SET google_tasklist_id = ? WHERE id = ?", (name, pid))
    await db.commit()
    return ids


async def _live_tasks(db):
    rows = await (await db.execute(
        "SELECT profile_id, title, google_task_id FROM tasks WHERE archived = 0"
    )).fetchall()
    return [tuple(r) for r in rows]


async def _move(admin_client, task_id, profile_id):
    resp = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": profile_id, "is_recurring": "true"})
    assert resp.status_code == 303


async def test_reassign_full_sync_moves_task_between_lists(db, admin_client, connected, google):
    fake = FakeTasks(google, ["A", "B"])
    fake.lists["A"]["g-old"] = "Feed cat"
    pa, pb = await _link_lists(db, ["A", "B"])
    task_id = await _add_task(db, pa, google_task_id="g-old")

    await _move(admin_client, task_id, pb)
    await task_sync.run_sync(db)
    await task_sync.run_sync(db)

    assert fake.calls == [("delete", "A", "g-old"), ("insert", "B", "B-1")]
    assert fake.lists == {"A": {}, "B": {"B-1": "Feed cat"}}
    assert await _live_tasks(db) == [(pb, "Feed cat", "B-1")]
    assert await _queue_payloads(db) == []
    task = await _task(db, task_id)
    assert (task["is_recurring"], task["recurrence_rule"]) == (1, None)


async def test_reassign_old_list_delete_rejected_does_not_resurrect(db, admin_client, connected, google):
    fake = FakeTasks(google, ["A", "B"])
    fake.lists["A"]["g-old"] = "Feed cat"
    fake.fail_delete.add("A")
    pa, pb = await _link_lists(db, ["A", "B"])
    task_id = await _add_task(db, pa, google_task_id="g-old")

    await _move(admin_client, task_id, pb)
    await task_sync.run_sync(db)
    await task_sync.run_sync(db)

    assert await _live_tasks(db) == [(pb, "Feed cat", "B-1")]
    assert [c for c in fake.calls if c[0] == "insert"] == [("insert", "B", "B-1")]
    assert ("delete", "A", "g-old") in fake.calls


async def test_reassign_twice_before_one_sync(db, admin_client, connected, google):
    fake = FakeTasks(google, ["A", "B", "C"])
    fake.lists["A"]["g-old"] = "Feed cat"
    pa, pb, pc = await _link_lists(db, ["A", "B", "C"])
    task_id = await _add_task(db, pa, google_task_id="g-old")

    await _move(admin_client, task_id, pb)
    await _move(admin_client, task_id, pc)
    await task_sync.run_sync(db)
    await task_sync.run_sync(db)

    # Two edits queue two task rows: insert then a (harmless) update.
    assert fake.calls == [("delete", "A", "g-old"), ("insert", "C", "C-1"), ("patch", "C", "C-1")]
    assert fake.lists == {"A": {}, "B": {}, "C": {"C-1": "Feed cat"}}
    assert await _live_tasks(db) == [(pc, "Feed cat", "C-1")]


async def test_reassign_never_synced_task(db, admin_client, connected, google):
    fake = FakeTasks(google, ["A", "B"])
    pa, pb = await _link_lists(db, ["A", "B"])
    task_id = await _add_task(db, pa)

    await _move(admin_client, task_id, pb)
    await task_sync.run_sync(db)
    await task_sync.run_sync(db)

    assert fake.calls == [("insert", "B", "B-1")]
    assert await _live_tasks(db) == [(pb, "Feed cat", "B-1")]
    tombstones = await (await db.execute("SELECT COUNT(*) FROM tasks WHERE archived = 1")).fetchone()
    assert tombstones[0] == 0


async def test_reassign_during_outage_keeps_queue_intact(db, admin_client, connected, google):
    pa, pb = await _link_lists(db, ["A", "B"])
    task_id = await _add_task(db, pa, google_task_id="g-old")
    await _move(admin_client, task_id, pb)
    queued = await _queue_payloads(db)
    assert len(queued) == 2 and queued[1] == {"task_id": task_id}
    google.delete(f"{TASKS_API}/A/tasks/g-old").mock(side_effect=httpx.ConnectError("offline"))
    insert = google.post(f"{TASKS_API}/B/tasks").respond(200, json={"id": "g-new"})

    for _ in range(3):
        await task_sync.run_sync(db)

    assert not insert.called
    assert await _queue_payloads(db) == queued
    retries = await (await db.execute("SELECT retry_count FROM sync_queue")).fetchall()
    assert [r["retry_count"] for r in retries] == [0, 0]


async def test_tombstone_is_hidden_from_dashboard_and_admin(db, admin_client, connected):
    pa, pb = await _link_lists(db, ["A", "B"])
    task_id = await _add_task(db, pa, title="Feed cat", google_task_id="g-old")
    await _move(admin_client, task_id, pb)

    admin_html = (await admin_client.get("/admin")).text
    widget_html = (await admin_client.get("/widgets/tasks")).text

    assert admin_html.count('class="task-row-label">Feed cat ') == 1
    assert widget_html.count("Feed cat") == 1
    tombstone = await (await db.execute(
        "SELECT profile_id, google_task_id FROM tasks WHERE archived = 1"
    )).fetchall()
    assert [tuple(r) for r in tombstone] == [(pa, "g-old")]
