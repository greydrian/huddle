"""Choosing which widgets are shown (spec 10.3, app/services/layout.py) and
the Photos widget's removal (spec 10.2, migration 5)."""

import sqlite3
from datetime import date

import pytest
from markupsafe import escape

from app import database, freshness, migrations, widgets
from app.routers import admin
from app.security import create_session_token
from app.services import layout, term_dates

# A family's customised layout (not the default), 12 columns:
#
#   columns 0-7                                   | columns 8-11
#   y 0-3   calendar                              | y 0-2 weather, then y 2-6 practice_words
#   y 3-6   tasks (0-3) | shopping (4-7)          |
#   y 6-8   meals (0-7, under both)               |
#   y 9-12  homework (0-3), a 1-row gap above it  |
CUSTOM = {
    "calendar": (0, 0, 8, 3),
    "weather": (8, 0, 4, 2),
    "practice_words": (8, 2, 4, 4),
    "tasks": (0, 3, 4, 3),
    "shopping": (4, 3, 4, 3),
    "meals": (0, 6, 8, 2),
    "homework": (0, 9, 4, 3),  # a family-made 1-row gap above it (meals ends at 8)
}


def _row(widget_id, x, y, w, h):
    return {"widget_id": widget_id, "grid_x": x, "grid_y": y, "grid_w": w, "grid_h": h}


def _boxes(rows):
    return {r["widget_id"]: (r["grid_x"], r["grid_y"], r["grid_w"], r["grid_h"]) for r in rows}


async def _set_layout(db, boxes):
    await db.execute("DELETE FROM layout_state")
    await db.executemany(
        "INSERT INTO layout_state (widget_id, grid_x, grid_y, grid_w, grid_h, is_visible) VALUES (?, ?, ?, ?, ?, 1)",
        [(wid, *box) for wid, box in boxes.items()],
    )
    await db.commit()


async def _saved(db, visible_only=False):
    rows = await (await db.execute(
        "SELECT widget_id, grid_x, grid_y, grid_w, grid_h, is_visible FROM layout_state"
    )).fetchall()
    return {r["widget_id"]: (r["grid_x"], r["grid_y"], r["grid_w"], r["grid_h"])
            for r in rows if r["is_visible"] or not visible_only}


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


# --- close_gap / free_spot ---

def test_close_gap_moves_only_the_widgets_below_in_its_columns():
    rows = [_row(w, *b) for w, b in CUSTOM.items()]
    after = _boxes(layout.close_gap(rows, _row("tasks", *CUSTOM["tasks"])))
    # meals spans tasks' columns and shopping's: shopping still holds it, so it can't rise.
    assert after == {w: b for w, b in CUSTOM.items() if w != "tasks"}


def test_close_gap_moves_a_stack_up_together_and_keeps_its_spacing():
    rows = [_row("a", 0, 0, 4, 3), _row("b", 0, 3, 4, 2), _row("c", 0, 7, 4, 2), _row("side", 4, 0, 4, 9)]
    after = _boxes(layout.close_gap(rows, _row("a", 0, 0, 4, 3)))
    assert after == {"b": (0, 0, 4, 2), "c": (0, 4, 4, 2), "side": (4, 0, 4, 9)}  # the 2-row gap b→c is kept


def test_close_gap_of_a_wide_widget_moves_everything_below_it():
    rows = [_row("top", 0, 0, 12, 7), _row("l", 0, 7, 4, 4), _row("r", 8, 7, 4, 2), _row("low", 0, 11, 6, 4)]
    after = _boxes(layout.close_gap(rows, rows[0]))
    assert after == {"l": (0, 0, 4, 4), "r": (8, 0, 4, 2), "low": (0, 4, 6, 4)}


def test_close_gap_never_uses_space_the_gone_widget_didnt_leave():
    rows = [
        _row("gone", 0, 4, 4, 2),
        _row("below", 0, 6, 6, 2),   # spans columns 4-5 too, under "blocker" (ends at 5, a 1-row gap)
        _row("blocker", 4, 0, 4, 5),
        _row("elsewhere", 8, 9, 4, 2),  # not in gone's columns: never moves
        _row("above", 0, 0, 4, 3),   # above the gone widget: never moves
    ]
    after = _boxes(layout.close_gap(rows, rows[0]))
    assert after["below"] == (0, 6, 6, 2)  # "blocker" didn't move, so neither does it
    assert after["elsewhere"] == (8, 9, 4, 2) and after["above"] == (0, 0, 4, 3)
    # Nor does one whose other columns are empty above: that space is the family's.
    rows = [_row("gone", 0, 4, 4, 2), _row("wide", 0, 6, 8, 2)]
    assert _boxes(layout.close_gap(rows, rows[0]))["wide"] == (0, 6, 8, 2)
    # Unless what's above those columns moved up as far.
    rows = [_row("gone", 0, 4, 8, 2), _row("left", 0, 6, 4, 2), _row("wide", 0, 8, 8, 2)]
    assert _boxes(layout.close_gap(rows, rows[0])) == {"left": (0, 4, 4, 2), "wide": (0, 6, 8, 2)}
    # Nothing rises further than the space that was freed.
    rows = [_row("gone", 0, 4, 4, 2), _row("far", 0, 20, 4, 2)]
    assert _boxes(layout.close_gap(rows, rows[0]))["far"] == (0, 18, 4, 2)


def test_free_spot_prefers_the_saved_place_then_the_nearest_same_columns():
    others = [_row("x", 0, 0, 4, 3), _row("y", 4, 0, 8, 6)]
    assert layout.free_spot(others, _row("me", 0, 3, 4, 2)) == (0, 3)  # free: exactly where it was
    assert layout.free_spot(others, _row("me", 0, 1, 4, 2)) == (0, 3)  # taken: just below, same columns
    # Same distance either way: its own columns win.
    others = [_row("x", 4, 0, 4, 4), _row("y", 0, 2, 4, 2), _row("z", 8, 0, 4, 2)]
    assert layout.free_spot(others, _row("me", 4, 2, 4, 2)) == (4, 4)


# --- Hide / show through Admin ---

async def test_hide_then_show_changes_nothing_but_the_widgets_that_moved_up(admin_client, db):
    await _set_layout(db, CUSTOM)
    resp = await admin_client.post("/admin/widgets/shopping/visibility", data={"visible": "false"})
    assert resp.status_code == 303 and resp.headers["location"] == "/admin?tab=display#widgets"

    hidden = await _saved(db, visible_only=True)
    assert "shopping" not in hidden
    # meals was held down by shopping and tasks; tasks is still there, so meals stays.
    assert hidden == {w: b for w, b in CUSTOM.items() if w != "shopping"}
    assert (await _saved(db))["shopping"] == CUSTOM["shopping"]  # its saved position is kept

    await admin_client.post("/admin/widgets/shopping/visibility", data={"visible": "true"})
    assert await _saved(db, visible_only=True) == CUSTOM  # back exactly where it was


async def test_hiding_moves_up_the_stack_below_and_showing_moves_it_back(admin_client, db):
    await _set_layout(db, CUSTOM)
    await admin_client.post("/admin/widgets/weather/visibility", data={"visible": "false"})
    after = await _saved(db, visible_only=True)
    moved = {w for w in after if after[w] != CUSTOM[w]}
    assert moved == {"practice_words"}
    assert after["practice_words"] == (8, 0, 4, 4)

    await admin_client.post("/admin/widgets/weather/visibility", data={"visible": "true"})
    assert await _saved(db, visible_only=True) == CUSTOM  # exactly as before


async def test_a_row_for_an_unregistered_widget_is_ignored(admin_client, db):
    # A leftover row (e.g. a widget removed without a migration) isn't on the wall: it neither
    # holds anything down nor moves.
    await _set_layout(db, {**CUSTOM, "clock": (8, 0, 4, 2)})
    await admin_client.post("/admin/widgets/weather/visibility", data={"visible": "false"})
    saved = await _saved(db)
    assert saved["practice_words"] == (8, 0, 4, 4) and saved["clock"] == (8, 0, 4, 2)


async def test_after_a_rearrangement_showing_uses_the_nearest_free_space(admin_client, client, db):
    await _set_layout(db, CUSTOM)
    await admin_client.post("/admin/widgets/weather/visibility", data={"visible": "false"})
    # Meanwhile the family drags practice_words down a row, into part of weather's old place.
    await client.post("/api/layout", json={"items": [{"id": "practice_words", "x": 8, "y": 1, "w": 4, "h": 4}]})
    rearranged = await _saved(db, visible_only=True)
    assert rearranged["practice_words"] == (8, 1, 4, 4)

    await admin_client.post("/admin/widgets/weather/visibility", data={"visible": "true"})
    shown = await _saved(db, visible_only=True)
    assert shown["weather"] == (8, 5, 4, 2)  # its place is taken: the nearest free space, same columns
    assert {w: b for w, b in shown.items() if w != "weather"} == rearranged  # nothing else moved


async def test_hiding_a_widget_spanning_columns_moves_each_column_up(admin_client, db):
    await _set_layout(db, CUSTOM)
    await admin_client.post("/admin/widgets/calendar/visibility", data={"visible": "false"})
    after = await _saved(db, visible_only=True)
    assert after["tasks"] == (0, 0, 4, 3) and after["shopping"] == (4, 0, 4, 3)
    assert after["meals"] == (0, 3, 8, 2)
    assert after["homework"] == (0, 6, 4, 3)  # the family's 1-row gap under meals is kept
    assert after["weather"] == CUSTOM["weather"] and after["practice_words"] == CUSTOM["practice_words"]


async def test_hide_and_show_twice_are_no_ops(admin_client, db):
    await _set_layout(db, CUSTOM)
    await admin_client.post("/admin/widgets/tasks/visibility", data={"visible": "false"})
    once = await _saved(db)
    await admin_client.post("/admin/widgets/tasks/visibility", data={"visible": "false"})
    assert await _saved(db) == once
    await admin_client.post("/admin/widgets/tasks/visibility", data={"visible": "true"})
    twice = await _saved(db)
    await admin_client.post("/admin/widgets/tasks/visibility", data={"visible": "true"})
    assert await _saved(db) == twice


async def test_widget_switches_need_admin(client, db):
    before = await _saved(db)
    for url, data in (("/admin/widgets/tasks/visibility", {"visible": "false"}),
                      ("/admin/widgets/tasks/school-days", {"enabled": "true"})):
        resp = await client.post(url, data=data)
        assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"
    assert await _saved(db) == before
    row = await (await db.execute("SELECT is_visible, school_days_only FROM layout_state WHERE widget_id = 'tasks'")).fetchone()
    assert tuple(row) == (1, 0)


async def test_unknown_widget_is_an_error_not_a_change(admin_client, db):
    before = await _saved(db)
    for url in ("/admin/widgets/photos/visibility", "/admin/widgets/nope/school-days"):
        resp = await admin_client.post(url, data={"visible": "false", "enabled": "true"})
        assert resp.headers["location"] == "/admin?tab=display&error=widget-missing#widgets"
    assert await _saved(db) == before
    html = (await admin_client.get("/admin?tab=display&error=widget-missing")).text
    assert "That widget no longer exists" in html


async def test_admin_lists_every_widget_with_both_switches(admin_client, db):
    await admin_client.post("/admin/widgets/homework/school-days", data={"enabled": "true"})
    await admin_client.post("/admin/widgets/meals/visibility", data={"visible": "false"})
    html = (await admin_client.get("/admin?tab=display")).text
    assert 'id="widgets"' in html
    for widget_id, widget in widgets.WIDGETS.items():
        assert f'action="/admin/widgets/{widget_id}/visibility"' in html
        assert f'action="/admin/widgets/{widget_id}/school-days"' in html
        assert f"Show: {escape(widget.label)}" in html
    assert "Photos" not in html
    assert await layout.admin_widgets(db) == [
        {"id": w, "label": widgets.WIDGETS[w].label, "visible": w != "meals", "school_days_only": w == "homework"}
        for w in widgets.WIDGETS
    ]


# --- The dashboard and /api/rev skip hidden widgets ---

@pytest.fixture
def load_counts(monkeypatch):
    counts = {w: 0 for w in widgets.WIDGETS}
    for widget_id, widget in list(widgets.WIDGETS.items()):
        def counted(db, _load=widget.load, _id=widget_id):
            counts[_id] += 1
            return _load(db)
        monkeypatch.setitem(widgets.WIDGETS, widget_id, widget._replace(load=counted))
    return counts


async def test_dashboard_skips_hidden_widgets_and_their_loaders(admin_client, db, load_counts):
    await admin_client.post("/admin/widgets/shopping/visibility", data={"visible": "false"})
    await admin_client.post("/admin/widgets/calendar/visibility", data={"visible": "false"})
    html = (await admin_client.get("/")).text
    assert 'gs-id="shopping"' not in html and 'id="widget-shopping"' not in html
    assert 'gs-id="calendar"' not in html
    assert 'gs-id="tasks"' in html
    assert load_counts["shopping"] == 0 and load_counts["calendar"] == 0
    assert load_counts["tasks"] == 1
    assert '"shopping"' not in html.split("data-revs='")[1].split("'")[0]


async def test_api_rev_leaves_out_hidden_widgets(admin_client, db, load_counts):
    before = (await admin_client.get("/api/rev")).json()
    assert set(before["widgets"]) == {*freshness.REFRESHED, freshness.BANNERS}
    assert before["shown"] == sorted(widgets.WIDGETS)

    await admin_client.post("/admin/widgets/homework/visibility", data={"visible": "false"})
    for key in load_counts:
        load_counts[key] = 0
    after = (await admin_client.get("/api/rev")).json()
    assert set(after["widgets"]) == {*freshness.REFRESHED, freshness.BANNERS} - {"homework"}
    assert "homework" not in after["shown"]
    assert load_counts["homework"] == 0
    assert load_counts["calendar"] == 0 and load_counts["weather"] == 0  # they poll themselves


async def test_layout_post_never_writes_a_hidden_widget(admin_client, client, db):
    await _set_layout(db, CUSTOM)
    await admin_client.post("/admin/widgets/weather/visibility", data={"visible": "false"})
    saved = await _saved(db)
    # A page that hasn't reloaded yet still has weather on its grid.
    stale = [{"id": w, "x": b[0], "y": b[1], "w": b[2], "h": b[3]} for w, b in CUSTOM.items()]
    stale[1] = {"id": "weather", "x": 0, "y": 20, "w": 4, "h": 2}
    await client.post("/api/layout", json={"items": stale})
    after = await _saved(db)
    assert after["weather"] == saved["weather"] == CUSTOM["weather"]


# --- School days only ---

AUTUMN = ("term", "2026-09-03", "2026-12-18", "Autumn term")
HALF_TERM = ("half_term", "2026-10-26", "2026-10-30", "October half term")


@pytest.fixture
async def school_year(db):
    for kind, start, end, label in (AUTUMN, HALF_TERM):
        await term_dates.insert_period(db, term_dates.clean_period(kind, start, end, label), "manual")
    await db.commit()


@pytest.fixture
def on_day(monkeypatch):
    def set_day(day: date):
        async def today(db):
            return day
        monkeypatch.setattr(layout, "family_today", today)
    return set_day


@pytest.mark.parametrize(("day", "shown"), [
    pytest.param(date(2026, 10, 1), True, id="term-thursday"),
    pytest.param(date(2026, 10, 27), False, id="half-term-tuesday"),
    pytest.param(date(2026, 10, 3), False, id="saturday"),
])
async def test_school_days_only_widget_follows_the_term_dates(
    admin_client, db, school_year, on_day, load_counts, day, shown
):
    await _set_layout(db, CUSTOM)
    await admin_client.post("/admin/widgets/weather/school-days", data={"enabled": "true"})
    on_day(day)
    html = (await admin_client.get("/")).text
    assert ('gs-id="weather"' in html) is shown
    assert (load_counts["weather"] == 1) is shown
    rev = (await admin_client.get("/api/rev")).json()
    assert ("weather" in rev["shown"]) is shown
    # Its gap is closed on the wall for that day (practice_words moves up), and never saved.
    assert ('gs-id="practice_words"\n       gs-x="8" gs-y="0"' in html) is not shown
    assert await _saved(db) == CUSTOM


async def test_school_days_only_falls_back_to_weekdays_without_term_dates(admin_client, db, on_day):
    await admin_client.post("/admin/widgets/homework/school-days", data={"enabled": "true"})
    wednesday, saturday = date(2026, 6, 10), date(2026, 6, 13)  # school year 2025-26: no terms entered
    assert (wednesday.weekday(), saturday.weekday()) == (2, 5)
    on_day(wednesday)
    assert 'gs-id="homework"' in (await admin_client.get("/")).text
    on_day(saturday)
    assert 'gs-id="homework"' not in (await admin_client.get("/")).text
    assert "homework" not in (await admin_client.get("/api/rev")).json()["shown"]


async def test_a_drag_on_a_day_off_saves_only_what_was_moved(client, db, school_year, on_day):
    await _set_layout(db, CUSTOM)
    await layout.set_school_days_only(db, "weather", True)
    await db.commit()
    on_day(date(2026, 10, 3))  # Saturday: weather is off, practice_words shows at y 0
    shown = _boxes(await layout.shown_layout(db))
    assert shown["practice_words"] == (8, 0, 4, 4)
    # The page posts every node it has: practice_words where the wall shows it, tasks dragged.
    items = [{"id": w, "x": b[0], "y": b[1], "w": b[2], "h": b[3]} for w, b in shown.items()]
    next(i for i in items if i["id"] == "tasks")["y"] = 12
    await client.post("/api/layout", json={"items": items})
    after = await _saved(db)
    assert after["practice_words"] == CUSTOM["practice_words"]  # the day-only move isn't saved
    assert after["tasks"] == (0, 12, 4, 3)  # the real drag is
    assert after["weather"] == CUSTOM["weather"]


async def test_school_day_widget_whose_space_was_taken_shows_in_the_nearest_space(db, school_year, on_day):
    await _set_layout(db, {**CUSTOM, "practice_words": (8, 0, 4, 4)})  # moved into weather's place on a day off
    await layout.set_school_days_only(db, "weather", True)
    await db.commit()
    shown = _boxes(await layout.shown_layout(db, date(2026, 10, 1)))
    assert shown["weather"] == (8, 4, 4, 2)
    assert shown["practice_words"] == (8, 0, 4, 4)


def _overlaps(boxes):
    ids = sorted(boxes)
    return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:] if layout._overlaps(boxes[a], boxes[b])]


async def test_a_drag_into_space_only_free_today_never_overlaps_on_monday(client, db, school_year, on_day):
    await _set_layout(db, CUSTOM)
    await layout.set_school_days_only(db, "weather", True)
    await db.commit()
    on_day(date(2026, 10, 3))  # Saturday: practice_words shows at y 0-4, but is saved at y 2-6
    shown = _boxes(await layout.shown_layout(db))
    assert shown["practice_words"] == (8, 0, 4, 4)
    # Homework is dragged to (8, 4), which looks empty on Saturday.
    items = [{"id": w, "x": b[0], "y": b[1], "w": b[2], "h": b[3]} for w, b in shown.items()]
    next(i for i in items if i["id"] == "homework").update(x=8, y=4)
    await client.post("/api/layout", json={"items": items})

    saved = await _saved(db, visible_only=True)
    assert saved["homework"] == (8, 4, 4, 3)
    assert not _overlaps(saved)
    assert saved["practice_words"] != CUSTOM["practice_words"]  # re-placed: its saved place was taken
    on_day(date(2026, 10, 5))  # Monday
    monday = _boxes(await layout.shown_layout(db))
    assert set(monday) == set(CUSTOM) and not _overlaps(monday)


# --- Stale pages (layout generation) ---

async def _generation(client):
    html = (await client.get("/")).text
    return html.split('data-layout="')[1].split('"')[0]


async def test_a_stale_page_cannot_undo_a_hide(admin_client, client, db):
    await _set_layout(db, CUSTOM)
    stale = await _generation(client)
    assert (await client.get("/api/rev")).json()["layout"] == stale
    await admin_client.post("/admin/widgets/weather/visibility", data={"visible": "false"})
    hidden = await _saved(db)
    current = (await client.get("/api/rev")).json()["layout"]
    assert current != stale and current == await _generation(client)

    # The old page still has practice_words below weather, and posts it.
    items = [{"id": w, "x": b[0], "y": b[1], "w": b[2], "h": b[3]} for w, b in CUSTOM.items()]
    resp = await client.post("/api/layout", json={"items": items, "generation": stale})
    assert resp.status_code == 409
    assert await _saved(db) == hidden

    # Hiding and showing again leaves the same widgets shown, but the page is still stale.
    await admin_client.post("/admin/widgets/weather/visibility", data={"visible": "true"})
    resp = await client.post("/api/layout", json={"items": items, "generation": current})
    assert resp.status_code == 409

    fresh = await _generation(client)
    items[0].update(y=20)
    resp = await client.post("/api/layout", json={"items": items, "generation": fresh})
    assert resp.status_code == 200 and (await _saved(db))[items[0]["id"]][1] == 20


async def test_a_widget_refreshing_itself_doesnt_make_the_page_stale(client, db):
    generation = await _generation(client)
    # A quick-add re-renders the tasks widget (and changes its revision), not the grid.
    before = (await client.get("/api/rev")).json()
    resp = await client.post("/api/tasks", data={"profile_id": 1, "title": "Water plants", "time_of_day": ""})
    assert resp.status_code == 200 and "Water plants" in (await client.get("/widgets/tasks")).text
    after = (await client.get("/api/rev")).json()
    assert after["widgets"]["tasks"] != before["widgets"]["tasks"]
    assert after["layout"] == before["layout"] == generation
    items = [{"id": "tasks", "x": 0, "y": 30, "w": 4, "h": 4}]
    assert (await client.post("/api/layout", json={"items": items, "generation": generation})).status_code == 200
    assert (await _saved(db))["tasks"] == (0, 30, 4, 4)


async def test_a_page_from_yesterday_is_stale(client, db, on_day):
    on_day(date(2026, 10, 1))
    yesterday = await _generation(client)
    on_day(date(2026, 10, 2))
    resp = await client.post("/api/layout", json={"items": [], "generation": yesterday})
    assert resp.status_code == 409
    # A body without a generation (a page from before this check) is still accepted.
    assert (await client.post("/api/layout", json={"items": []})).status_code == 200


# --- Photos removal (migration 5) ---

@pytest.fixture
def before_migration_5(tmp_path, monkeypatch):
    """A database as it was before migration 5 (with its Photos row)."""
    async def build(update_sql=()):
        path = tmp_path / "upgrade.db"
        monkeypatch.setattr(database, "DB_PATH", path)
        monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:4])
        monkeypatch.setattr(database, "DEFAULT_LAYOUT", list(migrations.BASELINE_LAYOUT))
        await database.init_db()
        with sqlite3.connect(path) as conn:
            for sql in update_sql:
                conn.execute(sql)
        monkeypatch.undo()
        monkeypatch.setattr(database, "DB_PATH", path)
        return path
    return build


def _file_layout(path):
    with sqlite3.connect(path) as conn:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT widget_id, grid_x, grid_y, grid_w, grid_h, is_visible, school_days_only FROM layout_state")}


async def test_migration_5_removes_photos_and_closes_its_gap(before_migration_5):
    # A customised layout: a stack under Photos in its columns, a wide widget
    # partly under it (the rest of its columns open: the family's space).
    path = await before_migration_5([
        "UPDATE layout_state SET grid_x = 8, grid_y = 11, grid_w = 2, grid_h = 3 WHERE widget_id = 'homework'",
        "UPDATE layout_state SET grid_x = 8, grid_y = 15, grid_w = 2, grid_h = 2 WHERE widget_id = 'practice_words'",
        "UPDATE layout_state SET grid_x = 0, grid_y = 11, grid_w = 4, grid_h = 4 WHERE widget_id = 'tasks'",
        "UPDATE layout_state SET grid_x = 10, grid_y = 11, grid_w = 2, grid_h = 2 WHERE widget_id = 'shopping'",
    ])
    with sqlite3.connect(path) as conn:
        before = {r[0]: r[1:] for r in conn.execute("SELECT widget_id, grid_x, grid_y, grid_w, grid_h FROM layout_state")}
    assert before["photos"] == (8, 9, 2, 2)

    await database.init_db()
    await database.init_db()  # startup_checks must not add it back

    after = _file_layout(path)
    assert "photos" not in after
    assert after["homework"] == (8, 9, 2, 3, 1, 0)  # moved up into Photos' space
    assert after["practice_words"] == (8, 13, 2, 2, 1, 0)  # and the one below it, keeping its gap
    for widget_id, box in before.items():
        if widget_id not in ("photos", "homework", "practice_words"):
            assert after[widget_id] == (*box, 1, 0), widget_id
    with sqlite3.connect(path) as conn:
        assert 5 in {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


async def test_migration_5_leaves_the_layout_alone_when_photos_was_hidden(before_migration_5):
    path = await before_migration_5([
        "UPDATE layout_state SET is_visible = 0 WHERE widget_id = 'photos'",
        "UPDATE layout_state SET grid_x = 8, grid_y = 11, grid_w = 4, grid_h = 3 WHERE widget_id = 'homework'",
    ])
    with sqlite3.connect(path) as conn:
        before = {r[0]: r[1:] for r in conn.execute(
            "SELECT widget_id, grid_x, grid_y, grid_w, grid_h FROM layout_state WHERE widget_id != 'photos'")}
    await database.init_db()
    after = _file_layout(path)
    assert "photos" not in after
    assert {w: row[:4] for w, row in after.items()} == before


async def test_fresh_install_has_no_photos(db):
    assert "photos" not in await _saved(db)
    assert "photos" not in widgets.WIDGETS
    assert all(widget_id != "photos" for widget_id, *_ in database.DEFAULT_LAYOUT)
