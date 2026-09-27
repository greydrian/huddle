"""
Bidirectional Google Tasks sync for the shopping list and per-person task
lists. Runs on a schedule (app/scheduler.py) — never inline with a request,
so a slow/unreachable Google API never blocks the dashboard.

Two halves each cycle:
- push_pending_changes: drains sync_queue (rows written by shopping.py/
  tasks.py/admin.py on every local mutation — the "optimistic, offline-
  first" part) and pushes them to Google.
- reconcile_*: pulls each configured list's current state from Google and
  reconciles it against the local rows — this is what picks up changes
  made from the phone. Conflicts are last-write-wins by timestamp.

Reconcile only runs after the queue has fully drained: reconciling while
local changes are still pending would let Google's stale view overwrite
them (e.g. resurrect an item deleted offline).
"""

import json
from datetime import datetime, timezone

import httpx

from app.database import get_setting, set_setting
from app import google_oauth, google_tasks

SHOPPING_TASKLIST_SETTING = "google_shopping_tasklist"
MAX_RETRY = 5


class _StopCycle(Exception):
    """Google is unreachable/rate-limiting/unauthorised — stop this cycle
    and leave the queue untouched so nothing is lost; retry next cycle."""


def _parse_local_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _parse_google_ts(s: str) -> datetime:
    # Truncated to seconds to match SQLite's datetime('now') precision —
    # otherwise Google's milliseconds make every row look newer every cycle.
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0)


def _to_local_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


async def _queue(db, service: str, payload: dict):
    await db.execute(
        "INSERT INTO sync_queue (service, payload_json) VALUES (?, ?)", (service, json.dumps(payload))
    )


async def relink_shopping(db):
    """The shopping list was linked to a (different) Google list: forget old
    Google ids and queue every item so it's pushed to the new list, instead
    of reconcile treating them as "deleted on Google" and wiping them."""
    await db.execute("UPDATE shopping_items SET google_task_id = NULL")
    for row in await (await db.execute("SELECT id FROM shopping_items")).fetchall():
        await _queue(db, "shopping", {"item_id": row["id"]})
    await db.commit()


async def relink_profile(db, profile_id: int):
    """Same as relink_shopping, for one family member's task list."""
    await db.execute("UPDATE tasks SET google_task_id = NULL WHERE profile_id = ?", (profile_id,))
    rows = await (await db.execute(
        "SELECT id FROM tasks WHERE profile_id = ? AND archived = 0", (profile_id,)
    )).fetchall()
    for row in rows:
        await _queue(db, "tasks", {"task_id": row["id"]})
    await db.commit()


# --- Push: local mutation -> Google -------------------------------------

async def _push_shopping_change(db, access_token: str, payload: dict):
    tasklist = await get_shopping_tasklist(db)
    if not tasklist:
        return  # not linked — relink_shopping backfills everything on link

    if payload.get("action") == "delete":
        gid = payload.get("google_task_id")
        if gid:
            await google_tasks.delete_task(access_token, tasklist["id"], gid)
        return

    item_id = payload.get("item_id")
    item = await (await db.execute("SELECT * FROM shopping_items WHERE id = ?", (item_id,))).fetchone()
    if item is None:
        return  # deleted locally since this row was queued; the delete has its own row

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
        return  # not linked — relink_profile backfills on link

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


def _is_transient(exc: httpx.HTTPError) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
    return status in (401, 403, 408, 429) or status >= 500


async def push_pending_changes(db, access_token: str):
    """Raises _StopCycle (queue untouched) when Google is unreachable,
    rate-limiting, or refusing auth — those are never counted as retries,
    so an outage can't burn through MAX_RETRY and silently drop changes."""
    rows = await (await db.execute("SELECT * FROM sync_queue ORDER BY id")).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        try:
            if row["service"] == "shopping":
                await _push_shopping_change(db, access_token, payload)
            elif row["service"] == "tasks":
                await _push_task_change(db, access_token, payload)
            await db.execute("DELETE FROM sync_queue WHERE id = ?", (row["id"],))
        except httpx.HTTPError as exc:
            if _is_transient(exc):
                await db.commit()
                raise _StopCycle from exc
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
            new_count = row["retry_count"] + 1
            if status in (400, 404, 410) or new_count >= MAX_RETRY:
                # Permanently rejected (or gone on Google's side) — reconcile
                # will re-align the two sides from Google's current state.
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
        elif rtask.get("title"):  # Google keeps blank placeholder tasks around; skip them
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

    # Archived rows included: otherwise an archived task whose Google copy
    # still exists looks brand new and gets re-inserted as a duplicate.
    local_rows = await (await db.execute(
        "SELECT * FROM tasks WHERE profile_id = ?", (profile["id"],)
    )).fetchall()
    local_by_gid = {r["google_task_id"]: r for r in local_rows if r["google_task_id"]}

    for gid, rtask in remote_by_id.items():
        remote_completed = rtask.get("status") == "completed"
        remote_title = rtask.get("title") or "(untitled)"
        remote_updated = _parse_google_ts(rtask["updated"])
        completed_at = _now_iso() if remote_completed else None

        local = local_by_gid.get(gid)
        if local is not None and local["archived"]:
            # Removed here but still on Google (its delete push was dropped):
            # re-queue the delete rather than resurrecting it.
            payload = json.dumps({"task_id": local["id"]})
            already = await (await db.execute(
                "SELECT 1 FROM sync_queue WHERE service = 'tasks' AND payload_json = ?", (payload,)
            )).fetchone()
            if not already:
                await _queue(db, "tasks", {"task_id": local["id"]})
        elif local is not None:
            if remote_updated > _parse_local_ts(local["updated_at"]):
                await db.execute(
                    """UPDATE tasks SET title = ?, is_completed = ?, completed_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (remote_title, int(remote_completed), completed_at, _to_local_ts(remote_updated), local["id"]),
                )
        elif rtask.get("title"):
            await db.execute(
                """INSERT INTO tasks (profile_id, google_task_id, title, is_completed, completed_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (profile["id"], gid, remote_title, int(remote_completed), completed_at, _to_local_ts(remote_updated)),
            )

    for gid, local in local_by_gid.items():
        if gid not in remote_by_id and not local["archived"]:
            await db.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (local["id"],))

    await db.commit()


# --- Orchestration ---------------------------------------------------------

async def run_sync(db):
    try:
        access_token = await google_oauth.get_valid_access_token(db)
        if not access_token:
            return

        await push_pending_changes(db, access_token)

        await reconcile_shopping(db, access_token)
        profiles = await (await db.execute(
            "SELECT * FROM profiles WHERE google_tasklist_id IS NOT NULL"
        )).fetchall()
        for profile in profiles:
            await reconcile_profile_tasks(db, access_token, profile)
    except (_StopCycle, httpx.HTTPError):
        # Offline, rate-limited, or the token lacks the `tasks` scope (needs
        # a reconnect) — skip this cycle quietly; the next one retries.
        pass
