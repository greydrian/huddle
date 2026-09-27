"""
Family member task lists: recurring + one-off tasks, ticked via touch,
with a checkmark animation. Purely functional — no points/rewards
(Section 4.3 of the spec).

Toggling a task writes to SQLite immediately (optimistic, offline-first)
and drops a row in sync_queue; app/task_sync.py pushes it to the family
member's linked Google Tasks list on its next scheduled cycle.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.database import family_today, get_db, get_setting, set_setting
from app.templating import templates

router = APIRouter()


async def get_profiles_with_tasks(db):
    """Return profiles, each with their non-archived tasks attached."""
    profiles = [dict(row) for row in await (await db.execute(
        "SELECT * FROM profiles ORDER BY sort_order"
    )).fetchall()]

    tasks = [dict(row) for row in await (await db.execute(
        "SELECT * FROM tasks WHERE archived = 0 ORDER BY is_completed, created_at"
    )).fetchall()]

    for profile in profiles:
        profile["tasks"] = [t for t in tasks if t["profile_id"] == profile["id"]]

    return profiles


def _queue_sync(db, service: str, payload: dict):
    """Fire-and-forget: queue a mutation for background push to Google."""
    return db.execute(
        "INSERT INTO sync_queue (service, payload_json) VALUES (?, ?)",
        (service, json.dumps(payload)),
    )


@router.get("/widgets/tasks", response_class=HTMLResponse)
async def tasks_widget(request: Request):
    async with get_db() as db:
        profiles = await get_profiles_with_tasks(db)
    return templates.TemplateResponse(
        request, "widgets/tasks.html", {"profiles": profiles}
    )


@router.post("/api/tasks/{task_id}/toggle", response_class=HTMLResponse)
async def toggle_task(request: Request, task_id: int):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        task = await cursor.fetchone()
        if task is None:
            return HTMLResponse(status_code=404, content="Task not found")

        now_completed = not bool(task["is_completed"])
        completed_at = datetime.now(timezone.utc).isoformat() if now_completed else None

        await db.execute(
            "UPDATE tasks SET is_completed = ?, completed_at = ?, updated_at = datetime('now') WHERE id = ?",
            (int(now_completed), completed_at, task_id),
        )

        await _queue_sync(db, "tasks", {"task_id": task_id, "is_completed": now_completed})
        await db.commit()

        profiles = await get_profiles_with_tasks(db)

    return templates.TemplateResponse(
        request,
        "widgets/tasks.html",
        {"profiles": profiles, "just_completed_id": task_id if now_completed else None},
    )


async def _update_and_queue(db, where: str, set_clause: str):
    """Apply a bulk change and queue each affected task for Google sync —
    a bare UPDATE would leave Google out of step until someone touched each
    task again (and last-write-wins could then undo the reset)."""
    rows = await (await db.execute(f"SELECT id FROM tasks WHERE {where}")).fetchall()
    for row in rows:
        await db.execute(
            f"UPDATE tasks SET {set_clause}, updated_at = datetime('now') WHERE id = ?", (row["id"],)
        )
        await _queue_sync(db, "tasks", {"task_id": row["id"]})
    return len(rows)


async def archive_completed_one_off_tasks(db) -> int:
    """End-of-day cleanup (spec 4.3): ticked one-off tasks are removed from
    view — archived locally, deleted from Google on the next sync."""
    return await _update_and_queue(
        db, "is_recurring = 0 AND is_completed = 1 AND archived = 0", "archived = 1"
    )


async def reset_recurring_tasks(db) -> int:
    """Start of a new day: untick every recurring task."""
    return await _update_and_queue(
        db, "is_recurring = 1 AND is_completed = 1 AND archived = 0", "is_completed = 0, completed_at = NULL"
    )


async def run_daily_reset_if_due(db) -> bool:
    """Runs the end-of-day reset once per family-local calendar day.

    Called often by the scheduler rather than as a midnight cron job, so a
    reset missed while the server was off still happens on the next check,
    and "midnight" follows the household's timezone, not the container's
    UTC. The very first check only records today — resetting then would
    wipe ticks made earlier on the day the feature was switched on."""
    today = (await family_today(db)).isoformat()
    last = await get_setting(db, "last_daily_reset")
    if last == today:
        return False
    if last is not None:
        await archive_completed_one_off_tasks(db)
        await reset_recurring_tasks(db)
    await set_setting(db, "last_daily_reset", today)
    await db.commit()
    return last is not None
