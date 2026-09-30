"""Spec 10.10 extras: subject icons and colours, the reading log and the
homework done history (app/services/homework.py, migration 7)."""

import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app import database, freshness, migrations
from app.routers import admin
from app.security import create_session_token
from app.services import homework

STATIC = Path(database.__file__).parent / "static"
TODAY = date(2026, 3, 11)  # a Wednesday, in term (the fallback: weekdays are school days)


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


def _frozen_now(monkeypatch, module, instant: datetime):
    """datetime.now(tz) in `module` returns `instant` (a UTC moment) in tz."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(module, "datetime", Frozen)


async def _ids(db):
    rows = await (await db.execute("SELECT id, name FROM profiles ORDER BY sort_order")).fetchall()
    return {r["name"]: r["id"] for r in rows}


async def _parents(db, *names):
    for name in names:
        await db.execute("UPDATE profiles SET is_parent = 1 WHERE name = ?", (name,))
    await db.commit()


async def _rows(db, sql, *args):
    return [tuple(r) for r in await (await db.execute(sql, args)).fetchall()]


# --- Subjects ---


@pytest.mark.parametrize(
    "text, key",
    [
        ("Maths", "maths"),
        ("maths", "maths"),
        ("Math", "maths"),
        ("Numeracy", "maths"),
        ("Times tables", "maths"),
        ("TT Rockstars", "maths"),
        ("Mathematics homework", "maths"),
        ("English", "english"),
        ("Spelling", "english"),
        ("Spellings", "english"),
        ("Phonics", "english"),
        ("SPaG", "english"),
        ("Grammar & punctuation", "english"),
        ("Literacy", "english"),
        ("Reading", "reading"),
        ("Reading book", "reading"),
        ("Reading comprehension", "reading"),
        ("Bug Club", "reading"),
        ("Library book", "reading"),
        ("Science", "science"),
        ("science investigation", "science"),
        ("Topic", "topic"),
        ("History", "topic"),
        ("Geography project", "topic"),
        ("French", "other"),
        ("PE", "other"),
        ("", "other"),
        (None, "other"),
        ("Mathsy", "other"),
        # The earliest match in the text wins.
        ("Reading and spelling", "reading"),
        ("Spelling then reading", "english"),
        # Phonics schemes and times-tables apps.
        ("Read Write Inc", "english"),
        ("RWI", "english"),
        ("RWI book bag", "english"),
        ("TTRS", "maths"),
        ("TTRockstars", "maths"),
        ("Times Tables Rock Stars", "maths"),
        # Languages are Other, even with a word that would otherwise match.
        ("French", "other"),
        ("Spanish reading", "other"),
        ("German vocabulary", "other"),
        ("MFL", "other"),
        ("Languages", "other"),
        ("Modern Foreign Languages", "other"),
        # "book" alone means nothing; "read" counts only when nothing else matched.
        ("Homework book", "other"),
        ("Books", "other"),
        ("Read chapter 3", "reading"),
        ("Read", "reading"),
        ("Read the maths sheet", "maths"),
    ],
)
def test_subject_synonyms(text, key):
    assert homework.subject_key_for(text) == key
    assert migrations._m7_subject_key(text) == key  # the migration's frozen copy agrees today


def test_migration_copy_of_the_synonyms_matches():
    """Migration 7 froze a copy of the table; at the time of writing they agree."""
    assert migrations._M7_SYNONYMS == homework.SUBJECT_SYNONYMS
    assert migrations._M7_WEAK_SYNONYMS == homework.WEAK_SYNONYMS
    assert migrations._M7_LANGUAGE_WORDS == homework.LANGUAGE_WORDS
    for key, phrases in homework.SUBJECT_SYNONYMS.items():
        for phrase in phrases:
            assert migrations._m7_subject_key(phrase) == homework.subject_key_for(phrase) == key


def test_explicit_pick_wins_and_unknown_is_ignored():
    assert homework.parse_subject_key("topic", "Maths") == "topic"
    assert homework.parse_subject_key("", "Maths") == "maths"
    assert homework.parse_subject_key("bogus", "Phonics") == "english"
    assert homework.subject_info("nope", "")["key"] == "other"
    assert homework.subject_info("maths", "")["text"] == "Maths"
    assert homework.subject_info("english", "Spellings")["text"] == "Spellings"


def test_every_subject_icon_is_in_the_sprite():
    sprite = (STATIC / "icons" / "lucide" / "lucide.svg").read_text(encoding="utf-8")
    for _label, icon in homework.SUBJECTS.values():
        assert f'<symbol id="{icon}"' in sprite
    assert '<symbol id="book-open-text"' in sprite  # the reading row


def _luminance(hex_colour):
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _mix(colour, base, share):
    return "#" + "".join(
        f"{round(int(colour[i : i + 2], 16) * share + int(base[i : i + 2], 16) * (1 - share)):02x}" for i in (1, 3, 5)
    )


def _ratio(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tokens(css, selector):
    block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css).group(1)
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))


@pytest.mark.parametrize("mode", ["day", "night"])
def test_subject_colours_meet_contrast(mode):
    """Each subject's text/icon colour on its own tint (over the card) is at
    least 4.5:1, day and night."""
    selector = ":root" if mode == "day" else ':root[data-mode="night"]'
    style = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    card = _tokens(style, selector)["card"].strip()
    subjects = _tokens((STATIC / "css" / "homework.css").read_text(encoding="utf-8"), selector)
    tint = float(subjects["subj-tint"].rstrip("%")) / 100
    for key in homework.SUBJECTS:
        colour = subjects[f"subj-{key}"].strip()
        assert _ratio(colour, _mix(colour, card, tint)) >= 4.5, (mode, key, colour)


# --- Migration 7 ---


async def test_migration_7_upgrade_keeps_data_and_backfills(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    with monkeypatch.context() as m:
        m.setattr(migrations, "MIGRATIONS", [x for x in migrations.MIGRATIONS if x.version < 7])
        await database.init_db()
    with sqlite3.connect(path) as conn:
        assert "subject_key" not in [c[1] for c in conn.execute("PRAGMA table_info(homework)")]
        riley = conn.execute("SELECT id FROM profiles WHERE name = 'Riley'").fetchone()[0]
        conn.executemany(
            "INSERT INTO homework (profile_id, subject, title, done, done_at) VALUES (?, ?, ?, ?, ?)",
            [
                (riley, "Spelling", "Week 3 list", 0, None),
                (riley, "Numeracy", "Number bonds", 1, "2026-03-10T16:40:00+00:00"),
                (riley, "", "Poster", 0, None),
                (riley, "Reading comprehension", "Chapter 2", 1, None),
            ],
        )
        conn.commit()
        before = {
            t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
            for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if t not in ("schema_migrations", "sqlite_sequence")
        }

    await database.init_db()  # the full list: runs 7 only

    with sqlite3.connect(path) as conn:
        assert {v for (v,) in conn.execute("SELECT version FROM schema_migrations")} == {
            m.version for m in migrations.MIGRATIONS
        }
        for table, rows in before.items():
            after = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            if table == "homework":
                after = [row[:-1] for row in after]  # all but the new subject_key column
            elif rows:  # later migrations may add columns (profiles: avatars, 8)
                after = [row[: len(rows[0])] for row in after]
            assert after == rows, table
        assert conn.execute("SELECT title, subject_key FROM homework ORDER BY id").fetchall() == [
            ("Week 3 list", "english"),
            ("Number bonds", "maths"),
            ("Poster", "other"),
            ("Chapter 2", "reading"),
        ]
        # The one done row with a time starts the done history.
        assert conn.execute("SELECT homework_id, done, at FROM homework_events").fetchall() == [
            (2, 1, "2026-03-10T16:40:00+00:00")
        ]
        assert conn.execute("SELECT COUNT(*) FROM reading_log").fetchone() == (0,)


async def test_migration_7_is_idempotent(db):
    """Run again (a restored pre-7 backup): nothing doubles."""
    riley = (await _ids(db))["Riley"]
    await db.execute(
        "INSERT INTO homework (profile_id, subject, title, done, done_at) "
        "VALUES (?, 'Maths', 'Sheet', 1, '2026-03-10T16:40:00+00:00')",
        (riley,),
    )
    await db.commit()
    for _ in range(2):
        await migrations.m0007_homework_extras(db)
        await db.commit()
    assert await _rows(db, "SELECT subject_key FROM homework") == [("maths",)]
    assert len(await _rows(db, "SELECT * FROM homework_events")) == 1


async def test_migration_7_rerun_keeps_admin_picks(db):
    riley = (await _ids(db))["Riley"]
    await db.execute(
        "INSERT INTO homework (profile_id, subject, subject_key, title) VALUES (?, 'Maths', 'topic', 'Picked')",
        (riley,),
    )
    await db.execute("INSERT INTO homework (profile_id, subject, title) VALUES (?, 'Phonics', 'Default')", (riley,))
    await db.commit()
    await migrations.m0007_homework_extras(db)
    await db.commit()
    assert await _rows(db, "SELECT title, subject_key FROM homework ORDER BY id") == [
        ("Picked", "topic"),
        ("Default", "english"),
    ]


# --- Widget: subject chips ---


async def test_widget_shows_subject_chip(db, client, admin_client, today):
    riley = (await _ids(db))["Riley"]
    await admin_client.post("/admin/homework", data={"profile_id": riley, "subject": "Phonics", "title": "Sounds"})
    await admin_client.post(
        "/admin/homework", data={"profile_id": riley, "subject": "", "subject_key": "science", "title": "Grow cress"}
    )
    assert await _rows(db, "SELECT title, subject_key FROM homework ORDER BY id") == [
        ("Sounds", "english"),
        ("Grow cress", "science"),
    ]
    html = (await client.get("/widgets/homework")).text
    assert 'class="hw-subject subj-english"' in html and "#spell-check" in html and "Phonics</span>" in html
    assert 'class="hw-subject subj-science"' in html and "#flask-conical" in html and "Science</span>" in html


async def test_admin_edit_subject_pick(db, admin_client, today):
    riley = (await _ids(db))["Riley"]
    await admin_client.post("/admin/homework", data={"profile_id": riley, "subject": "Maths", "title": "Sheet"})
    hw_id = (await _rows(db, "SELECT id FROM homework"))[0][0]
    page = (await admin_client.get("/admin?tab=family")).text
    assert '<option value="" selected' not in page  # "match" is the default: nothing explicitly selected
    # Changing the text re-maps it; an explicit pick sticks.
    await admin_client.post(
        f"/admin/homework/{hw_id}/edit", data={"profile_id": riley, "subject": "History", "title": "Sheet"}
    )
    assert await _rows(db, "SELECT subject_key FROM homework") == [("topic",)]
    await admin_client.post(
        f"/admin/homework/{hw_id}/edit",
        data={"profile_id": riley, "subject": "History", "subject_key": "reading", "title": "Sheet"},
    )
    assert await _rows(db, "SELECT subject_key FROM homework") == [("reading",)]
    page = (await admin_client.get("/admin?tab=family")).text
    assert '<option value="reading" selected>Icon: Reading</option>' in page


# --- Reading log ---


@pytest.fixture
async def readers(db):
    """Riley and Jamie have year groups (so they're readers); Mum and Dad are parents."""
    await db.execute("UPDATE profiles SET school_year = 'Year 4' WHERE name = 'Riley'")
    await db.execute("UPDATE profiles SET school_year = 'Year 2' WHERE name = 'Jamie'")
    await db.commit()
    await _parents(db, "Mum", "Dad")


async def test_seeded_profiles_without_year_groups_get_no_reading_row(db, client, admin_client, today):
    """A fresh install: every profile is is_parent = 0 and none has a year."""
    html = (await client.get("/widgets/homework")).text
    assert "rd-row" not in html and "Read tonight" not in html
    assert (await client.post(f"/api/reading/{(await _ids(db))['Riley']}/toggle")).status_code == 404
    page = (await admin_client.get("/admin?tab=family")).text
    assert "Set a year group for each child in Family Members to start the reading log." in page
    assert 'class="rd-grid"' not in page


async def test_only_non_parents_with_a_year_group_are_readers(db, client, today):
    ids = await _ids(db)
    await db.execute("UPDATE profiles SET school_year = 'Year 4' WHERE name IN ('Riley', 'Mum')")
    await db.execute("UPDATE profiles SET school_year = '   ' WHERE name = 'Jamie'")  # blank counts as none
    await db.commit()
    await _parents(db, "Mum")
    html = (await client.get("/widgets/homework")).text
    assert 'aria-label="Riley read tonight"' in html
    assert "Mum read tonight" not in html and "Jamie read tonight" not in html and "Dad read tonight" not in html
    assert (await client.post(f"/api/reading/{ids['Mum']}/toggle")).status_code == 404
    assert (await client.post(f"/api/reading/{ids['Dad']}/toggle")).status_code == 404


async def test_reading_tick_on_and_off_for_children_only(db, client, today, readers):
    ids = await _ids(db)
    html = (await client.get("/widgets/homework")).text
    assert "Read tonight" in html and ">Riley<" in html and ">Jamie<" in html
    assert ">Mum<" not in html and ">Dad<" not in html

    resp = await client.post(f"/api/reading/{ids['Riley']}/toggle")
    assert resp.status_code == 200 and 'id="widget-homework"' in resp.text
    assert 'aria-label="Riley read tonight" aria-pressed="true"' in resp.text
    assert 'aria-label="Jamie read tonight" aria-pressed="false"' in resp.text
    assert await _rows(db, "SELECT profile_id, read_on FROM reading_log") == [(ids["Riley"], "2026-03-11")]

    resp = await client.post(f"/api/reading/{ids['Riley']}/toggle")
    assert 'aria-label="Riley read tonight" aria-pressed="false"' in resp.text
    assert await _rows(db, "SELECT * FROM reading_log") == []

    assert (await client.post(f"/api/reading/{ids['Mum']}/toggle")).status_code == 404
    assert (await client.post("/api/reading/9999/toggle")).status_code == 404
    assert await _rows(db, "SELECT * FROM reading_log") == []


async def test_reading_row_shows_without_homework(db, client, today, readers):
    html = (await client.get("/widgets/homework")).text
    assert "Read tonight" in html and "No homework right now" in html


async def test_reading_day_rolls_over_in_the_family_timezone(db, client, monkeypatch, readers):
    """23:30 UTC on 10 June is 00:30 on the 11th in London (BST): a tick then
    is the 11th's, and the 10th's tick no longer shows as today's."""
    riley = (await _ids(db))["Riley"]
    _frozen_now(monkeypatch, database, datetime(2026, 6, 10, 22, 30, tzinfo=UTC))  # 23:30 BST
    await client.post(f"/api/reading/{riley}/toggle")
    assert 'aria-pressed="true"' in (await client.get("/widgets/homework")).text

    _frozen_now(monkeypatch, database, datetime(2026, 6, 10, 23, 30, tzinfo=UTC))  # 00:30 BST, the 11th
    html = (await client.get("/widgets/homework")).text
    assert 'aria-label="Riley read tonight" aria-pressed="false"' in html
    await client.post(f"/api/reading/{riley}/toggle")
    assert await _rows(db, "SELECT read_on FROM reading_log ORDER BY read_on") == [("2026-06-10",), ("2026-06-11",)]


async def test_rev_changes_when_reading_is_ticked(db, client, readers):
    riley = (await _ids(db))["Riley"]
    before = (await client.get("/api/rev")).json()["widgets"]
    await client.post(f"/api/reading/{riley}/toggle")
    after = (await client.get("/api/rev")).json()["widgets"]
    assert sorted(k for k in before if before[k] != after[k]) == ["homework"]
    assert "homework" in freshness.REFRESHED


async def test_reading_history_grid(db, admin_client, today, readers):
    ids = await _ids(db)
    await _parents(db, "Mum", "Dad")
    for day in ("2026-02-16", "2026-03-09", "2026-03-11", "2026-02-15"):  # the last is before the grid
        await db.execute("INSERT INTO reading_log (profile_id, read_on) VALUES (?, ?)", (ids["Riley"], day))
    await db.execute("INSERT INTO reading_log (profile_id, read_on) VALUES (?, '2026-03-10')", (ids["Jamie"],))
    await db.commit()

    history = await homework.get_reading_history(db, TODAY)
    assert [c["name"] for c in history["children"]] == ["Riley", "Jamie"]
    riley = history["children"][0]
    assert len(riley["weeks"]) == 4 and all(len(w) == 7 for w in riley["weeks"])
    cells = [c for week in riley["weeks"] for c in week]
    assert cells[0]["date"] == "2026-02-16" and cells[-1]["date"] == "2026-03-15"  # Mon .. Sun
    assert [c["date"] for c in cells if c["read"]] == ["2026-02-16", "2026-03-09", "2026-03-11"]
    assert [c["date"] for c in cells if c["future"]] == ["2026-03-12", "2026-03-13", "2026-03-14", "2026-03-15"]
    assert [c["date"] for c in cells if c["today"]] == ["2026-03-11"]
    # With no term dates, weekdays are school days and weekends aren't.
    assert [c["school_day"] for c in riley["weeks"][0]] == [True] * 5 + [False] * 2
    jamie = [c["date"] for week in history["children"][1]["weeks"] for c in week if c["read"]]
    assert jamie == ["2026-03-10"]

    page = (await admin_client.get("/admin?tab=family")).text
    assert 'id="reading"' in page and "Reading Log" in page
    assert page.count('class="rd-grid"') == 2
    assert 'title="Wed 11 Mar: read"' in page


# --- Done history ---


async def test_done_history_records_each_tick_with_family_time(db, client, admin_client, monkeypatch, today):
    riley = (await _ids(db))["Riley"]
    await admin_client.post("/admin/homework", data={"profile_id": riley, "subject": "Maths", "title": "Sheet"})
    hw_id = (await _rows(db, "SELECT id FROM homework"))[0][0]

    _frozen_now(monkeypatch, homework, datetime(2026, 3, 11, 16, 40, tzinfo=UTC))  # GMT: 16:40 local
    await client.post(f"/api/homework/{hw_id}/toggle")
    _frozen_now(monkeypatch, homework, datetime(2026, 3, 11, 16, 45, 30, tzinfo=UTC))
    await client.post(f"/api/homework/{hw_id}/toggle")  # untick
    _frozen_now(monkeypatch, homework, datetime(2026, 3, 11, 19, 5, tzinfo=UTC))
    await client.post(f"/api/homework/{hw_id}/toggle")

    assert await _rows(db, "SELECT done, at FROM homework_events ORDER BY id") == [
        (1, "2026-03-11T16:40:00+00:00"),
        (0, "2026-03-11T16:45:30+00:00"),
        (1, "2026-03-11T19:05:00+00:00"),
    ]
    assert await _rows(db, "SELECT done, done_at FROM homework") == [(1, "2026-03-11T19:05:00+00:00")]

    page = (await admin_client.get("/admin?tab=family")).text
    assert "done Wed 11 Mar, 19:05" in page  # on the item
    assert "Recently ticked" in page
    events = await homework.get_recent_homework_events(db)
    assert [(e["when"], e["done"]) for e in events] == [
        ("Wed 11 Mar, 19:05", 1),
        ("Wed 11 Mar, 16:45", 0),
        ("Wed 11 Mar, 16:40", 1),
    ]
    assert "Riley unticked" in page and "Riley ticked" in page


async def test_done_history_keeps_the_family_offset(db, client, monkeypatch, today):
    """In summer London is UTC+1: the stored time is local, with its offset."""
    riley = (await _ids(db))["Riley"]
    await db.execute("INSERT INTO homework (profile_id, title, subject_key) VALUES (?, 'Sheet', 'maths')", (riley,))
    await db.commit()
    _frozen_now(monkeypatch, homework, datetime(2026, 6, 10, 15, 40, tzinfo=UTC))
    await client.post("/api/homework/1/toggle")
    assert await _rows(db, "SELECT at FROM homework_events") == [("2026-06-10T16:40:00+01:00",)]
    assert homework.event_time_label("2026-06-10T16:40:00+01:00") == "Wed 10 Jun, 16:40"


async def test_racing_taps_log_the_state_each_left(db, today):
    """Two taps at once flip twice and log done, then undone: never two "done"s."""
    import asyncio

    riley = (await _ids(db))["Riley"]
    await db.execute("INSERT INTO homework (profile_id, title) VALUES (?, 'Sheet')", (riley,))
    await db.commit()

    async def tap():
        async with database.get_db() as conn:
            return await homework.toggle_homework(conn, 1)

    assert await asyncio.gather(tap(), tap()) == [True, True]
    assert await _rows(db, "SELECT done FROM homework_events ORDER BY id") == [(1,), (0,)]
    assert await _rows(db, "SELECT done, done_at FROM homework") == [(0, None)]


async def test_recent_events_are_newest_first_across_a_clock_change(db, today):
    """Ordered by insertion, not by the ISO text: 01:30+01:00 (BST) is before
    01:10+00:00 (GMT) on the night the clocks go back, though it sorts after."""
    riley = (await _ids(db))["Riley"]
    await db.execute("INSERT INTO homework (profile_id, title) VALUES (?, 'Sheet')", (riley,))
    await db.executemany(
        "INSERT INTO homework_events (homework_id, done, at) VALUES (1, ?, ?)",
        [(1, "2026-10-25T01:30:00+01:00"), (0, "2026-10-25T01:10:00+00:00")],
    )
    await db.commit()
    assert [e["done"] for e in await homework.get_recent_homework_events(db)] == [0, 1]


async def test_deleting_homework_deletes_its_history(db, client, admin_client, today):
    riley = (await _ids(db))["Riley"]
    await admin_client.post("/admin/homework", data={"profile_id": riley, "title": "Sheet"})
    await client.post("/api/homework/1/toggle")
    await admin_client.post("/admin/homework/1/delete")
    assert await _rows(db, "SELECT * FROM homework_events") == []


# --- Auth ---


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/admin?tab=family"),
        ("post", "/admin/homework"),
        ("post", "/admin/homework/1/edit"),
        ("post", "/admin/homework/1/archive"),
        ("post", "/admin/homework/1/delete"),
    ],
)
async def test_admin_homework_routes_require_admin(db, client, method, path):
    await db.execute("INSERT INTO homework (profile_id, title) VALUES (3, 'Sheet')")
    await db.commit()
    resp = await getattr(client, method)(
        path, **({"data": {"profile_id": 3, "title": "X", "subject_key": "maths"}} if method == "post" else {})
    )
    assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login")
    assert await _rows(db, "SELECT title, subject_key, archived FROM homework") == [("Sheet", "other", 0)]


async def test_kiosk_taps_need_no_pin(db, client, today, readers):
    riley = (await _ids(db))["Riley"]
    await db.execute("INSERT INTO homework (profile_id, title) VALUES (?, 'Sheet')", (riley,))
    await db.commit()
    assert (await client.post(f"/api/reading/{riley}/toggle")).status_code == 200
    assert (await client.post("/api/homework/1/toggle")).status_code == 200


async def test_family_tab_walk(db, admin_client, today):
    """Every section of the Family tab renders, and the reading log is one."""
    page = await admin_client.get("/admin?tab=family")
    assert page.status_code == 200
    for section in ("family", "tasks", "homework", "reading", "practice-words"):
        assert f'id="{section}"' in page.text
