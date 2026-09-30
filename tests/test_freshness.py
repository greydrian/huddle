"""Keeping the wall fresh (app/freshness.py + static/js/fresh.js): per-widget
revisions, the refresh markup, and the family date for the top bar. The
browser-side "never swap a widget in use" rules are checked with Playwright
(see the PR)."""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import database, freshness
from app.widgets import WIDGETS

STATIC_JS = Path(database.__file__).parent / "static" / "js"
REFRESH_ROUTES = {
    "tasks": "/widgets/tasks",
    "shopping": "/widgets/shopping",
    "meals": "/widgets/meals",
    "homework": "/widgets/homework",
    "practice_words": "/widgets/practice-words",
}


@pytest.fixture(autouse=True)
def midday_banners(monkeypatch):
    """The banner bar's clock at midday on the real family date: from 18:00
    (the Evening group) chore banners appear for a ticked or archived task
    too, which would move the banners revision and make these tests depend
    on the time of day."""
    from app.services import banners

    monkeypatch.setattr(banners, "_clock", lambda tz: datetime.now(tz).replace(hour=12, minute=0))


async def _revs(client):
    resp = await client.get("/api/rev")
    assert resp.status_code == 200
    return resp.json()["widgets"]


def _changed(before, after):
    return sorted(k for k in before if before[k] != after[k])


# --- /api/rev ---


async def test_rev_is_stable_when_nothing_changes(client):
    assert await _revs(client) == await _revs(client)
    assert set(await _revs(client)) == {*freshness.REFRESHED, freshness.BANNERS}


async def test_rev_changes_only_for_the_shopping_list_on_add_and_delete(db, client):
    before = await _revs(client)
    await client.post("/api/shopping", data={"title": "Milk"})
    added = await _revs(client)
    assert _changed(before, added) == ["shopping"]

    item_id = (await (await db.execute("SELECT id FROM shopping_items")).fetchone())["id"]
    await client.post(f"/api/shopping/{item_id}/delete")
    # A delete leaves no updated_at behind; the revision must still move.
    assert _changed(added, await _revs(client)) == ["shopping"]


async def test_rev_changes_when_a_task_is_ticked(db, client):
    await db.execute("INSERT INTO tasks (profile_id, title) VALUES (1, 'Feed the cat')")
    await db.commit()
    before = await _revs(client)
    task_id = (await (await db.execute("SELECT id FROM tasks")).fetchone())["id"]

    await client.post(f"/api/tasks/{task_id}/toggle")

    assert _changed(before, await _revs(client)) == ["tasks"]


async def test_rev_changes_when_sync_archives_a_task_without_touching_updated_at(db, client):
    # task_sync's "deleted in Google" path only flips archived.
    await db.execute("INSERT INTO tasks (profile_id, title) VALUES (1, 'Gone on the phone')")
    await db.commit()
    before = await _revs(client)
    await db.execute("UPDATE tasks SET archived = 1")
    await db.commit()
    assert _changed(before, await _revs(client)) == ["tasks"]


async def test_rev_changes_when_meals_change(client):
    before = await _revs(client)
    today = (await client.get("/api/today")).json()["date"]
    await client.post(f"/api/meals/{today}", data={"description": "Tacos"})
    assert _changed(before, await _revs(client)) == ["meals"]


async def test_rev_changes_when_a_profile_is_renamed(db, client):
    before = await _revs(client)
    await db.execute("UPDATE profiles SET name = 'Mummy' WHERE id = 1")
    await db.commit()
    changed = _changed(before, await _revs(client))
    assert "tasks" in changed and "shopping" not in changed


async def test_rev_and_today_change_when_the_day_rolls_over(db, client):
    # UTC-11 and UTC+14 are always on different calendar days.
    await database.set_setting(db, database.CALENDAR_TIMEZONE_SETTING, "Pacific/Pago_Pago")
    await db.commit()
    before = (await client.get("/api/rev")).json()
    await database.set_setting(db, database.CALENDAR_TIMEZONE_SETTING, "Pacific/Kiritimati")
    await db.commit()
    after = (await client.get("/api/rev")).json()

    assert date.fromisoformat(after["today"]) > date.fromisoformat(before["today"])
    assert "meals" in _changed(before["widgets"], after["widgets"])  # the week moved on


async def test_rev_changes_when_the_onscreen_keyboard_is_toggled(db, client):
    before = await _revs(client)
    await database.set_onscreen_keyboard(db, True)
    try:
        assert "shopping" in _changed(before, await _revs(client))
    finally:
        await database.set_onscreen_keyboard(db, False)


async def test_dashboard_starts_with_the_same_revisions_as_the_endpoint(db, client):
    # Otherwise every page load would re-fetch every widget straight away.
    await db.execute("INSERT INTO shopping_items (title) VALUES ('Bread')")
    await db.commit()
    html = (await client.get("/")).text
    match = re.search(r"data-revs='([^']*)'", html)
    assert match
    assert json.loads(match.group(1)) == await _revs(client)


# --- Refresh markup ---


def test_every_refreshed_widget_is_registered():
    assert set(freshness.REFRESHED) == set(REFRESH_ROUTES)
    assert all(widget_id in WIDGETS for widget_id in freshness.REFRESHED)


@pytest.mark.parametrize("widget_id", freshness.REFRESHED)
async def test_refresh_markup_is_on_the_widget_from_both_render_paths(client, widget_id):
    marker = f'data-refresh="{REFRESH_ROUTES[widget_id]}" data-rev-key="{widget_id}"'
    own = await client.get(REFRESH_ROUTES[widget_id])
    assert own.status_code == 200
    assert marker in own.text
    assert marker in (await client.get("/")).text


async def test_google_widgets_keep_their_own_polls_and_no_rev(client):
    html = (await client.get("/")).text
    assert 'data-rev-key="calendar"' not in html and 'data-rev-key="weather"' not in html


async def test_dashboard_loads_the_refresh_script(client):
    html = (await client.get("/")).text
    assert '<script src="/static/js/fresh.js"></script>' in html
    assert html.index("gridstack-all.js") < html.index("fresh.js")
    resp = await client.get("/static/js/fresh.js")
    assert resp.status_code == 200


def test_refresh_script_guards_widgets_in_use():
    source = (STATIC_JS / "fresh.js").read_text(encoding="utf-8")
    for guard in (
        "osk-open",
        "details[open]",
        ".just-ticked",
        ".htmx-request",
        "ui-draggable-dragging",
        "ui-resizable-resizing",
        "shouldSwap = false",
    ):
        assert guard in source, guard


# --- The family date ---


async def test_today_endpoint_uses_the_family_timezone(db, client):
    body = (await client.get("/api/today")).json()
    assert body["date"] == (await database.family_today(db)).isoformat()
    assert 1 <= body["next_change_in"] <= 86400


async def test_top_bar_carries_the_date_and_seconds_to_midnight(db, client):
    html = (await client.get("/")).text
    today = (await database.family_today(db)).isoformat()
    match = re.search(r'id="today-date" data-iso="([^"]+)" data-next-change="(\d+)"', html)
    assert match and match.group(1) == today
    assert 1 <= int(match.group(2)) <= 86400


async def test_today_info_at_a_fixed_time(db):
    now = datetime(2026, 9, 28, 22, 30, tzinfo=ZoneInfo("Europe/London"))
    assert await freshness.today_info(db, now) == {"date": "2026-09-28", "next_change_in": 90 * 60}


async def test_today_info_converts_a_utc_now_to_the_family_zone(db):
    # 23:30 UTC on the 28th is already 00:30 on the 29th in London (BST).
    now = datetime(2026, 9, 28, 23, 30, tzinfo=ZoneInfo("UTC"))
    info = await freshness.today_info(db, now)
    assert info == {"date": "2026-09-29", "next_change_in": 23 * 3600 + 30 * 60}


@pytest.mark.parametrize(
    ("local", "expected_hours"),
    [
        (datetime(2026, 3, 29, 0, 30), 22.5),  # London springs forward at 01:00: a 23h day
        (datetime(2026, 10, 25, 0, 30), 24.5),  # falls back at 02:00: a 25h day
        (datetime(2026, 10, 24, 12, 0), 12),  # ordinary day before the change
    ],
)
def test_seconds_until_tomorrow_across_dst(local, expected_hours):
    now = local.replace(tzinfo=ZoneInfo("Europe/London"))
    assert freshness.seconds_until_tomorrow(now) == int(expected_hours * 3600)


def test_seconds_until_tomorrow_rounds_up_and_is_never_zero():
    tz = ZoneInfo("America/New_York")
    just_before = datetime(2026, 1, 1, 23, 59, 59, 999000, tzinfo=tz)
    assert freshness.seconds_until_tomorrow(just_before) == 1
    midnight = datetime(2026, 1, 2, tzinfo=tz)
    assert freshness.seconds_until_tomorrow(midnight) == 86400
    assert freshness.seconds_until_tomorrow(midnight - timedelta(microseconds=1)) == 1
