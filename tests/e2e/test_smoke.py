"""Smoke tests for the things unit tests can't see: layout, touch, JS.

1280x800, the kiosk's landscape size. Uses native clicks and waits on the
expected result (HTMX makes networkidle-style waits unreliable).
"""

import asyncio

import pytest

pytestmark = pytest.mark.e2e

WIDGETS = ("calendar", "tasks", "shopping", "meals", "weather", "photos", "homework", "practice_words")


async def _touch_drag(page, start, end, steps=12):
    """A real touch gesture via CDP (Playwright has no touch-move API)."""
    cdp = await page.context.new_cdp_session(page)
    (x0, y0), (x1, y1) = start, end
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x0, "y": y0}]})
    for i in range(1, steps + 1):
        x = x0 + (x1 - x0) * i / steps
        y = y0 + (y1 - y0) * i / steps
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
        await asyncio.sleep(0.02)
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    await cdp.detach()


async def _centre(locator):
    box = await locator.bounding_box()
    assert box, "element not visible"
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _layout(server):
    return {row[0]: row[1:] for row in server.query("SELECT widget_id, grid_x, grid_y, grid_w, grid_h FROM layout_state")}


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.1)
    return True


async def test_dashboard_renders_every_widget(start_server, page):
    server = start_server()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.goto(server.url + "/")
    await page.wait_for_selector(".grid-stack-item")
    for widget in WIDGETS:
        assert await page.locator(f"#widget-{widget}").count() == 1, widget
    assert await page.locator(".grid-stack-item").count() == len(WIDGETS)
    # Seeded data made it through, and the weather came from the cache.
    await page.wait_for_selector("#widget-tasks >> text=Feed the cat")
    await page.wait_for_selector("#widget-weather >> text=Reading")
    assert errors == []


async def test_header_drag_moves_a_widget_but_a_body_swipe_scrolls(start_server, page):
    server = start_server()
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-weather .drag-handle")
    before = _layout(server)
    scroller = page.locator("#dashboard-scroll")

    # A swipe up on a card's body scrolls the dashboard and moves nothing.
    x, y = await _centre(page.locator("#widget-calendar .widget-body"))
    await _touch_drag(page, (x, y + 150), (x, y - 150))
    await page.wait_for_function("document.getElementById('dashboard-scroll').scrollTop > 50")
    await asyncio.sleep(0.5)
    assert _layout(server) == before

    # Dragging a header moves that widget, and the move is saved.
    await scroller.evaluate("el => el.scrollTop = 0")
    await page.locator("#widget-weather").scroll_into_view_if_needed()
    handle = page.locator("#widget-weather .drag-handle")
    x, y = await _centre(handle)
    await _touch_drag(page, (x, y), (x - 230, y), steps=20)
    assert await _wait_for(lambda: _layout(server)["weather"] != before["weather"]), _layout(server)


async def test_ticking_a_task_folds_it_into_done(start_server, page):
    server = start_server()
    await page.goto(server.url + "/")
    row = page.locator("#widget-tasks .task-row", has_text="Feed the cat")
    await row.locator(".task-check").click()
    summary = await page.wait_for_selector("#widget-tasks details.task-done summary")
    assert (await summary.inner_text()).strip() == "✓ 1 done"
    # Folded away: still one open task above the fold, the ticked one inside it.
    assert await page.locator("#widget-tasks details.task-done").get_attribute("open") is None
    assert await page.locator("#widget-tasks .task-group > .task-row").count() == 1
    assert server.query("SELECT is_completed FROM tasks WHERE title = 'Feed the cat'") == [(1,)]


async def test_onscreen_keyboard_opens_on_the_shopping_input(start_server, page):
    server = start_server(keyboard=True)
    await page.goto(server.url + "/")
    field = page.locator("#widget-shopping input[name=title]")
    await field.click()
    await page.wait_for_selector(".osk.osk--open", state="visible")
    assert await field.get_attribute("inputmode") == "none"  # Android's keyboard stays down
    await page.locator(".osk .hg-button[data-skbtn='M']").click()
    assert await field.input_value() == "M"


async def test_admin_login_and_scroll(start_server, page):
    server = start_server()
    await page.goto(server.url + "/")
    await page.click("a[href='/admin']")
    await page.wait_for_selector("input[name=pin]")
    await page.fill("input[name=pin]", "1234")
    await page.click("button[type=submit]")
    await page.wait_for_selector("h2:has-text('Family Members')")
    assert page.url.rstrip("/").endswith("/admin")

    # The Family tab is taller than the screen and must scroll (the
    # dashboard locks html/body, so this is easy to break).
    last = page.locator("h3:has-text('Handwriting style')")
    assert (await last.bounding_box())["y"] > 800
    await page.mouse.move(640, 400)
    for _ in range(40):
        await page.mouse.wheel(0, 600)
    await page.wait_for_function("window.scrollY > 800")
    box = await last.bounding_box()
    assert box and 0 <= box["y"] < 800

    # Tabs are links: System has the PIN, and only its own sections.
    await page.click("a.admin-tab:has-text('System')")
    await page.wait_for_selector("h2:has-text('Change Admin PIN')")
    assert page.url.endswith("/admin?tab=system")
    assert await page.locator("h2:has-text('Family Members')").count() == 0

    # An old hash-only link (the sync dot's, before tabs) still lands on its tab.
    await page.goto(server.url + "/admin#sync")
    await page.wait_for_selector("#sync")
    assert "tab=google" in page.url and page.url.endswith("#sync")
