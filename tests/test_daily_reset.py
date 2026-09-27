from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import database
from app.services import tasks


async def _seed(db):
    await db.executemany(
        "INSERT INTO tasks (profile_id, title, is_recurring, is_completed) VALUES (1, ?, ?, ?)",
        [("Brush teeth", 1, 1), ("Return library book", 0, 1), ("Tidy room", 0, 0)],
    )
    await db.commit()


async def _state(db):
    rows = await (await db.execute("SELECT title, is_completed, archived FROM tasks ORDER BY title")).fetchall()
    return {r["title"]: (r["is_completed"], r["archived"]) for r in rows}


async def test_first_check_only_records_the_day(db):
    await _seed(db)

    assert await tasks.run_daily_reset_if_due(db) is False

    assert (await _state(db))["Brush teeth"] == (1, 0), "must not wipe today's ticks on first run"
    assert await database.get_setting(db, "last_daily_reset") is not None


async def test_same_day_is_a_no_op(db):
    await _seed(db)
    await tasks.run_daily_reset_if_due(db)

    assert await tasks.run_daily_reset_if_due(db) is False
    assert (await _state(db))["Return library book"] == (1, 0)


async def test_new_day_resets_recurring_archives_done_one_offs_and_queues_sync(db):
    await _seed(db)
    await database.set_setting(db, "last_daily_reset", date(2000, 1, 1).isoformat())
    await db.commit()

    assert await tasks.run_daily_reset_if_due(db) is True

    state = await _state(db)
    assert state["Brush teeth"] == (0, 0)           # recurring: unticked, still visible
    assert state["Return library book"] == (1, 1)   # finished one-off: archived
    assert state["Tidy room"] == (0, 0)             # unfinished one-off: untouched
    queued = await (await db.execute("SELECT COUNT(*) FROM sync_queue WHERE service = 'tasks'")).fetchone()
    assert queued[0] == 2


async def test_day_boundary_follows_the_family_timezone(db):
    # Kiritimati is UTC+14 — its "today" is often a different date from UTC's.
    await database.set_setting(db, "calendar_timezone", "Pacific/Kiritimati")
    await db.commit()

    assert await database.family_today(db) == datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
