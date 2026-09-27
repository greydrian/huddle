"""The dashboard scrolls inside a wrapper Gridstack doesn't size.

Gridstack puts an inline height on #dashboard-grid, so the grid itself can
never be the scroller; with html/body locked for the kiosk, the bottom row
was unreachable. The scroll behaviour itself is checked with Playwright (see
the PR); this pins the markup it depends on.
"""


async def test_grid_sits_inside_its_own_scroll_container(db, client):
    html = (await client.get("/")).text
    scroller = html.index('id="dashboard-scroll"')
    grid = html.index('id="dashboard-grid"')
    assert scroller < grid
    # Nothing else between the wrapper's opening tag and the grid.
    assert html[scroller:grid].count("<div") == 1


async def test_widgets_drag_by_header_so_touch_swipes_can_scroll(db, client):
    # Gridstack preventDefault()s touchmove on its drag handles; a whole-card
    # handle (its default) would swallow every scroll swipe.
    html = (await client.get("/")).text
    assert "handle: '.widget-header, .widget-tab'" in html
