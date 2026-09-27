from datetime import date

import pytest

from app import recurrence
from app.routers import admin, tasks
from app.security import create_session_token

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


@pytest.mark.parametrize(("rule", "expected"), [
    (None, None),
    ("", None),
    ("Mon,Wed,Fri", {MON, WED, FRI}),
    ("mon wed", {MON, WED}),                 # older free-text input
    ("Monday, Friday", {MON, FRI}),
    ("weekdays", {MON, TUE, WED, THU, FRI}),
    ("weekends", {SAT, SUN}),
    ("daily", None),
    ("Mon,Tue,Wed,Thu,Fri,Sat,Sun", None),   # all seven == every day
    ("gibberish", None),                     # never hide a task over a typo
])
def test_parse_rule(rule, expected):
    assert recurrence.parse_rule(rule) == expected


def test_normalize_orders_and_dedupes():
    assert recurrence.normalize_rule(["Fri", "Mon", "Fri"]) == "Mon,Fri"
    assert recurrence.normalize_rule([]) is None


def test_describe():
    assert recurrence.describe(None) == "every day"
    assert recurrence.describe("Mon,Tue,Wed,Thu,Fri") == "weekdays"
    assert recurrence.describe("Mon,Wed") == "Mon, Wed"


async def test_dashboard_only_shows_recurring_tasks_on_their_days(db, monkeypatch):
    await db.executemany(
        "INSERT INTO tasks (profile_id, title, is_recurring, recurrence_rule) VALUES (1, ?, ?, ?)",
        [("Bins", 1, "Mon"), ("Swimming", 1, "Tue,Thu"), ("Brush teeth", 1, None), ("Call dentist", 0, None)],
    )
    await db.commit()

    async def fake_today(_db):
        return date(2026, 9, 28)  # a Monday
    monkeypatch.setattr(tasks, "family_today", fake_today)

    profiles = await tasks.get_profiles_with_tasks(db)

    shown = {t["title"] for p in profiles for t in p["tasks"]}
    assert shown == {"Bins", "Brush teeth", "Call dentist"}


async def test_admin_day_checkboxes_create_a_recurring_task(db, client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())

    resp = await client.post(
        "/admin/tasks", data={"profile_id": "1", "title": "Piano", "days": ["Wed", "Mon"]}
    )

    assert resp.status_code == 303
    row = await (await db.execute("SELECT is_recurring, recurrence_rule FROM tasks WHERE title = 'Piano'")).fetchone()
    assert (row["is_recurring"], row["recurrence_rule"]) == (1, "Mon,Wed")


async def test_admin_one_off_task_stores_no_rule(db, client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())

    await client.post("/admin/tasks", data={"profile_id": "1", "title": "Post letter"})

    row = await (await db.execute("SELECT is_recurring, recurrence_rule FROM tasks WHERE title = 'Post letter'")).fetchone()
    assert (row["is_recurring"], row["recurrence_rule"]) == (0, None)
