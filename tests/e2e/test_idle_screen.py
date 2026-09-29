"""The idle screen (spec 10.2, static/js/idle.js) in a real browser, with a
fake clock (page.clock) so minutes pass instantly: idle after the delay,
never while the wall is in use or just after a banner, the first tap only
wakes (a task is NOT ticked), the dim overlay, Fully Kiosk's brightness
calls, the slideshow's crossfade and overlay, the clock screen with no
photos, and banners above the slideshow."""

import io
import json
import secrets
import sqlite3

import pytest
from PIL import Image

pytestmark = pytest.mark.e2e

# A stand-in for Fully Kiosk's JavaScript interface (Fully PLUS).
FAKE_FULLY = """
window.__brightness = [];
window.fully = {
  getScreenBrightness: () => 180,
  setScreenBrightness: (v) => { window.__brightness.push(v); },
};
"""


def _settings(server, **values):
    settings = {"mode": "slideshow", "night_mode": "dim", "delay_minutes": 5, "night_start": "", "night_end": "",
                "dim_percent": 8, "interval_seconds": 20, **values}
    server.query("INSERT INTO app_settings (key, value) VALUES ('idle_settings', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(settings),))


def _appearance(server, mode):
    server.query("INSERT INTO app_settings (key, value) VALUES ('appearance', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("light" if mode == "day" else "dark",))


def _seed_photos(server, count=3):
    """Photos as google_photos.py leaves them: files under DATA_DIR/photos, one row each."""
    folder = server.db_path.parent / "photos"
    folder.mkdir(exist_ok=True)
    with sqlite3.connect(server.db_path) as conn:
        for i in range(count):
            stem = secrets.token_hex(16)
            out = io.BytesIO()
            Image.new("RGB", (320, 200), (60 * i, 120, 200 - 50 * i)).save(out, "JPEG")
            (folder / f"{stem}.jpg").write_bytes(out.getvalue())
            (folder / f"{stem}_t.jpg").write_bytes(out.getvalue())
            conn.execute("INSERT INTO photos (filename, thumb, width, height, bytes) VALUES (?, ?, 320, 200, ?)",
                         (f"{stem}.jpg", f"{stem}_t.jpg", len(out.getvalue())))


async def _state(page):
    return await page.evaluate("window.HuddleIdle.state()")


async def _open(page, server):
    await page.clock.install()
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-tasks >> text=Feed the cat")
    await page.wait_for_function("window.HuddleIdle && window.HuddleIdle.state")


async def _tick_box(page):
    """Where a finger would tick "Feed the cat" (scrolled into view first)."""
    check = page.locator("#widget-tasks .task-row", has_text="Feed the cat").locator(".task-check")
    await check.scroll_into_view_if_needed()
    box = await check.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


@pytest.mark.parametrize("mode", ["day", "night"])
async def test_idle_after_the_delay_and_the_first_tap_only_wakes(start_server, page, mode):
    server = start_server()
    _appearance(server, mode)
    _settings(server, night_mode="slideshow")  # the clock screen by day and night
    await _open(page, server)
    assert await page.get_attribute("html", "data-mode") == mode
    x, y = await _tick_box(page)

    await page.clock.run_for("04:50")
    assert (await _state(page))["idle"] is False
    await page.clock.run_for("00:15")
    await page.wait_for_selector("#idle-screen.is-clock", state="visible")
    # No photos: a calm clock with the family's time and date.
    assert await page.locator(".idle-time").inner_text() != ""
    assert ":" in await page.locator(".idle-time").inner_text()
    assert await page.locator(".idle-date").inner_text() != ""
    await page.screenshot(path=str(server.db_path.parent / f"idle-clock-{mode}.png"))

    # The first tap, right on a task's tick box, only wakes the screen.
    await page.touchscreen.tap(x, y)
    await page.wait_for_selector("#idle-screen", state="hidden")
    await page.clock.run_for(1000)
    assert server.query("SELECT is_completed FROM tasks WHERE title = 'Feed the cat'") == [(0,)]
    assert await page.locator("#widget-tasks .just-ticked").count() == 0
    # The next tap works normally.
    await page.touchscreen.tap(x, y)
    await page.wait_for_selector("#widget-tasks details.task-done summary")
    assert server.query("SELECT is_completed FROM tasks WHERE title = 'Feed the cat'") == [(1,)]


async def test_a_mouse_click_wakes_without_clicking_either(start_server, page):
    server = start_server()
    _settings(server, mode="dim")
    await _open(page, server)
    x, y = await _tick_box(page)
    await page.clock.run_for("05:05")
    await page.wait_for_selector("#idle-screen.is-dim", state="visible")
    await page.mouse.click(x, y)
    await page.wait_for_selector("#idle-screen", state="hidden")
    await page.clock.run_for(1000)
    assert server.query("SELECT is_completed FROM tasks WHERE title = 'Feed the cat'") == [(0,)]


async def test_never_idle_while_the_keyboard_is_in_use(start_server, page):
    server = start_server(keyboard=True)
    await _open(page, server)
    await page.click("#widget-shopping input[name=title]")
    await page.wait_for_selector(".osk.osk--open", state="visible")
    for _ in range(10):  # someone typing now and then for 10 minutes
        await page.clock.run_for("01:00")
        await page.click('.osk [data-skbtn="{space}"]')
    assert (await _state(page))["idle"] is False
    assert await page.locator(".osk.osk--open").count() == 1
    await page.click('.osk [data-skbtn="{done}"]')
    await page.wait_for_selector(".osk.osk--open", state="hidden")
    # The full delay starts again once the keyboard is closed.
    await page.clock.run_for("04:30")
    assert (await _state(page))["idle"] is False
    await page.clock.run_for("00:45")
    assert (await _state(page))["idle"] is True


async def test_an_abandoned_keyboard_is_closed_so_the_wall_still_idles(start_server, page):
    server = start_server(keyboard=True)
    _settings(server, mode="dim")
    await _open(page, server)
    await page.click("#widget-shopping input[name=title]")
    await page.wait_for_selector(".osk.osk--open", state="visible")
    await page.click('.osk [data-skbtn="{space}"]')  # typed a little, then walked away
    await page.clock.run_for("01:50")
    assert await page.locator(".osk.osk--open").count() == 1  # still within 2 minutes: left alone
    await page.clock.run_for("00:15")
    await page.wait_for_selector(".osk.osk--open", state="hidden")
    assert await page.evaluate("document.activeElement === document.body")
    assert (await _state(page))["idle"] is False
    await page.clock.run_for("05:05")  # then the usual delay
    await page.wait_for_selector("#idle-screen.is-dim", state="visible")


async def test_an_abandoned_focused_field_is_let_go(start_server, page):
    server = start_server()  # the tablet's own keyboard: only the focus holds the wall
    await _open(page, server)
    await page.click("#widget-shopping input[name=title]")
    await page.keyboard.type("Bread")
    await page.clock.run_for("02:05")
    assert await page.evaluate("document.activeElement === document.body")
    assert await page.input_value("#widget-shopping input[name=title]") == "Bread"  # what was typed stays
    await page.clock.run_for("05:05")
    assert (await _state(page))["idle"] is True


async def test_never_idle_while_busy_but_a_widget_refreshing_itself_is_not_busy(start_server, page):
    server = start_server()
    await _open(page, server)
    # Someone mid-tick (fresh.js's busy rules: a tick's reveal still showing).
    await page.evaluate("document.querySelector('#widget-tasks .task-row').classList.add('just-ticked')")
    await page.clock.run_for("08:00")
    assert (await _state(page))["idle"] is False
    await page.evaluate("document.querySelector('#widget-tasks .just-ticked').classList.remove('just-ticked')")
    # The calendar polling itself (every 3 minutes) is nobody at the wall.
    await page.evaluate("document.getElementById('widget-calendar').classList.add('htmx-request')")
    await page.clock.run_for("05:05")
    assert (await _state(page))["idle"] is True


async def test_not_just_after_a_banner_appears(start_server, page):
    server = start_server()
    await _open(page, server)
    await page.clock.run_for("04:30")
    # A new banner arrives the way fresh.js brings one: the bar is swapped in.
    await page.evaluate("""() => {
      const bar = document.getElementById('banner-bar');
      const b = document.createElement('button');
      b.className = 'banner'; b.dataset.key = 'chores:new'; b.textContent = 'New';
      bar.appendChild(b);
      window.__bannerEvents = 0;
      document.addEventListener('huddle:banner', () => { window.__bannerEvents += 1; });
      document.body.dispatchEvent(new CustomEvent('htmx:afterSwap', { bubbles: true }));
    }""")
    assert await page.evaluate("window.__bannerEvents") == 1
    await page.clock.run_for("00:45")  # 5:15 since the last touch, but only 45s since the banner
    assert (await _state(page))["idle"] is False
    await page.clock.run_for("00:20")
    assert (await _state(page))["idle"] is True


async def test_dim_is_a_dark_overlay_without_fully(start_server, page):
    server = start_server()
    _settings(server, mode="dim")
    await _open(page, server)
    await page.clock.run_for("05:05")
    screen = page.locator("#idle-screen")
    await page.wait_for_selector("#idle-screen.is-dim", state="visible")
    assert await screen.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0.85)"
    assert await page.locator(".idle-overlay").is_hidden()


async def test_dim_turns_fullys_backlight_down_and_back(start_server, page):
    await page.add_init_script(FAKE_FULLY)
    server = start_server()
    _settings(server, mode="dim", dim_percent=10)
    await _open(page, server)
    await page.clock.run_for("05:05")
    await page.wait_for_selector("#idle-screen.is-dim-backlight", state="visible")
    assert await page.evaluate("window.__brightness") == [26]  # 10% of 255
    # The overlay itself is see-through: the backlight does the dimming.
    assert await page.locator("#idle-screen").evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
    await page.touchscreen.tap(640, 400)
    await page.wait_for_selector("#idle-screen", state="hidden")
    assert await page.evaluate("window.__brightness") == [26, 180]


@pytest.mark.parametrize("reading", ["0", "undefined", "'n/a'"])
async def test_an_unknown_brightness_is_never_changed(start_server, page, reading):
    """Fully can't say what the backlight is: nothing to put back on wake, so
    it isn't touched and the dark overlay dims instead."""
    await page.add_init_script(FAKE_FULLY.replace("() => 180", f"() => {reading}"))
    server = start_server()
    _settings(server, mode="dim")
    await _open(page, server)
    await page.clock.run_for("05:05")
    await page.wait_for_selector("#idle-screen.is-dim", state="visible")
    await page.touchscreen.tap(640, 400)
    await page.wait_for_selector("#idle-screen", state="hidden")
    assert await page.evaluate("window.__brightness") == []


async def test_a_reload_while_dim_comes_back_dim_and_restores_later(start_server, page):
    await page.add_init_script(FAKE_FULLY)
    server = start_server()
    _settings(server, mode="dim")
    await _open(page, server)
    await page.clock.run_for("05:05")
    await page.wait_for_selector("#idle-screen.is-dim-backlight", state="visible")
    await page.reload()  # e.g. fresh.js reloading for a new day
    await page.wait_for_function("window.HuddleIdle && window.HuddleIdle.state().idle")
    assert (await _state(page))["savedBrightness"] == 180
    await page.touchscreen.tap(640, 400)
    await page.wait_for_function("window.__brightness.includes(180)")


async def test_night_uses_the_night_behaviour(start_server, page):
    server = start_server()
    _settings(server, night_start="00:00", night_end="23:59")  # Admin's own night: all day
    _seed_photos(server)
    await _open(page, server)
    await page.clock.run_for("05:05")
    assert (await _state(page))["kind"] == "dim"


async def test_slideshow_crossfades_with_the_overlay(start_server, page):
    server = start_server()
    server.seed_banners()
    # The slideshow by night too: otherwise after 19:00 (night colours) the
    # default night behaviour dims instead, and the test depends on the clock.
    _settings(server, interval_seconds=20, night_mode="slideshow")
    _seed_photos(server, 3)

    async def with_event(route):
        response = await route.fetch()
        body = await response.json()
        body["next_event"] = {"title": "Swimming", "time": "16:30"}
        await route.fulfill(response=response, json=body)

    await page.route("**/api/idle", with_event)
    await _open(page, server)
    await page.clock.run_for("05:05")
    await page.wait_for_selector("#idle-screen.is-slideshow", state="visible")
    shown = page.locator(".idle-photo.is-shown")
    await shown.wait_for()
    first = await shown.get_attribute("src")
    assert first.startswith("/photos/")
    assert await shown.evaluate("img => img.naturalWidth") == 320
    # Opacity only, for the tablet.
    assert await shown.evaluate("img => getComputedStyle(img).transitionProperty") == "opacity"
    # The overlay: family time, date, the next event and the weather (from the caches).
    assert ":" in await page.locator(".idle-time").inner_text()
    await page.wait_for_selector(".idle-event:not([hidden])")
    assert "16:30" in await page.locator(".idle-event").inner_text()
    assert "Swimming" in await page.locator(".idle-event").inner_text()
    assert "14°" in await page.locator(".idle-weather").inner_text()
    # Banners stay visible above the slideshow.
    banner = page.locator("#banner-bar .banner").first
    box = await banner.bounding_box()
    on_top = await page.evaluate("([x, y]) => !!document.elementFromPoint(x, y).closest('#banner-bar')",
                                 [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
    assert on_top
    await page.screenshot(path=str(server.db_path.parent / "idle-slideshow.png"))

    await page.clock.run_for("00:21")
    await page.wait_for_function("(src) => document.querySelector('.idle-photo.is-shown').getAttribute('src') !== src",
                                 arg=first)
    assert await page.locator(".idle-photo.is-shown").count() == 1
    assert (await _state(page))["index"] == 1
