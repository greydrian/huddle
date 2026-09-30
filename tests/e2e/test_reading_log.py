"""The reading log (spec 10.10) on the wall, by touch: a child taps "Read
tonight", the tick shows, it's stored for today, and tapping again undoes
it. The tap must not start a Gridstack drag (which would persist a layout).
And the row stays compact: at the default layout the homework still shows."""

import pytest

pytestmark = pytest.mark.e2e


def _layout(server):
    return server.query("SELECT widget_id, grid_x, grid_y, grid_w, grid_h FROM layout_state ORDER BY widget_id")


def _readers(server):
    """Riley and Jamie are children with year groups; Mum and Dad are parents."""
    server.query("UPDATE profiles SET is_parent = 1 WHERE name IN ('Mum', 'Dad')")
    server.query("UPDATE profiles SET school_year = 'Year 4' WHERE name = 'Riley'")
    server.query("UPDATE profiles SET school_year = 'Year 2' WHERE name = 'Jamie'")


@pytest.mark.parametrize("mode", ["day", "night"])
async def test_tick_reading_by_touch(start_server, page, mode):
    server = start_server()
    _readers(server)
    server.query(
        "INSERT INTO homework (profile_id, subject, subject_key, title) VALUES (3, 'Spellings', 'english', 'Week 4')"
    )
    server.query(
        "INSERT INTO app_settings (key, value) VALUES ('appearance', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("light" if mode == "day" else "dark",),
    )
    layout = _layout(server)
    await page.goto(server.url + "/")
    assert await page.get_attribute("html", "data-mode") == mode
    riley = "#widget-homework button[aria-label='Riley read tonight']"
    await page.wait_for_selector(riley + "[aria-pressed='false']")
    assert await page.locator("#widget-homework button[aria-label='Mum read tonight']").count() == 0
    assert await page.locator("#widget-homework .hw-subject.subj-english").count() == 1

    await page.tap(riley)
    await page.wait_for_selector(riley + "[aria-pressed='true']")
    assert len(server.query("SELECT * FROM reading_log")) == 1

    await page.tap(riley)
    await page.wait_for_selector(riley + "[aria-pressed='false']")
    assert server.query("SELECT * FROM reading_log") == []
    assert _layout(server) == layout  # no drag was started by the taps


async def test_homework_shows_below_the_reading_row_at_the_default_size(start_server, page):
    """1280x800, default layout (Homework is 2x4), two children: the row is at
    most two lines and the first homework item is inside the widget body."""
    server = start_server()
    _readers(server)
    server.query("INSERT INTO homework (profile_id, subject, subject_key, title) VALUES (3, 'Maths', 'maths', 'Sheet')")
    server.query(
        "INSERT INTO homework (profile_id, subject, subject_key, title) VALUES (4, 'Topic', 'topic', 'Poster')"
    )
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-homework .rd-tick")
    await page.locator("#widget-homework").scroll_into_view_if_needed()

    body = await page.locator("#widget-homework .widget-body").bounding_box()
    chips = [await page.locator("#widget-homework .rd-tick").nth(i).bounding_box() for i in range(2)]
    tops = sorted({round(c["y"]) for c in chips})
    assert len(tops) <= 2, tops  # at most two lines of chips
    first = await page.locator("#widget-homework .hw-item").first.bounding_box()
    assert first["y"] >= body["y"] and first["y"] + first["height"] <= body["y"] + body["height"], (first, body)
