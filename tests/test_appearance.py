"""Day/night appearance (decided in the family's timezone, never the
container's UTC clock) and the person-colour ink helper."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import appearance, database

LONDON = ZoneInfo("Europe/London")
SYDNEY = ZoneInfo("Australia/Sydney")


def _at(hour, minute=0, tz=LONDON, day=(2026, 6, 15)):
    return datetime(*day, hour, minute, tzinfo=tz)


@pytest.mark.parametrize(
    ("hour", "minute", "mode", "switch"),
    [
        (7, 0, "day", (6, 15, 19)),     # morning edge: day starts at 07:00 sharp
        (12, 30, "day", (6, 15, 19)),
        (18, 59, "day", (6, 15, 19)),
        (19, 0, "night", (6, 16, 7)),   # evening edge: night starts at 19:00 sharp
        (23, 30, "night", (6, 16, 7)),
        (0, 0, "night", (6, 15, 7)),    # after midnight: switches back the same morning
        (6, 59, "night", (6, 15, 7)),
    ],
)
def test_auto_mode_boundaries(hour, minute, mode, switch):
    got_mode, switch_at = appearance.mode_at(_at(hour, minute), "auto")
    assert got_mode == mode
    month, day, switch_hour = switch
    assert switch_at == datetime(2026, month, day, switch_hour, tzinfo=LONDON)


def test_pinned_appearance_never_switches():
    assert appearance.mode_at(_at(12), "dark") == ("night", None)
    assert appearance.mode_at(_at(22), "light") == ("day", None)


async def test_mode_follows_family_timezone_not_utc(db):
    # 10:00 UTC is 20:00 in Sydney: night there, day in London.
    utc_now = datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("UTC"))
    assert (await appearance.current_mode(db, utc_now))["mode"] == "day"  # conftest: Europe/London

    await database.set_setting(db, "calendar_timezone", "Australia/Sydney")
    await db.commit()
    result = await appearance.current_mode(db, utc_now)
    assert result["mode"] == "night"
    assert result["switch_in"] == 11 * 3600  # until 07:00 Sydney time


async def test_switch_in_counts_real_seconds_across_dst(db):
    # Night of the UK clocks going forward (29 Mar 2026): 19:00 -> 07:00 is 11 real hours.
    now = datetime(2026, 3, 28, 19, 0, tzinfo=LONDON)
    result = await appearance.current_mode(db, now)
    assert result == {"mode": "night", "switch_in": 11 * 3600}


async def test_setting_is_used_and_validated(db):
    assert await appearance.get_appearance(db) == "auto"
    await appearance.set_appearance(db, "dark")
    assert (await appearance.current_mode(db, _at(12))) == {"mode": "night", "switch_in": None}
    await database.set_setting(db, appearance.APPEARANCE_SETTING, "purple")
    assert await appearance.get_appearance(db) == "auto"


async def test_pages_carry_the_mode(db, client):
    await appearance.set_appearance(db, "dark")
    for path in ("/", "/admin/login"):
        html = (await client.get(path)).text
        assert '<html lang="en" data-mode="night">' in html, path
        assert "/api/appearance" in html, path
    assert (await client.get("/api/appearance")).json() == {"mode": "night", "switch_in": None}


async def test_admin_saves_appearance(db, client):
    from app.routers.admin import SESSION_COOKIE
    from app.security import create_session_token

    client.cookies.set(SESSION_COOKIE, create_session_token())
    resp = await client.post("/admin/appearance", data={"value": "light"})
    assert resp.status_code == 303
    assert await appearance.get_appearance(db) == "light"
    assert 'name="value" value="light" checked' in (await client.get("/admin")).text
    assert (await client.post("/admin/appearance", data={"value": "sepia"})).status_code == 400


@pytest.mark.parametrize(
    ("colour", "ink"),
    [
        ("#FFFFFF", "dark"),
        ("#000000", "light"),
        ("#D6A02C", "dark"),   # ochre: white text would be ~2.3:1
        ("#F2C94C", "dark"),
        ("#3D6E93", "light"),
        ("#C1584A", "light"),
        ("#8E5BB5", "light"),
        ("#abc", "dark"),       # shorthand hex
        ("not-a-colour", "light"),
    ],
)
def test_person_ink(colour, ink):
    assert appearance.person_ink(colour) == ink


def test_person_ink_always_clears_large_text_contrast():
    # Pills are large bold text (AA needs 3:1); the better ink always clears it.
    for r in range(0, 256, 15):
        for g in range(0, 256, 15):
            for b in range(0, 256, 15):
                c = f"#{r:02X}{g:02X}{b:02X}"
                ink = appearance.LIGHT_INK if appearance.person_ink(c) == "light" else appearance.DARK_INK
                assert appearance.contrast(c, ink) >= 3.0, c


async def test_widget_pills_use_person_ink(db, client):
    await db.execute("UPDATE profiles SET colour_hex = '#F2C94C' WHERE sort_order = 0")
    await db.commit()
    html = (await client.get("/widgets/tasks")).text
    assert 'class="person-pill ink-dark" style="--person: #F2C94C;"' in html


async def test_switch_in_counts_real_seconds_across_autumn_dst(db):
    # UK clocks go back on 25 Oct 2026: 19:00 -> 07:00 is 13 real hours.
    now = datetime(2026, 10, 24, 19, 0, tzinfo=LONDON)
    assert await appearance.current_mode(db, now) == {"mode": "night", "switch_in": 13 * 3600}


@pytest.mark.parametrize("tz_setting", ["Not/AZone", None])
async def test_bad_or_missing_timezone_falls_back_to_utc(db, tz_setting):
    if tz_setting is None:
        await db.execute("DELETE FROM app_settings WHERE key = 'calendar_timezone'")
    else:
        await database.set_setting(db, "calendar_timezone", tz_setting)
    await db.commit()
    # 06:30 UTC is still night in UTC (it's 07:30, day, in London).
    now = datetime(2026, 6, 15, 6, 30, tzinfo=ZoneInfo("UTC"))
    assert await appearance.current_mode(db, now) == {"mode": "night", "switch_in": 30 * 60}


def test_person_ink_clears_large_text_contrast_with_night_inks():
    # Night swaps the pill inks for softer ones (no pure white); still >= 3:1.
    night_light, night_dark = "#F4F1EA", "#14171C"  # --pill-light / --pill-dark
    for r in range(0, 256, 15):
        for g in range(0, 256, 15):
            for b in range(0, 256, 15):
                c = f"#{r:02X}{g:02X}{b:02X}"
                ink = night_light if appearance.person_ink(c) == "light" else night_dark
                assert appearance.contrast(c, ink) >= 3.0, c
