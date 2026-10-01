"""Countdowns (app/services/countdowns.py): family dates from Admin and the
next school break from the term dates, shown in the "next up" strip."""

from datetime import date, timedelta

import pytest

from app import database
from app.routers import admin
from app.security import create_session_token
from app.services import countdowns, next_up, term_dates
from tests.test_banners import at

TODAY = date(2026, 10, 5)
MUM, RILEY = 1, 3


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


async def add(db, title, days, person=""):
    return await countdowns.add(db, title, (TODAY + timedelta(days=days)).isoformat(), str(person), TODAY)


def shown(items):
    return [(c["title"], c["when"], c["person"]["name"] if c["person"] else None) for c in items]


async def test_nearest_first_capped_and_past_ones_drop_off(db):
    await add(db, "Trip to Gran", 3, RILEY)
    await add(db, "Christmas", 81)
    await add(db, "Bonfire night", 31)
    await add(db, "Dentist", 0, MUM)
    await add(db, "Pantomime", 1)
    assert shown(await countdowns.upcoming(db, TODAY)) == [
        ("Dentist", "today", "Mum"),
        ("Pantomime", "tomorrow", None),
        ("Trip to Gran", "in 3 days", "Riley"),
    ]
    # Four days on, the dentist and the pantomime have passed.
    later = await countdowns.upcoming(db, TODAY + timedelta(days=4))
    assert [c["title"] for c in later] == ["Bonfire night", "Christmas"]
    # Admin still lists all five, soonest first.
    assert [c["title"] for c in await countdowns.list_all(db)][0] == "Dentist"


async def test_next_school_break_counts_down_within_the_horizon(db, monkeypatch):
    await term_dates.add_period(db, "term", "2026-09-02", "2026-12-18", "Autumn term")
    await term_dates.add_period(db, "half_term", "2026-10-26", "2026-10-30")
    await term_dates.add_period(db, "inset", "2026-10-16", "2026-10-16")  # not a break worth counting to
    await term_dates.add_period(db, "holiday", "2026-12-19", "2027-01-03", "Christmas holidays")
    items = await countdowns.upcoming(db, TODAY)
    assert shown(items) == [("Half term", "in 21 days", None)]
    assert items[0]["school"] and items[0]["key"] == "break1"

    # Once half term has started, the next break is Christmas.
    assert shown(await countdowns.upcoming(db, date(2026, 10, 27))) == [("Christmas holidays", "in 53 days", None)]
    # Further off than the horizon: not worth the space yet.
    monkeypatch.setattr(countdowns, "BREAK_HORIZON", 20)
    assert await countdowns.upcoming(db, TODAY) == []
    monkeypatch.undo()

    await countdowns.set_breaks_enabled(db, False)
    assert await countdowns.upcoming(db, TODAY) == []


async def test_a_deleted_person_leaves_the_countdown_as_everyones(db):
    await add(db, "Party", 5, RILEY)
    await db.execute("DELETE FROM profiles WHERE id = ?", (RILEY,))
    await db.commit()
    assert shown(await countdowns.upcoming(db, TODAY)) == [("Party", "in 5 days", None)]


@pytest.mark.parametrize(
    ("title", "target", "person", "code"),
    [
        ("", "2026-10-10", "", "countdown-title"),
        ("x" * (countdowns.MAX_TITLE + 1), "2026-10-10", "", "countdown-title"),
        ("Trip", "", "", "countdown-date"),
        ("Trip", "10/10/2026", "", "countdown-date"),
        ("Trip", "2026-10-04", "", "countdown-date"),  # yesterday
        ("Trip", "2028-12-25", "", "countdown-date"),  # over two years off
        ("Trip", "2026-10-10", "99", "countdown-person"),
        ("Trip", "2026-10-10", "one", "countdown-person"),
        ("Trip", "2026-10-10", "²", "countdown-person"),
        ("Trip", "2026-10-10", "99999999999999999999999", "countdown-person"),
    ],
)
async def test_bad_forms_are_refused_with_a_code(db, title, target, person, code):
    with pytest.raises(countdowns.CountdownError) as caught:
        await countdowns.add(db, title, target, person, TODAY)
    assert str(caught.value) == code and code in admin.ADMIN_ERRORS
    assert await countdowns.list_all(db) == []


async def test_the_table_is_bounded(db, monkeypatch):
    monkeypatch.setattr(countdowns, "MAX_COUNTDOWNS", 2)
    await add(db, "One", 1)
    await add(db, "Two", 2)
    with pytest.raises(countdowns.CountdownError, match="countdown-full"):
        await add(db, "Three", 3)


def test_in_days():
    assert [countdowns.in_days(d) for d in (0, 1, 2, 40)] == ["today", "tomorrow", "in 2 days", "in 40 days"]


async def test_strip_shows_countdowns_even_with_next_up_off(db, client, monkeypatch):
    monkeypatch.setattr(next_up, "_clock", lambda tz: at(9))
    assert 'class="next-up is-empty"' in (await client.get("/next-up")).text

    await add(db, "Trip to Gran", 3, RILEY)
    await next_up.set_enabled(db, False)
    for path in ("/", "/next-up"):
        html = (await client.get(path)).text
        assert 'id="next-up" class="next-up"' in html, path
        assert "Counting down" in html and "Trip to Gran" in html and "in 3 days" in html, path
        assert "Next up</span>" not in html, path


async def test_rev_moves_when_a_countdown_is_added_or_the_day_turns(db, client, monkeypatch):
    revs = []

    async def rev():
        revs.append((await client.get("/api/rev")).json()["widgets"]["next_up"])

    monkeypatch.setattr(next_up, "_clock", lambda tz: at(9))
    await rev()
    await add(db, "Trip to Gran", 3)
    await rev()
    monkeypatch.setattr(next_up, "_clock", lambda tz: at(9, day=TODAY + timedelta(days=1)))
    await rev()
    assert len(set(revs)) == 3


async def test_admin_adds_lists_and_deletes(db, admin_client, monkeypatch):
    monkeypatch.setattr(admin.common, "family_today", lambda db: _today())
    resp = await admin_client.post(
        "/admin/countdowns", data={"title": " Trip to Gran ", "target_date": "2026-10-08", "profile_id": str(RILEY)}
    )
    assert resp.status_code == 303 and resp.headers["location"].endswith("tab=display#countdowns")
    [saved] = await countdowns.list_all(db)
    assert (saved["title"], saved["target_date"], saved["person"]) == ("Trip to Gran", "2026-10-08", "Riley")

    page = (await admin_client.get("/admin?tab=display")).text
    assert 'id="countdowns"' in page and "Trip to Gran" in page and "2026-10-08 · Riley" in page

    resp = await admin_client.post("/admin/countdowns", data={"title": "Old", "target_date": "2026-01-01"})
    assert resp.headers["location"].endswith("error=countdown-date#countdowns")
    page = (await admin_client.get(resp.headers["location"])).text
    assert "Pick a date from today to two years ahead" in page

    resp = await admin_client.post(f"/admin/countdowns/{saved['id']}/delete")
    assert resp.status_code == 303 and await countdowns.list_all(db) == []
    # Already gone: still fine.
    assert (await admin_client.post(f"/admin/countdowns/{saved['id']}/delete")).status_code == 303


async def test_admin_greys_passed_ones_and_switches_school_breaks(db, admin_client, monkeypatch):
    await add(db, "Pantomime", 1)
    monkeypatch.setattr(admin.common, "family_today", lambda db: _today(days=2))
    assert "countdown-past" in (await admin_client.get("/admin?tab=display")).text

    resp = await admin_client.post("/admin/countdowns/school-breaks", data={})
    assert resp.status_code == 303 and resp.headers["location"].endswith("#countdowns")
    assert await database.get_setting(db, countdowns.BREAKS_SETTING) == "0"
    await admin_client.post("/admin/countdowns/school-breaks", data={"enabled": "true"})
    assert await countdowns.breaks_enabled(db)


async def _today(days=0):
    return TODAY + timedelta(days=days)
