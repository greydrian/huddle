"""Family members' avatars (spec 10.9, app/avatars.py): migration 8, emoji
validation, the photo upload (guarded, capped, EXIF stripped, HEIC), the
PIN-free /avatars route, rendering in every widget and banner, and backups."""

import io
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageOps

from app import avatars, backup, database, migrations, security
from app.admin_tabs import admin_url
from app.routers import admin
from app.security import create_session_token
from app.services import banners
from app.upload_guard import MAX_AVATAR_BODY

MUM, DAD, RILEY, JAMIE = 1, 2, 3, 4
MB = 1024 * 1024
FAMILY = admin_url("family")


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


def _image(size=(400, 300), colour="red", fmt="JPEG", **save) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format=fmt, **save)
    return out.getvalue()


def _error(code: str) -> str:
    return admin_url("family", error=code)


async def _profile(db, profile_id):
    return dict(await (await db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))).fetchone())


async def _upload(client, profile_id, data, name="me.jpg", mime="image/jpeg"):
    return await client.post(f"/admin/profiles/{profile_id}/avatar/photo", files={"photo": (name, data, mime)})


# --- Migration 8 ---

async def test_migration_8_adds_avatar_columns_without_touching_data(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(migrations, "MIGRATIONS", [m for m in migrations.MIGRATIONS if m.version < 8])
    await database.init_db()  # a database as it was before migration 8
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE profiles SET school_year = 'Year 4', email = 'r@example.com' WHERE name = 'Riley'")
        before = conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
        other = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("tasks", "layout_state", "homework")}
    monkeypatch.undo()
    monkeypatch.setattr(database, "DB_PATH", path)

    await database.init_db()
    await database.init_db()  # and again: nothing more happens

    with sqlite3.connect(path) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(profiles)")}
        after = conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
        assert {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in other} == other
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    width = len(before[0])
    assert [row[:width] for row in after] == before  # every old column unchanged
    assert columns["avatar_kind"][2:5] == ("TEXT", 1, "'initial'")
    assert columns["avatar_photo"][2] == "BLOB"
    new = ("avatar_kind", "avatar_emoji", "avatar_photo", "avatar_hash")
    assert {tuple(row[columns[c][0]] for c in new) for row in after} == {("initial", None, None, None)}
    assert 8 in versions


# --- Emoji ---

@pytest.mark.parametrize("text", [
    "🐶", "⚽", "🌟", "❤️", "☀️", "👍🏽", "👩‍🚀", "👨‍👩‍👧‍👦", "🏳️‍🌈", "🇬🇧", "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "#️⃣", "1️⃣", " 🦄 ",
    *avatars.CURATED_EMOJI,
])
def test_one_emoji_is_accepted(text):
    assert avatars.clean_emoji(text) == text.strip()


@pytest.mark.parametrize("text", [
    "", " ", "A", "ab", "1", "#", "→", "ℕ", "🐶🐱", "🐶 🐱", "🐶a", "a🐶", "🇬", "🇬🇧🇫🇷", "‍🐶", "🐶‍", "🐶‍‍🐱", "️", "🏽",
    "<script>", "🐶" * 20, "​", "\U000e0067",
])
def test_anything_but_one_emoji_is_refused(text):
    with pytest.raises(avatars.AvatarError) as exc:
        avatars.clean_emoji(text)
    assert exc.value.code == "avatar-emoji"


def test_curated_grid_is_about_forty_distinct_emoji():
    assert 36 <= len(avatars.CURATED_EMOJI) <= 48
    assert len(set(avatars.CURATED_EMOJI)) == len(avatars.CURATED_EMOJI)


async def test_picking_an_emoji_from_the_grid(db, admin_client):
    r = await admin_client.post(f"/admin/profiles/{RILEY}/avatar", data={"kind": "emoji", "emoji": "🦊"})
    assert r.status_code == 303 and r.headers["location"] == FAMILY
    riley = await _profile(db, RILEY)
    assert (riley["avatar_kind"], riley["avatar_emoji"]) == ("emoji", "🦊")


async def test_a_typed_emoji_wins_and_a_bad_one_changes_nothing(db, admin_client):
    r = await admin_client.post(f"/admin/profiles/{RILEY}/avatar",
                                data={"kind": "emoji", "emoji": "🦊", "custom_emoji": "🧁"})
    assert r.headers["location"] == FAMILY
    assert (await _profile(db, RILEY))["avatar_emoji"] == "🧁"
    r = await admin_client.post(f"/admin/profiles/{RILEY}/avatar", data={"kind": "emoji", "custom_emoji": "Hi"})
    assert r.headers["location"] == _error("avatar-emoji")
    assert (await _profile(db, RILEY))["avatar_emoji"] == "🧁"
    html = (await admin_client.get(r.headers["location"])).text
    assert "type exactly one emoji" in html


async def test_switching_kinds_keeps_the_emoji_for_later(db, admin_client):
    await admin_client.post(f"/admin/profiles/{RILEY}/avatar", data={"kind": "emoji", "emoji": "🦊"})
    await admin_client.post(f"/admin/profiles/{RILEY}/avatar", data={"kind": "initial"})
    riley = await _profile(db, RILEY)
    assert (riley["avatar_kind"], riley["avatar_emoji"]) == ("initial", "🦊")
    r = await admin_client.post(f"/admin/profiles/{RILEY}/avatar", data={"kind": "emoji"})  # "Show: Emoji"
    assert r.headers["location"] == FAMILY
    assert (await _profile(db, RILEY))["avatar_kind"] == "emoji"


@pytest.mark.parametrize(("data", "code"), [
    ({"kind": "sticker"}, "avatar-kind"),
    ({"kind": "photo"}, "avatar-no-photo"),
    ({"kind": "emoji"}, "avatar-emoji"),  # no emoji chosen yet
])
async def test_avatar_choices_that_cant_be_saved(db, admin_client, data, code):
    r = await admin_client.post(f"/admin/profiles/{JAMIE}/avatar", data=data)
    assert r.headers["location"] == _error(code)
    assert (await _profile(db, JAMIE))["avatar_kind"] == "initial"


async def test_a_missing_person(admin_client):
    for path, kwargs in [("/avatar", {"data": {"kind": "initial"}}), ("/avatar/photo/remove", {}),
                         ("/avatar/photo", {"files": {"photo": ("a.jpg", _image(), "image/jpeg")}})]:
        r = await admin_client.post(f"/admin/profiles/999{path}", **kwargs)
        assert r.headers["location"] == _error("profile-missing"), path


# --- Photo upload ---

async def test_photo_is_cropped_resized_stored_and_used(db, admin_client):
    r = await _upload(admin_client, MUM, _image((800, 400)))
    assert r.status_code == 303 and r.headers["location"] == FAMILY
    mum = await _profile(db, MUM)
    assert mum["avatar_kind"] == "photo"
    photo = bytes(mum["avatar_photo"])
    assert photo[:4] == b"RIFF" and photo[8:12] == b"WEBP"
    assert mum["avatar_hash"] == avatars.photo_hash(photo)
    with Image.open(io.BytesIO(photo)) as image:
        assert image.size == (256, 256)


def test_centre_crop_keeps_the_middle():
    # Left third blue, middle red, right third green: the square crop is the middle.
    image = Image.new("RGB", (900, 300), "red")
    image.paste((0, 0, 255), (0, 0, 300, 300))
    image.paste((0, 255, 0), (600, 0, 900, 300))
    out = io.BytesIO()
    image.save(out, format="PNG")
    with Image.open(io.BytesIO(avatars.process_photo(out.getvalue()))) as square:
        red, green, blue = square.convert("RGB").getpixel((128, 128))
        assert red > 200 and green < 40 and blue < 40
        assert square.convert("RGB").getpixel((3, 128))[0] > 200  # no blue at the edge


def test_exif_is_stripped_and_orientation_applied():
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    exif[0x0110] = "Phone 15"  # Model
    exif[0x0132] = "2026:09:29 08:00:00"  # DateTime
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to view
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (51.0, 27.0, 0.0)
    # Stored sideways: left half black. Turned upright (90° clockwise), that
    # half is the top; left as stored, it would stay on the left.
    raw = Image.new("RGB", (600, 300), "white")
    raw.paste((0, 0, 0), (0, 0, 300, 300))
    out = io.BytesIO()
    raw.save(out, format="JPEG", exif=exif.tobytes(), quality=95)
    data = out.getvalue()
    assert b"PhoneMaker" in data

    photo = avatars.process_photo(data)
    assert b"PhoneMaker" not in photo and b"Exif" not in photo and b"EXIF" not in photo
    with Image.open(io.BytesIO(photo)) as image:
        assert not image.getexif()
        assert "exif" not in image.info and "icc_profile" not in image.info and "xmp" not in image.info
        pixels = image.convert("L")
        assert pixels.getpixel((200, 20)) < 60 and pixels.getpixel((56, 236)) > 200


def test_heic_is_accepted():
    pillow_heif = pytest.importorskip("pillow_heif")
    image = pillow_heif.from_pillow(Image.new("RGB", (64, 48), "blue"))
    out = io.BytesIO()
    try:
        image.save(out, quality=50)
    except (OSError, RuntimeError, ValueError):
        pytest.skip("no HEIC encoder in this build")
    photo = avatars.process_photo(out.getvalue())
    with Image.open(io.BytesIO(photo)) as result:
        assert result.format == "WEBP" and result.size == (256, 256)


@pytest.mark.parametrize("mode", ["I;16", "I;16B"])
def test_16_bit_greyscale_keeps_its_tone(mode):
    """Mid-grey in a 16-bit PNG stays mid-grey (not clipped to white)."""
    out = io.BytesIO()
    Image.new(mode, (64, 64), 32768).save(out, format="PNG")
    with Image.open(io.BytesIO(avatars.process_photo(out.getvalue()))) as image:
        red, green, blue = image.convert("RGB").getpixel((128, 128))
        assert 118 <= red <= 138 and red == green == blue


def test_big_jpeg_is_decoded_at_a_reduced_scale(monkeypatch):
    """A large JPEG decodes via draft() at >= 1024 px, not at full size."""
    seen = []
    real_fit = ImageOps.fit

    def spy(image, *args, **kwargs):
        seen.append(image.size)
        return real_fit(image, *args, **kwargs)

    monkeypatch.setattr(ImageOps, "fit", spy)
    photo = avatars.process_photo(_image((4800, 3600)))
    assert seen and max(seen[0]) < 4800 and min(seen[0]) >= avatars.DECODE_SIZE
    with Image.open(io.BytesIO(photo)) as image:
        assert image.size == (256, 256)


def test_transparent_png_keeps_its_alpha():
    out = io.BytesIO()
    Image.new("RGBA", (100, 100), (255, 0, 0, 0)).save(out, format="PNG")
    with Image.open(io.BytesIO(avatars.process_photo(out.getvalue()))) as image:
        assert image.mode == "RGBA"


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
@pytest.mark.parametrize("size", [(9_000, 9_000), (20_000, 10_000)])  # our header check; Pillow's own
def test_pixel_bomb_is_refused_before_decoding(size, monkeypatch):
    out = io.BytesIO()
    Image.new("1", size).save(out, format="PNG")
    assert len(out.getvalue()) < 2 * MB

    def no_decoding(self):
        raise AssertionError("decoded a pixel bomb")

    monkeypatch.setattr(Image.Image, "load", no_decoding)
    with pytest.raises(avatars.AvatarError) as exc:
        avatars.process_photo(out.getvalue())
    assert exc.value.code == "avatar-too-big"


@pytest.mark.parametrize("data", [b"hello there", b"%PDF-1.4\n%%EOF", _image()[:40], b"\x89PNG\r\n\x1a\nnope"])
async def test_not_a_photo_is_refused(db, admin_client, data):
    r = await _upload(admin_client, MUM, data)
    assert r.headers["location"] == _error("avatar-bad-type")
    assert (await _profile(db, MUM))["avatar_photo"] is None


async def test_no_file_chosen(admin_client):
    r = await admin_client.post(f"/admin/profiles/{MUM}/avatar/photo", files={"photo": ("", b"", "image/jpeg")})
    assert r.headers["location"] == _error("avatar-empty")
    r = await admin_client.post(f"/admin/profiles/{MUM}/avatar/photo", data={})
    assert r.headers["location"] == _error("avatar-empty")


async def test_a_file_over_8_mb_is_refused(db, admin_client):
    # Under the request cap (so the route itself sees it), over the file cap.
    data = _image(fmt="PNG") + b"\x00" * (avatars.MAX_PHOTO_BYTES + 1)
    assert len(data) < MAX_AVATAR_BODY - 1024
    r = await _upload(admin_client, MUM, data, "big.png", "image/png")
    assert r.headers["location"] == _error("avatar-too-big")
    assert (await _profile(db, MUM))["avatar_photo"] is None


async def test_remove_photo_goes_back_to_the_initial(db, admin_client):
    await _upload(admin_client, MUM, _image())
    r = await admin_client.post(f"/admin/profiles/{MUM}/avatar/photo/remove")
    assert r.headers["location"] == FAMILY
    mum = await _profile(db, MUM)
    assert (mum["avatar_kind"], mum["avatar_photo"], mum["avatar_hash"]) == ("initial", None, None)


async def test_removing_a_photo_that_isnt_shown_keeps_the_emoji(db, admin_client):
    await _upload(admin_client, MUM, _image())
    await admin_client.post(f"/admin/profiles/{MUM}/avatar", data={"kind": "emoji", "emoji": "🌻"})
    await admin_client.post(f"/admin/profiles/{MUM}/avatar/photo/remove")
    mum = await _profile(db, MUM)
    assert (mum["avatar_kind"], mum["avatar_emoji"], mum["avatar_photo"]) == ("emoji", "🌻", None)


# --- Upload guard: auth and size before the body is read ---

MULTIPART_HEAD = (b'--abc\r\nContent-Disposition: form-data; name="photo"; filename="a.jpg"\r\n'
                  b"Content-Type: image/jpeg\r\n\r\n")
MULTIPART = {"content-type": "multipart/form-data; boundary=abc"}


def _streamed(chunks: int, sent: list):
    async def body():
        sent.append(len(MULTIPART_HEAD))
        yield MULTIPART_HEAD
        for _ in range(chunks):
            sent.append(MB)
            yield b"\x00" * MB
    return body()


async def test_unauthenticated_upload_is_refused_before_the_body_is_read(db, client):
    sent = []
    r = await client.post(f"/admin/profiles/{MUM}/avatar/photo", content=_streamed(50, sent), headers=MULTIPART)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert sum(sent) <= len(MULTIPART_HEAD)
    assert (await _profile(db, MUM))["avatar_photo"] is None


async def test_upload_while_pin_is_default_is_refused_unread(db, admin_client):
    await database.set_setting(db, "pin_is_default", "1")
    await db.commit()
    sent = []
    r = await admin_client.post(f"/admin/profiles/{MUM}/avatar/photo", content=_streamed(20, sent), headers=MULTIPART)
    assert r.status_code == 303 and r.headers["location"] == "/admin/new-pin"
    assert sum(sent) <= len(MULTIPART_HEAD)


async def test_oversize_upload_gets_413(db, admin_client):
    sent = []
    r = await admin_client.post(f"/admin/profiles/{MUM}/avatar/photo", content=_streamed(30, sent),
                                headers={**MULTIPART, "content-length": str(30 * MB)})
    assert r.status_code == 413 and sum(sent) <= len(MULTIPART_HEAD)

    sent.clear()  # streamed with no length: cut off just past the 8 MB cap
    r = await admin_client.post(f"/admin/profiles/{MUM}/avatar/photo", content=_streamed(100, sent), headers=MULTIPART)
    assert r.status_code == 413 and sum(sent) <= 10 * MB
    assert (await _profile(db, MUM))["avatar_photo"] is None


@pytest.mark.parametrize(("method", "path", "kwargs"), [
    ("post", f"/admin/profiles/{MUM}/avatar", {"data": {"kind": "emoji", "emoji": "🐶"}}),
    ("post", f"/admin/profiles/{MUM}/avatar/photo", {"files": {"photo": ("a.jpg", b"x", "image/jpeg")}}),
    ("post", f"/admin/profiles/{MUM}/avatar/photo/remove", {}),
])
async def test_every_avatar_admin_route_needs_the_pin(db, client, method, path, kwargs):
    await db.execute("UPDATE profiles SET avatar_kind = 'photo', avatar_photo = x'00', avatar_hash = 'h' WHERE id = ?",
                     (MUM,))
    await db.commit()
    r = await getattr(client, method)(path, **kwargs)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    mum = await _profile(db, MUM)
    assert (mum["avatar_kind"], mum["avatar_emoji"], mum["avatar_hash"]) == ("photo", None, "h")


# --- Serving ---

async def test_photo_is_served_pin_free_and_cached_for_good(db, admin_client, client):
    await _upload(admin_client, MUM, _image())
    mum = await _profile(db, MUM)
    client.cookies.clear()  # the kiosk has no Admin session
    r = await client.get(f"/avatars/{MUM}-{mum['avatar_hash']}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content == bytes(mum["avatar_photo"])


async def test_a_new_photo_gets_a_new_url_and_the_old_one_is_gone(db, admin_client):
    await _upload(admin_client, MUM, _image(colour="red"))
    old = (await _profile(db, MUM))["avatar_hash"]
    await _upload(admin_client, MUM, _image(colour="blue"))
    new = (await _profile(db, MUM))["avatar_hash"]
    assert new != old
    assert (await admin_client.get(f"/avatars/{MUM}-{old}")).status_code == 404
    assert (await admin_client.get(f"/avatars/{MUM}-{new}")).status_code == 200


@pytest.mark.parametrize("key", [
    "{hash}", "{id}", "{id}-", "0-{hash}", "01-{hash}", "-1-{hash}", "{id}-{upper}", "{id}-{hash}0", "{id}-{short}",
    "{id}.{hash}", "{id}-{hash}.webp", "2-{hash}", "999-{hash}", "abc-{hash}", "{id}-..%2F..%2Fx", "1e3-{hash}",
])
async def test_bad_or_unknown_keys_are_404(db, admin_client, client, key):
    await _upload(admin_client, MUM, _image())
    photo_hash = (await _profile(db, MUM))["avatar_hash"]
    url = "/avatars/" + key.format(id=MUM, hash=photo_hash, upper=photo_hash.upper(), short=photo_hash[:-1])
    r = await client.get(url)
    assert r.status_code == 404, url


async def test_overlong_key_is_refused(client):
    assert (await client.get("/avatars/" + "1" * 40)).status_code in (404, 422)


async def test_no_photo_is_404_even_with_a_kind_of_photo(db, client):
    await db.execute("UPDATE profiles SET avatar_kind = 'photo', avatar_hash = ? WHERE id = ?", ("0" * 16, MUM))
    await db.commit()
    assert (await client.get(f"/avatars/{MUM}-{'0' * 16}")).status_code == 404


# --- Rendering: every pill and dot ---

@pytest.fixture
async def faces(db, admin_client):
    """Mum has a photo, Riley an emoji; Dad and Jamie keep their initials."""
    await _upload(admin_client, MUM, _image())
    await admin_client.post(f"/admin/profiles/{RILEY}/avatar", data={"kind": "emoji", "emoji": "🦖"})
    photo_hash = (await _profile(db, MUM))["avatar_hash"]
    return f'<img src="/avatars/{MUM}-{photo_hash}"'


def _pills(html: str) -> dict[str, str]:
    """Each person pill's name -> its avatar markup."""
    return {
        name: face for face, name in re.findall(
            r'<span class="person-pill[^"]*"[^>]*>\s*(<span class="person-avatar.*?</span>)\s*'
            r'<span class="person-name">([^<]+)</span>', html, re.S)
    }


def _assert_faces(pills: dict[str, str], photo: str, names=("Mum", "Riley")):
    if "Mum" in names:
        assert photo in pills["Mum"] and "avatar-photo" in pills["Mum"]
    if "Riley" in names:
        assert "🦖" in pills["Riley"] and "avatar-emoji" in pills["Riley"]
    for name, face in pills.items():
        assert 'aria-hidden="true"' in face
        if name in ("Dad", "Jamie"):
            assert "avatar-initial" in face and f">{name[0]}</span>" in face


async def test_tasks_widget(db, client, faces):
    await db.executemany("INSERT INTO tasks (profile_id, title) VALUES (?, ?)",
                         [(MUM, "Bins"), (DAD, "Car"), (RILEY, "Bag")])
    await db.commit()
    pills = _pills((await client.get("/widgets/tasks")).text)
    assert set(pills) >= {"Mum", "Dad", "Riley"}
    _assert_faces(pills, faces)


async def test_homework_and_practice_words_widgets(db, client, faces):
    await db.execute("INSERT INTO homework (profile_id, subject, title) VALUES (?, 'Maths', 'Fractions')", (RILEY,))
    await db.execute("INSERT INTO homework (profile_id, subject, title) VALUES (?, 'Maths', 'Sums')", (MUM,))
    await db.execute("INSERT INTO practice_word_lists (profile_id, title, words) VALUES (?, 'Week 4', 'because')",
                     (RILEY,))
    await db.execute("INSERT INTO practice_word_lists (profile_id, title, words) VALUES (?, 'Week 4', 'which')",
                     (JAMIE,))
    await db.commit()
    homework_pills = _pills((await client.get("/widgets/homework")).text)
    assert set(homework_pills) == {"Mum", "Riley"}
    _assert_faces(homework_pills, faces)
    word_pills = _pills((await client.get("/widgets/practice-words")).text)
    assert set(word_pills) == {"Riley", "Jamie"}
    _assert_faces(word_pills, faces, names=("Riley",))


async def test_read_tonight_chips(db, client, faces):
    """The reading log's compact chips (spec 10.10) show each child's avatar too."""
    await db.execute("UPDATE profiles SET school_year = 'Year 4' WHERE id IN (?, ?)", (RILEY, JAMIE))
    await db.commit()
    html = (await client.get("/widgets/homework")).text
    chips = dict(re.findall(r'<span class="rd-dot" aria-hidden="true">(.*?)</span>.*?'
                            r'<span class="rd-name">([^<]+)</span>', html, re.S))
    chips = {name: face for face, name in chips.items()}
    assert set(chips) == {"Riley", "Jamie"}
    assert "rd-initial avatar-emoji" in chips["Riley"] and "🦖" in chips["Riley"]
    assert "rd-initial avatar-initial" in chips["Jamie"] and chips["Jamie"].endswith('aria-hidden="true">J')


async def test_banners(db, client, faces, monkeypatch):
    london = ZoneInfo("Europe/London")
    now = datetime.now(london).replace(hour=10, minute=0)
    monkeypatch.setattr(banners, "_clock", lambda tz: now.astimezone(tz))
    for pid in (MUM, RILEY):
        await db.execute("INSERT INTO homework (profile_id, title, due_date) VALUES (?, 'Poem', ?)",
                         (pid, now.date().isoformat()))
    await db.commit()
    pills = _pills((await client.get("/banners")).text)
    assert set(pills) == {"Mum", "Riley"}
    _assert_faces(pills, faces)


async def test_dashboard_renders_every_face(db, client, faces):
    await db.executemany("INSERT INTO tasks (profile_id, title) VALUES (?, ?)", [(MUM, "Bins"), (RILEY, "Bag")])
    await db.commit()
    html = (await client.get("/")).text
    assert faces in html and "🦖" in html


async def test_admin_lists_show_the_avatar_and_the_picker(db, admin_client, faces):
    html = (await admin_client.get("/admin?tab=family")).text
    assert html.count(faces) >= 2  # Family Members and Task Schedules
    assert html.count('class="person-dot avatar-emoji') >= 2
    for emoji in avatars.CURATED_EMOJI:
        assert f'value="{emoji}"' in html
    assert f'action="/admin/profiles/{MUM}/avatar/photo/remove"' in html  # Mum has a photo
    assert f'action="/admin/profiles/{DAD}/avatar/photo/remove"' not in html
    assert 'aria-pressed="true">🦖</button>' in html
    assert 'enctype="multipart/form-data"' in html


async def test_contrast_ink_still_applies_to_every_kind(db, admin_client, client, faces):
    """person_ink picks the pill's text colour from the person's colour for
    every kind of avatar, as before."""
    await db.execute("UPDATE profiles SET colour_hex = '#F5E663' WHERE id = ?", (RILEY,))  # pale yellow
    await db.execute("INSERT INTO tasks (profile_id, title) VALUES (?, 'Bag')", (RILEY,))
    await db.commit()
    html = (await client.get("/widgets/tasks")).text
    assert re.search(r'class="person-pill ink-dark"[^>]*>\s*<span class="person-avatar avatar-emoji ink-dark"', html)


# --- Backups ---

async def test_backups_include_the_photo_and_emoji(db, admin_client):
    await _upload(admin_client, MUM, _image())
    await admin_client.post(f"/admin/profiles/{RILEY}/avatar", data={"kind": "emoji", "emoji": "🦖"})
    security._get_secret_key()
    mum = await _profile(db, MUM)

    path = await backup.create_backup()

    with sqlite3.connect(path) as conn:
        rows = dict((r[0], r[1:]) for r in conn.execute(
            "SELECT id, avatar_kind, avatar_emoji, avatar_photo, avatar_hash FROM profiles"))
    assert rows[MUM] == ("photo", None, mum["avatar_photo"], mum["avatar_hash"])
    assert rows[RILEY] == ("emoji", "🦖", None, None)
