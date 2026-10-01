"""The grouped shopping list (spec 10.8) in a real browser: the invisible
aisle picker covers the item's row (a 48px tap target) between the tick and
delete buttons, choosing an aisle moves the item, and both buttons still work."""

import sqlite3

import pytest

pytestmark = pytest.mark.e2e


async def test_the_aisle_picker_covers_the_row_and_moves_the_item(start_server, page):
    server = start_server()
    with sqlite3.connect(server.db_path) as conn:
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('shopping_view', 'grouped')")
        conn.execute("INSERT INTO shopping_items (title) VALUES ('Party rings')")
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-shopping .shopping-group")

    picker = page.locator("select[aria-label='Aisle for Party rings']")
    row = page.locator(".shopping-row", has=picker)
    box, row_box = await picker.bounding_box(), await row.bounding_box()
    assert box["height"] >= 48 and box["height"] >= row_box["height"] - 2
    assert box["width"] >= row_box["width"] - 84 - 2  # all but the two buttons

    await picker.select_option("snacks")
    await page.wait_for_selector("#widget-shopping h3:text('Snacks & sweets')")
    await page.click("button[aria-label='Tick Party rings']")
    await page.wait_for_selector("button[aria-label='Untick Party rings']")
    await page.click("button[aria-label='Remove Milk']")
    await page.wait_for_function("!document.querySelector(\"button[aria-label='Remove Milk']\")")
