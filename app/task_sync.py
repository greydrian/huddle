"""
Bidirectional Google Tasks sync for the shopping list and per-person task
lists. Runs on a schedule (app/scheduler.py) — never inline with a request,
so a slow/unreachable Google API never blocks the dashboard.

Two halves each cycle:
- push_pending_changes: drains sync_queue (rows written via queue_sync()
  on every local mutation — the "optimistic, offline-
  first" part) and pushes them to Google.
- reconcile_*: pulls each configured list's current state from Google and
  reconciles it against the local rows — this is what picks up changes
  made from the phone. Conflicts are last-write-wins by timestamp.

Reconcile only runs after the queue has fully drained: reconciling while
local changes are still pending would let Google's stale view overwrite
them (e.g. resurrect an item deleted offline).
"""

import asyncio
import json
import logging
from datetime import date, datetime, timezone

import httpx

from app import google_oauth, google_tasks, http_client, sync_status
from app.database import family_today, get_setting, set_setting

SHOPPING_TASKLIST_SETTING = "google_shopping_tasklist"
MAX_RETRY = 5
SYNC_OUTAGE_KEY = "Google Tasks sync"
SYNC_ATTENTION_KEY = "Google Tasks sync access"
SYNC_ERROR_KEY = "Google Tasks sync (unexpected error)"

logger = logging.getLogger(__name__)


class _StopCycle(Exception):
    """Google is unreachable/rate-limiting/unauthorised — stop this cycle
    and leave the queue untouched so nothing is lost; retry next cycle."""


def _parse_local_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _parse_google_ts(s: str) -> datetime:
    # Truncated to seconds to match SQLite's datetime('now') precision —
    # otherwise Google's milliseconds make every row look newer every cycle.
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0)


def _google_due(rtask: dict) -> str | None:
    """Google's `due` as an ISO date. It carries no time ("...T00:00:00.000Z"
    is the date itself), so the date part is read as-is, not converted."""
    due = rtask.get("due")
    if not isinstance(due, str) or len(due) < 10:
        return None
    try:
        return date.fromisoformat(due[:10]).isoformat()
    except ValueError:
        return None


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
    except ValueError, TypeError:
        return None


async def set_shopping_tasklist(db, tasklist: dict):
    await set_setting(db, SHOPPING_TASKLIST_SETTING, json.dumps(tasklist))
    await db.commit()


async def queue_sync(db, service: str, payload: dict) -> None:
    """Queue a local mutation for the next background push to Google.

    `service` is "shopping" or "tasks"; the payload identifies the row by id
    ("item_id" / "task_id"). A hard-deleted shopping item's payload must also
    carry {"action": "delete", "google_task_id": ...}, since its row is gone
    by the time the queue drains. The caller commits."""
    await db.execute("INSERT INTO sync_queue (service, payload_json) VALUES (?, ?)", (service, json.dumps(payload)))


async def relink_shopping(db):
    """The shopping list was linked to a (different) Google list: forget old
    Google ids and queue every item so it's pushed to the new list, instead
    of reconcile treating them as "deleted on Google" and wiping them."""
    await db.execute("UPDATE shopping_items SET google_task_id = NULL")
    for row in await (await db.execute("SELECT id FROM shopping_items")).fetchall():
        await queue_sync(db, "shopping", {"item_id": row["id"]})
    await db.commit()


async def relink_profile(db, profile_id: int):
    """Same as relink_shopping, for one family member's task list."""
    await db.execute("UPDATE tasks SET google_task_id = NULL WHERE profile_id = ?", (profile_id,))
    rows = await (
        await db.execute("SELECT id FROM tasks WHERE profile_id = ? AND archived = 0", (profile_id,))
    ).fetchall()
    for row in rows:
        await queue_sync(db, "tasks", {"task_id": row["id"]})
    await db.commit()


async def detach_from_profile_list(db, task_id: int):
    """A task is moving to another family member: its Google copy lives on
    the old member's list. Leave an archived tombstone row there holding the
    old Google id, so the normal archived-task path deletes that copy (and
    reconcile re-queues rather than resurrects it if the delete fails), then
    forget the id so the task's next push inserts it into the new member's
    list. Call before changing profile_id; the caller queues the task and commits."""
    task = await (await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))).fetchone()
    if task is None or not task["google_task_id"]:
        return
    cursor = await db.execute(
        """INSERT INTO tasks (profile_id, title, google_task_id, archived, updated_at)
           VALUES (?, ?, ?, 1, datetime('now'))""",
        (task["profile_id"], task["title"], task["google_task_id"]),
    )
    await queue_sync(db, "tasks", {"task_id": cursor.lastrowid})
    await db.execute("UPDATE tasks SET google_task_id = NULL WHERE id = ?", (task_id,))


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
        try:
            await google_tasks.update_task(
                access_token,
                tasklist["id"],
                item["google_task_id"],
                title=item["title"],
                completed=bool(item["is_checked"]),
            )
            return
        except httpx.HTTPStatusError as exc:
            # "revive": something was just added on the wall into this row
            # (shopping.add_item), so if the family deleted the task in Google
            # Tasks meanwhile, the new item must not vanish with it: it goes
            # back as a new task. Any other change to a deleted task is
            # dropped, and reconcile removes the row.
            if not (payload.get("revive") and exc.response.status_code in (404, 410)):
                raise
        remote = await google_tasks.insert_task(
            access_token, tasklist["id"], item["title"], completed=bool(item["is_checked"])
        )
        await db.execute("UPDATE shopping_items SET google_task_id = ? WHERE id = ?", (remote["id"], item_id))
    else:
        remote = await google_tasks.insert_task(
            access_token,
            tasklist["id"],
            item["title"],
            completed=bool(item["is_checked"]),
        )
        await db.execute("UPDATE shopping_items SET google_task_id = ? WHERE id = ?", (remote["id"], item_id))


async def _push_task_change(db, access_token: str, payload: dict):
    task_id = payload.get("task_id")
    task = await (await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))).fetchone()
    if task is None:
        return

    profile = await (
        await db.execute("SELECT google_tasklist_id FROM profiles WHERE id = ?", (task["profile_id"],))
    ).fetchone()
    tasklist_id = profile["google_tasklist_id"] if profile else None
    if not tasklist_id:
        return  # not linked — relink_profile backfills on link

    if task["archived"]:
        if task["google_task_id"]:
            await google_tasks.delete_task(access_token, tasklist_id, task["google_task_id"])
        return

    if task["google_task_id"]:
        await google_tasks.update_task(
            access_token,
            tasklist_id,
            task["google_task_id"],
            title=task["title"],
            completed=bool(task["is_completed"]),
        )
    else:
        remote = await google_tasks.insert_task(
            access_token,
            tasklist_id,
            task["title"],
            completed=bool(task["is_completed"]),
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
                logger.warning(
                    "Dropping queued %s sync change %s (%s): %s",
                    row["service"],
                    row["id"],
                    "rejected" if status in (400, 404, 410) else "out of retries",
                    http_client.describe(exc),
                )
                await db.execute("DELETE FROM sync_queue WHERE id = ?", (row["id"],))
            else:
                await db.execute("UPDATE sync_queue SET retry_count = ? WHERE id = ?", (new_count, row["id"]))
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
    local_rows = await (await db.execute("SELECT * FROM tasks WHERE profile_id = ?", (profile["id"],))).fetchall()
    local_by_gid = {r["google_task_id"]: r for r in local_rows if r["google_task_id"]}
    today = (await family_today(db)).isoformat()

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
            already = await (
                await db.execute("SELECT 1 FROM sync_queue WHERE service = 'tasks' AND payload_json = ?", (payload,))
            ).fetchone()
            if not already:
                await queue_sync(db, "tasks", {"task_id": local["id"]})
        elif local is not None:
            if remote_updated > _parse_local_ts(local["updated_at"]):
                # A due date changed on the phone moves the carry-over date too.
                await db.execute(
                    """UPDATE tasks SET title = ?, is_completed = ?, completed_at = ?, updated_at = ?,
                           due_on = COALESCE(?, due_on)
                       WHERE id = ?""",
                    (
                        remote_title,
                        int(remote_completed),
                        completed_at,
                        _to_local_ts(remote_updated),
                        _google_due(rtask),
                        local["id"],
                    ),
                )
        elif rtask.get("title"):
            # due_on (local, for carry-over labels) is the day it arrived here,
            # or Google's own due date if that's later: linking a list with old
            # past-due tasks mustn't flood the wall with "late" labels.
            google_due = _google_due(rtask)
            await db.execute(
                """INSERT INTO tasks (profile_id, google_task_id, title, is_completed, completed_at, updated_at,
                                      due_on)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile["id"],
                    gid,
                    remote_title,
                    int(remote_completed),
                    completed_at,
                    _to_local_ts(remote_updated),
                    max(google_due, today) if google_due else today,
                ),
            )

    for gid, local in local_by_gid.items():
        if gid not in remote_by_id and not local["archived"]:
            await db.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (local["id"],))

    await db.commit()


# --- Orchestration ---------------------------------------------------------

# Held for a whole cycle. The scheduler's max_instances=1 only stops two
# scheduled runs overlapping; Admin's "Sync now" is a second caller, and two
# cycles pushing the same queue rows would create duplicate Google tasks.
_sync_lock = asyncio.Lock()


def sync_in_progress() -> bool:
    return _sync_lock.locked()


async def run_sync(db) -> bool:
    """One sync cycle. False (and nothing done) when a cycle is already
    running — that cycle covers whatever this one would have done."""
    if _sync_lock.locked():
        return False
    async with _sync_lock:
        await _run_cycle(db)
    return True


async def _record_status(record, db, *args) -> bool:
    """Writes the cycle's sync_status row. The sync work itself is already
    committed, so a failure here (e.g. "database is locked") must not turn
    the cycle into an error: a WARNING, and the next cycle writes it again."""
    try:
        return bool(await record(db, *args))
    except Exception as exc:
        logger.warning("Couldn't save the sync status: %s", type(exc).__name__)
        return False


async def _rollback(db):
    """Nothing half-done from a failed step is kept."""
    try:
        await db.rollback()
    except Exception as exc:
        logger.warning("Rollback after a failed sync cycle failed: %s", type(exc).__name__)


async def _run_cycle(db):
    """Never raises (short of cancellation): the scheduler job and Admin's
    "Sync now" both just want the cycle done and its outcome recorded."""
    try:
        access_token = await google_oauth.get_valid_access_token(db)
        if access_token:
            await push_pending_changes(db, access_token)

            await reconcile_shopping(db, access_token)
            profiles = await (
                await db.execute("SELECT id, google_tasklist_id FROM profiles WHERE google_tasklist_id IS NOT NULL")
            ).fetchall()
            for profile in profiles:
                await reconcile_profile_tasks(db, access_token, profile)
    except (_StopCycle, httpx.HTTPError) as exc:
        # Offline, rate-limited, or the token lacks the `tasks` scope (needs
        # a reconnect) — skip this cycle; the next one retries. Logged once
        # per outage, not once a minute.
        cause = exc.__cause__ if isinstance(exc, _StopCycle) and exc.__cause__ else exc
        http_client.report_failure(
            logger,
            SYNC_OUTAGE_KEY,
            "Google Tasks sync failing; retrying every cycle: %s",
            http_client.describe(cause),
        )
        await _rollback(db)
        if await _record_status(sync_status.record_failure, db, cause):
            # The same 401/403 for ATTENTION_AFTER cycles: not a blip any
            # more. Still never drops queue rows — they push once reconnected.
            http_client.report_failure(
                logger,
                SYNC_ATTENTION_KEY,
                "Google Tasks sync needs attention: Google refused access (%s) for %d cycles "
                "in a row; reconnect in Admin",
                http_client.describe(cause),
                sync_status.ATTENTION_AFTER,
            )
    except Exception as exc:
        # Not Google: a bug (e.g. a KeyError on an odd task) or SQLite
        # ("database is locked"). Swallowed rather than raised, so neither the
        # scheduler nor Sync now errors out, but recorded as "error" (never
        # towards needs-attention) so the dot turns amber if it persists.
        # Traceback logged once per outage; it's not an httpx error, so it
        # carries no request URL.
        http_client.report_failure(
            logger,
            SYNC_ERROR_KEY,
            "Google Tasks sync failing with an unexpected error: %s",
            type(exc).__name__,
            exc_info=exc,
        )
        await _rollback(db)
        await _record_status(sync_status.record_failure, db, exc)
    else:
        if not access_token:
            await _record_status(sync_status.record_not_connected, db)
            return
        http_client.report_success(logger, SYNC_OUTAGE_KEY)
        http_client.report_success(logger, SYNC_ATTENTION_KEY)
        http_client.report_success(logger, SYNC_ERROR_KEY)
        await _record_status(sync_status.record_success, db)
