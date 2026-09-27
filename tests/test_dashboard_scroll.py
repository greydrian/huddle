"""The dashboard scrolls inside a wrapper Gridstack doesn't size.

Gridstack puts an inline height on #dashboard-grid, so the grid itself can
never be the scroller; with html/body locked for the kiosk, the bottom row
was unreachable. The scroll behaviour itself is checked with Playwright (see
the PR); this pins the markup it depends on.
"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).parent.parent / "app" / "templates"
DRAG_HANDLE = re.compile(r'class="[^"]*\bdrag-handle\b')


async def test_grid_sits_inside_its_own_scroll_container(db, client):
    html = (await client.get("/")).text
    scroller = html.index('id="dashboard-scroll"')
    grid = html.index('id="dashboard-grid"')
    assert scroller < grid
    # Nothing else between the wrapper's opening tag and the grid.
    assert html[scroller:grid].count("<div") == 1


# Gridstack preventDefault()s touchmove on its drag handles, and silently
# falls back to the whole card as the handle when none matches — which would
# swallow every scroll swipe on that card. So each widget needs exactly one.

def test_every_widget_template_has_exactly_one_drag_handle():
    templates = [p for p in (TEMPLATES / "widgets").glob("*.html") if not p.name.startswith("_")]
    assert templates
    for path in templates:
        assert len(DRAG_HANDLE.findall(path.read_text(encoding="utf-8"))) == 1, path.name


def test_dashboard_configures_the_drag_handle():
    assert "handle: '.drag-handle'" in (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")


async def test_dashboard_renders_one_drag_handle_per_visible_widget(db, client):
    visible = (await (await db.execute(
        "SELECT COUNT(*) FROM layout_state WHERE is_visible = 1"
    )).fetchone())[0]
    assert visible
    html = (await client.get("/")).text
    assert html.count('class="grid-stack-item"') == visible
    assert len(DRAG_HANDLE.findall(html)) == visible
