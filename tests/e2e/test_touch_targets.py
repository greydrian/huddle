"""Touch targets are at least 48 x 48 px (spec 10.0): young children use the
display. WCAG asks for 24 px (2.5.8 AA) or 44 px (2.5.5 AAA); we go further.

Measures every visible interactive element on the dashboard (with the
on-screen keyboard open) and on each Admin tab, at 1280x800. A checkbox or
radio inside a <label> is measured by its label, which is what a finger
hits. A control that sets --hit-x/--hit-y has its tappable box grown by an
invisible ::after ("Hit areas" in style.css), which a bounding box misses.
"""

import pytest

from app.admin_tabs import TABS

pytestmark = pytest.mark.e2e

MIN = 48

# Justified exceptions, each a CSS selector. Keep this short.
ALLOWLIST = (
    # Links inside a sentence (WCAG 2.5.8's "inline" exception): Admin only,
    # each also reachable as a full-size tab.
    ".inline-link",
    # The colour picker for a new family member (Admin): a native swatch;
    # its row's inputs and buttons are full size.
    "input[type=color]",
)

MEASURE = """
([selectors, allow]) => {
  const out = [];
  for (const el of document.querySelectorAll(selectors)) {
    if (allow.some((s) => el.matches(s))) continue;
    // A checkbox/radio is tapped through its label.
    if ((el.type === 'checkbox' || el.type === 'radio') && el.closest('label')) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;  // inside a closed <details>, etc.
    let w = r.width, h = r.height;
    if (style.getPropertyValue('--hit-x').trim()) {
      const a = getComputedStyle(el, '::after');
      if (a.content !== 'none' && a.position === 'absolute') {
        w = Math.max(w, r.width - parseFloat(a.left) - parseFloat(a.right));
        h = Math.max(h, r.height - parseFloat(a.top) - parseFloat(a.bottom));
      }
    }
    if (w < 48 || h < 48) {
      const label = (el.getAttribute('aria-label') || el.textContent || el.value || el.name || '').trim().slice(0, 40);
      out.push(`${el.tagName.toLowerCase()}.${[...el.classList].join('.')} "${label}" ${Math.round(w)}x${Math.round(h)}`);
    }
  }
  return out;
}
"""

SELECTORS = ", ".join([
    "a[href]", "button", "select", "textarea", "summary",
    # hx-* on a form is submitted by its button; a polling hx-get (every Ns) isn't tapped.
    "[hx-get]:not(form):not([hx-trigger^=every])", "[hx-post]:not(form)",
    "input:not([type=hidden])", "label:has(input[type=checkbox])", "label:has(input[type=radio])",
    ".osk .hg-button",
])


async def _too_small(page):
    return await page.evaluate(MEASURE, [SELECTORS, list(ALLOWLIST)])


def _seed_content(server):
    """Homework and a word list, so their widgets and Admin rows show buttons."""
    server.query("INSERT INTO homework (profile_id, subject, title, due_date) VALUES (3, 'Maths', 'Fractions', NULL)")
    server.query("INSERT INTO practice_word_lists (profile_id, title, words) VALUES (3, 'Week 4', 'because\nwhich')")


async def test_dashboard_touch_targets(start_server, page):
    server = start_server(keyboard=True)
    _seed_content(server)
    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-tasks >> text=Feed the cat")
    await page.wait_for_selector("#widget-homework >> text=Fractions")
    await page.click("#widget-shopping input[name=title]")
    await page.wait_for_selector(".osk.osk--open", state="visible")
    found = await _too_small(page)
    assert not found, "Under 48 px:\n" + "\n".join(found)


@pytest.mark.parametrize("mode", ["day", "night"])
async def test_admin_touch_targets(start_server, page, mode):
    server = start_server(keyboard=True)
    _seed_content(server)
    server.query("INSERT INTO app_settings (key, value) VALUES ('appearance', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("light" if mode == "day" else "dark",))
    await page.goto(server.url + "/admin/login")
    await page.fill("input[name=pin]", "1234")
    await page.click("button[type=submit]")
    await page.wait_for_selector("nav.admin-tabs")
    assert await page.get_attribute("html", "data-mode") == mode
    problems = {}
    for tab in TABS:
        await page.goto(f"{server.url}/admin?tab={tab}")
        await page.wait_for_selector(".admin-tab.is-current")
        # Measure what the "Edit"/"Schedule" rows open too.
        await page.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
        found = await _too_small(page)
        if found:
            problems[tab] = found
    assert not problems, "Under 48 px:\n" + "\n".join(f"{t}: {x}" for t, xs in problems.items() for x in xs)
