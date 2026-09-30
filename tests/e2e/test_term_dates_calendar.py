"""Term dates (spec 10.6) end to end: a period added in Admin shows on the
wall calendar as a local-only school bar, even with Google out of reach."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.e2e


async def test_a_period_added_in_admin_shows_on_the_calendar(start_server, page):
    server = start_server()
    server.connect_google()  # the calendar draws its grid only when connected

    # A half term in the middle of this month, so it's on the grid shown by default.
    first = datetime.now(ZoneInfo("Europe/London")).date().replace(day=1)
    start, end = first + timedelta(days=9), first + timedelta(days=12)

    await page.goto(server.url + "/admin/login")
    await page.fill("input[name=pin]", "1234")
    await page.click("button[type=submit]")
    await page.wait_for_selector("nav.admin-tabs")
    await page.goto(server.url + "/admin?tab=school#term-dates")
    await page.wait_for_selector("#term-dates >> text=Term dates missing for")

    form = page.locator("form.term-add")
    await form.locator("select[name=kind]").select_option("half_term")
    await form.locator("input[name=start_date]").fill(start.isoformat())
    await form.locator("input[name=end_date]").fill(end.isoformat())
    await form.locator("input[name=label]").fill("Test half term")
    await form.locator("button[type=submit]").click()
    await page.wait_for_selector("#term-dates >> text=Test half term")
    assert server.query("SELECT kind, start_date, end_date, source FROM school_periods") == [
        ("half_term", start.isoformat(), end.isoformat(), "manual")
    ]

    await page.goto(server.url + "/")
    bar = page.locator("#widget-calendar .cal-bar.cal-school-half_term")
    await bar.first.wait_for(timeout=15000)
    assert "Test half term" in await bar.first.inner_text()
    assert await bar.first.locator("svg use").get_attribute("href") == "/static/icons/lucide/lucide.svg#school"
