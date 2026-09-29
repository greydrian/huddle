from pathlib import Path

from app import database
from app.routers import admin
from app.security import create_session_token

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
    assert await database.get_setting(db, database.ONSCREEN_KEYBOARD_SETTING) is None
    assert database.onscreen_keyboard_enabled is False

    for path in ("/", "/admin/login"):
        html = (await client.get(path)).text
        assert not any(asset in html for asset in ASSETS), path
        assert "data-osk" not in html, path
        assert 'inputmode="none"' not in html, path


async def test_admin_route_toggles_the_setting(db, client):
    resp = await _enable(client)
    assert resp.status_code == 303
    assert await database.get_setting(db, database.ONSCREEN_KEYBOARD_SETTING) == "1"
    assert database.onscreen_keyboard_enabled is True
    assert "checked" in (await client.get("/admin?tab=display")).text.split("/admin/onscreen-keyboard")[1][:400]

    # Unticked checkboxes aren't submitted at all.
    await client.post("/admin/onscreen-keyboard", data={})
    assert await database.get_setting(db, database.ONSCREEN_KEYBOARD_SETTING) == "0"
    assert database.onscreen_keyboard_enabled is False


async def test_setting_is_loaded_at_startup(db):
    await database.set_setting(db, database.ONSCREEN_KEYBOARD_SETTING, "1")
    await db.commit()

    await database.init_db()

    assert database.onscreen_keyboard_enabled is True


async def test_toggle_requires_admin(db, client):
    resp = await client.post("/admin/onscreen-keyboard", data={"enabled": "true"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert await database.get_setting(db, database.ONSCREEN_KEYBOARD_SETTING) is None


async def test_enabled_dashboard_includes_assets_and_marks_inputs(db, client):
    await _enable(client)

    html = (await client.get("/")).text
    assert all(asset in html for asset in ASSETS)
    assert 'placeholder="Add an item…" required autocomplete="off" data-osk="text"' in html
    meal_inputs = html.count('class="meal-desc-input"')
    assert meal_inputs == 7
    assert html.count('data-osk="text"') == meal_inputs + 2  # + the shopping and task quick-add inputs
    assert 'class="task-add-title"' in html
    # keyboard.js sets inputmode="none" only once it has started, so a
    # broken keyboard never leaves the kiosk without the native one.
    assert 'inputmode="none"' not in html


async def test_widget_rerenders_keep_the_marker(db, client):
    await _enable(client)

    resp = await client.post("/api/shopping", data={"title": "Milk"})
    assert resp.status_code == 200
    assert "Milk" in resp.text
    assert 'data-osk="text"' in resp.text


async def test_enabled_login_uses_numeric_layout(db, client):
    await _enable(client)
    client.cookies.clear()

    html = (await client.get("/admin/login")).text
    assert all(asset in html for asset in ASSETS)
    assert 'inputmode="numeric" pattern="[0-9]*" maxlength="8" autofocus autocomplete="off" data-osk="numeric"' in html


async def test_dashboard_renders_shopping_items_on_first_load(db, client):
    await db.execute("INSERT INTO shopping_items (title) VALUES ('Oat milk')")
    await db.commit()

    html = (await client.get("/")).text

    assert '<span class="shopping-title">Oat milk</span>' in html


async def test_dashboard_renders_meal_plan_on_first_load(db, client):
    today = (await database.family_today(db)).isoformat()
    await db.execute("INSERT INTO meal_plans (date, meal_description) VALUES (?, 'Lasagne')", (today,))
    await db.commit()

    html = (await client.get("/")).text

    assert 'value="Lasagne"' in html


def test_vendored_files_exist():
    for asset in ASSETS:
        path = STATIC / asset.removeprefix("/static/")
        assert path.is_file() and path.stat().st_size > 0, asset
    assert "simple-keyboard v3.8.192" in (STATIC / "vendor/simple-keyboard/index.modern.js").read_text(
        encoding="utf-8"
    )
