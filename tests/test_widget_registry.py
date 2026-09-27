from pathlib import Path

from app import database
from app.widgets import WIDGETS

WIDGET_TEMPLATES = Path(database.__file__).parent / "templates" / "widgets"


def test_every_default_layout_widget_is_registered():
    missing = [widget_id for widget_id, *_ in database.DEFAULT_LAYOUT if widget_id not in WIDGETS]
    assert missing == []


def test_every_registered_template_exists():
    missing = [w.template for w in WIDGETS.values() if not (WIDGET_TEMPLATES / w.template).is_file()]
    assert missing == []


async def test_dashboard_renders_every_registered_widget(client):
    html = (await client.get("/")).text
    for widget_id, *_ in database.DEFAULT_LAYOUT:
        assert f'gs-id="{widget_id}"' in html
    assert 'id="widget-tasks"' in html and 'id="widget-calendar"' in html
