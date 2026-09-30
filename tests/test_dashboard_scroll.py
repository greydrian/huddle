"""The dashboard scrolls inside a wrapper Gridstack doesn't size.

Gridstack puts an inline height on #dashboard-grid, so the grid itself can
never be the scroller; with html/body locked for the kiosk, the bottom row
was unreachable. The scroll behaviour itself is checked with Playwright (see
the PR); this pins the markup it depends on.
"""

import re
from html.parser import HTMLParser
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

WIDGET_HEADER_CALL = re.compile(r"(?:\{\{|\{%\s*call)\s*widget_header\(")


def test_every_widget_template_has_exactly_one_drag_handle():
    # The handle normally comes from the widget_header macro (widgets/_widget.html);
    # a hand-written one counts too, but a template must have exactly one of either.
    templates = [p for p in (TEMPLATES / "widgets").glob("*.html") if not p.name.startswith("_")]
    assert templates
    for path in templates:
        source = path.read_text(encoding="utf-8")
        handles = len(DRAG_HANDLE.findall(source)) + len(WIDGET_HEADER_CALL.findall(source))
        assert handles == 1, path.name


def test_widget_header_macro_renders_exactly_one_drag_handle():
    from app.templating import templates

    macros = templates.env.get_template("widgets/_widget.html").module
    for html in (
        macros.widget_header("image", "Photos"),
        macros.widget_header("calendar-days", "March 2026", split=True, cls="cal-header"),
    ):
        assert len(DRAG_HANDLE.findall(str(html))) == 1
        assert _controls_in_handles(str(html)) == []


async def test_every_widget_route_renders_exactly_one_drag_handle(db, client):
    # Each widget re-renders itself from its own route (HTMX swaps), not just
    # inside the dashboard, so check those too.
    routes = [
        "/widgets/tasks",
        "/widgets/shopping",
        "/widgets/meals",
        "/widgets/calendar",
        "/widgets/weather",
        "/widgets/homework",
        "/widgets/practice-words",
    ]
    for path in routes:
        html = (await client.get(path)).text
        assert len(DRAG_HANDLE.findall(html)) == 1, path
        assert _controls_in_handles(html) == [], path


def test_dashboard_configures_the_drag_handle():
    assert "handle: '.drag-handle'" in (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")


async def test_dashboard_renders_one_drag_handle_per_visible_widget(db, client):
    visible = (await (await db.execute("SELECT COUNT(*) FROM layout_state WHERE is_visible = 1")).fetchone())[0]
    assert visible
    html = (await client.get("/")).text
    assert html.count('class="grid-stack-item"') == visible
    assert len(DRAG_HANDLE.findall(html)) == visible


# Gridstack's touchstart on a handle preventDefault()s and sets a global
# "touch handled" flag; when the target is a button it bails out before
# attaching the touchend that clears it, so every other tap on a control
# inside a handle is swallowed. Handles hold only an icon and a title.

INTERACTIVE_TAGS = {"button", "a", "input", "select", "textarea"}
VOID_TAGS = {"img", "input", "br", "hr", "meta", "link", "use", "source"}


class _HandleContents(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []  # (tag, inside a drag handle?)
        self.offenders = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        inside = bool(self.stack and self.stack[-1][1])
        if inside and (tag in INTERACTIVE_TAGS or any(k.startswith("hx-") for k in attrs)):
            self.offenders.append(tag)
        is_handle = "drag-handle" in (attrs.get("class") or "").split()
        if tag not in VOID_TAGS:
            self.stack.append((tag, inside or is_handle))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


def _controls_in_handles(html):
    parser = _HandleContents()
    parser.feed(html)
    return parser.offenders


def test_handle_checker_catches_controls():
    assert _controls_in_handles('<div class="x drag-handle"><h2>T</h2><button>Next</button></div>') == ["button"]
    assert _controls_in_handles('<div class="drag-handle"><span hx-get="/x">T</span></div>') == ["span"]
    assert _controls_in_handles('<div class="drag-handle"><h2>T</h2></div><button>Next</button>') == []


def test_no_controls_inside_drag_handles_in_templates():
    for path in (TEMPLATES / "widgets").glob("*.html"):
        assert _controls_in_handles(path.read_text(encoding="utf-8")) == [], path.name


async def test_no_controls_inside_drag_handles_on_dashboard(db, client):
    html = (await client.get("/")).text
    assert DRAG_HANDLE.search(html)
    assert _controls_in_handles(html) == []
