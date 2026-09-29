"""The reading log (spec 10.10) on the wall, by touch: a child taps "Read
tonight", the tick shows, it's stored for today, and tapping again undoes
it. The tap must not start a Gridstack drag (which would persist a layout)."""

import pytest

pytestmark = pytest.mark.e2e


def _layout(server):
    return server.query("SELECT widget_id, grid_x, grid_y, grid_w, grid_h FROM layout_state ORDER BY widget_id")


@pytest.mark.parametrize("mode", ["day", "night"])
async def test_tick_reading_by_touch(start_server, page, mode):
    server = start_server()
    server.query("UPDATE profiles SET is_parent = 1 WHERE name IN ('Mum', 'Dad')")
    server.query("INSERT INTO homework (profile_id, subject, subject_key, title) VALUES (3, 'Spellings', 'english', 'Week 4')")
    server.query("INSERT INTO app_settings (key, value) VALUES ('appearance', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("light" if mode == "day" else "dark",))
    layout = _layout(server)
    await page.goto(server.url + "/")
    assert await page.get_attribute("html", "data-mode") == mode
    riley = "#widget-homework button[aria-label='Riley read tonight']"
    await page.wait_for_selector(riley + "[aria-pressed='false']")
    assert await page.locator("#widget-homework button[aria-label='Mum read tonight']").count() == 0
    assert await page.locator("#widget-homework .hw-subject.subj-english").count() == 1

    box = await page.locator(riley).bounding_box()
    assert box["height"] >= 48 and box["width"] >= 48

    await page.tap(riley)
    await page.wait_for_selector(riley + "[aria-pressed='true']")
    assert len(server.query("SELECT * FROM reading_log")) == 1

    await page.tap(riley)
    await page.wait_for_selector(riley + "[aria-pressed='false']")
    assert server.query("SELECT * FROM reading_log") == []
    assert _layout(server) == layout  # no drag was started by the taps
