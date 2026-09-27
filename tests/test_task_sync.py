import json

import httpx

from app import task_sync

TASKS_API = "https://tasks.googleapis.com/tasks/v1/lists"
SHOP_LIST = {"id": "shop", "title": "Shopping"}


async def _queue_rows(db):
    return await (await db.execute("SELECT * FROM sync_queue ORDER BY id")).fetchall()


async def test_item_added_on_dashboard_is_pushed_to_google(db, connected, client, google):
    await task_sync.set_shopping_tasklist(db, SHOP_LIST)
    insert = google.post(f"{TASKS_API}/shop/tasks").respond(200, json={"id": "g-milk"})

    resp = await client.post("/api/shopping", data={"title": "Milk"})
    assert resp.status_code == 200

    await task_sync.push_pending_changes(db, "tok")

    assert insert.called
    assert json.loads(insert.calls.last.request.content)["title"] == "Milk"
    row = await (await db.execute("SELECT google_task_id FROM shopping_items")).fetchone()
    assert row["google_task_id"] == "g-milk"
    assert await _queue_rows(db) == []


async def test_outage_keeps_queue_and_does_not_burn_retries(db, connected, google):
    await task_sync.set_shopping_tasklist(db, SHOP_LIST)
    cur = await db.execute("INSERT INTO shopping_items (title) VALUES ('Eggs')")
    await db.execute(
        "INSERT INTO sync_queue (service, payload_json) VALUES ('shopping', ?)",
        (json.dumps({"item_id": cur.lastrowid}),),
    )
    await db.commit()
    google.post(f"{TASKS_API}/shop/tasks").mock(side_effect=httpx.ConnectError("offline"))

    for _ in range(10):  # well past MAX_RETRY
        await task_sync.run_sync(db)

    rows = await _queue_rows(db)
    assert len(rows) == 1
    assert rows[0]["retry_count"] == 0


async def test_permanent_rejection_drops_queue_row(db, connected, google):
    await task_sync.set_shopping_tasklist(db, SHOP_LIST)
    await db.execute(
        "INSERT INTO sync_queue (service, payload_json) VALUES ('shopping', ?)",
        (json.dumps({"action": "delete", "google_task_id": "gone"}),),
    )
    await db.commit()
    google.delete(f"{TASKS_API}/shop/tasks/gone").respond(400)

    await task_sync.push_pending_changes(db, "tok")

    assert await _queue_rows(db) == []


async def test_reconcile_reads_every_page_before_deleting_anything(db, connected, google):
    await task_sync.set_shopping_tasklist(db, SHOP_LIST)
    await db.execute(
        "INSERT INTO shopping_items (title, google_task_id, updated_at) VALUES ('Bread', 'g2', datetime('now'))"
    )
    await db.commit()
    stamp = "2020-01-01T00:00:00.000Z"
    route = google.get(f"{TASKS_API}/shop/tasks")
    route.side_effect = [
        httpx.Response(200, json={"items": [{"id": "g1", "title": "Milk", "updated": stamp}], "nextPageToken": "p2"}),
        httpx.Response(200, json={"items": [{"id": "g2", "title": "Bread", "updated": stamp}]}),
    ]

    await task_sync.reconcile_shopping(db, "tok")

    titles = sorted(r["title"] for r in await (await db.execute("SELECT title FROM shopping_items")).fetchall())
    assert titles == ["Bread", "Milk"]
    assert route.call_count == 2


async def test_archived_task_is_not_resurrected_by_reconcile(db, connected, google):
    await db.execute("UPDATE profiles SET google_tasklist_id = 'kids' WHERE id = 1")
    await db.execute(
        """INSERT INTO tasks (profile_id, title, google_task_id, archived, updated_at)
           VALUES (1, 'Old chore', 'g-old', 1, datetime('now'))"""
    )
    await db.commit()
    google.get(f"{TASKS_API}/kids/tasks").respond(
        200, json={"items": [{"id": "g-old", "title": "Old chore", "updated": "2020-01-01T00:00:00Z"}]}
    )
    profile = await (await db.execute("SELECT * FROM profiles WHERE id = 1")).fetchone()

    await task_sync.reconcile_profile_tasks(db, "tok", profile)
    await task_sync.reconcile_profile_tasks(db, "tok", profile)

    count = await (await db.execute("SELECT COUNT(*) FROM tasks WHERE google_task_id = 'g-old'")).fetchone()
    assert count[0] == 1
    assert len(await _queue_rows(db)) == 1, "should queue the delete once, not once per cycle"


async def test_linking_a_new_list_backfills_instead_of_wiping(db, connected, google):
    await task_sync.set_shopping_tasklist(db, SHOP_LIST)
    await db.execute("INSERT INTO shopping_items (title, google_task_id) VALUES ('Tea', 'old-list-id')")
    await db.commit()

    await task_sync.set_shopping_tasklist(db, {"id": "new", "title": "New"})
    await task_sync.relink_shopping(db)

    google.post(f"{TASKS_API}/new/tasks").respond(200, json={"id": "new-tea"})
    google.get(f"{TASKS_API}/new/tasks").respond(
        200, json={"items": [{"id": "new-tea", "title": "Tea", "updated": "2020-01-01T00:00:00Z"}]}
    )
    await task_sync.run_sync(db)

    rows = await (await db.execute("SELECT title, google_task_id FROM shopping_items")).fetchall()
    assert [(r["title"], r["google_task_id"]) for r in rows] == [("Tea", "new-tea")]
