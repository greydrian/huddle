"""Hiding and showing a widget in Admin (spec 10.3), end to end: the wall
reloads itself (static/js/fresh.js), the gap closes, and it comes back."""

import pytest

pytestmark = pytest.mark.e2e


def _layout(server):
    return {row[0]: row[1:] for row in server.query(
        "SELECT widget_id, grid_x, grid_y, grid_w, grid_h, is_visible FROM layout_state")}


async def _wake(page):
    """What the tablet does on waking: fresh.js polls /api/rev at once
    (instead of waiting up to 30s for its timer)."""
    await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


async def test_hide_and_show_a_widget_from_admin(start_server, page):
    server = start_server()
    before = _layout(server)
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-calendar")
    calendar_top = (await page.locator("#widget-calendar").bounding_box())["y"]

    admin = await page.context.new_page()
    await admin.goto(server.url + "/admin?tab=display")
    await admin.fill("input[name=pin]", "1234")
    await admin.click("button[type=submit]")
    await admin.wait_for_selector("#widgets")
    switch = admin.locator("button[aria-label='Show: Calendar']")
    assert await switch.get_attribute("aria-checked") == "true"
    await switch.click()
    await admin.wait_for_selector("button[aria-label='Show: Calendar'][aria-checked='false']")

    # The wall notices, reloads, and the widgets below the calendar fill its space.
    await _wake(page)
    await page.wait_for_selector("#widget-calendar", state="detached")
    await page.wait_for_selector("#widget-tasks")
    assert await page.locator(".grid-stack-item[gs-id='tasks']").get_attribute("gs-y") == "0"
    assert abs((await page.locator("#widget-tasks").bounding_box())["y"] - calendar_top) < 20
    hidden = _layout(server)
    assert hidden["calendar"] == before["calendar"][:4] + (0,)  # its saved place is kept
    assert all(hidden[w][1] == before[w][1] - 7 for w in hidden if w != "calendar")  # all were below it
    assert all((hidden[w][0], *hidden[w][2:4]) == (before[w][0], *before[w][2:4]) for w in hidden)  # x, w, h kept

    # Showing it again moves everything back: exactly the layout from before.
    await admin.locator("button[aria-label='Show: Calendar']").click()
    await admin.wait_for_selector("button[aria-label='Show: Calendar'][aria-checked='true']")
    assert _layout(server) == before

    # The wall hasn't noticed yet, and a drag there now would save the old
    # (gap-closed) positions: the server refuses it (409) and the page reloads.
    await page.evaluate("persistLayout()")
    await page.wait_for_selector("#widget-calendar")
    assert _layout(server) == before
    assert await page.locator(".grid-stack-item[gs-id='tasks']").get_attribute("gs-y") == "7"
    assert await page.locator(".grid-stack-item").count() == len(before)
