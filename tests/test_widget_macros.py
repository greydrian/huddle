"""tick_button (widgets/_widget.html): only ticking an open row reveals the
tick first and holds the swap 450ms; unticking a done row swaps at once."""

import re
from datetime import datetime

from app import database


def _buttons(html, marker):
    return [b for b in re.findall(r"<button\b[^>]*>", html, re.S) if marker in b]


def _assert_open_and_done(buttons, is_done):
    open_ = [b for b in buttons if not is_done(b)]
    done = [b for b in buttons if is_done(b)]
    assert len(open_) == 1 and len(done) == 1, buttons
    assert "swap:450ms" in open_[0] and "hx-on:click" in open_[0]
    assert "swap:450ms" not in done[0] and "hx-on:click" not in done[0]


async def _profile(db):
    return (await (await db.execute("SELECT id FROM profiles ORDER BY sort_order")).fetchone())[0]


async def test_task_tick_reveals_only_when_ticking(db, client):
    pid = await _profile(db)
    await db.execute(
        "INSERT INTO tasks (profile_id, title, is_completed) VALUES (?, 'Open', 0), (?, 'Done', 1)", (pid, pid)
    )
    await db.commit()

    html = (await client.get("/widgets/tasks")).text
    _assert_open_and_done(_buttons(html, 'class="task-check"'), lambda b: 'aria-label="Mark not done"' in b)


async def test_homework_tick_reveals_only_when_ticking(db, client):
    pid = await _profile(db)
    now = datetime.now(await database.family_timezone(db)).isoformat()
    await db.execute(
        "INSERT INTO homework (profile_id, title, done, done_at) VALUES (?, 'Open', 0, NULL), (?, 'Done', 1, ?)",
        (pid, pid, now),
    )
    await db.commit()

    html = (await client.get("/widgets/homework")).text
    _assert_open_and_done(_buttons(html, 'class="hw-item'), lambda b: 'aria-pressed="true"' in b)


async def test_practice_words_tick_reveals_only_when_ticking(db, client):
    pid = await _profile(db)
    await db.execute(
        "INSERT INTO practice_word_lists (profile_id, title, words) VALUES (?, 'A', 'cat'), (?, 'B', 'dog')", (pid, pid)
    )
    done_id = (await (await db.execute("SELECT id FROM practice_word_lists WHERE title = 'B'")).fetchone())[0]
    today = (await database.family_today(db)).isoformat()
    await db.execute("INSERT INTO practice_log (list_id, practised_on) VALUES (?, ?)", (done_id, today))
    await db.commit()

    html = (await client.get("/widgets/practice-words")).text
    _assert_open_and_done(_buttons(html, 'class="pw-practised'), lambda b: 'aria-pressed="true"' in b)
