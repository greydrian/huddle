"""The calendar by touch (spec 10.5): switching views, filtering by person,
opening a day from the month grid's hit areas, adding an event with the
on-screen keyboard, and the return to the default view after 3 minutes
untouched. Google is unreachable (the dead proxy): every view is drawn from
the seeded calendar_cache, and an add fails as offline."""

import pytest

pytestmark = pytest.mark.e2e


def _layout(server):
    return server.query("SELECT widget_id, grid_x, grid_y, grid_w, grid_h FROM layout_state ORDER BY widget_id")


async def _open(start_server, page, **kwargs):
    server = start_server(**kwargs)
    server.seed_calendar()
    before = _layout(server)
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-calendar[data-view=month] .cal-day-hit")
    return server, before


@pytest.mark.parametrize("mode", ["light", "dark"])
async def test_switch_views_and_filter_by_touch(start_server, page, mode):
    server = start_server()
    server.query("INSERT INTO app_settings (key, value) VALUES ('appearance', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (mode,))
    server.seed_calendar()
    before = _layout(server)
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-calendar[data-view=month] .cal-day-hit")
    assert await page.locator("#widget-calendar .cal-stale").count() == 1  # from the saved copy
    assert await page.locator("#widget-calendar >> text=+2 more").count() == 1

    await page.tap("#widget-calendar .cal-view-btn >> text=Week")
    await page.wait_for_selector("#widget-calendar[data-view=week] .cal-wk-event >> text=Swimming")
    assert await page.locator("#widget-calendar .cal-wk-event >> text=Jamie: Dentist").count() == 1

    await page.tap("#widget-calendar .cal-person >> text=Riley")
    await page.wait_for_selector("#widget-calendar[data-view=week][data-person]")
    assert await page.locator("#widget-calendar >> text=Swimming").count() == 1
    assert await page.locator("#widget-calendar >> text=Jamie: Dentist").count() == 0
    assert await page.locator("#widget-calendar >> text=Bins out").count() == 1  # everyone's

    await page.tap("#widget-calendar .cal-view-btn >> text=Agenda")  # the filter stays on
    await page.wait_for_selector("#widget-calendar[data-view=agenda][data-person] .cal-agenda-label >> text=Tomorrow")
    assert await page.locator("#widget-calendar >> text=Jamie: Dentist").count() == 0

    await page.tap("#widget-calendar .cal-person[aria-pressed=true]")  # tap again: everyone
    await page.wait_for_selector("#widget-calendar[data-view=agenda]:not([data-person]) >> text=Jamie: Dentist")

    await page.tap("#widget-calendar .cal-view-btn >> text=Month")
    await page.wait_for_selector("#widget-calendar[data-view=month]")
    await _tap_today(page)
    await page.wait_for_selector("#widget-calendar[data-view=day]")
    await page.screenshot(path=str(server.db_path.parent / f"calendar-{mode}.png"))
    await page.tap("#widget-calendar .cal-back-btn")
    await page.wait_for_selector("#widget-calendar[data-view=month]")

    assert _layout(server) == before  # no tap turned into a Gridstack drag


async def _tap_today(page):
    """Today's hit area in the month grid (a tap anywhere in its column)."""
    week = page.locator("#widget-calendar .cal-week", has=page.locator(".cal-day-num.today"))
    today = await week.locator(".cal-day-num").evaluate_all(
        "(els) => els.findIndex((e) => e.classList.contains('today'))")
    await week.locator(".cal-day-hit").nth(today).tap()


async def test_day_hit_opens_that_day(start_server, page):
    await _open(start_server, page)
    await _tap_today(page)
    await page.wait_for_selector("#widget-calendar[data-view=day] >> text=Jamie: Dentist")
    assert await page.locator("#widget-calendar >> text=Swimming").count() == 1


async def test_add_event_with_the_on_screen_keyboard(start_server, page):
    server, before = await _open(start_server, page, keyboard=True)

    await page.tap("#widget-calendar .cal-add-toggle")
    await page.wait_for_selector("#cal-add-form:not([hidden])")
    await page.select_option("#cal-add-form select[name=start_time]", "16:30")
    await page.tap("#cal-add-form .task-chip-person >> text=Riley")
    assert await page.is_checked("#cal-add-form .task-chip-person:has-text('Riley') input")
    await page.tap("#cal-add-form input[name=title]")
    await page.wait_for_selector(".osk.osk--open", state="visible")
    for key in "Gym":  # the keyboard starts shifted
        await page.tap(f".osk .hg-button[data-skbtn='{key}']")
    assert await page.input_value("#cal-add-form input[name=title]") == "Gym"
    await page.tap(".osk .hg-button[data-skbtn='{enter}']")  # Enter sends the form

    # Google is out of reach: the form stays open with what was typed.
    await page.wait_for_selector("#cal-add-form .cal-add-error >> text=Couldn't reach Google Calendar")
    assert await page.input_value("#cal-add-form input[name=title]") == "Gym"
    assert await page.is_checked("#cal-add-form .task-chip-person:has-text('Riley') input")
    assert await page.input_value("#cal-add-form select[name=start_time]") == "16:30"
    assert _layout(server) == before


async def test_returns_to_the_default_view_after_three_idle_minutes(start_server, page):
    server = start_server()
    server.seed_calendar()
    server.query("INSERT INTO app_settings (key, value) VALUES ('calendar_default_view', '\"agenda\"')")
    await page.clock.install()
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-calendar[data-view=agenda]")

    await page.tap("#widget-calendar .cal-view-btn >> text=Week")
    await page.wait_for_selector("#widget-calendar[data-view=week]")
    await page.tap("#widget-calendar .cal-person >> text=Riley")
    await page.wait_for_selector("#widget-calendar[data-person]")

    await page.clock.run_for("02:00")
    assert await page.locator("#widget-calendar[data-view=week][data-person]").count() == 1
    await page.clock.run_for("01:10")
    await page.wait_for_selector("#widget-calendar[data-view=agenda]:not([data-person])")
