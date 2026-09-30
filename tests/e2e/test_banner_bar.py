"""Notification banner (spec 10.1) in a real browser, by touch: it shows
under the top bar, "+N more" opens the rest, a tap dismisses one (and it
stays dismissed after a reload), the chime rings once, the text is readable
by day and night, and no widget moves."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.e2e

CONTRAST = """
(el) => {
  const rgb = (s) => s.match(/\\d+(\\.\\d+)?/g).slice(0, 3).map(Number);
  const lum = (c) => {
    const [r, g, b] = c.map((v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const text = el.querySelector('.banner-text');
  const fg = lum(rgb(getComputedStyle(text).color)), bg = lum(rgb(getComputedStyle(el).backgroundColor));
  return (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
}
"""


# A stand-in AudioContext: its state starts as window.__audioStart (the real
# one's depends on the browser's autoplay policy), resume() starts it, and
# window.__notes counts the notes actually scheduled.
FAKE_AUDIO = """
window.__notes = 0;
window.AudioContext = class {
  constructor() { this.state = window.__audioStart || 'running'; this.currentTime = 0; this.destination = {}; }
  resume() { this.state = 'running'; return Promise.resolve(); }
  createOscillator() {
    return { frequency: {}, connect: (n) => n, start: () => { window.__notes += 1; }, stop: () => {} };
  }
  createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect: (n) => n }; }
};
"""


def _skip_quiet_minutes():
    if datetime.now(ZoneInfo("Europe/London")).strftime("%H:%M") < "00:03":
        pytest.skip("inside the seeded quiet hours (00:00-00:02)")


def _layout(server):
    return {
        row[0]: row[1:] for row in server.query("SELECT widget_id, grid_x, grid_y, grid_w, grid_h FROM layout_state")
    }


@pytest.mark.parametrize("mode", ["day", "night"])
async def test_banner_shows_expands_and_dismisses_by_touch(start_server, page, mode):
    _skip_quiet_minutes()
    await page.add_init_script(FAKE_AUDIO)  # autoplay allowed
    server = start_server()
    server.seed_banners(sound=True)
    server.query(
        "INSERT INTO app_settings (key, value) VALUES ('appearance', ?)", ("light" if mode == "day" else "dark",)
    )
    before = _layout(server)

    await page.goto(server.url + "/")
    await page.wait_for_selector("#banner-bar .banner")
    assert await page.get_attribute("html", "data-mode") == mode
    shown = page.locator("#banner-bar .banner:visible")
    assert await shown.count() == 2
    assert await shown.nth(0).inner_text() == "M\nMum\n2 chores still to do"
    assert (await page.locator("#banner-bar [data-more]").inner_text()).strip() == "+1 more"
    assert await page.evaluate("window.huddleChimes") == 1  # one chime for the new banners, not three

    # In the page's flow: below the top bar, above the grid, overlapping neither.
    bar = await page.locator("#banner-bar").bounding_box()
    top = await page.locator(".topbar").bounding_box()
    scroller = await page.locator("#dashboard-scroll").bounding_box()
    assert top["y"] + top["height"] <= bar["y"] and bar["y"] + bar["height"] <= scroller["y"]
    for i in range(2):
        box = await shown.nth(i).bounding_box()
        assert box["height"] >= 48 and box["width"] >= 48
        assert await shown.nth(i).evaluate(CONTRAST) >= 4.5

    # "+1 more" opens the third.
    await page.tap("#banner-bar [data-more]")
    await page.wait_for_selector("#banner-bar .banner-extra:visible")
    assert await page.locator("#banner-bar .banner:visible").count() == 3

    # Tap Mum's banner: it goes, and stays gone after a reload.
    key = await shown.nth(0).get_attribute("data-key")
    await page.tap(f"#banner-bar [data-key='{key}']")
    await page.wait_for_selector(f"#banner-bar [data-key='{key}']", state="detached")
    await page.reload()
    await page.wait_for_selector("#banner-bar .banner")
    assert await page.locator(f"#banner-bar [data-key='{key}']").count() == 0
    assert await page.locator("#banner-bar .banner:visible").count() == 2
    assert await page.locator("#banner-bar [data-more]").count() == 0
    assert await page.evaluate("window.huddleChimes") == 0  # already heard: no repeat on reload

    assert _layout(server) == before  # the bar pushed the grid down; no widget moved


async def test_no_chime_while_audio_is_suspended_and_none_replayed_after(start_server, page):
    """Before the first touch audio is suspended: nothing is scheduled (it
    would all play at once on the touch) or marked as chimed. After the
    touch, only a banner that appears from then on chimes."""
    _skip_quiet_minutes()
    await page.add_init_script("window.__audioStart = 'suspended';" + FAKE_AUDIO)
    server = start_server()
    server.seed_banners(sound=True)
    await page.goto(server.url + "/")
    await page.wait_for_selector("#banner-bar .banner")
    assert await page.evaluate("[window.huddleChimes, window.__notes]") == [0, 0]
    assert await page.evaluate("localStorage.getItem('huddle-banner-chimed')") is None

    await page.tap(".topbar .today")  # the first touch: audio starts
    await page.wait_for_timeout(300)
    assert await page.evaluate("[window.huddleChimes, window.__notes]") == [0, 0]  # nothing stale replayed

    server.query("INSERT INTO tasks (profile_id, title) VALUES (2, 'Recycling')")  # Dad: a new banner
    await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")  # fresh.js polls now
    await page.wait_for_selector("#banner-bar [data-key^='chores:2:']", state="attached")
    await page.wait_for_function("window.huddleChimes === 1")
    assert await page.evaluate("window.__notes") == 2
    chimed = await page.evaluate("Object.keys(JSON.parse(localStorage.getItem('huddle-banner-chimed')))")
    assert len(chimed) == 1 and chimed[0].startswith("chores:2:")
