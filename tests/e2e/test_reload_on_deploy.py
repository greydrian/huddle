"""After a deploy the wall reloads itself (app/build_info.py, static/js/
fresh.js): /api/rev's build differs from the page's data-build. It waits
while someone is typing, like the layout reload."""

import json

import pytest

pytestmark = pytest.mark.e2e


async def _wake(page):
    """What the tablet does on waking: fresh.js polls /api/rev at once."""
    await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


async def _pretend_a_new_build(page):
    """Rewrites /api/rev's answer as the new build would give it."""

    async def new_build(route):
        response = await route.fetch()
        body = await response.json()
        body["build"] = "0123456789ab"
        await route.fulfill(response=response, body=json.dumps(body))

    await page.route("**/api/rev", new_build)


async def test_the_wall_reloads_after_a_deploy_once_nobody_is_typing(start_server, page):
    server = start_server()
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-shopping input[name=title]")
    served = await page.get_attribute("#dashboard-grid", "data-build")
    assert served and len(served) == 12

    # Same build: polling leaves the page alone.
    await page.evaluate("window.huddleMarker = 1")
    await _wake(page)
    await page.wait_for_timeout(1000)
    assert await page.evaluate("window.huddleMarker") == 1

    # A new build while someone is typing: no reload yet.
    field = page.locator("#widget-shopping input[name=title]")
    await field.click()
    await field.fill("Milk")
    await _pretend_a_new_build(page)
    await _wake(page)
    await page.wait_for_timeout(1500)
    assert await page.evaluate("window.huddleMarker") == 1
    assert await field.input_value() == "Milk"

    # They stop typing: the next poll reloads the page (the marker is gone).
    await page.evaluate("document.activeElement.blur()")
    async with page.expect_navigation():
        await _wake(page)
    await page.wait_for_selector("#dashboard-grid")
    assert await page.evaluate("window.huddleMarker") is None
