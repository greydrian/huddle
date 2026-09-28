"""The /pen-test diagnostic page (spec §3 return-window checks, §10.10).

The drawing and readout behaviour is checked with Playwright (see the PR);
these pin the route's access and the markup the script depends on.
"""

from pathlib import Path

from app.auth import SESSION_COOKIE

DEFAULT_PIN = "1234"  # seeded by init_db()
SCRIPT = Path(__file__).parent.parent / "app" / "static" / "js" / "pen-test.js"


async def test_pen_test_requires_admin(client):
    resp = await client.get("/pen-test")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


async def test_pen_test_renders_canvas_and_script(client):
    login = await client.post("/admin/login", data={"pin": DEFAULT_PIN})
    assert SESSION_COOKIE in login.cookies

    resp = await client.get("/pen-test")

    assert resp.status_code == 200
    html = resp.text
    assert '<canvas id="pt-canvas"' in html
    assert '<script src="/static/js/pen-test.js"></script>' in html
    for element_id in ("pt-type", "pt-pressure", "pt-hover", "pt-count", "pt-rejected", "pt-clear", "chk-palm"):
        assert f'id="{element_id}"' in html
    assert 'href="/admin"' in html
    assert (await client.get("/static/js/pen-test.js")).status_code == 200


async def test_admin_links_to_pen_test(client):
    await client.post("/admin/login", data={"pin": DEFAULT_PIN})

    assert 'href="/pen-test"' in (await client.get("/admin")).text


def test_script_touches_nothing_on_the_server():
    script = SCRIPT.read_text(encoding="utf-8")
    for call in ("fetch(", "XMLHttpRequest", "sendBeacon", "localStorage", "htmx."):
        assert call not in script
