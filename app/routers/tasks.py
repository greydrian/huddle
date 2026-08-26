"""
Family member task lists: recurring + one-off tasks, ticked via touch,
with a checkmark animation. Purely functional — no points/rewards
(Section 4.3 of the spec).

Google Tasks sync isn't wired up yet — toggling a task writes to SQLite
immediately (the "optimistic, offline-first" part) and drops a row in
sync_queue for a background worker to push later. The background worker
itself is a follow-up step once Google OAuth is wired in.
"""

import json
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
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
        completed_at = datetime.utcnow().isoformat() if now_completed else None

        await db.execute(
            "UPDATE tasks SET is_completed = ?, completed_at = ? WHERE id = ?",
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


async def archive_completed_one_off_tasks(db):
    """
    End-of-day cleanup: one-off tasks that were ticked get archived (removed
    from view) at midnight; recurring tasks are reset separately. Intended to
    be called from a scheduled job (see deploy/scheduler.md).
    """
    await db.execute(
        """UPDATE tasks SET archived = 1
           WHERE is_recurring = 0 AND is_completed = 1"""
    )
    await db.commit()


async def reset_recurring_tasks(db):
    """Uncheck recurring tasks at the start of a new day."""
    await db.execute(
        "UPDATE tasks SET is_completed = 0, completed_at = NULL WHERE is_recurring = 1"
    )
    await db.commit()
