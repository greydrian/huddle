"""
Family member tasks (Section 4.3): today's tasks per profile, ticking, and
the end-of-day reset. Purely functional — no points/rewards.

Every mutation writes SQLite immediately (optimistic, offline-first) and
queues a sync_queue row; app/task_sync.py pushes it to the family member's
linked Google Tasks list on its next scheduled cycle.
"""

from datetime import datetime, timezone

from app import recurrence
from app.database import family_today, get_setting, set_setting
from app.task_sync import queue_sync


async def get_profiles_with_tasks(db) -> list[dict]:
    """Return profiles, each with today's non-archived tasks attached — a
    recurring task set to specific days only shows on those days (in the
    household's timezone, not the container's)."""
    profiles = [dict(row) for row in await (await db.execute(
        "SELECT * FROM profiles ORDER BY sort_order"
    )).fetchall()]

    weekday = (await family_today(db)).weekday()
    tasks = [
        dict(row) for row in await (await db.execute(
            "SELECT * FROM tasks WHERE archived = 0 ORDER BY is_completed, created_at"
        )).fetchall()
        if not row["is_recurring"] or recurrence.is_due(row["recurrence_rule"], weekday)
    ]

    for profile in profiles:
        profile["tasks"] = [t for t in tasks if t["profile_id"] == profile["id"]]

    return profiles


async def get_admin_tasks(db) -> list[dict]:
    """Every live task for Admin's schedule panel, with a readable schedule
    and the day chips to pre-tick in the edit form (empty = every day, or
    a one-off)."""
    tasks = [dict(r) for r in await (await db.execute(
        "SELECT tasks.* FROM tasks "
        "JOIN profiles ON profiles.id = tasks.profile_id "
        "WHERE archived = 0 ORDER BY profiles.sort_order, tasks.created_at"
    )).fetchall()]
    for task in tasks:
        task["schedule"] = recurrence.describe(task["recurrence_rule"]) if task["is_recurring"] else None
        days = recurrence.parse_rule(task["recurrence_rule"]) if task["is_recurring"] else None
        task["days"] = [recurrence.WEEKDAYS[i] for i in sorted(days)] if days else []
    return tasks


async def toggle_task(db, task_id: int) -> bool | None:
    """Tick or untick a task and queue it for sync. Returns the new
    completed state, or None if there's no such task."""
    cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    task = await cursor.fetchone()
    if task is None:
        return None

    now_completed = not bool(task["is_completed"])
    completed_at = datetime.now(timezone.utc).isoformat() if now_completed else None

    await db.execute(
        "UPDATE tasks SET is_completed = ?, completed_at = ?, updated_at = datetime('now') WHERE id = ?",
        (int(now_completed), completed_at, task_id),
    )

    await queue_sync(db, "tasks", {"task_id": task_id, "is_completed": now_completed})
    await db.commit()
    return now_completed


async def _update_and_queue(db, where: str, set_clause: str) -> int:
    """Apply a bulk change and queue each affected task for Google sync —
    a bare UPDATE would leave Google out of step until someone touched each
    task again (and last-write-wins could then undo the reset)."""
    rows = await (await db.execute(f"SELECT id FROM tasks WHERE {where}")).fetchall()
    for row in rows:
        await db.execute(
            f"UPDATE tasks SET {set_clause}, updated_at = datetime('now') WHERE id = ?", (row["id"],)
        )
        await queue_sync(db, "tasks", {"task_id": row["id"]})
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
