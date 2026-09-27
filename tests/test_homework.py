from datetime import date

import pytest

from app import database
from app.routers import admin
from app.security import create_session_token
from app.services import homework

TODAY = date(2026, 3, 11)  # a Wednesday


@pytest.fixture
def today(monkeypatch):
    async def fake_today(db):
        return TODAY

    monkeypatch.setattr(homework, "family_today", fake_today)
    monkeypatch.setattr(admin, "family_today", fake_today)
    return TODAY


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


async def _profiles(db):
    rows = await (await db.execute("SELECT id FROM profiles ORDER BY sort_order")).fetchall()
    return [r["id"] for r in rows]


async def _add_homework(db, profile_id, title, due_date=None, done=0, done_at=None, subject=""):
    cur = await db.execute(
        "INSERT INTO homework (profile_id, subject, title, due_date, done, done_at) VALUES (?, ?, ?, ?, ?, ?)",
        (profile_id, subject, title, due_date, done, done_at),
    )
    await db.commit()
    return cur.lastrowid


async def _add_list(db, profile_id, title="Week 3", words="cat\nhat", starts_on=None, ends_on=None, archived=0):
    cur = await db.execute(
        """INSERT INTO practice_word_lists (profile_id, title, words, starts_on, ends_on, archived)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (profile_id, title, words, starts_on, ends_on, archived),
    )
    await db.commit()
    return cur.lastrowid


async def _count(db, table):
    return (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0]


async def _rows(db, sql):
    return [tuple(r) for r in await (await db.execute(sql)).fetchall()]


async def _layout(db):
    rows = await (await db.execute("SELECT * FROM layout_state ORDER BY widget_id")).fetchall()
    return {r["widget_id"]: tuple(r)[1:5] for r in rows}


# --- Migrations / layout ---

async def test_init_db_is_idempotent(db):
    await database.init_db()
    await database.init_db()
    for table in ("homework", "practice_word_lists", "practice_log"):
        assert await _count(db, table) == 0
    rows = await (await db.execute("SELECT widget_id FROM layout_state WHERE widget_id = 'practice_words'")).fetchall()
    assert len(rows) == 1


async def test_fresh_install_gets_default_layout(db):
    layout = await _layout(db)
    assert layout["practice_words"] == (0, 11, 6, 4)
    assert layout["homework"] == (10, 7, 2, 4)


async def test_existing_layout_is_kept_and_new_widget_added_below(db):
    # An existing install: customised positions, no practice_words row yet.
    await db.execute("DELETE FROM layout_state WHERE widget_id = 'practice_words'")
    await db.execute("UPDATE layout_state SET grid_x = 0, grid_y = 14, grid_w = 3, grid_h = 5 WHERE widget_id = 'homework'")
    await db.execute("UPDATE layout_state SET grid_y = 2 WHERE widget_id = 'calendar'")
    await db.execute("UPDATE layout_state SET is_visible = 0 WHERE widget_id = 'photos'")
    await db.commit()
    before = await _layout(db)

    await database.init_db()
    await database.init_db()

    after = await _layout(db)
    new = after.pop("practice_words")
    assert after == before
    assert after["homework"] == (0, 14, 3, 5)
    assert new == (0, 19, 6, 4)  # below the lowest widget (homework ends at 14 + 5)
    assert await database.get_setting(db, "layout_version") == database.LAYOUT_VERSION


# --- Homework widget logic ---

def test_due_labels():
    assert homework.due_label(None, TODAY) is None
    assert homework.due_label(date(2026, 3, 11), TODAY) == "Due today"
    assert homework.due_label(date(2026, 3, 12), TODAY) == "Due tomorrow"
    assert homework.due_label(date(2026, 3, 13), TODAY) == "Due Friday"
    assert homework.due_label(date(2026, 3, 25), TODAY) == "Due 25 Mar"
    assert homework.due_label(date(2026, 3, 10), TODAY) == "Overdue"


async def test_widget_groups_sorts_and_flags(db, client, today):
    riley, jamie = (await _profiles(db))[2:4]
    await _add_homework(db, riley, "No date")
    await _add_homework(db, riley, "Tomorrow", "2026-03-12", subject="Maths")
    await _add_homework(db, riley, "Late", "2026-03-09")
    await _add_homework(db, jamie, "Today", "2026-03-11")

    groups = await homework.get_homework_groups(db)

    assert [g["name"] for g in groups] == ["Riley", "Jamie"]  # only children with homework, in profile order
    assert [h["title"] for h in groups[0]["homework"]] == ["Late", "Tomorrow", "No date"]
    late, tomorrow, _ = groups[0]["homework"]
    assert late["overdue"] and late["label"] == "Overdue"
    assert tomorrow["due_soon"] and not tomorrow["overdue"] and tomorrow["label"] == "Due tomorrow"
    assert groups[1]["homework"][0]["label"] == "Due today"

    html = (await client.get("/widgets/homework")).text
    assert 'id="widget-homework"' in html
    assert "hw-item overdue" in html and "Due tomorrow" in html
    assert '<span class="hw-subject">Maths</span>' in html


async def test_done_items_drop_off_after_their_day(db, today):
    riley = (await _profiles(db))[2]
    await _add_homework(db, riley, "Due today, done", "2026-03-11", 1, "2026-03-10T18:00:00+00:00")
    await _add_homework(db, riley, "Due later, done early", "2026-03-13", 1, "2026-03-09T18:00:00+00:00")
    await _add_homework(db, riley, "Was due yesterday, done", "2026-03-10", 1, "2026-03-10T18:00:00+00:00")
    await _add_homework(db, riley, "Overdue, done today", "2026-03-02", 1, "2026-03-11T08:00:00+00:00")
    await _add_homework(db, riley, "Undated, done today", None, 1, "2026-03-11T08:00:00+00:00")
    await _add_homework(db, riley, "Undated, done yesterday", None, 1, "2026-03-10T20:00:00+00:00")
    await _add_homework(db, riley, "Still overdue", "2026-03-01")

    groups = await homework.get_homework_groups(db)
    titles = {h["title"] for h in groups[0]["homework"]}

    assert titles == {
        "Due today, done", "Due later, done early", "Overdue, done today", "Undated, done today", "Still overdue",
    }
    done_overdue = next(h for h in groups[0]["homework"] if h["title"] == "Overdue, done today")
    assert not done_overdue["overdue"] and done_overdue["label"] is None


async def test_toggle_homework(db, client, today):
    riley = (await _profiles(db))[2]
    hw_id = await _add_homework(db, riley, "Spellings", "2026-03-12")

    resp = await client.post(f"/api/homework/{hw_id}/toggle")
    assert resp.status_code == 200
    assert 'class="hw-item done' in resp.text
    row = await (await db.execute("SELECT done, done_at FROM homework WHERE id = ?", (hw_id,))).fetchone()
    assert row["done"] == 1 and row["done_at"]

    resp = await client.post(f"/api/homework/{hw_id}/toggle")
    row = await (await db.execute("SELECT done, done_at FROM homework WHERE id = ?", (hw_id,))).fetchone()
    assert (row["done"], row["done_at"]) == (0, None)
    assert "hw-item done" not in resp.text

    assert (await client.post("/api/homework/9999/toggle")).status_code == 404


async def test_homework_is_not_queued_for_google(db, admin_client):
    riley = (await _profiles(db))[2]
    await admin_client.post("/admin/homework", data={"profile_id": riley, "title": "Read"})
    hw_id = (await (await db.execute("SELECT id FROM homework")).fetchone())[0]
    await admin_client.post(f"/api/homework/{hw_id}/toggle")
    assert await _count(db, "sync_queue") == 0


async def test_empty_states(db, client):
    assert "Add homework in Admin" in (await client.get("/widgets/homework")).text
    assert "Add a word list in Admin" in (await client.get("/widgets/practice-words")).text


# --- Practice words ---

def test_normalise_words():
    assert homework.normalise_words(" cat, Hat\n\nhat ,CAT\n  big   dog \n,,") == ["cat", "Hat", "big dog"]
    assert homework.normalise_words("") == []
    assert homework.normalise_words(None) == []
    many = homework.normalise_words("\n".join(f"w{i}" for i in range(100)))
    assert len(many) == homework.MAX_WORDS and many[0] == "w0"
    assert homework.normalise_words("x" * 100) == ["x" * homework.MAX_WORD_LENGTH]


async def test_active_list_window(db, today):
    riley = (await _profiles(db))[2]
    await _add_list(db, riley, "open-ended")
    await _add_list(db, riley, "this week", starts_on="2026-03-09", ends_on="2026-03-15")
    await _add_list(db, riley, "ends today", ends_on="2026-03-11")
    await _add_list(db, riley, "starts today", starts_on="2026-03-11")
    await _add_list(db, riley, "ended", ends_on="2026-03-10")
    await _add_list(db, riley, "future", starts_on="2026-03-12")
    await _add_list(db, riley, "archived", archived=1)

    titles = [wl["title"] for wl in await homework.get_practice_lists(db)]

    assert titles == ["open-ended", "this week", "ends today", "starts today"]


async def test_practised_toggle_is_per_day(db, client, today):
    riley = (await _profiles(db))[2]
    list_id = await _add_list(db, riley, words="ship\nshop")
    await db.execute("INSERT INTO practice_log (list_id, practised_on) VALUES (?, '2026-03-10')", (list_id,))
    await db.commit()

    html = (await client.get("/widgets/practice-words")).text
    assert "pw-practised on" not in html  # yesterday doesn't count
    assert "<li>ship</li><li>shop</li>" in html

    resp = await client.post(f"/api/practice-words/{list_id}/practised")
    assert resp.status_code == 200 and "pw-practised on" in resp.text
    log = await (await db.execute("SELECT practised_on FROM practice_log ORDER BY practised_on")).fetchall()
    assert [r[0] for r in log] == ["2026-03-10", "2026-03-11"]

    resp = await client.post(f"/api/practice-words/{list_id}/practised")
    assert "pw-practised on" not in resp.text
    log = await (await db.execute("SELECT practised_on FROM practice_log")).fetchall()
    assert [r[0] for r in log] == ["2026-03-10"]

    assert (await client.post("/api/practice-words/9999/practised")).status_code == 404


async def test_handwriting_style_setting(db, admin_client):
    riley = (await _profiles(db))[2]
    await _add_list(db, riley)
    assert "pw-words hand-semijoined" in (await admin_client.get("/widgets/practice-words")).text

    resp = await admin_client.post("/admin/handwriting-style", data={"style": "joined"})
    assert resp.status_code == 303
    assert "pw-words hand-joined" in (await admin_client.get("/widgets/practice-words")).text
    assert "pw-words hand-joined" in (await admin_client.get("/")).text

    assert (await admin_client.post("/admin/handwriting-style", data={"style": "comic"})).status_code == 400
    assert await database.get_setting(db, homework.HANDWRITING_SETTING) == "joined"


# --- Admin ---

async def test_admin_homework_crud(db, admin_client):
    riley, jamie = (await _profiles(db))[2:4]
    resp = await admin_client.post("/admin/homework", data={
        "profile_id": riley, "subject": " Maths ", "title": " Times tables ", "details": "", "due_date": "2026-03-12",
    })
    assert resp.status_code == 303
    row = await (await db.execute("SELECT * FROM homework")).fetchone()
    assert (row["profile_id"], row["subject"], row["title"], row["details"], row["due_date"], row["source"]) == (
        riley, "Maths", "Times tables", None, "2026-03-12", "manual",
    )

    assert f'action="/admin/homework/{row["id"]}/edit"' in (await admin_client.get("/admin")).text

    resp = await admin_client.post(f"/admin/homework/{row['id']}/edit", data={
        "profile_id": jamie, "subject": "", "title": "Reading", "details": "Chapter 2", "due_date": "",
    })
    assert resp.status_code == 303
    row = await (await db.execute("SELECT * FROM homework")).fetchone()
    assert (row["profile_id"], row["title"], row["details"], row["due_date"]) == (jamie, "Reading", "Chapter 2", None)

    assert (await admin_client.post(f"/admin/homework/{row['id']}/delete")).status_code == 303
    assert await _count(db, "homework") == 0


async def test_admin_word_list_crud(db, admin_client, today):
    riley = (await _profiles(db))[2]
    resp = await admin_client.post("/admin/practice-words", data={
        "profile_id": riley, "title": "Week 3 handwriting", "words": "light, night\nLIGHT\n right ",
        "starts_on": "", "ends_on": "2026-03-15",
    })
    assert resp.status_code == 303
    row = await (await db.execute("SELECT * FROM practice_word_lists")).fetchone()
    assert row["words"] == "light\nnight\nright" and row["ends_on"] == "2026-03-15" and row["source"] == "manual"
    assert row["starts_on"] is None

    admin_html = (await admin_client.get("/admin")).text
    assert "3 words" in admin_html

    await admin_client.post(f"/admin/practice-words/{row['id']}/edit", data={
        "profile_id": riley, "title": "Week 4", "words": "sight", "starts_on": "2026-03-09", "ends_on": "",
    })
    row = await (await db.execute("SELECT * FROM practice_word_lists")).fetchone()
    assert (row["title"], row["words"], row["starts_on"], row["ends_on"]) == ("Week 4", "sight", "2026-03-09", None)

    await admin_client.post(f"/admin/practice-words/{row['id']}/archive")
    assert await homework.get_practice_lists(db) == []
    await admin_client.post(f"/admin/practice-words/{row['id']}/archive")
    assert len(await homework.get_practice_lists(db)) == 1

    await db.execute("INSERT INTO practice_log (list_id, practised_on) VALUES (?, '2026-03-11')", (row["id"],))
    await db.commit()
    assert (await admin_client.post(f"/admin/practice-words/{row['id']}/delete")).status_code == 303
    assert await _count(db, "practice_word_lists") == 0
    assert await _count(db, "practice_log") == 0


@pytest.mark.parametrize("data, message", [
    ({"title": "   "}, "Title can&#39;t be blank"),
    ({"profile_id": "9999"}, "no longer exists"),
    ({"profile_id": "abc"}, "Choose a family member"),
    ({"due_date": "31/02/2026"}, "Due date isn&#39;t a valid date"),
    ({"title": "x" * 500}, "Title is too long"),
])
async def test_admin_homework_validation(db, admin_client, data, message):
    riley = (await _profiles(db))[2]
    form = {"profile_id": riley, "title": "Read", **data}
    resp = await admin_client.post("/admin/homework", data=form)
    assert resp.status_code == 400
    assert message in resp.text
    assert await _count(db, "homework") == 0


@pytest.mark.parametrize("data, message", [
    ({"title": ""}, "Title can&#39;t be blank"),
    ({"words": " , \n "}, "Add at least one word"),
    ({"profile_id": "9999"}, "no longer exists"),
    ({"starts_on": "next week"}, "Start date isn&#39;t a valid date"),
    ({"starts_on": "2026-03-10", "ends_on": "2026-03-01"}, "end date is before the start date"),
])
async def test_admin_word_list_validation(db, admin_client, data, message):
    riley = (await _profiles(db))[2]
    form = {"profile_id": riley, "title": "Week 3", "words": "cat", **data}
    resp = await admin_client.post("/admin/practice-words", data=form)
    assert resp.status_code == 400
    assert message in resp.text
    assert await _count(db, "practice_word_lists") == 0


async def test_admin_edit_validation_and_missing_rows(db, admin_client):
    riley = (await _profiles(db))[2]
    hw_id = await _add_homework(db, riley, "Keep me")
    list_id = await _add_list(db, riley, "Keep me too")

    assert (await admin_client.post(f"/admin/homework/{hw_id}/edit", data={"profile_id": riley, "title": ""})).status_code == 400
    assert (await admin_client.post(f"/admin/practice-words/{list_id}/edit", data={
        "profile_id": riley, "title": "X", "words": "",
    })).status_code == 400
    assert (await admin_client.post("/admin/homework/9999/edit", data={"profile_id": riley, "title": "X"})).status_code == 404
    assert (await admin_client.post("/admin/practice-words/9999/edit", data={
        "profile_id": riley, "title": "X", "words": "a",
    })).status_code == 404
    assert (await (await db.execute("SELECT title FROM homework")).fetchone())[0] == "Keep me"
    assert (await (await db.execute("SELECT title FROM practice_word_lists")).fetchone())[0] == "Keep me too"


@pytest.mark.parametrize("path", [
    "/admin/homework",
    "/admin/homework/1/edit",
    "/admin/homework/1/archive",
    "/admin/homework/1/delete",
    "/admin/practice-words",
    "/admin/practice-words/1/edit",
    "/admin/practice-words/1/archive",
    "/admin/practice-words/1/delete",
    "/admin/handwriting-style",
])
async def test_admin_mutations_require_auth(db, client, path):
    riley = (await _profiles(db))[2]
    await _add_homework(db, riley, "Keep me")
    await _add_list(db, riley, "Keep me too")

    resp = await client.post(path, data={"profile_id": riley, "title": "Hacked", "words": "x", "style": "joined"})

    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"
    assert await _rows(db, "SELECT title, archived FROM homework") == [("Keep me", 0)]
    assert await _rows(db, "SELECT title, archived FROM practice_word_lists") == [("Keep me too", 0)]
    assert await database.get_setting(db, homework.HANDWRITING_SETTING) is None


async def test_user_text_is_escaped(db, admin_client, today):
    riley = (await _profiles(db))[2]
    await _add_homework(db, riley, "<script>alert(1)</script>")
    await _add_list(db, riley, "<b>list</b>", words="<i>word</i>")

    for path in ("/", "/admin", "/widgets/homework", "/widgets/practice-words"):
        html = (await admin_client.get(path)).text
        assert "<script>alert(1)</script>" not in html and "<i>word</i>" not in html, path


# --- Dashboard ---

async def test_dashboard_renders_both_widgets(db, client, today):
    riley = (await _profiles(db))[2]
    await _add_homework(db, riley, "Spellings", "2026-03-11")
    await _add_list(db, riley, "Week 3", words="ship\nshop")

    html = (await client.get("/")).text

    assert 'gs-id="homework"' in html and 'id="widget-homework"' in html
    assert 'gs-id="practice_words"' in html and 'id="widget-practice_words"' in html
    assert "Spellings" in html and "Due today" in html
    assert "<li>ship</li>" in html
    assert "/static/css/homework.css" in html
    assert "Not connected" not in html.split('id="widget-homework"')[1].split("widget-card")[0]


async def test_admin_keyboard_markers(db, admin_client):
    html = (await admin_client.get("/admin")).text
    assert "data-osk" not in html

    await admin_client.post("/admin/onscreen-keyboard", data={"enabled": "true"})
    html = (await admin_client.get("/admin")).text
    assert "/static/js/keyboard.js" in html
    assert 'autocapitalize="off" spellcheck="false" data-osk="text"' in html  # the words textarea
    assert "placeholder=\"What's the homework?\" required aria-label=\"Homework\" autocomplete=\"off\" data-osk=\"text\"" in html


def test_fonts_are_vendored():
    from pathlib import Path

    fonts = Path(__file__).parent.parent / "app" / "static" / "fonts"
    for name in ("playwrite-gb-s-400-normal.woff2", "playwrite-gb-j-400-normal.woff2"):
        assert (fonts / name).read_bytes()[:4] == b"wOF2", name
    assert "SIL Open Font License" in (fonts / "OFL-Playwrite.txt").read_text(encoding="utf-8")


# --- Review follow-ups ---

async def test_hidden_widget_is_not_re_added(db, client):
    await db.execute("UPDATE layout_state SET is_visible = 0 WHERE widget_id IN ('practice_words', 'homework')")
    await db.commit()
    before = await _rows(db, "SELECT * FROM layout_state ORDER BY widget_id")

    await database.init_db()

    assert await _rows(db, "SELECT * FROM layout_state ORDER BY widget_id") == before
    hidden = await _rows(db, "SELECT widget_id, is_visible FROM layout_state WHERE is_visible = 0 ORDER BY widget_id")
    assert hidden == [("homework", 0), ("practice_words", 0)]
    html = (await client.get("/")).text
    assert 'gs-id="practice_words"' not in html and 'gs-id="homework"' not in html


async def test_toggles_404_for_items_not_on_the_widget(db, client, today):
    riley = (await _profiles(db))[2]
    dropped = await _add_homework(db, riley, "Dropped", "2026-03-10", 1, "2026-03-10T09:00:00+00:00")
    archived_hw = await _add_homework(db, riley, "Archived", "2026-03-12")
    await db.execute("UPDATE homework SET archived = 1 WHERE id = ?", (archived_hw,))
    await db.commit()
    archived = await _add_list(db, riley, "archived", archived=1)
    expired = await _add_list(db, riley, "expired", ends_on="2026-03-10")
    future = await _add_list(db, riley, "future", starts_on="2026-03-12")

    for hw_id in (dropped, archived_hw):
        assert (await client.post(f"/api/homework/{hw_id}/toggle")).status_code == 404
    for list_id in (archived, expired, future):
        assert (await client.post(f"/api/practice-words/{list_id}/practised")).status_code == 404

    assert await _rows(db, "SELECT done FROM homework ORDER BY id") == [(1,), (0,)]
    assert await _count(db, "practice_log") == 0


async def test_deleting_a_profile_cascades(db, admin_client, today):
    riley, jamie = (await _profiles(db))[2:4]
    await _add_homework(db, riley, "Riley's")
    await _add_homework(db, jamie, "Jamie's")
    list_id = await _add_list(db, riley)
    await db.execute("INSERT INTO practice_log (list_id, practised_on) VALUES (?, '2026-03-11')", (list_id,))
    await db.commit()

    resp = await admin_client.post(f"/admin/profiles/{riley}/delete")

    assert resp.status_code == 303
    assert await _rows(db, "SELECT title FROM homework") == [("Jamie's",)]
    assert await _count(db, "practice_word_lists") == 0
    assert await _count(db, "practice_log") == 0
    for path in ("/", "/admin", "/widgets/homework", "/widgets/practice-words"):
        assert (await admin_client.get(path)).status_code == 200, path


async def test_admin_archives_and_tucks_away_finished_homework(db, admin_client, today):
    riley = (await _profiles(db))[2]
    await _add_homework(db, riley, "Current")
    await _add_homework(db, riley, "Still showing", "2026-03-11", 1, "2026-03-11T08:00:00+00:00")
    await _add_homework(db, riley, "Long done", "2026-03-02", 1, "2026-03-02T08:00:00+00:00")
    to_archive = await _add_homework(db, riley, "Archive me", "2026-03-20")

    resp = await admin_client.post(f"/admin/homework/{to_archive}/archive")
    assert resp.status_code == 303
    assert "Archive me" not in (await admin_client.get("/widgets/homework")).text

    html = (await admin_client.get("/admin")).text
    current, finished = html.split('<details class="finished-homework">')
    finished = finished.split('action="/admin/homework">')[0]
    assert "Show finished (2)" in finished
    assert "Long done" in finished and "Archive me" in finished
    assert "Current" in current and "Still showing" in current
    assert "Long done" not in current and "Archive me" not in current

    await admin_client.post(f"/admin/homework/{to_archive}/archive")  # restore
    assert "Archive me" in (await admin_client.get("/widgets/homework")).text


async def test_validation_error_keeps_what_was_typed(db, admin_client):
    riley, jamie = (await _profiles(db))[2:4]
    words = "\n".join(f"word{i}" for i in range(40))
    resp = await admin_client.post("/admin/practice-words", data={
        "profile_id": jamie, "title": "Week 5", "words": words, "starts_on": "2026-03-10", "ends_on": "2026-03-01",
    })
    assert resp.status_code == 400
    assert f'required placeholder="Words — one per line, or separated by commas" aria-label="Words" autocapitalize="off" spellcheck="false">{words}</textarea>' in resp.text
    assert 'value="Week 5"' in resp.text and 'value="2026-03-10"' in resp.text and 'value="2026-03-01"' in resp.text
    add_form = resp.text.split('action="/admin/practice-words">')[1].split("</form>")[0]
    assert f'<option value="{jamie}" selected>' in add_form

    resp = await admin_client.post("/admin/homework", data={
        "profile_id": riley, "subject": "Maths", "title": "", "details": "p12 <b>", "due_date": "2026-03-12",
    })
    assert resp.status_code == 400
    add_form = resp.text.split('action="/admin/homework">')[1].split("</form>")[0]
    assert 'value="Maths"' in add_form and 'value="p12 &lt;b&gt;"' in add_form and 'value="2026-03-12"' in add_form

    # A failed edit reopens that row's form with the submitted values.
    hw_id = await _add_homework(db, riley, "Original")
    resp = await admin_client.post(f"/admin/homework/{hw_id}/edit", data={
        "profile_id": riley, "title": "Renamed", "due_date": "not a date",
    })
    assert resp.status_code == 400
    edit = resp.text.split(f'action="/admin/homework/{hw_id}/edit"')[0].rsplit("<details", 1)[1]
    assert edit.startswith(' class="task-edit" open>')
    assert 'value="Renamed"' in resp.text.split(f'action="/admin/homework/{hw_id}/edit"')[1].split("</form>")[0]


async def test_handwriting_label_and_stored_value(db, admin_client):
    assert homework.HANDWRITING_STYLES["semijoined"] == "Semi-joined (Playwrite GB S)"
    await database.set_setting(db, homework.HANDWRITING_SETTING, "semijoined")
    await db.commit()
    html = (await admin_client.get("/admin")).text
    assert 'value="semijoined" checked' in html and "Semi-joined (Playwrite GB S)" in html
