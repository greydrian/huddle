"""
Bidirectional Google Tasks sync for the shopping list and per-person task
lists. Runs on a schedule (app/scheduler.py) — never inline with a request,
so a slow/unreachable Google API never blocks the dashboard.

Two halves each cycle:
- push_pending_changes: drains sync_queue (rows written by shopping.py/
  tasks.py/admin.py on every local mutation — the "optimistic, offline-
  first" part already in place) and pushes them to Google.
- reconcile_*: pulls each configured list's current state from Google and
  reconciles it against the local rows — this is what picks up changes
  made from the phone. Conflicts are last-write-wins by timestamp.
"""

import json
from datetime import datetime

import httpx

from app.database import get_setting, set_setting
from app import google_oauth, google_tasks

SHOPPING_TASKLIST_SETTING = "google_shopping_tasklist"
MAX_RETRY = 5


def _parse_local_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _parse_google_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def _to_local_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def get_shopping_tasklist(db) -> dict | None:
    raw = await get_setting(db, SHOPPING_TASKLIST_SETTING)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def set_shopping_tasklist(db, tasklist: dict):
    await set_setting(db, SHOPPING_TASKLIST_SETTING, json.dumps(tasklist))
    await db.commit()


# --- Push: local mutation -> Google -------------------------------------

async def _push_shopping_change(db, access_token: str, payload: dict):
    tasklist = await get_shopping_tasklist(db)
    if not tasklist:
        return

    if payload.get("action") == "delete":
        gid = payload.get("google_task_id")
        if gid:
            await google_tasks.delete_task(access_token, tasklist["id"], gid)
        return

    item_id = payload.get("item_id")
    item = await (await db.execute("SELECT * FROM shopping_items WHERE id = ?", (item_id,))).fetchone()
    if item is None:
        return  # already deleted locally since this row was queued

    if item["google_task_id"]:
        await google_tasks.update_task(
            access_token, tasklist["id"], item["google_task_id"],
            title=item["title"], completed=bool(item["is_checked"]),
        )
    else:
        remote = await google_tasks.insert_task(
            access_token, tasklist["id"], item["title"], completed=bool(item["is_checked"]),
        )
        await db.execute("UPDATE shopping_items SET google_task_id = ? WHERE id = ?", (remote["id"], item_id))


async def _push_task_change(db, access_token: str, payload: dict):
    task_id = payload.get("task_id")
    task = await (await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))).fetchone()
    if task is None:
        return

    profile = await (await db.execute(
        "SELECT google_tasklist_id FROM profiles WHERE id = ?", (task["profile_id"],)
    )).fetchone()
    tasklist_id = profile["google_tasklist_id"] if profile else None
    if not tasklist_id:
        return

    if task["archived"]:
        if task["google_task_id"]:
            await google_tasks.delete_task(access_token, tasklist_id, task["google_task_id"])
        return

    if task["google_task_id"]:
        await google_tasks.update_task(
            access_token, tasklist_id, task["google_task_id"],
            title=task["title"], completed=bool(task["is_completed"]),
        )
    else:
        remote = await google_tasks.insert_task(
            access_token, tasklist_id, task["title"], completed=bool(task["is_completed"]),
        )
        await db.execute("UPDATE tasks SET google_task_id = ? WHERE id = ?", (remote["id"], task_id))


async def push_pending_changes(db, access_token: str):
    rows = await (await db.execute("SELECT * FROM sync_queue ORDER BY id")).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        try:
            if row["service"] == "shopping":
                await _push_shopping_change(db, access_token, payload)
            elif row["service"] == "tasks":
                await _push_task_change(db, access_token, payload)
            await db.execute("DELETE FROM sync_queue WHERE id = ?", (row["id"],))
        except httpx.HTTPError:
            new_count = row["retry_count"] + 1
            if new_count >= MAX_RETRY:
                await db.execute("DELETE FROM sync_queue WHERE id = ?", (row["id"],))
            else:
                await db.execute(
                    "UPDATE sync_queue SET retry_count = ? WHERE id = ?", (new_count, row["id"])
                )
        await db.commit()


# --- Reconcile: Google -> local -------------------------------------------

async def reconcile_shopping(db, access_token: str):
    tasklist = await get_shopping_tasklist(db)
    if not tasklist:
        return

    remote_tasks = await google_tasks.fetch_tasks(access_token, tasklist["id"])
    remote_by_id = {t["id"]: t for t in remote_tasks}

    local_rows = await (await db.execute("SELECT * FROM shopping_items")).fetchall()
    local_by_gid = {r["google_task_id"]: r for r in local_rows if r["google_task_id"]}

    for gid, rtask in remote_by_id.items():
        remote_completed = rtask.get("status") == "completed"
        remote_title = rtask.get("title") or "(untitled)"
        remote_updated = _parse_google_ts(rtask["updated"])

        if gid in local_by_gid:
            local = local_by_gid[gid]
            if remote_updated > _parse_local_ts(local["updated_at"]):
                await db.execute(
                    "UPDATE shopping_items SET title = ?, is_checked = ?, updated_at = ? WHERE id = ?",
                    (remote_title, int(remote_completed), _to_local_ts(remote_updated), local["id"]),
                )
        else:
            await db.execute(
                "INSERT INTO shopping_items (title, is_checked, google_task_id, updated_at) VALUES (?, ?, ?, ?)",
                (remote_title, int(remote_completed), gid, _to_local_ts(remote_updated)),
            )

    for gid, local in local_by_gid.items():
        if gid not in remote_by_id:
            await db.execute("DELETE FROM shopping_items WHERE id = ?", (local["id"],))

    await db.commit()


async def reconcile_profile_tasks(db, access_token: str, profile):
    tasklist_id = profile["google_tasklist_id"]
    if not tasklist_id:
        return

    remote_tasks = await google_tasks.fetch_tasks(access_token, tasklist_id)
    remote_by_id = {t["id"]: t for t in remote_tasks}

    local_rows = await (await db.execute(
        "SELECT * FROM tasks WHERE profile_id = ? AND archived = 0", (profile["id"],)
    )).fetchall()
    local_by_gid = {r["google_task_id"]: r for r in local_rows if r["google_task_id"]}

    for gid, rtask in remote_by_id.items():
        remote_completed = rtask.get("status") == "completed"
        remote_title = rtask.get("title") or "(untitled)"
        remote_updated = _parse_google_ts(rtask["updated"])
        completed_at = datetime.utcnow().isoformat() if remote_completed else None

        if gid in local_by_gid:
            local = local_by_gid[gid]
            if remote_updated > _parse_local_ts(local["updated_at"]):
                await db.execute(
                    """UPDATE tasks SET title = ?, is_completed = ?, completed_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (remote_title, int(remote_completed), completed_at, _to_local_ts(remote_updated), local["id"]),
                )
        else:
            await db.execute(
                """INSERT INTO tasks (profile_id, google_task_id, title, is_completed, completed_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (profile["id"], gid, remote_title, int(remote_completed), completed_at, _to_local_ts(remote_updated)),
            )

    for gid, local in local_by_gid.items():
        if gid not in remote_by_id:
            await db.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (local["id"],))

    await db.commit()


# --- Orchestration ---------------------------------------------------------

async def run_sync(db):
    access_token = await google_oauth.get_valid_access_token(db)
    if not access_token:
        return

    try:
        await push_pending_changes(db, access_token)
        await reconcile_shopping(db, access_token)

        profiles = await (await db.execute(
            "SELECT * FROM profiles WHERE google_tasklist_id IS NOT NULL"
        )).fetchall()
        for profile in profiles:
            await reconcile_profile_tasks(db, access_token, profile)
    except httpx.HTTPStatusError:
        # Most likely the connected account's token predates the `tasks`
        # scope, or a transient Google-side error — skip this cycle, the
        # next scheduled run tries again rather than crashing the scheduler.
        pass
