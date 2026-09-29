"""Spec 10.4 tasks: the local-only marker, the kiosk quick add, time-of-day
groups, carry-over of missed one-offs, the school-days repeat option, and
that none of the local fields ever reach Google."""

import asyncio
import json
import re
import sqlite3
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app import database, migrations, recurrence, task_sync
from app.routers import admin
from app.security import create_session_token
from app.services import tasks, term_dates

TASKS_API = "https://tasks.googleapis.com/tasks/v1/lists"
LONDON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    tasks.reset_rate_limit()
    yield
    tasks.reset_rate_limit()


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


def freeze(monkeypatch, day: date, at: time = time(9, 0)):
    """The family's date and wall clock."""
    async def fake_today(_db):
        return day
    monkeypatch.setattr(tasks, "family_today", fake_today)
    monkeypatch.setattr(tasks, "_clock", lambda tz: datetime.combine(day, at, tzinfo=tz))


async def _pid(db, n=0):
    rows = await (await db.execute("SELECT id FROM profiles ORDER BY sort_order")).fetchall()
    return rows[n]["id"]


async def _link(db, pid, list_id="kids"):
    await db.execute("UPDATE profiles SET google_tasklist_id = ? WHERE id = ?", (list_id, pid))
    await db.commit()


async def _add(db, pid, title, **cols):
    cols = {"profile_id": pid, "title": title, "updated_at": "2020-01-01 00:00:00", **cols}
    cur = await db.execute(
        f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values())
    )
    await db.commit()
    return cur.lastrowid


async def _queue(db):
    rows = await (await db.execute("SELECT payload_json FROM sync_queue ORDER BY id")).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def _row(html, title):
    return re.search(r'<div class="task-row[^"]*">(?:(?!<div class="task-row).)*?' + re.escape(title) + r".*?</div>",
                     html, re.S).group(0)


def _add_form_tag(html):
    return re.search(r'<form id="task-add-form"[^>]*>', html).group(0)


def _sections(html):
    """[(data-group, is-current)] in page order."""
    return [(m.group(2), "is-current" in m.group(1))
            for m in re.finditer(r'<section class="task-section([^"]*)"\s+data-group="([^"]+)"', html)]


# --- 1. Local-only marker ---

async def test_local_only_marker_only_on_unlinked_people(db, client):
    linked, local = await _pid(db, 0), await _pid(db, 1)
    await _link(db, linked)
    await _add(db, linked, "Synced chore")
    await _add(db, local, "Wall chore")

    html = (await client.get("/widgets/tasks")).text

    assert 'aria-label="On this display only"' in _row(html, "Wall chore")
    assert "On this display only" not in _row(html, "Synced chore")


# --- 2. Quick add ---

async def test_quick_add_for_a_linked_person_queues_one_sync(db, client):
    pid = await _pid(db)
    await _link(db, pid)

    resp = await client.post("/api/tasks", data={"profile_id": pid, "title": "  Water plants ", "time_of_day": ""})

    assert resp.status_code == 200 and "Water plants" in resp.text
    row = await (await db.execute("SELECT * FROM tasks WHERE title = 'Water plants'")).fetchone()
    assert (row["is_recurring"], row["time_of_day"], row["archived"]) == (0, None, 0)
    assert row["due_on"] == (await database.family_today(db)).isoformat()
    assert await _queue(db) == [{"task_id": row["id"]}]
    assert "On this display only" not in _row(resp.text, "Water plants")


async def test_quick_add_for_an_unlinked_person_stays_local(db, client):
    pid = await _pid(db, 2)

    resp = await client.post("/api/tasks", data={"profile_id": pid, "title": "Feed fish", "time_of_day": "evening"})

    row = await (await db.execute("SELECT * FROM tasks WHERE title = 'Feed fish'")).fetchone()
    assert row["time_of_day"] == "evening"
    assert await _queue(db) == []
    assert 'aria-label="On this display only"' in _row(resp.text, "Feed fish")


@pytest.mark.parametrize(("data", "message"), [
    ({"title": ""}, "Give the task a name"),
    ({"title": "   "}, "Give the task a name"),
    ({"title": "x" * 201}, "Give the task a name"),
    ({"title": "Ok", "profile_id": "999"}, "Choose who the task is for"),
    ({"title": "Ok", "profile_id": "abc"}, "Choose who the task is for"),
    ({"title": "Ok", "profile_id": ""}, "Choose who the task is for"),
    ({"title": "Ok", "time_of_day": "midnight"}, "Choose a time of day"),
])
async def test_quick_add_validation(db, client, data, message):
    pid = await _pid(db)
    resp = await client.post("/api/tasks", data={"profile_id": pid, **data})

    assert resp.status_code == 200
    assert message in resp.text
    assert "hidden" not in _add_form_tag(resp.text)  # still open
    assert (await (await db.execute("SELECT COUNT(*) FROM tasks")).fetchone())[0] == 0


async def test_quick_add_title_of_200_is_fine(db, client):
    resp = await client.post("/api/tasks", data={"profile_id": await _pid(db), "title": "y" * 200})
    assert "task-add-error" not in resp.text
    assert (await (await db.execute("SELECT COUNT(*) FROM tasks")).fetchone())[0] == 1


async def test_quick_add_rate_limit(db, client, monkeypatch):
    pid = await _pid(db)
    clock = [1000.0]
    monkeypatch.setattr(tasks._time, "monotonic", lambda: clock[0])
    for n in range(tasks.QUICK_ADD_LIMIT):
        await client.post("/api/tasks", data={"profile_id": pid, "title": f"T{n}"})

    resp = await client.post("/api/tasks", data={"profile_id": pid, "title": "One too many"})
    assert "Try again in a little while" in resp.text
    assert "One too many" in resp.text  # kept in the form, not lost
    assert (await (await db.execute("SELECT COUNT(*) FROM tasks")).fetchone())[0] == tasks.QUICK_ADD_LIMIT

    clock[0] += tasks.QUICK_ADD_WINDOW
    resp = await client.post("/api/tasks", data={"profile_id": pid, "title": "Next hour"})
    assert "task-add-error" not in resp.text


async def test_quick_add_rate_limit_holds_under_concurrency(db, monkeypatch):
    pid = await _pid(db)
    monkeypatch.setattr(tasks._time, "monotonic", lambda: 1000.0)
    for n in range(tasks.QUICK_ADD_LIMIT - 1):
        await tasks.quick_add(db, pid, f"T{n}")

    async def add(n):
        async with database.get_db() as conn:
            try:
                return await tasks.quick_add(conn, pid, f"Burst {n}")
            except tasks.QuickAddError as exc:
                return exc.code

    results = await asyncio.gather(*(add(n) for n in range(10)))

    assert sum(isinstance(r, int) for r in results) == 1
    assert results.count("rate") == 9
    assert (await (await db.execute("SELECT COUNT(*) FROM tasks")).fetchone())[0] == tasks.QUICK_ADD_LIMIT


async def test_failed_insert_gives_the_rate_limit_slot_back(db, monkeypatch):
    pid = await _pid(db)

    async def broken_today(_db):
        raise RuntimeError("boom")
    monkeypatch.setattr(tasks, "family_today", broken_today)
    with pytest.raises(RuntimeError):
        await tasks.quick_add(db, pid, "Nope")
    assert len(tasks._recent_adds) == 0


async def test_quick_add_refuses_a_foreign_origin(db, client):
    resp = await client.post("/api/tasks", data={"profile_id": await _pid(db), "title": "X"},
                             headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert (await (await db.execute("SELECT COUNT(*) FROM tasks")).fetchone())[0] == 0


async def test_quick_added_task_syncs_exactly_once(db, connected, client, google):
    pid = await _pid(db)
    await _link(db, pid)
    insert = google.post(f"{TASKS_API}/kids/tasks").respond(200, json={"id": "g-plants"})
    google.get(f"{TASKS_API}/kids/tasks").respond(200, json={"items": [
        {"id": "g-plants", "title": "Water plants", "status": "needsAction", "updated": "2020-01-01T00:00:00Z"},
    ]})

    await client.post("/api/tasks", data={"profile_id": pid, "title": "Water plants", "time_of_day": "morning"})
    await task_sync.run_sync(db)
    await task_sync.run_sync(db)

    assert insert.call_count == 1
    assert set(json.loads(insert.calls.last.request.content)) == {"title", "status"}  # nothing local
    rows = await (await db.execute("SELECT google_task_id, time_of_day FROM tasks")).fetchall()
    assert [(r["google_task_id"], r["time_of_day"]) for r in rows] == [("g-plants", "morning")]
    assert await _queue(db) == []


# --- 3. Time-of-day groups ---

@pytest.mark.parametrize(("at", "group"), [
    (time(0, 0), "morning"), (time(11, 59), "morning"),
    (time(12, 0), "after_school"), (time(17, 59), "after_school"),
    (time(18, 0), "evening"), (time(23, 59), "evening"),
])
def test_group_boundaries_default(at, group):
    assert tasks.group_at(at, ("12:00", "18:00")) == group


async def _grouped(db):
    pid, other = await _pid(db, 0), await _pid(db, 1)
    await _add(db, pid, "Brush teeth", time_of_day="morning")
    await _add(db, pid, "Homework", time_of_day="after_school")
    await _add(db, other, "Bath", time_of_day="evening")
    await _add(db, other, "Tidy room")


@pytest.mark.parametrize(("at", "order"), [
    (time(7, 0), ["morning", "any", "after_school", "evening"]),
    (time(12, 0), ["after_school", "morning", "any", "evening"]),
    (time(18, 0), ["evening", "morning", "after_school", "any"]),
])
async def test_current_group_first_and_highlighted(db, client, monkeypatch, at, order):
    await _grouped(db)
    freeze(monkeypatch, date(2026, 9, 29), at)

    sections = _sections((await client.get("/widgets/tasks")).text)

    assert [key for key, _ in sections] == order
    assert [current for _, current in sections] == [True, False, False, False]


async def test_highlight_moves_at_the_boundary_via_the_rev_hash(db, client, monkeypatch):
    await _grouped(db)
    freeze(monkeypatch, date(2026, 9, 29), time(11, 59))
    before = (await client.get("/api/rev")).json()["widgets"]["tasks"]
    assert (await client.get("/api/rev")).json()["widgets"]["tasks"] == before  # stable within a group

    freeze(monkeypatch, date(2026, 9, 29), time(12, 0))
    after = (await client.get("/api/rev")).json()["widgets"]["tasks"]

    assert after != before
    assert _sections((await client.get("/widgets/tasks")).text)[0] == ("after_school", True)


@pytest.mark.parametrize(("utc", "group"), [
    # Last Sunday of October 2026: London is back on GMT (UTC+0) from 01:00 UTC.
    (datetime(2026, 10, 25, 11, 59, tzinfo=UTC), "morning"),
    (datetime(2026, 10, 25, 12, 0, tzinfo=UTC), "after_school"),
    (datetime(2026, 10, 25, 18, 0, tzinfo=UTC), "evening"),
    # Last Sunday of March 2026: London is on BST (UTC+1) from 01:00 UTC.
    (datetime(2026, 3, 29, 10, 59, tzinfo=UTC), "morning"),
    (datetime(2026, 3, 29, 11, 0, tzinfo=UTC), "after_school"),
    (datetime(2026, 3, 29, 16, 59, tzinfo=UTC), "after_school"),
    (datetime(2026, 3, 29, 17, 0, tzinfo=UTC), "evening"),
])
async def test_groups_follow_the_family_clock_on_dst_days(db, monkeypatch, utc, group):
    """The boundaries are family wall-clock times, whatever the UTC offset that day."""
    monkeypatch.setattr(tasks, "_clock", lambda tz: utc.astimezone(tz))
    assert await tasks.current_group(db) == group


async def test_rev_changes_only_through_the_group_component(db, client, monkeypatch):
    """No data changes across a boundary, yet /api/rev moves: the loader's
    current_group is the only thing that differs."""
    await _add(db, await _pid(db), "Feed cat")  # ungrouped: the rev still follows the clock
    freeze(monkeypatch, date(2026, 9, 29), time(17, 59))
    before_ctx = await tasks.widget_context(db)
    before = (await client.get("/api/rev")).json()["widgets"]
    freeze(monkeypatch, date(2026, 9, 29), time(18, 0))
    after_ctx = await tasks.widget_context(db)
    after = (await client.get("/api/rev")).json()["widgets"]

    assert (before_ctx["current_group"], after_ctx["current_group"]) == ("after_school", "evening")
    assert {k: v for k, v in before_ctx.items() if k != "current_group"} == \
        {k: v for k, v in after_ctx.items() if k != "current_group"}
    assert after["tasks"] != before["tasks"]
    assert {k: v for k, v in after.items() if k != "tasks"} == {k: v for k, v in before.items() if k != "tasks"}


async def test_earlier_groups_keep_unfinished_tasks(db, client, monkeypatch):
    pid = await _pid(db)
    await _add(db, pid, "Pack bag", time_of_day="morning")
    await _add(db, pid, "Make bed", time_of_day="morning", is_completed=1)
    freeze(monkeypatch, date(2026, 9, 29), time(19, 0))

    html = (await client.get("/widgets/tasks")).text
    morning = html.split('data-group="morning"')[1].split("</section>")[0]

    assert "earlier" in morning
    assert "Pack bag" in morning.split('<details class="task-done">')[0]
    assert "Make bed" in morning.split('<details class="task-done">')[1]  # folded, still undoable
    assert _sections(html)[0] == ("evening", True)
    assert "Nothing for now." in html.split('data-group="evening"')[1].split("</section>")[0]


async def test_no_groups_in_use_renders_the_plain_list(db, client):
    await _add(db, await _pid(db), "Feed cat")
    html = (await client.get("/widgets/tasks")).text
    assert "task-section" not in html
    assert '<div class="task-group">' in html


async def test_custom_boundaries_are_used(db, client, monkeypatch):
    await _grouped(db)
    await tasks.set_group_boundaries(db, "15:30", "19:00")
    freeze(monkeypatch, date(2026, 9, 29), time(15, 0))
    assert _sections((await client.get("/widgets/tasks")).text)[0] == ("morning", True)
    freeze(monkeypatch, date(2026, 9, 29), time(18, 30))
    assert _sections((await client.get("/widgets/tasks")).text)[0] == ("after_school", True)


@pytest.mark.parametrize(("after_school", "evening"), [
    ("18:00", "12:00"), ("12:00", "12:00"), ("00:00", "18:00"), ("noon", "18:00"), ("12:00", "25:00"), ("", ""),
])
async def test_admin_group_times_are_validated(db, admin_client, after_school, evening):
    resp = await admin_client.post("/admin/tasks/groups",
                                   data={"after_school_start": after_school, "evening_start": evening})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin?tab=family&error=task-group-times#tasks"
    assert await tasks.get_group_boundaries(db) == ("12:00", "18:00")


async def test_admin_group_times_save(db, admin_client):
    resp = await admin_client.post("/admin/tasks/groups",
                                   data={"after_school_start": "15:15", "evening_start": "18:30"})
    assert resp.headers["location"] == "/admin?tab=family#tasks"
    assert await tasks.get_group_boundaries(db) == ("15:15", "18:30")
    page = (await admin_client.get("/admin?tab=family")).text
    assert 'name="after_school_start" value="15:15"' in page


async def test_admin_sets_a_group_locally_without_a_push(db, admin_client):
    pid = await _pid(db)
    await _link(db, pid)
    task_id = await _add(db, pid, "Piano", google_task_id="g-piano")

    resp = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": pid, "time_of_day": "evening"})

    assert resp.status_code == 303
    row = await (await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))).fetchone()
    assert row["time_of_day"] == "evening"
    assert row["updated_at"] == "2020-01-01 00:00:00"
    assert await _queue(db) == []
    # A form without the field (older page) leaves it alone; "any" clears it.
    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": pid})
    assert (await (await db.execute("SELECT time_of_day FROM tasks")).fetchone())[0] == "evening"
    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": pid, "time_of_day": "any"})
    assert (await (await db.execute("SELECT time_of_day FROM tasks")).fetchone())[0] is None


async def test_admin_rejects_an_unknown_group(db, admin_client):
    pid = await _pid(db)
    task_id = await _add(db, pid, "Piano")
    resp = await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": pid, "time_of_day": "noon"})
    assert resp.headers["location"] == "/admin?tab=family&error=task-group#tasks"


# --- 4. Carry-over ---

@pytest.mark.parametrize(("due", "label"), [
    ("2026-09-29", None), ("2026-09-30", None), (None, None),
    ("2026-09-28", "from yesterday"), ("2026-09-26", "from Sat"), ("2026-09-23", "from Wed"),
    ("2026-09-22", "from 22 Sep"),
])
def test_late_labels(due, label):
    assert tasks.late_label(due, date(2026, 9, 29)) == label


async def test_missed_one_off_carries_over_across_midnight_and_the_reset(db, connected, client, google, monkeypatch):
    pid = await _pid(db)
    await _link(db, pid)
    insert = google.post(f"{TASKS_API}/kids/tasks").respond(200, json={"id": "g-letter"})
    google.get(f"{TASKS_API}/kids/tasks").respond(200, json={"items": [
        {"id": "g-letter", "title": "Sign letter", "status": "needsAction", "updated": "2020-01-01T00:00:00Z"},
    ]})
    freeze(monkeypatch, date(2026, 9, 28))
    await client.post("/api/tasks", data={"profile_id": pid, "title": "Sign letter"})
    await task_sync.run_sync(db)
    assert "task-late" not in _row((await client.get("/widgets/tasks")).text, "Sign letter")

    # Midnight: the daily reset runs, and the unfinished one-off stays.
    await database.set_setting(db, "last_daily_reset", "2026-09-28")
    await db.commit()
    freeze(monkeypatch, date(2026, 9, 29))
    assert await tasks.run_daily_reset_if_due(db) is True
    await task_sync.run_sync(db)

    assert "from yesterday" in _row((await client.get("/widgets/tasks")).text, "Sign letter")
    freeze(monkeypatch, date(2026, 10, 1))
    assert "from Mon" in _row((await client.get("/widgets/tasks")).text, "Sign letter")

    # Ticked: no label, and the next reset archives it (a Google delete, not a copy).
    task_id = (await (await db.execute("SELECT id FROM tasks")).fetchone())[0]
    html = (await client.post(f"/api/tasks/{task_id}/toggle")).text
    assert "task-late" not in _row(html, "Sign letter")
    assert insert.call_count == 1
    rows = await (await db.execute("SELECT google_task_id FROM tasks")).fetchall()
    assert [r[0] for r in rows] == ["g-letter"]


async def test_recurring_tasks_are_never_late(db, client, monkeypatch):
    await _add(db, await _pid(db), "Brush teeth", is_recurring=1, due_on="2026-09-01")
    freeze(monkeypatch, date(2026, 9, 29))
    assert "task-late" not in (await client.get("/widgets/tasks")).text


def _sync_today(monkeypatch, day):
    async def fake_today(_db):
        return day
    monkeypatch.setattr(task_sync, "family_today", fake_today)


async def test_imported_task_is_due_from_today_or_its_later_google_due(db, connected, google, monkeypatch):
    """First link/import: old past-due Google tasks start today, not all late at once."""
    pid = await _pid(db)
    await _link(db, pid)
    _sync_today(monkeypatch, date(2026, 9, 29))
    google.get(f"{TASKS_API}/kids/tasks").respond(200, json={"items": [
        {"id": "g1", "title": "From phone", "updated": "2020-01-01T00:00:00Z"},
        {"id": "g2", "title": "Old due", "due": "2026-09-20T00:00:00.000Z", "updated": "2020-01-01T00:00:00Z"},
        {"id": "g3", "title": "Due Friday", "due": "2026-10-02T00:00:00.000Z", "updated": "2020-01-01T00:00:00Z"},
    ]})
    profile = await (await db.execute("SELECT * FROM profiles WHERE id = ?", (pid,))).fetchone()

    await task_sync.reconcile_profile_tasks(db, "tok", profile)

    due = {r["title"]: r["due_on"] for r in await (await db.execute("SELECT title, due_on FROM tasks")).fetchall()}
    assert due == {"From phone": "2026-09-29", "Old due": "2026-09-29", "Due Friday": "2026-10-02"}
    assert tasks.late_label(due["Old due"], date(2026, 9, 29)) is None
    # It still goes late normally once its day has passed.
    assert tasks.late_label(due["Old due"], date(2026, 9, 30)) == "from yesterday"


async def test_due_date_changed_in_google_moves_due_on(db, connected, google, monkeypatch):
    pid = await _pid(db)
    await _link(db, pid)
    _sync_today(monkeypatch, date(2026, 9, 29))
    task_id = await _add(db, pid, "Sign letter", google_task_id="g1", due_on="2026-09-28")
    google.get(f"{TASKS_API}/kids/tasks").respond(200, json={"items": [
        {"id": "g1", "title": "Sign letter", "due": "2026-10-02T00:00:00.000Z", "updated": "2026-09-29T08:00:00Z"},
    ]})
    profile = await (await db.execute("SELECT * FROM profiles WHERE id = ?", (pid,))).fetchone()

    await task_sync.reconcile_profile_tasks(db, "tok", profile)

    row = await (await db.execute("SELECT due_on FROM tasks WHERE id = ?", (task_id,))).fetchone()
    assert row["due_on"] == "2026-10-02"
    assert tasks.late_label(row["due_on"], date(2026, 9, 29)) is None


async def test_google_update_without_a_due_keeps_due_on(db, connected, google, monkeypatch):
    pid = await _pid(db)
    await _link(db, pid)
    task_id = await _add(db, pid, "Sign letter", google_task_id="g1", due_on="2026-09-28")
    google.get(f"{TASKS_API}/kids/tasks").respond(200, json={"items": [
        {"id": "g1", "title": "Sign the letter", "updated": "2026-09-29T08:00:00Z"},
    ]})
    profile = await (await db.execute("SELECT * FROM profiles WHERE id = ?", (pid,))).fetchone()

    await task_sync.reconcile_profile_tasks(db, "tok", profile)

    row = await (await db.execute("SELECT title, due_on FROM tasks WHERE id = ?", (task_id,))).fetchone()
    assert (row["title"], row["due_on"]) == ("Sign the letter", "2026-09-28")


async def test_admin_group_edit_leaves_a_pre_migration_one_off_undated(db, admin_client):
    pid = await _pid(db)
    task_id = await _add(db, pid, "Old one-off")  # due_on NULL, as before migration 0004

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": pid, "time_of_day": "evening"})

    row = await (await db.execute("SELECT time_of_day, due_on FROM tasks")).fetchone()
    assert (row["time_of_day"], row["due_on"]) == ("evening", None)


async def test_admin_making_a_task_one_off_starts_it_today(db, admin_client, monkeypatch):
    pid = await _pid(db)
    task_id = await _add(db, pid, "Piano", is_recurring=1, due_on="2026-01-01")
    monkeypatch.setattr(admin, "family_today", lambda _db: _today(date(2026, 9, 29)))

    await admin_client.post(f"/admin/tasks/{task_id}/edit", data={"profile_id": pid})

    assert (await (await db.execute("SELECT due_on FROM tasks")).fetchone())[0] == "2026-09-29"


async def _today(day):
    return day


# --- 5. School days ---

def test_school_days_rule():
    assert recurrence.normalize_rule(["school"]) == "school"
    assert recurrence.normalize_rule(["Mon", "school"]) == "school"
    assert recurrence.describe("school") == "school days"
    assert recurrence.parse_rule("school") == {0, 1, 2, 3, 4}
    assert recurrence.is_due("school", 0, school_day=False) is False
    assert recurrence.is_due("school", 5, school_day=True) is True
    assert recurrence.is_due("school", 2) is True and recurrence.is_due("school", 6) is False  # no answer: Mon–Fri


async def _school_task_shown(db, monkeypatch, day):
    freeze(monkeypatch, day)
    profiles = await tasks.get_profiles_with_tasks(db)
    return "Pack bag" in {t["title"] for p in profiles for t in p["tasks"]}


async def test_school_days_follow_term_dates(db, monkeypatch):
    await _add(db, await _pid(db), "Pack bag", is_recurring=1, recurrence_rule="school")
    await term_dates.add_period(db, "term", "2026-09-02", "2026-10-23")
    await term_dates.add_period(db, "inset", "2026-10-02", "2026-10-02")
    await term_dates.add_period(db, "half_term", "2026-10-26", "2026-10-30")
    await term_dates.add_period(db, "term", "2026-11-02", "2026-12-18")

    assert await _school_task_shown(db, monkeypatch, date(2026, 9, 29)) is True    # term, Tuesday
    assert await _school_task_shown(db, monkeypatch, date(2026, 10, 2)) is False   # INSET day
    assert await _school_task_shown(db, monkeypatch, date(2026, 10, 27)) is False  # half term
    assert await _school_task_shown(db, monkeypatch, date(2026, 10, 3)) is False   # weekend


async def test_school_days_fall_back_to_weekdays_minus_bank_holidays(db, monkeypatch):
    await _add(db, await _pid(db), "Pack bag", is_recurring=1, recurrence_rule="school")
    await db.execute("INSERT INTO bank_holidays (date, title) VALUES ('2027-05-03', 'Early May bank holiday')")
    await db.commit()
    # No term dates for 2026-27.
    assert await _school_task_shown(db, monkeypatch, date(2027, 5, 4)) is True     # Tuesday
    assert await _school_task_shown(db, monkeypatch, date(2027, 5, 3)) is False    # bank holiday Monday
    assert await _school_task_shown(db, monkeypatch, date(2027, 5, 8)) is False    # Saturday


async def test_admin_school_days_option(db, admin_client):
    pid = await _pid(db)
    task_id = await _add(db, pid, "Pack bag")

    await admin_client.post(f"/admin/tasks/{task_id}/edit",
                            data={"profile_id": pid, "school_days": "true", "days": ["Sat"]})

    row = await (await db.execute("SELECT is_recurring, recurrence_rule, updated_at FROM tasks")).fetchone()
    assert (row["is_recurring"], row["recurrence_rule"]) == (1, "school")
    assert row["updated_at"] == "2020-01-01 00:00:00"
    assert await _queue(db) == []
    page = (await admin_client.get("/admin?tab=family")).text
    assert "repeats school days" in page
    assert re.search(r'name="school_days" value="true" checked', page)
    assert "Term dates missing for" in page  # none entered, and a task uses them


# --- 6. Sync safety ---

async def test_local_fields_are_never_pushed(db, connected, google):
    pid = await _pid(db)
    await _link(db, pid)
    task_id = await _add(db, pid, "Bins", google_task_id="g-bins", is_recurring=1, recurrence_rule="school",
                         time_of_day="evening", due_on="2026-09-01")
    await task_sync.queue_sync(db, "tasks", {"task_id": task_id})
    new_id = await _add(db, pid, "New", time_of_day="morning", due_on="2026-09-01")
    await task_sync.queue_sync(db, "tasks", {"task_id": new_id})
    await db.commit()
    patch = google.patch(f"{TASKS_API}/kids/tasks/g-bins").respond(200, json={"id": "g-bins"})
    insert = google.post(f"{TASKS_API}/kids/tasks").respond(200, json={"id": "g-new"})

    await task_sync.push_pending_changes(db, "tok")

    for route in (patch, insert):
        body = json.loads(route.calls.last.request.content)
        assert set(body) <= {"title", "status"}
        assert not {"due", "notes"} & set(body)


async def test_reassign_still_uses_the_tombstone(db, admin_client):
    p1, p2 = await _pid(db, 0), await _pid(db, 1)
    await _link(db, p1, "list-a")
    await _link(db, p2, "list-b")
    task_id = await _add(db, p1, "Feed cat", google_task_id="g-cat", time_of_day="morning")

    await admin_client.post(f"/admin/tasks/{task_id}/edit",
                            data={"profile_id": p2, "time_of_day": "morning", "school_days": "true"})

    rows = await (await db.execute("SELECT * FROM tasks ORDER BY id")).fetchall()
    moved, tomb = rows[0], rows[1]
    assert (moved["profile_id"], moved["google_task_id"], moved["time_of_day"]) == (p2, None, "morning")
    assert (tomb["profile_id"], tomb["google_task_id"], tomb["archived"]) == (p1, "g-cat", 1)
    assert await _queue(db) == [{"task_id": tomb["id"]}, {"task_id": task_id}]


# --- Migration ---

async def test_migration_4_is_a_no_op_for_existing_data(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    every = list(migrations.MIGRATIONS)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(migrations, "MIGRATIONS", every[:3])
    await database.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE profiles SET google_tasklist_id = 'kids' WHERE id = 1")
        conn.execute("""INSERT INTO tasks (profile_id, title, google_task_id, is_recurring, recurrence_rule,
                                           is_completed, updated_at)
                        VALUES (1, 'Bins', 'g1', 1, 'Mon,Thu', 1, '2020-01-01 00:00:00')""")
        conn.execute("INSERT INTO tasks (profile_id, title) VALUES (2, 'Tidy room')")
        conn.execute("INSERT INTO sync_queue (service, payload_json) VALUES ('tasks', '{\"task_id\": 1}')")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
                  for t in tables if t != "schema_migrations"}

    monkeypatch.setattr(migrations, "MIGRATIONS", every[:4])
    await database.init_db()
    await database.init_db()  # and again: a no-op

    with sqlite3.connect(path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
        after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in before}
        new = conn.execute("SELECT time_of_day, due_on FROM tasks").fetchall()
    assert cols[-2:] == ["time_of_day", "due_on"]
    assert new == [(None, None), (None, None)]
    after["tasks"] = [r[:-2] for r in after["tasks"]]
    assert after == before
