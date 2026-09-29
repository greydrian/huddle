"""School term dates (spec 10.6): the service (school days, missing years),
migration 3, the GOV.UK bank holidays job, Admin's Term dates panel, the
School inbox's term-dates candidate, and the calendar's local-only bars."""

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from app import bank_holidays, database, http_client, migrations
from app.admin_tabs import admin_url
from app.routers import admin
from app.security import create_session_token
from app.services import extraction, imports, term_dates

TODAY = date(2026, 9, 29)  # a Tuesday, early in 2026–27
EVENTS_URL_PATTERN = r"https://www\.googleapis\.com/calendar/v3/calendars/.+/events"

# Gresham-style 2026–27 autumn and summer terms (spring left out on purpose).
AUTUMN = ("term", "2026-09-03", "2026-12-18", "Autumn term")
HALF_TERM = ("half_term", "2026-10-26", "2026-10-30", "October half term")
INSET = ("inset", "2026-11-13", "2026-11-13", "INSET day")
CHRISTMAS = ("holiday", "2026-12-19", "2027-01-04", "Christmas holidays")
SUMMER = ("term", "2027-04-19", "2027-07-21", "Summer term")


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


async def _add(db, *periods, source="manual"):
    for kind, start, end, label in periods:
        await term_dates.insert_period(db, term_dates.clean_period(kind, start, end, label), source)
    await db.commit()


async def _bank(db, *days):
    await db.executemany("INSERT INTO bank_holidays (date, title) VALUES (?, ?)", [(d, "Bank holiday") for d in days])
    await db.commit()


async def _rows(db, sql, *args):
    return [dict(r) for r in await (await db.execute(sql, args)).fetchall()]


@pytest.fixture
async def school_year(db):
    await _add(db, AUTUMN, HALF_TERM, INSET, CHRISTMAS, SUMMER)
    await _bank(db, "2026-12-25", "2027-05-03", "2025-12-25")


# --- is_school_day / school_day_status ---

@pytest.mark.parametrize(("day", "expected"), [
    ("2026-10-07", True),    # a Wednesday in the autumn term
    ("2026-09-03", True),    # first day of term (inclusive)
    ("2026-12-18", True),    # last day of term (inclusive)
    ("2026-09-02", False),   # the day before term starts
    ("2026-10-10", False),   # Saturday in term
    ("2026-10-28", False),   # half term, inside the term
    ("2026-11-13", False),   # INSET day
    ("2026-12-22", False),   # Christmas holidays
    ("2027-05-03", False),   # bank holiday Monday in the summer term
    ("2027-05-04", True),
    ("2027-02-10", False),   # the spring term isn't entered: not a school day this year
])
async def test_school_days_follow_the_term_dates(db, school_year, day, expected):
    assert await term_dates.school_day_status(db, date.fromisoformat(day)) == (expected, term_dates.TERM_DATES)
    assert await term_dates.is_school_day(db, date.fromisoformat(day)) is expected


@pytest.mark.parametrize(("day", "expected"), [
    ("2025-10-07", True),    # 2025–26 has no term dates: Mon–Fri...
    ("2025-12-25", False),   # ...minus bank holidays
    ("2025-10-11", False),   # and weekends
    ("2027-10-06", True),    # 2027–28 has none either
])
async def test_years_without_term_dates_fall_back_to_weekdays(db, school_year, day, expected):
    assert await term_dates.school_day_status(db, date.fromisoformat(day)) == (expected, term_dates.FALLBACK)


async def test_an_empty_database_falls_back(db):
    assert await term_dates.school_day_status(db, TODAY) == (True, term_dates.FALLBACK)
    await _bank(db, TODAY.isoformat())
    assert await term_dates.school_day_status(db, TODAY) == (False, term_dates.FALLBACK)


async def test_the_fallback_still_honours_entered_days_off(db, school_year):
    """2026–27's summer holiday runs into September 2027, and 2027–28 has no
    terms yet: its first days are still holiday, not fallback school days."""
    await _add(db, ("holiday", "2027-07-22", "2027-09-02", "Summer holidays"),
               ("inset", "2027-09-03", "2027-09-03", "INSET day"))
    assert await term_dates.school_day_status(db, date(2027, 9, 1)) == (False, term_dates.FALLBACK)
    assert await term_dates.school_day_status(db, date(2027, 9, 2)) == (False, term_dates.FALLBACK)
    assert await term_dates.school_day_status(db, date(2027, 9, 3)) == (False, term_dates.FALLBACK)
    assert await term_dates.school_day_status(db, date(2027, 9, 6)) == (True, term_dates.FALLBACK)


async def test_a_closure_inside_a_term_is_not_a_school_day(db, school_year):
    await _add(db, ("closure", "2026-10-07", "2026-10-07", "Polling station"))
    assert await term_dates.school_day_status(db, date(2026, 10, 7)) == (False, term_dates.TERM_DATES)


# --- missing_years ---

async def test_missing_years(db):
    assert await term_dates.missing_years(db, TODAY) == ["2026–27"]
    assert await term_dates.missing_years(db, date(2027, 6, 1)) == ["2026–27", "2027–28"]  # next year, from May
    await _add(db, AUTUMN)
    assert await term_dates.missing_years(db, TODAY) == []
    assert await term_dates.missing_years(db, date(2027, 3, 1)) == []  # next year isn't asked for yet
    assert await term_dates.missing_years(db, date(2027, 5, 1)) == ["2027–28"]
    assert await term_dates.missing_years(db, date(2027, 9, 1)) == ["2027–28"]  # now the current year
    # Holidays alone don't count as term dates.
    await _add(db, ("holiday", "2027-12-20", "2028-01-03", "Christmas"))
    assert await term_dates.missing_years(db, date(2027, 9, 1)) == ["2027–28"]


async def test_incomplete_years(db):
    assert await term_dates.incomplete_years(db, TODAY) == []  # nothing entered: missing_years() says so
    await _add(db, AUTUMN)
    assert await term_dates.incomplete_years(db, TODAY) == [
        "2026–27 has only 'Autumn term': add the rest of the year's terms."]
    await _add(db, ("term", "2027-01-11", "2027-03-26", "Spring term"), SUMMER)
    # Christmas isn't entered: 21 Dec–8 Jan fall in no term and no holiday.
    assert await term_dates.incomplete_years(db, TODAY) == [
        "2026–27 has a gap between terms that no holiday covers (21 Dec–8 Jan): add the missing holiday or term."]
    await _add(db, ("holiday", "2026-12-19", "2027-01-08", "Christmas holidays"))
    # Still Easter: 29 Mar–16 Apr (Good Friday and Easter Monday are bank holidays, so skipped).
    await _bank(db, "2027-03-26", "2027-03-29")
    [warning] = await term_dates.incomplete_years(db, TODAY)
    assert "(30 Mar–16 Apr)" in warning
    await _add(db, ("holiday", "2027-03-27", "2027-04-18", "Easter holidays"))
    assert await term_dates.incomplete_years(db, TODAY) == []
    # The school-day answer is unchanged by the warning: a gap day is simply not a school day.
    assert await term_dates.school_day_status(db, date(2027, 7, 26)) == (False, term_dates.TERM_DATES)


async def test_admin_shows_the_incomplete_year_warning(db, admin_client, admin_today):
    await _add(db, AUTUMN)
    html = (await admin_client.get("/admin?tab=school")).text
    assert "2026–27 has only &#39;Autumn term&#39;: add the rest" in html
    assert "Term dates missing for 2026–27" not in html


def test_school_year_labels():
    assert term_dates.school_year_of(date(2026, 9, 1)) == 2026
    assert term_dates.school_year_of(date(2026, 8, 31)) == 2025
    assert term_dates.school_year_label(2026) == "2026–27"
    assert term_dates.school_year_label(2099) == "2099–00"


async def test_periods_between_includes_bank_holidays(db, school_year):
    periods = await term_dates.periods_between(db, date(2026, 12, 1), date(2026, 12, 31))
    assert [(p["kind"], p["start_date"], p["source"]) for p in periods] == [
        ("term", "2026-09-03", "manual"),
        ("holiday", "2026-12-19", "manual"),
        ("bank_holiday", "2026-12-25", "bank_holiday"),
    ]


# --- Validation ---

@pytest.mark.parametrize(("args", "code"), [
    (("term", "2026-12-18", "2026-09-03"), "term-dates"),       # end before start
    (("term", "2026-09-03", ""), "term-dates"),
    (("term", "2026-02-30", "2026-03-02"), "term-dates"),       # not a real date
    (("term", "3/9/2026", "2026-12-18"), "term-dates"),
    (("term", "1999-09-03", "1999-12-18"), "term-dates"),       # out of range
    (("lesson", "2026-09-03", "2026-09-04"), "term-kind"),
    (("bank_holiday", "2026-12-25", "2026-12-25"), "term-kind"),  # the feed's, not a parent's
    (("inset", "2026-11-13", "2026-11-18"), "term-length"),     # INSET: 5 days at most
    (("holiday", "2027-07-22", "2027-10-05"), "term-length"),   # holiday: 75 days at most
    (("term", "2026-09-01", "2026-12-30"), "term-length"),      # 121 days
    (("half_term", "2026-10-19", "2026-11-06"), "term-length"),
])
def test_clean_period_refuses(args, code):
    with pytest.raises(term_dates.PeriodError) as exc:
        term_dates.clean_period(*args)
    assert exc.value.code == code


def test_clean_period_tidies():
    assert term_dates.clean_period("inset", "2026-11-13", "2026-11-14", "  INSET\n days ") == {
        "kind": "inset", "start_date": "2026-11-13", "end_date": "2026-11-14", "label": "INSET days"}
    assert term_dates.clean_period("half_term", "2026-10-26", "2026-10-30")["label"] == "Half term"
    with pytest.raises(term_dates.PeriodError, match="term-label"):
        term_dates.clean_period("term", "2026-09-03", "2026-12-18", "x" * 81)


def test_which_overlaps_are_allowed():
    term = {"kind": "term", "start_date": "2026-09-03", "end_date": "2026-12-18"}
    inside = {"start_date": "2026-10-26", "end_date": "2026-10-30"}
    for kind in ("half_term", "inset", "closure"):
        assert term_dates.clash({**inside, "kind": kind}, [term]) is None
    for kind in ("term", "holiday"):
        assert term_dates.clash({**inside, "kind": kind}, [term]) == term
    half = {**inside, "kind": "half_term"}
    assert term_dates.clash({**inside, "kind": "inset"}, [half]) == half
    christmas = {"kind": "holiday", "start_date": "2026-12-19", "end_date": "2027-01-04"}
    assert term_dates.clash(christmas, [term]) is None
    # An INSET day may sit inside a holiday (letters often list both).
    assert term_dates.clash({"kind": "inset", "start_date": "2027-01-04", "end_date": "2027-01-04"}, [christmas]) is None
    assert term_dates.clash({"kind": "closure", "start_date": "2027-01-04", "end_date": "2027-01-04"},
                            [christmas]) == christmas
    assert term_dates.clean_period("holiday", "2027-07-22", "2027-09-02")  # a long summer: 43 days, fine


# --- Migration 3 ---

async def test_migration_3_adds_the_tables_and_changes_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:2])
    await database.init_db()  # a database as it was before migration 3
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE profiles SET school_year = 'Year 4', is_parent = 1 WHERE name = 'Riley'")
        conn.execute("INSERT INTO homework (profile_id, subject, title) VALUES (3, 'Maths', 'Fractions')")
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('calendar_timezone', 'Europe/London')")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables if t != "schema_migrations"}
    assert "school_periods" not in tables and "bank_holidays" not in tables

    monkeypatch.undo()
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:3])  # later ones change other tables
    await database.init_db()
    await database.init_db()  # and again: a no-op

    with sqlite3.connect(path) as conn:
        after = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in before}
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert conn.execute("SELECT COUNT(*) FROM school_periods").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM bank_holidays").fetchone() == (0,)
    assert after == before
    assert versions == [m.version for m in migrations.MIGRATIONS]  # 3 and any later ones


# --- Bank holidays (GOV.UK) ---

def _feed(*days, division="england-and-wales"):
    events = [{"title": title, "date": day, "notes": "", "bunting": True} for day, title in days]
    return {
        division: {"division": division, "events": events},
        "scotland": {"division": "scotland", "events": [{"title": "St Andrew’s Day", "date": "2026-11-30"}]},
    }


async def test_bank_holidays_are_fetched_for_england_and_wales(db, google):
    route = google.get(bank_holidays.FEED_URL).respond(200, json=_feed(
        ("2026-12-25", "Christmas Day"), ("2026-12-28", "Boxing Day"), ("2027-01-01", "New Year’s Day")))

    assert await bank_holidays.refresh(db) is True
    assert route.call_count == 1
    rows = await _rows(db, "SELECT date, title FROM bank_holidays ORDER BY date")
    assert rows == [{"date": "2026-12-25", "title": "Christmas Day"}, {"date": "2026-12-28", "title": "Boxing Day"},
                    {"date": "2027-01-01", "title": "New Year’s Day"}]  # no St Andrew's Day
    status = await bank_holidays.status(db)
    assert status["count"] == 3 and status["stale"] is False and status["updated_at"] is not None

    # Idempotent by date; a date the feed drops (within its range) goes, the rest update in place.
    google.get(bank_holidays.FEED_URL).respond(200, json=_feed(
        ("2026-12-25", "Christmas Day"), ("2027-01-01", "New Year's Day")))
    assert await bank_holidays.refresh(db) is True
    rows = await _rows(db, "SELECT date, title FROM bank_holidays ORDER BY date")
    assert rows == [{"date": "2026-12-25", "title": "Christmas Day"}, {"date": "2027-01-01", "title": "New Year's Day"}]


async def test_a_date_dropped_from_the_feed_is_deleted_within_its_range(db, google):
    await db.executemany("INSERT INTO bank_holidays (date, title) VALUES (?, ?)", [
        ("2019-05-06", "Early May bank holiday"),   # older than the feed now covers: kept
        ("2026-05-08", "VE Day (moved)"),          # inside the feed's range, no longer listed: deleted
        ("2026-12-25", "Christmas Day"),
    ])
    await db.commit()
    google.get(bank_holidays.FEED_URL).respond(200, json=_feed(
        ("2026-01-01", "New Year’s Day"), ("2026-12-25", "Christmas Day"), ("2027-01-01", "New Year’s Day")))
    assert await bank_holidays.refresh(db) is True
    assert [r["date"] for r in await _rows(db, "SELECT date FROM bank_holidays ORDER BY date")] == [
        "2019-05-06", "2026-01-01", "2026-12-25", "2027-01-01"]


@pytest.mark.parametrize("reply", [
    httpx.Response(503),
    httpx.Response(200, json={"scotland": {"events": []}}),   # no England and Wales
    httpx.Response(200, json={"england-and-wales": {"events": []}}),
    httpx.Response(200, text="<html>not json</html>"),
])
async def test_a_failed_fetch_keeps_the_old_data_and_logs_once(db, google, caplog, reply):
    await _bank(db, "2026-12-25")
    await database.set_setting(db, bank_holidays.UPDATED_SETTING, "2026-01-01T00:00:00+00:00")
    await db.commit()
    google.get(bank_holidays.FEED_URL).mock(return_value=reply)

    with caplog.at_level(logging.INFO, logger="app.bank_holidays"):
        assert await bank_holidays.refresh(db) is False
        assert await bank_holidays.refresh(db) is False
    warnings = [r for r in caplog.records if r.name == "app.bank_holidays" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "gov.uk" not in warnings[0].getMessage().lower()  # no URL, just a code
    assert await _rows(db, "SELECT date FROM bank_holidays") == [{"date": "2026-12-25"}]
    assert await database.get_setting(db, bank_holidays.UPDATED_SETTING) == "2026-01-01T00:00:00+00:00"
    assert (await bank_holidays.status(db))["stale"] is True


async def test_a_network_outage_then_recovery(db, google, caplog):
    google.get(bank_holidays.FEED_URL).mock(side_effect=[
        httpx.ConnectError("offline"), httpx.ConnectError("offline"),
        httpx.Response(200, json=_feed(("2026-12-25", "Christmas Day"))),
    ])
    with caplog.at_level(logging.INFO, logger="app.bank_holidays"):
        for _ in range(3):
            await bank_holidays.refresh(db)
    records = [r for r in caplog.records if r.name == "app.bank_holidays"]
    assert [r.levelno for r in records].count(logging.WARNING) == 1
    assert any("recovered" in r.getMessage() for r in records)
    assert len(await _rows(db, "SELECT * FROM bank_holidays")) == 1


async def test_run_if_due_fetches_when_missing_or_stale_only(db, google):
    route = google.get(bank_holidays.FEED_URL).respond(200, json=_feed(("2026-12-25", "Christmas Day")))

    assert await bank_holidays.run_if_due() is True   # nothing stored yet
    assert await bank_holidays.run_if_due() is False  # fresh
    assert route.call_count == 1

    week_old = datetime.now(UTC) - bank_holidays.REFRESH_AFTER - timedelta(minutes=1)
    await database.set_setting(db, bank_holidays.UPDATED_SETTING, week_old.isoformat())
    await db.commit()
    assert await bank_holidays.run_if_due() is True  # weekly refresh
    assert route.call_count == 2

    await db.execute("DELETE FROM bank_holidays")  # a recent fetch, but the rows are gone
    await db.commit()
    assert await bank_holidays.run_if_due() is True
    assert route.call_count == 3


def test_the_job_is_scheduled_weekly_and_after_startup(monkeypatch):
    from app import scheduler

    jobs = {}
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda fn, *a, **kw: jobs.setdefault(kw["id"], (fn, a, kw)))
    monkeypatch.setattr(scheduler.scheduler, "start", lambda: None)
    scheduler.start()
    fn, args, kwargs = jobs["bank_holidays"]
    assert fn is bank_holidays.run_if_due
    assert args == ("interval",) and kwargs["seconds"] <= bank_holidays.REFRESH_AFTER.total_seconds()
    assert kwargs["next_run_time"] > datetime.now(UTC)


# --- Admin: Term dates panel ---

@pytest.fixture
def admin_today(monkeypatch):
    async def fake_today(db):
        return TODAY

    monkeypatch.setattr(admin, "family_today", fake_today)


async def test_admin_adds_edits_and_deletes_periods(db, admin_client, admin_today):
    html = (await admin_client.get("/admin?tab=school")).text
    assert 'id="term-dates"' in html
    assert "Term dates missing for 2026–27" in html
    assert "haven't been fetched from GOV.UK yet" in html

    r = await admin_client.post("/admin/term-dates", data={
        "kind": "term", "start_date": "2026-09-03", "end_date": "2026-12-18", "label": " Autumn term "})
    assert (r.status_code, r.headers["location"]) == (303, admin_url("term-dates"))
    r = await admin_client.post("/admin/term-dates", data={
        "kind": "half_term", "start_date": "2026-10-26", "end_date": "2026-10-30", "label": ""})
    assert r.status_code == 303  # a half term may sit inside a term
    rows = await _rows(db, "SELECT id, kind, start_date, end_date, label, source FROM school_periods ORDER BY id")
    assert [(r["kind"], r["label"], r["source"]) for r in rows] == [
        ("term", "Autumn term", "manual"), ("half_term", "Half term", "manual")]

    html = (await admin_client.get("/admin?tab=school")).text
    assert "Term dates missing" not in html
    assert "2026–27" in html and "Autumn term" in html and "2026-09-03 to 2026-12-18" in html

    half = rows[1]["id"]
    r = await admin_client.post(f"/admin/term-dates/{half}/edit", data={
        "kind": "half_term", "start_date": "2026-10-19", "end_date": "2026-10-23", "label": "Half term"})
    assert r.headers["location"] == admin_url("term-dates")
    assert (await term_dates.get_period(db, half))["start_date"] == "2026-10-19"

    r = await admin_client.post(f"/admin/term-dates/{half}/delete")
    assert r.headers["location"] == admin_url("term-dates")
    assert await term_dates.get_period(db, half) is None


@pytest.mark.parametrize(("data", "message"), [
    ({"kind": "term", "start_date": "2027-01-05", "end_date": "2027-01-04"}, "end on or after the start"),
    ({"kind": "inset", "start_date": "2027-01-04", "end_date": "2027-01-11"}, "too long for its kind"),
    ({"kind": "nope", "start_date": "2027-01-04", "end_date": "2027-01-04"}, "Pick what kind"),
    ({"kind": "term", "start_date": "2026-12-01", "end_date": "2027-02-01"},  # term over term
     "That clashes with &#39;Autumn term&#39; (3 Sep–18 Dec)"),
    ({"kind": "holiday", "start_date": "2026-12-10", "end_date": "2027-01-04"},
     "That clashes with &#39;Autumn term&#39; (3 Sep–18 Dec)"),
])
async def test_admin_refuses_bad_periods_and_keeps_what_was_typed(db, admin_client, admin_today, data, message):
    await _add(db, AUTUMN)
    r = await admin_client.post("/admin/term-dates", data={**data, "label": "Typed label"})
    assert r.status_code == 400
    assert message in r.text
    assert 'value="Typed label"' in r.text and f'value="{data["start_date"]}"' in r.text
    assert len(await _rows(db, "SELECT * FROM school_periods")) == 1


async def test_admin_edit_refuses_a_clash_and_a_missing_period(db, admin_client, admin_today):
    await _add(db, AUTUMN, SUMMER)
    summer = (await _rows(db, "SELECT id FROM school_periods WHERE label = 'Summer term'"))[0]["id"]
    r = await admin_client.post(f"/admin/term-dates/{summer}/edit", data={
        "kind": "term", "start_date": "2026-12-01", "end_date": "2027-03-01", "label": "Spring"})
    assert r.status_code == 400 and "That clashes with &#39;Autumn term&#39; (3 Sep–18 Dec)" in r.text
    assert (await term_dates.get_period(db, summer))["label"] == "Summer term"
    # Editing a period in place never clashes with itself.
    r = await admin_client.post(f"/admin/term-dates/{summer}/edit", data={
        "kind": "term", "start_date": "2027-04-20", "end_date": "2027-07-21", "label": "Summer term"})
    assert r.status_code == 303

    r = await admin_client.post("/admin/term-dates/999999/edit", data={
        "kind": "term", "start_date": "2027-04-20", "end_date": "2027-07-21"})
    assert r.headers["location"] == admin_url("term-dates", error="term-missing")


async def test_term_date_routes_need_admin(db, client):
    await _add(db, AUTUMN)
    period_id = (await _rows(db, "SELECT id FROM school_periods"))[0]["id"]
    before = await _rows(db, "SELECT * FROM school_periods")
    for url, data in (
        ("/admin/term-dates", {"kind": "inset", "start_date": "2026-11-13", "end_date": "2026-11-13"}),
        (f"/admin/term-dates/{period_id}/edit", {"kind": "term", "start_date": "2026-09-04", "end_date": "2026-12-18"}),
        (f"/admin/term-dates/{period_id}/delete", {}),
    ):
        r = await client.post(url, data=data)
        assert (r.status_code, r.headers["location"]) == (303, "/admin/login"), url
    assert await _rows(db, "SELECT * FROM school_periods") == before


async def test_admin_shows_when_bank_holidays_were_updated(db, admin_client, admin_today):
    await _bank(db, "2026-12-25")
    await database.set_setting(db, bank_holidays.UPDATED_SETTING, datetime.now(UTC).isoformat())
    await db.commit()
    html = (await admin_client.get("/admin?tab=school")).text
    local = datetime.now(UTC).astimezone(await database.family_timezone(db))
    assert f"Bank holidays (England and Wales) updated {local.day} {local:%b %Y}." in html

    await database.set_setting(db, bank_holidays.UPDATED_SETTING, "2026-01-01T00:00:00+00:00")
    await db.commit()
    html = (await admin_client.get("/admin?tab=school")).text
    assert "over a month ago" in html


# --- Extraction: the term_dates candidate ---

TERM_ITEMS = [
    {"kind": "term", "start_date": "2026-09-03", "end_date": "2026-12-18", "label": "Autumn term",
     "evidence": "Autumn term: Thursday 3 September – Friday 18 December"},
    {"kind": "half_term", "start_date": "2026-10-26", "end_date": "2026-10-30", "label": "Half term", "evidence": ""},
    {"kind": "inset", "start_date": "2026-11-13", "end_date": "2026-11-13", "label": "INSET day", "evidence": ""},
]


def _term_candidate(items, today=TODAY):
    found = extraction.validate_output({"term_dates": items}, [], today)
    return found[0] if found else None


def test_extraction_accepts_valid_term_dates():
    candidate = _term_candidate(list(reversed(TERM_ITEMS)))
    assert candidate.kind == "term_dates" and candidate.profile_id is None
    assert [p["kind"] for p in candidate.payload["periods"]] == ["term", "half_term", "inset"]  # by date
    assert candidate.payload["periods"][0] == {
        "kind": "term", "start_date": "2026-09-03", "end_date": "2026-12-18", "label": "Autumn term"}
    assert candidate.evidence == TERM_ITEMS[0]["evidence"]  # the first period that quoted any


@pytest.mark.parametrize("bad", [
    {"kind": "term", "start_date": "2026-02-30", "end_date": "2026-03-20", "label": "No such day"},
    {"kind": "term", "start_date": "03/09/2026", "end_date": "2026-12-18", "label": "Not ISO"},
    {"kind": "term", "start_date": None, "end_date": "2026-12-18", "label": "No start"},
    {"kind": "spring", "start_date": "2027-01-05", "end_date": "2027-03-26", "label": "Unknown kind"},
    "not an object",
])
def test_extraction_drops_unreadable_periods(bad):
    assert _term_candidate([bad]) is None
    candidate = _term_candidate([bad, TERM_ITEMS[0]])
    assert [p["label"] for p in candidate.payload["periods"]] == ["Autumn term"]


@pytest.mark.parametrize(("bad", "problem"), [
    ({"kind": "term", "start_date": "2026-12-18", "end_date": "2026-09-03"}, "It ends before it starts."),
    ({"kind": "inset", "start_date": "2026-11-09", "end_date": "2026-11-16"},
     "8 days is too long (INSET day: at most 5 days)."),
    ({"kind": "holiday", "start_date": "2027-07-01", "end_date": "2027-09-30"},
     "92 days is too long (Holiday: at most 75 days)."),
    ({"kind": "term", "start_date": "2031-09-03", "end_date": "2031-12-18"}, "years away from today"),
    ({"kind": "term", "start_date": "2020-09-03", "end_date": "2020-12-18"}, "years away from today"),
])
def test_extraction_keeps_over_limit_periods_with_the_reason(bad, problem):
    candidate = _term_candidate([{**bad, "label": "Odd one", "evidence": ""}, TERM_ITEMS[0]])
    flagged = [p for p in candidate.payload["periods"] if p["label"] == "Odd one"]
    assert len(flagged) == 1 and problem in flagged[0]["problem"]
    assert "problem" not in next(p for p in candidate.payload["periods"] if p["label"] == "Autumn term")


async def test_the_inbox_shows_a_flagged_period_unticked_with_its_reason(db, admin_client):
    periods = [term_dates.clean_period(*AUTUMN),
               {"kind": "inset", "start_date": "2026-11-09", "end_date": "2026-11-16", "label": "INSET week",
                "problem": "8 days is too long (INSET day: at most 5 days)."}]
    source_id, candidate_id = await _term_source(db, periods)
    html = (await admin_client.get(f"/admin/inbox/sources/{source_id}")).text
    assert "Not added as read: 8 days is too long" in html
    assert 'name="period_include" value="0" checked' in html
    assert 'name="period_include" value="1" aria-label' in html  # unticked
    # Approving as ticked leaves it out; ticking it unfixed is refused.
    r = await admin_client.post(f"/admin/inbox/candidates/{candidate_id}/approve", data=_approve_form(
        [AUTUMN, ("inset", "2026-11-09", "2026-11-16", "INSET week")]), headers={"HX-Request": "true"})
    assert "too long for its kind" in r.text
    assert await _rows(db, "SELECT * FROM school_periods") == []
    await admin_client.post(f"/admin/inbox/candidates/{candidate_id}/approve", data=_approve_form(
        [AUTUMN, ("inset", "2026-11-09", "2026-11-16", "INSET week")], [0]))
    assert [r["label"] for r in await _rows(db, "SELECT label FROM school_periods")] == ["Autumn term"]


def test_extraction_caps_count_and_labels_and_merges_repeats():
    many = [{"kind": "inset", "start_date": (date(2026, 9, 1) + timedelta(days=i)).isoformat(),
             "end_date": (date(2026, 9, 1) + timedelta(days=i)).isoformat(), "label": "x" * 500, "evidence": ""}
            for i in range(60)]
    candidate = _term_candidate([TERM_ITEMS[0], TERM_ITEMS[0], *many])
    periods = candidate.payload["periods"]
    assert len(periods) == extraction.MAX_TERM_PERIODS - 1  # the repeat was merged
    assert all(len(p["label"]) <= term_dates.MAX_LABEL for p in periods)


def test_extraction_asks_for_term_dates():
    schema = extraction.TOOL["input_schema"]
    assert "term_dates" in schema["required"]
    item = schema["properties"]["term_dates"]["items"]
    assert item["properties"]["kind"]["enum"] == list(term_dates.KINDS)
    assert item["additionalProperties"] is False
    prompt = extraction.SYSTEM_PROMPT
    assert "colour-coded grid" in prompt and "INSET" in prompt and "half term" in prompt
    assert "Never invent, extrapolate or guess term dates" in prompt


def test_a_term_dates_candidate_is_kept_when_the_others_hit_the_cap():
    homework = [{"subject": "Maths", "title": f"Sheet {i}", "details": "", "due_date": None, "child": None,
                 "evidence": ""} for i in range(40)]
    found = extraction.validate_output({"homework": homework, "term_dates": TERM_ITEMS}, [], TODAY)
    assert len(found) == extraction.MAX_CANDIDATES
    assert found[-1].kind == "term_dates"


# --- School inbox: approve a term-dates candidate ---

async def _term_source(db, periods=None) -> tuple[int, int]:
    """A source with one pending term-dates candidate: (source id, candidate id)."""
    source = await db.execute(
        "INSERT INTO import_sources (kind, source_ref, subject, status) VALUES ('upload', 'td', 'Term dates.pdf', 'extracted')")
    payload = {"periods": periods or [term_dates.clean_period(*p) for p in (AUTUMN, HALF_TERM, INSET)]}
    candidate = await db.execute(
        "INSERT INTO import_candidates (source_id, kind, payload_json, evidence) VALUES (?, 'term_dates', ?, 'Key: grey = INSET')",
        (source.lastrowid, json.dumps(payload)),
    )
    await db.commit()
    return source.lastrowid, candidate.lastrowid


def _approve_form(periods, include=None):
    include = range(len(periods)) if include is None else include
    return {
        "period_kind": [p[0] for p in periods], "period_start": [p[1] for p in periods],
        "period_end": [p[2] for p in periods], "period_label": [p[3] for p in periods],
        "period_include": [str(i) for i in include],
    }


async def test_ingest_stores_one_term_dates_candidate(db, monkeypatch, caplog):
    class Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @property
        def messages(self):
            return self

        async def create(self, **kwargs):
            from anthropic.types import Message
            return Message.model_validate({
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "id": "t1", "name": extraction.TOOL_NAME, "input": {
                    "word_lists": [], "homework": [], "events": [], "term_dates": TERM_ITEMS}}],
                "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
            })

    async def fake_today(db):
        return TODAY

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(extraction, "client_factory", lambda key: Fake())
    monkeypatch.setattr(imports, "family_today", fake_today)
    await _add(db, AUTUMN)

    with caplog.at_level(logging.DEBUG):
        doc = imports.SourceDocument(kind="paste", source_ref="letter", text="Term dates 2026-27: SECRET-TEXT")
        result = await imports.ingest(db, doc)
    assert (result.status, result.candidate_count) == ("extracted", 1)
    assert "SECRET-TEXT" not in caplog.text and "Autumn term" not in caplog.text

    source = await imports.get_source(db, result.source_id)
    (candidate,) = source["candidates"]
    assert candidate["kind"] == "term_dates" and candidate["profile_id"] is None
    assert [p["already"] for p in candidate["payload"]["periods"]] == [True, False, False]
    assert candidate["duplicate"] is False
    assert source["approvable"] == 0  # never part of "Approve all"


async def test_inbox_renders_the_set_with_editable_rows(db, admin_client):
    await _add(db, AUTUMN)
    await _term_source(db)
    html = (await admin_client.get("/admin?tab=school")).text
    assert "Term dates" in html and "Whole school · 3 dates" in html
    assert html.count('name="period_kind"') == 3
    assert 'value="October half term"' in html
    assert html.count("Already added") == 1
    # The already-added row starts unticked; the others ticked.
    assert html.count('name="period_include" value="0" aria-label') == 1
    assert 'name="period_include" value="1" checked' in html


async def test_approve_adds_every_ticked_period_in_one_go(db, admin_client):
    await _add(db, AUTUMN)  # already there: skipped, not doubled
    source_id, candidate_id = await _term_source(db)
    edited = [AUTUMN, ("half_term", "2026-10-26", "2026-10-30", "Half term (edited)"), INSET]
    r = await admin_client.post(f"/admin/inbox/candidates/{candidate_id}/approve", data=_approve_form(edited))
    assert (r.status_code, r.headers["location"]) == (303, admin_url("inbox"))

    rows = await _rows(db, "SELECT kind, label, source FROM school_periods ORDER BY start_date, id")
    assert rows == [
        {"kind": "term", "label": "Autumn term", "source": "manual"},
        {"kind": "half_term", "label": "Half term (edited)", "source": "import"},
        {"kind": "inset", "label": "INSET day", "source": "import"},
    ]
    candidate = (await _rows(db, "SELECT * FROM import_candidates WHERE id = ?", candidate_id))[0]
    assert (candidate["status"], candidate["created_table"]) == ("approved", "school_periods")

    # A second approve (a double tap) finds nothing waiting and adds nothing.
    r = await admin_client.post(f"/admin/inbox/candidates/{candidate_id}/approve", data=_approve_form(edited))
    assert r.headers["location"] == admin_url("inbox", error="import-missing")
    assert len(await _rows(db, "SELECT * FROM school_periods")) == 3


async def test_unticked_rows_are_left_out(db, admin_client):
    _, candidate_id = await _term_source(db)
    periods = [AUTUMN, HALF_TERM, INSET]
    await admin_client.post(f"/admin/inbox/candidates/{candidate_id}/approve", data=_approve_form(periods, [0, 2]))
    assert [r["kind"] for r in await _rows(db, "SELECT kind FROM school_periods ORDER BY start_date")] == ["term", "inset"]


@pytest.mark.parametrize(("periods", "include", "message"), [
    ([AUTUMN, ("inset", "2026-11-13", "2026-11-20", "INSET week?")], None, "too long for its kind"),
    ([AUTUMN, ("term", "2026-12-01", "2027-03-01", "Spring")], None,  # two rows of the set clash
     "That clashes with &#39;Spring&#39; (1 Dec–1 Mar)"),
    ([AUTUMN, ("half_term", "2026-10-30", "2026-10-26", "Backwards")], None, "end on or after the start"),
    ([AUTUMN, HALF_TERM], [], "Tick at least one"),
])
async def test_a_bad_row_saves_nothing(db, admin_client, periods, include, message):
    source_id, candidate_id = await _term_source(db)
    r = await admin_client.post(
        f"/admin/inbox/candidates/{candidate_id}/approve", data=_approve_form(periods, include),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and f'id="inbox-source-{source_id}"' in r.text
    assert message in r.text
    assert f'value="{periods[-1][1]}"' in r.text  # what was typed comes back
    assert await _rows(db, "SELECT * FROM school_periods") == []
    status = (await _rows(db, "SELECT status FROM import_candidates WHERE id = ?", candidate_id))[0]["status"]
    assert status == "pending"


async def test_approve_names_a_clash_with_a_stored_period(db, admin_client):
    await _add(db, CHRISTMAS)
    _, candidate_id = await _term_source(db)
    r = await admin_client.post(f"/admin/inbox/candidates/{candidate_id}/approve", data=_approve_form(
        [("term", "2026-09-03", "2026-12-21", "Autumn term")]))
    assert r.status_code == 400
    assert "That clashes with &#39;Christmas holidays&#39; (19 Dec–4 Jan)" in r.text


async def test_concurrent_approves_cannot_both_add_clashing_periods(db):
    """Two term-dates candidates (say, two copies of the letter) whose terms
    overlap, approved at the same moment: the clash check runs after the
    claim, inside the transaction, so exactly one set goes in."""
    _, first = await _term_source(db, [term_dates.clean_period(*AUTUMN)])
    source = await db.execute(
        "INSERT INTO import_sources (kind, source_ref, status) VALUES ('upload', 'td2', 'extracted')")
    other = await db.execute(
        "INSERT INTO import_candidates (source_id, kind, payload_json) VALUES (?, 'term_dates', '{\"periods\": []}')",
        (source.lastrowid,))
    await db.commit()
    rows_a = [{"kind": "term", "start_date": "2026-09-03", "end_date": "2026-12-18", "label": "Autumn term"}]
    rows_b = [{"kind": "term", "start_date": "2026-09-07", "end_date": "2026-12-16", "label": "Autumn (council)"}]

    async def approve(candidate_id, rows):
        async with database.get_db() as conn:
            return await imports.approve_term_dates(conn, candidate_id, rows)

    results = await asyncio.gather(approve(first, rows_a), approve(other.lastrowid, rows_b), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["PeriodError", "int"]
    assert len(await _rows(db, "SELECT * FROM school_periods")) == 1
    statuses = sorted(r["status"] for r in await _rows(db, "SELECT status FROM import_candidates"))
    assert statuses == ["approved", "pending"]  # the loser is still waiting


async def test_a_failure_mid_approve_rolls_everything_back(db, monkeypatch):
    _, candidate_id = await _term_source(db)
    real_insert = term_dates.insert_period
    calls = []

    async def fail_second(db, period, source):
        calls.append(period)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        return await real_insert(db, period, source)

    monkeypatch.setattr(term_dates, "insert_period", fail_second)
    rows = [dict(zip(("kind", "start_date", "end_date", "label"), p, strict=True)) for p in (AUTUMN, HALF_TERM, INSET)]
    with pytest.raises(sqlite3.OperationalError):
        await imports.approve_term_dates(db, candidate_id, rows)
    assert await _rows(db, "SELECT * FROM school_periods") == []
    assert (await _rows(db, "SELECT status FROM import_candidates"))[0]["status"] == "pending"


async def test_a_term_dates_set_already_added_is_marked(db, admin_client):
    await _add(db, AUTUMN, HALF_TERM, INSET)
    source_id, _ = await _term_source(db)
    source = await imports.get_source(db, source_id)
    assert source["candidates"][0]["duplicate"] is True
    html = (await admin_client.get(f"/admin/inbox/sources/{source_id}")).text
    assert html.count("Already added") == 4  # the set, and each row


# --- The calendar ---

@pytest.fixture
def events(google, connected):
    def serve(*items):
        return google.get(url__regex=EVENTS_URL_PATTERN).respond(200, json={"items": list(items)})
    return serve


def _bars(week):
    return {b["event"]["title"]: (b["col_start"], b["col_end"], b["slot"], b["event"].get("school"))
            for b in week["bars"]}


async def test_the_month_grid_shows_school_periods_as_local_bars(db, events, google, school_year):
    from app import google_calendar

    # October 2026's grid starts Mon 28 Sep; half term is week 5 (26-30 Oct), Mon-Fri.
    events({"summary": "Grandma visits", "start": {"date": "2026-10-26"}, "end": {"date": "2026-10-27"}})
    grid = await google_calendar.get_month_grid(db, 2026, 10)

    week = grid["weeks"][4]
    # Google's own events are packed first: the half term never pushes one out.
    assert _bars(week) == {"Grandma visits": (1, 1, 0, None), "October half term": (1, 5, 1, "half_term")}
    assert not any(b["event"].get("school") == "term" for w in grid["weeks"] for b in w["bars"])
    markers = {d["date"]: d["term_marker"] for w in grid["weeks"] for d in w["days"] if d["term_marker"]}
    assert markers == {}  # October has no term start or end
    assert all(call.request.method == "GET" for call in google.calls)  # nothing written to Google

    grid = await google_calendar.get_month_grid(db, 2026, 9)
    markers = {d["date"]: d["term_marker"] for w in grid["weeks"] for d in w["days"] if d["term_marker"]}
    assert markers == {"2026-09-03": "Term starts"}

    grid = await google_calendar.get_month_grid(db, 2026, 12)
    titles = {b["event"]["title"] for w in grid["weeks"] for b in w["bars"]}
    assert "Christmas holidays" in titles
    assert "Bank holiday" not in titles  # Google's own holiday calendars show those


def _fridays(count):
    """Timed Google events on Fri 30 Oct 2026, 09:00, 10:00, ..."""
    return [{"summary": f"Friday {n}", "start": {"dateTime": f"2026-10-30T{9 + n:02d}:00:00+00:00"},
             "end": {"dateTime": f"2026-10-30T{10 + n:02d}:00:00+00:00"}} for n in range(count)]


async def test_a_school_bar_fills_free_space_left_of_google_events(db, events, google):
    """Three Friday events fill every row on Friday; a Mon–Wed half term
    still has room in the first row, so it gets a bar, not "+1 more"."""
    from app import google_calendar

    await _add(db, AUTUMN, ("half_term", "2026-10-26", "2026-10-28", "Half term"))
    events(*_fridays(3))
    week = (await google_calendar.get_month_grid(db, 2026, 10))["weeks"][4]  # 26 Oct–1 Nov

    assert _bars(week) == {
        "Friday 0": (5, 5, 0, None), "Friday 1": (5, 5, 1, None), "Friday 2": (5, 5, 2, None),
        "Half term": (1, 3, 0, "half_term"),
    }
    assert [d["hidden_count"] for d in week["days"]] == [0] * 7


async def test_school_bars_never_displace_google_events(db, events, google):
    """A full Friday plus a fourth event there: the Google events keep their
    rows and "+N more" counts, and a half term over the full day is what's hidden."""
    from app import google_calendar

    await _add(db, AUTUMN, ("half_term", "2026-10-26", "2026-10-30", "Half term"))
    events(*_fridays(4))
    week = (await google_calendar.get_month_grid(db, 2026, 10))["weeks"][4]

    assert _bars(week) == {"Friday 0": (5, 5, 0, None), "Friday 1": (5, 5, 1, None), "Friday 2": (5, 5, 2, None)}
    assert [d["hidden_count"] for d in week["days"]] == [1, 1, 1, 1, 2, 0, 0]


async def test_the_calendar_widget_renders_school_bars_distinctly(db, admin_client, events, school_year):
    events()
    html = (await admin_client.get("/widgets/calendar?year=2026&month=11")).text
    assert 'class="cal-bar cal-school cal-school-inset"' in html
    assert "INSET day" in html and "#school" in html

    day = (await admin_client.get("/widgets/calendar/day/2026-10-28")).text
    assert "cal-school-half_term" in day and "October half term" in day
    day = (await admin_client.get("/widgets/calendar/day/2026-12-18")).text
    assert "Term ends" in day


async def test_school_bars_show_even_while_google_is_down(db, google, connected, school_year):
    from app import google_calendar

    google.get(url__regex=EVENTS_URL_PATTERN).mock(side_effect=httpx.ConnectError("offline"))
    grid = await google_calendar.get_month_grid(db, 2026, 10)
    assert grid["offline"] is True
    assert "October half term" in {b["event"]["title"] for w in grid["weeks"] for b in w["bars"]}
    http_client.reset_failures()
