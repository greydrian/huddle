"""Avatars (spec 10.9), end to end: pick an emoji and upload a photo in
Admin → Family Members, and both show on the dashboard's name pills."""

import pytest
from PIL import Image

pytestmark = pytest.mark.e2e


async def test_set_an_emoji_and_a_photo_and_see_them_on_the_wall(start_server, page, tmp_path):
    server = start_server()
    server.query("INSERT INTO tasks (profile_id, title) VALUES (3, 'Pack PE kit')")  # Riley
    photo = tmp_path / "mum.png"
    Image.new("RGB", (640, 480), (40, 120, 200)).save(photo)

    await page.goto(server.url + "/admin?tab=family")
    await page.fill("input[name=pin]", "1234")
    await page.click("button[type=submit]")
    await page.wait_for_selector("#family")

    riley = page.locator(".family-row", has_text="Riley")
    await riley.locator(".avatar-edit summary").click()
    await riley.locator("button.emoji-choice", has_text="🦖").click()
    # Back on Admin (the Avatar panel closed again), with the dinosaur picked.
    await page.wait_for_selector(
        ".family-row:has-text('Riley') button.emoji-choice[aria-pressed=true]", state="attached"
    )
    assert (
        await page.locator(".family-row", has_text="Riley").locator(".admin-person .avatar-emoji").inner_text() == "🦖"
    )

    mum = page.locator(".family-row", has_text="Mum")
    await mum.locator(".avatar-edit summary").click()
    await mum.locator("input[type=file][name=photo]").set_input_files(str(photo))
    await mum.locator(".avatar-photo-form button[type=submit]").click()
    await page.wait_for_selector(".family-row:has-text('Mum') .admin-person .avatar-photo img")
    stored = server.query("SELECT avatar_kind, length(avatar_photo) FROM profiles WHERE id = 1")[0]
    assert stored[0] == "photo" and 0 < stored[1] < 60_000

    await page.goto(server.url + "/")
    await page.wait_for_selector("#widget-tasks >> text=Pack PE kit")
    tasks = page.locator("#widget-tasks")
    riley_pill = tasks.locator(".person-pill", has_text="Riley")
    assert await riley_pill.locator(".person-avatar.avatar-emoji").inner_text() == "🦖"
    mum_img = tasks.locator(".person-pill", has_text="Mum").locator(".person-avatar.avatar-photo img")
    await mum_img.wait_for()
    # The photo really loaded (a broken image has no natural size), round, in the pill.
    await page.wait_for_function("img => img.complete && img.naturalWidth === 256", arg=await mum_img.element_handle())
    box = await mum_img.bounding_box()
    assert 28 <= box["width"] <= 36 and abs(box["width"] - box["height"]) < 1
    assert await mum_img.evaluate("img => getComputedStyle(img).borderRadius") == "50%"
