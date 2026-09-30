"""Schools per child (spec 11.2, app/services/schools.py): migration 10,
per-school term dates and school days, senders, and Admin."""

import json
import sqlite3
from datetime import date

import pytest

from app import database, google_calendar, migrations, school_email
from app.routers import admin
from app.security import create_session_token
from app.services import countdowns, imports, schools, tasks, term_dates

MUM, DAD, RILEY, JAMIE = 1, 2, 3, 4
GRESHAM = 1  # made by migration 10 on every fresh database
MON = date(2026, 10, 26)  # Gresham's half term in these tests; the nursery is open


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


async def link(db, profile_id, school_id):
    await schools.set_profile_school(db, profile_id, school_id)
    await db.commit()


@pytest.fixture
async def two_schools(db):
    """Gresham (Riley) with an autumn term and a half term; a nursery
    (Jamie) with a term straight through."""
    nursery = await schools.add(db, "Little Acorns")
    await term_dates.add_period(db, "term", "2026-09-02", "2026-12-18", "Autumn term", GRESHAM)
    await term_dates.add_period(db, "half_term", "2026-10-26", "2026-10-30", "", GRESHAM)
    await term_dates.add_period(db, "term", "2026-09-07", "2026-12-16", "Autumn", nursery)
    await link(db, RILEY, GRESHAM)
    await link(db, JAMIE, nursery)
    return nursery


# --- Migration 10 ---


async def _before_schools(tmp_path, monkeypatch, setup_sql):
    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(migrations, "MIGRATIONS", [m for m in migrations.MIGRATIONS if m.version < 10])
    await database.init_db()
    with sqlite3.connect(path) as conn:
        conn.executescript(setup_sql)
    monkeypatch.undo()
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    await database.init_db()  # and again: nothing more happens
    return path


async def test_migration_10_moves_term_dates_and_senders_onto_gresham(tmp_path, monkeypatch):
    path = await _before_schools(
        tmp_path,
        monkeypatch,
        """UPDATE profiles SET school_year = 'Year 4' WHERE name = 'Riley';
           INSERT INTO school_periods (kind, start_date, end_date, label) VALUES
               ('term', '2026-09-02', '2026-12-18', 'Autumn term'),
               ('half_term', '2026-10-26', '2026-10-30', 'Half term');
           INSERT INTO app_settings (key, value) VALUES ('school_email_senders', 'office@example.sch.uk');""",
    )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id, name, senders FROM schools").fetchall() == [
            (1, "Gresham", "office@example.sch.uk")
        ]
        assert conn.execute("SELECT DISTINCT school_id FROM school_periods").fetchall() == [(1,)]
        assert conn.execute("SELECT name, school_id FROM profiles ORDER BY id").fetchall() == [
            ("Mum", None),
            ("Dad", None),
            ("Riley", 1),
            ("Jamie", None),
        ]
        assert conn.execute("SELECT 1 FROM app_settings WHERE key = 'school_email_senders'").fetchone() is None


async def test_migration_10_uses_the_default_senders_when_none_were_saved(tmp_path, monkeypatch):
    await _before_schools(tmp_path, monkeypatch, "")
    async with database.get_db() as db:
        assert await school_email.get_senders(db) == ["office@greshamprimary.school", "*@gresham.croydon.sch.uk"]


# --- School days ---


async def test_each_child_follows_their_own_school(db, two_schools):
    assert not await term_dates.is_school_day(db, MON, GRESHAM)
    assert await term_dates.is_school_day(db, MON, two_schools)
    assert not await term_dates.person_school_day(db, MON, RILEY)
    assert await term_dates.person_school_day(db, MON, JAMIE)
    # A parent follows the family: some child has school today.
    assert await term_dates.person_school_day(db, MON, MUM)
    assert await term_dates.family_school_day(db, MON)
    # Saturday: nobody.
    assert not await term_dates.family_school_day(db, date(2026, 10, 24))


async def test_the_family_day_ignores_a_school_no_child_goes_to(db, two_schools):
    await link(db, JAMIE, None)
    assert not await term_dates.family_school_day(db, MON)  # only Riley's school counts
    assert not await term_dates.person_school_day(db, MON, JAMIE)  # no school: the family's


async def test_with_no_one_linked_every_school_counts_and_with_none_mon_to_fri(db):
    await term_dates.add_period(db, "term", "2026-09-02", "2026-12-18", "Autumn term")
    await term_dates.add_period(db, "half_term", "2026-10-26", "2026-10-30")
    assert not await term_dates.family_school_day(db, MON)  # Gresham's half term, nobody linked
    await schools.delete(db, GRESHAM)
    assert await term_dates.family_school_day(db, MON)  # a Monday, no bank holiday


async def test_school_days_chores_follow_their_owners_school(db, two_schools):
    for profile_id, title in ((RILEY, "Book bag"), (JAMIE, "Nursery bag"), (MUM, "Pack lunches")):
        await db.execute(
            """INSERT INTO tasks (title, profile_id, is_recurring, recurrence_rule, created_at, updated_at)
               VALUES (?, ?, 1, 'school', datetime('now'), datetime('now'))""",
            (title, profile_id),
        )
    await db.commit()
    shown = {t["title"] for p in await tasks.get_profiles_with_tasks(db, MON) for t in p["tasks"]}
    assert shown == {"Nursery bag", "Pack lunches"}


async def test_overlaps_are_only_checked_within_a_school(db, two_schools):
    # The nursery's own half term may overlap Gresham's term; a second
    # term over its own autumn term can't.
    await term_dates.add_period(db, "half_term", "2026-10-19", "2026-10-23", "", two_schools)
    with pytest.raises(term_dates.PeriodError, match="term-overlap"):
        await term_dates.add_period(db, "term", "2026-11-02", "2026-11-20", "", two_schools)


async def test_calendar_and_countdowns_name_the_school_once_there_are_two(db, two_schools):
    await term_dates.add_period(db, "half_term", "2026-10-19", "2026-10-23", "", two_schools)
    periods = await term_dates.periods_between(db, date(2026, 10, 19), date(2026, 10, 26))
    labels = [term_dates.with_school(p, p["label"]) for p in periods if p["kind"] == "half_term"]
    assert labels == ["Little Acorns: Half term", "Gresham: Half term"]
    items = await countdowns.upcoming(db, date(2026, 10, 5))
    assert [(c["title"], c["days"]) for c in items] == [("Little Acorns: Half term", 14), ("Gresham: Half term", 21)]


async def test_one_school_reads_as_before(db):
    await term_dates.add_period(db, "half_term", "2026-10-26", "2026-10-30")
    [period] = await term_dates.periods_between(db, MON, MON)
    assert period["school"] is None and term_dates.with_school(period, "Half term") == "Half term"


# --- School email and the inbox ---


async def test_senders_are_every_schools_and_an_email_knows_its_school(db, two_schools):
    await schools.update(db, two_schools, "Little Acorns", "hello@acorns.example\n*@gresham.croydon.sch.uk")
    assert await school_email.get_senders(db) == [
        "office@greshamprimary.school",
        "*@gresham.croydon.sch.uk",
        "hello@acorns.example",
    ]
    assert await schools.for_sender(db, "Hello+news@Acorns.example") == two_schools
    assert await schools.for_sender(db, "head@gresham.croydon.sch.uk") == GRESHAM  # the oldest listing it
    assert await schools.for_sender(db, "someone@else.example") is None


async def test_a_schools_email_is_about_its_children(db, two_schools):
    assert [c.name for c in await imports.get_children(db, two_schools)] == ["Jamie"]
    assert [c.name for c in await imports.get_children(db, GRESHAM)] == ["Riley"]
    empty = await schools.add(db, "Nobody's school")
    assert [c.name for c in await imports.get_children(db, empty)] == ["Mum", "Dad", "Riley", "Jamie"]


async def _term_candidate(db, school_id, ref):
    cursor = await db.execute(
        "INSERT INTO import_sources (kind, source_ref, status) VALUES ('upload', ?, 'extracted')", (ref,)
    )
    payload = {"periods": [{"kind": "half_term", "start_date": "2026-10-19", "end_date": "2026-10-23"}]}
    payload["school_id"] = school_id
    cursor = await db.execute(
        "INSERT INTO import_candidates (source_id, kind, payload_json) VALUES (?, 'term_dates', ?)",
        (cursor.lastrowid, json.dumps(payload)),
    )
    await db.commit()
    return cursor.lastrowid


async def test_term_dates_from_the_inbox_go_to_their_school(db, two_schools):
    candidate = await _term_candidate(db, two_schools, "letter-1")
    row = {"kind": "half_term", "start_date": "2026-10-19", "end_date": "2026-10-23", "label": ""}
    assert await imports.approve_term_dates(db, candidate, [row]) == 1
    assert [p["start_date"] for p in await term_dates.list_periods(db, two_schools)][-1] == "2026-10-19"
    assert "2026-10-19" not in [p["start_date"] for p in await term_dates.list_periods(db, GRESHAM)]

    # Read for a school since deleted: the oldest school.
    gone = await schools.add(db, "Closed school")
    candidate = await _term_candidate(db, gone, "letter-3")
    await schools.delete(db, gone)
    row3 = row | {"start_date": "2026-11-09", "end_date": "2026-11-09", "kind": "inset"}
    await imports.approve_term_dates(db, candidate, [row3])
    assert "2026-11-09" in [p["start_date"] for p in await term_dates.list_periods(db, GRESHAM)]

    # The inbox can send them elsewhere.
    candidate = await _term_candidate(db, two_schools, "letter-2")
    await imports.approve_term_dates(db, candidate, [row], GRESHAM)
    assert "2026-10-19" in [p["start_date"] for p in await term_dates.list_periods(db, GRESHAM)]


# --- Admin ---


async def test_admin_adds_edits_and_deletes_a_school(db, admin_client):
    resp = await admin_client.post("/admin/schools", data={"name": "  Little   Acorns "})
    assert resp.status_code == 303 and resp.headers["location"].endswith("tab=school#schools")
    nursery = (await schools.list_schools(db))[-1]
    assert nursery["name"] == "Little Acorns"

    resp = await admin_client.post(
        f"/admin/schools/{nursery['id']}/edit", data={"name": "Acorns", "senders": "hello@acorns.example"}
    )
    assert resp.status_code == 303
    assert (await schools.list_schools(db))[-1] | {"id": 0} == {
        "id": 0,
        "name": "Acorns",
        "senders": ["hello@acorns.example"],
        "children": [],
    }

    resp = await admin_client.post(f"/admin/schools/{nursery['id']}/edit", data={"name": "Acorns", "senders": "nope"})
    assert resp.headers["location"].endswith("error=school-list-senders#schools")
    resp = await admin_client.post("/admin/schools", data={"name": ""})
    assert resp.headers["location"].endswith("error=school-name#schools")
    resp = await admin_client.post("/admin/schools/999/edit", data={"name": "Ghost"})
    assert resp.headers["location"].endswith("error=school-missing#schools")

    page = (await admin_client.get("/admin?tab=school")).text
    assert 'id="schools"' in page and "Acorns" in page and "Email from hello@acorns.example" in page
    assert '<select name="school_id" required>' in page  # two schools: term dates ask which

    await term_dates.add_period(db, "term", "2026-09-07", "2026-12-16", "", nursery["id"])
    await link(db, JAMIE, nursery["id"])
    await admin_client.post(f"/admin/schools/{nursery['id']}/delete")
    assert [s["name"] for s in await schools.list_schools(db)] == ["Gresham"]
    assert await schools.of_profile(db, JAMIE) is None
    (count,) = await (await db.execute("SELECT COUNT(*) FROM school_periods")).fetchone()
    assert count == 0


async def test_admin_links_a_child_to_a_school(db, admin_client):
    resp = await admin_client.post(f"/admin/profiles/{RILEY}/details", data={"school_year": "Year 4", "school_id": "1"})
    assert resp.status_code == 303 and await schools.of_profile(db, RILEY) == GRESHAM
    assert '<option value="1" selected>Gresham</option>' in (await admin_client.get("/admin?tab=family")).text

    resp = await admin_client.post(f"/admin/profiles/{RILEY}/details", data={"school_id": "42"})
    assert resp.headers["location"].endswith("error=profile-school#family")
    assert await schools.of_profile(db, RILEY) == GRESHAM

    await admin_client.post(f"/admin/profiles/{RILEY}/details", data={"school_year": "Year 4"})
    assert await schools.of_profile(db, RILEY) is None


async def test_admin_term_dates_need_a_school(db, admin_client, two_schools):
    form = {"kind": "inset", "start_date": "2026-11-02", "end_date": "2026-11-02", "school_id": str(two_schools)}
    assert (await admin_client.post("/admin/term-dates", data=form)).status_code == 303
    assert [p["kind"] for p in await term_dates.list_periods(db, two_schools)] == ["term", "inset"]

    resp = await admin_client.post("/admin/term-dates", data=form | {"school_id": "77"})
    assert resp.status_code == 400 and "Pick which school these dates are for" in resp.text

    page = (await admin_client.get("/admin?tab=school")).text
    assert '<h3 class="term-school">Little Acorns</h3>' in page


async def test_admin_exclusions_are_saved_on_their_own(db, admin_client):
    resp = await admin_client.post("/admin/school-email/exclusions", data={"exclusions": "pta@example.org"})
    assert resp.status_code == 303 and resp.headers["location"].endswith("#school-email")
    assert await school_email.get_exclusions(db) == ["pta@example.org"]
    assert await school_email.get_senders(db)  # the schools' senders are untouched


async def test_two_schools_starting_the_same_day_both_show_on_the_calendar(db, two_schools):
    await term_dates.add_period(db, "term", "2027-01-05", "2027-03-26", "Spring", GRESHAM)
    await term_dates.add_period(db, "term", "2027-01-05", "2027-03-24", "Spring", two_schools)
    _, markers = await google_calendar._school_periods(db, date(2027, 1, 1), date(2027, 1, 31))
    assert markers["2027-01-05"] == "Gresham: Term starts · Little Acorns: Term starts"
