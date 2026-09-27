import re


async def _first_profile(db):
    return (await (await db.execute("SELECT id FROM profiles ORDER BY sort_order")).fetchone())["id"]


async def _add_task(db, profile_id, title, completed=False):
    await db.execute(
        "INSERT INTO tasks (profile_id, title, is_completed, updated_at) VALUES (?, ?, ?, datetime('now'))",
        (profile_id, title, int(completed)),
    )
    await db.commit()


def _group(html, title):
    """The <div class="task-group"> containing a given task title."""
    groups = re.split(r'<div class="task-group">', html)
    return next(g for g in groups if title in g)


def _done_section(group):
    m = re.search(r'<details class="task-done">(.*?)</details>', group, re.S)
    return m.group(1) if m else ""


async def test_finished_tasks_fold_away_open_ones_stay(db, client):
    pid = await _first_profile(db)
    await _add_task(db, pid, "Feed cat")
    await _add_task(db, pid, "Make bed", completed=True)

    html = (await client.get("/widgets/tasks")).text
    group = _group(html, "Feed cat")
    done = _done_section(group)

    assert "Make bed" in done
    assert "Feed cat" not in done
    assert "✓ 1 done" in done
    assert "All done for today" not in group


async def test_all_done_message_when_every_task_finished(db, client):
    pid = await _first_profile(db)
    await _add_task(db, pid, "Make bed", completed=True)

    group = _group((await client.get("/widgets/tasks")).text, "Make bed")

    assert "All done for today" in group
    assert "Make bed" in _done_section(group)
    assert "No tasks yet" not in group


async def test_toggle_moves_task_into_and_out_of_done(db, client):
    pid = await _first_profile(db)
    await _add_task(db, pid, "Feed cat")
    task_id = (await (await db.execute("SELECT id FROM tasks WHERE title = 'Feed cat'")).fetchone())["id"]

    html = (await client.post(f"/api/tasks/{task_id}/toggle")).text
    assert "Feed cat" in _done_section(_group(html, "Feed cat"))

    html = (await client.post(f"/api/tasks/{task_id}/toggle")).text
    assert "Feed cat" not in _done_section(_group(html, "Feed cat"))


async def test_dashboard_renders_folded_tasks(db, client):
    pid = await _first_profile(db)
    await _add_task(db, pid, "Make bed", completed=True)

    html = (await client.get("/")).text

    assert '<details class="task-done">' in html
