from pathlib import Path

from app import database
from app.routers import admin
from app.security import create_session_token
from app.templating import ONSCREEN_KEYBOARD_SETTING

STATIC = Path(__file__).parent.parent / "app" / "static"
ASSETS = (
    "/static/vendor/simple-keyboard/index.modern.js",
    "/static/vendor/simple-keyboard/index.css",
    "/static/js/keyboard.js",
    "/static/css/keyboard.css",
)


async def _enable(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return await client.post("/admin/onscreen-keyboard", data={"enabled": "true"})


async def test_keyboard_is_off_by_default(db, client):
    assert await database.get_setting(db, ONSCREEN_KEYBOARD_SETTING) is None

    for path in ("/", "/admin/login"):
        html = (await client.get(path)).text
        assert not any(asset in html for asset in ASSETS), path
        assert 'inputmode="none"' not in html, path


async def test_admin_route_toggles_the_setting(db, client):
    resp = await _enable(client)
    assert resp.status_code == 303
    assert await database.get_setting(db, ONSCREEN_KEYBOARD_SETTING) == "1"
    assert "checked" in (await client.get("/admin")).text.split("/admin/onscreen-keyboard")[1][:400]

    # Unticked checkboxes aren't submitted at all.
    await client.post("/admin/onscreen-keyboard", data={})
    assert await database.get_setting(db, ONSCREEN_KEYBOARD_SETTING) == "0"


async def test_toggle_requires_admin(db, client):
    resp = await client.post("/admin/onscreen-keyboard", data={"enabled": "true"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert await database.get_setting(db, ONSCREEN_KEYBOARD_SETTING) is None


async def test_enabled_dashboard_includes_assets_and_suppresses_native_keyboard(db, client):
    await _enable(client)

    html = (await client.get("/")).text
    assert all(asset in html for asset in ASSETS)
    assert 'placeholder="Add an item…" required autocomplete="off" inputmode="none"' in html
    meal_inputs = html.count('class="meal-desc-input"')
    assert meal_inputs == 7
    assert html.count('inputmode="none"') == meal_inputs + 1  # + the shopping input


async def test_widget_rerenders_keep_inputmode_none(db, client):
    await _enable(client)

    resp = await client.post("/api/shopping", data={"title": "Milk"})
    assert resp.status_code == 200
    assert "Milk" in resp.text
    assert 'inputmode="none"' in resp.text


async def test_enabled_login_uses_numeric_layout(db, client):
    await _enable(client)
    client.cookies.clear()

    html = (await client.get("/admin/login")).text
    assert all(asset in html for asset in ASSETS)
    assert 'inputmode="none" data-osk-layout="numeric"' in html
    assert 'inputmode="numeric"' not in html


def test_vendored_files_exist():
    for asset in ASSETS:
        path = STATIC / asset.removeprefix("/static/")
        assert path.is_file() and path.stat().st_size > 0, asset
    assert "simple-keyboard v3.8.192" in (STATIC / "vendor/simple-keyboard/index.modern.js").read_text(
        encoding="utf-8"
    )
