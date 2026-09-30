"""Admin tabs (spec 10.0, app/admin_tabs.py) and family members' Parent +
email fields (migration 2)."""

import re
import sqlite3
import time
from pathlib import Path

import pytest

from app import admin_tabs, backup, database, google_oauth, migrations, school_email
from app.admin_tabs import SECTIONS, TABS, admin_url
from app.main import app
from app.routers import admin
from app.security import create_session_token
from app.services import weather

APP = Path(__file__).resolve().parents[1] / "app"
TASKLISTS_URL = "https://tasks.googleapis.com/tasks/v1/users/@me/lists"


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


@pytest.fixture
def google_lists(google, connected):
    """A connected account whose lists load, so every section (calendar picker, Task Sync) renders."""
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(
        200, json={"items": [{"id": "family@example.com", "summary": "Family", "primary": True, "accessRole": "owner"}]}
    )
    google.get(TASKLISTS_URL).respond(200, json={"items": [{"id": "list-shop", "title": "Groceries"}]})
    return google


@pytest.fixture
def google_lists_unconnected(google):
    """The same lists, for a test that connects part-way through."""
    google.get(google_oauth.CALENDAR_LIST_ENDPOINT).respond(
        200, json={"items": [{"id": "family@example.com", "summary": "Family", "primary": True, "accessRole": "owner"}]}
    )
    google.get(TASKLISTS_URL).respond(200, json={"items": [{"id": "list-shop", "title": "Groceries"}]})
    return google


def _current_tab(html: str) -> str:
    match = re.search(r'class="admin-tab is-current"\s*aria-current="page">([^<]+)<', html)
    assert match, "no current tab marked"
    label = match.group(1).replace("&amp;", "&")
    return next(slug for slug, name in TABS.items() if name == label)


# --- Tabs ---


async def test_every_tab_renders_only_its_own_sections(admin_client, google_lists):
    for tab in TABS:
        html = (await admin_client.get(f"/admin?tab={tab}")).text
        assert _current_tab(html) == tab
        for slug in TABS:  # every tab is a link, current or not
            assert f'href="/admin?tab={slug}"' in html
        shown = {s for s in SECTIONS if f'id="{s}"' in html}
        assert shown == {s for s, t in SECTIONS.items() if t == tab}, tab


async def test_no_or_unknown_tab_opens_family(admin_client):
    for url in ("/admin", "/admin?tab=nope"):
        html = (await admin_client.get(url)).text
        assert _current_tab(html) == "family"
        assert 'id="family"' in html and 'id="backups"' not in html


NO_ROW = "999999"  # an id nothing has: routes then take their "missing" redirect
TAB = "tab"

# Every Admin route. "tab" routes are called (as signed-in Admin, with the
# data given) and must answer 303 to an Admin tab: ?tab= plus a #section on
# that tab. The rest say why they don't. A new route fails
# test_every_admin_route_is_classified until it's added here.
ROUTES: dict[tuple[str, str], tuple[str, dict | str]] = {
    ("POST", "/admin/profiles"): (TAB, {"name": "Nan", "colour_hex": "#123456"}),
    ("POST", "/admin/profiles/{profile_id}/details"): (TAB, {"email": "not an email"}),
    ("POST", "/admin/profiles/{profile_id}/delete"): (TAB, {}),
    ("POST", "/admin/profiles/{profile_id}/avatar"): (TAB, {"kind": "emoji", "emoji": "🐶"}),
    ("POST", "/admin/profiles/{profile_id}/avatar/photo"): (TAB, {}),
    ("POST", "/admin/profiles/{profile_id}/avatar/photo/remove"): (TAB, {}),
    ("POST", "/admin/tasks/{task_id}/edit"): (TAB, {"profile_id": "1"}),
    ("POST", "/admin/tasks/groups"): (TAB, {"after_school_start": "18:00", "evening_start": "12:00"}),
    ("POST", "/admin/homework"): (TAB, {"profile_id": "1", "title": "Fractions"}),
    ("POST", "/admin/homework/{homework_id}/edit"): (TAB, {}),
    ("POST", "/admin/homework/{homework_id}/archive"): (TAB, {}),
    ("POST", "/admin/homework/{homework_id}/delete"): (TAB, {}),
    ("POST", "/admin/practice-words"): (TAB, {"profile_id": "1", "title": "Week 1", "words": "because"}),
    ("POST", "/admin/practice-words/{list_id}/edit"): (TAB, {}),
    ("POST", "/admin/practice-words/{list_id}/archive"): (TAB, {}),
    ("POST", "/admin/practice-words/{list_id}/delete"): (TAB, {}),
    ("POST", "/admin/handwriting-style"): (TAB, {"style": "nope"}),
    ("POST", "/admin/inbox/add"): (TAB, {"text": ""}),
    ("POST", "/admin/inbox/candidates/{candidate_id}/approve"): (TAB, {}),
    ("POST", "/admin/inbox/candidates/{candidate_id}/discard"): (TAB, {}),
    ("POST", "/admin/inbox/sources/{source_id}/approve-all"): (TAB, {}),
    ("POST", "/admin/inbox/sources/{source_id}/retry"): (TAB, {}),
    ("POST", "/admin/inbox/sources/{source_id}/delete"): (TAB, {}),
    ("POST", "/admin/school-email/schedule"): (TAB, {"mode": "nope", "time": "18:00"}),
    ("POST", "/admin/school-email/senders"): (TAB, {"senders": "", "exclusions": ""}),
    ("POST", "/admin/school-email/calendar"): (TAB, {"calendar_id": ""}),
    ("POST", "/admin/school-email/check"): (TAB, {}),
    ("POST", "/admin/term-dates"): (TAB, {"kind": "inset", "start_date": "2026-10-23", "end_date": "2026-10-23"}),
    ("POST", "/admin/term-dates/{period_id}/edit"): (TAB, {}),
    ("POST", "/admin/term-dates/{period_id}/delete"): (TAB, {}),
    ("POST", "/admin/google/shopping-list"): (TAB, {"tasklist_id": "list-shop"}),
    ("POST", "/admin/google/task-lists"): (TAB, {}),
    ("POST", "/admin/google/calendars"): (TAB, {}),
    ("POST", "/admin/google/family-calendar"): (TAB, {"calendar_id": "family@example.com"}),
    ("POST", "/admin/google/calendar-view"): (TAB, {"view": "week"}),
    ("POST", "/admin/google/calendar-people"): (TAB, {}),
    ("POST", "/admin/google/disconnect"): (TAB, {}),
    ("GET", "/admin/google/callback"): (TAB, {}),
    ("POST", "/admin/weather-location"): (TAB, {"place": "Nowhere"}),
    ("POST", "/admin/onscreen-keyboard"): (TAB, {}),
    ("POST", "/admin/widgets/{widget_id}/visibility"): (TAB, {"visible": "false"}),
    ("POST", "/admin/widgets/{widget_id}/school-days"): (TAB, {"enabled": "true"}),
    ("POST", "/admin/appearance"): (TAB, {"value": "nope"}),
    ("POST", "/admin/idle"): (TAB, {"mode": "nope"}),
    ("GET", "/admin/photos/callback"): (TAB, {}),
    ("POST", "/admin/photos/choose"): (TAB, {}),
    ("POST", "/admin/photos/cancel"): (TAB, {}),
    ("POST", "/admin/photos/remove"): (TAB, {}),
    ("POST", "/admin/photos/sign-out"): (TAB, {}),
    ("POST", "/admin/banners"): (TAB, {"lead_minutes": "3", "quiet_start": "21:00", "quiet_end": "07:00"}),
    ("POST", "/admin/change-pin"): (TAB, {"new_pin": "2580", "confirm_pin": "2580"}),
    ("POST", "/admin/backups/run"): (TAB, {}),
    ("POST", "/admin/sync"): (TAB, {}),
    ("GET", "/admin"): ("exempt", "the Admin page itself"),
    ("GET", "/admin/login"): ("exempt", "the PIN page"),
    ("POST", "/admin/login"): ("exempt", "goes back to where the PIN was asked for (login_return, tested below)"),
    ("POST", "/admin/logout"): ("exempt", "back to the PIN page"),
    ("GET", "/admin/new-pin"): ("exempt", "the forced 'Choose a new PIN' page"),
    ("POST", "/admin/new-pin"): ("exempt", "opens Admin after the forced PIN change"),
    ("GET", "/admin/google/connect"): ("exempt", "off to Google's consent screen"),
    ("GET", "/admin/photos/connect"): ("exempt", "off to Google's consent screen, for the Photos account"),
    ("GET", "/admin/photos/status"): ("exempt", "an HTMX fragment"),
    ("GET", "/admin/inbox/sources/{source_id}"): ("exempt", "an HTMX fragment"),
    ("GET", "/admin/school-email/status"): ("exempt", "an HTMX fragment"),
}


def _admin_routes() -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/admin")
        for method in operations
    }


def test_every_admin_route_is_classified():
    assert _admin_routes() == set(ROUTES)


@pytest.fixture
def quiet_side_effects(monkeypatch):
    """What the walk's routes would otherwise start: a backup, a school
    email check, a geocoder call."""

    async def backup_done():
        return Path("backup.db")

    async def nowhere(place):
        return None

    monkeypatch.setattr(backup, "create_backup", backup_done)
    monkeypatch.setattr(school_email, "start_check", lambda: False)
    monkeypatch.setattr(weather, "geocode", nowhere)


async def _walk(admin_client) -> list[tuple[str, str]]:
    """Calls every "tab" route; returns (route, Location) pairs."""
    locations = []
    for (method, path), (kind, data) in ROUTES.items():
        if kind != TAB:
            continue
        url = re.sub(r"\{[a-z_]+\}", NO_ROW, path)
        if method == "GET":
            resp = await admin_client.get(url)
        elif path == "/admin/inbox/add":
            resp = await admin_client.post(url, files={"text": (None, "")})
        else:
            resp = await admin_client.post(url, data=data)
        assert resp.status_code == 303, (path, resp.status_code)
        locations.append((path, resp.headers["location"]))
    return locations


async def test_every_admin_redirect_opens_the_tab_with_its_anchor(
    admin_client, db, google_lists_unconnected, quiet_side_effects
):
    targets = await _walk(admin_client)
    targets += [(f"error {code}", admin_url(section, error=code)) for code, (section, _) in admin.ADMIN_ERRORS.items()]
    targets += [
        ("status", admin_url("sync", sync="done")),
        ("status", admin_url("school-email", school_email="started")),
        ("status", admin_url("weather", weather_error="offline")),
    ]
    # Rendered connected, so every Google & Sync anchor (calendar picker, Task Sync) exists.
    await google_oauth.store_tokens(
        db, {"access_token": "tok", "refresh_token": "refresh", "expires_at": time.time() + 3600}, "family@example.com"
    )
    for route, location in targets:
        path, _, anchor = location.partition("#")
        assert path.startswith("/admin?tab=") and anchor in SECTIONS, (route, location)
        resp = await admin_client.get(path)
        assert resp.status_code == 200, (route, location)
        assert f'id="{anchor}"' in resp.text, (route, location)
        assert _current_tab(resp.text) == SECTIONS[anchor], (route, location)


def test_template_links_use_known_sections():
    for path in (APP / "templates").rglob("*.html"):
        for tab, section in re.findall(r'href="/admin\?tab=([a-z]+)#([a-z-]+)"', path.read_text(encoding="utf-8")):
            assert SECTIONS[section] == tab, (path.name, section)


async def test_error_messages_show_on_their_tab(admin_client):
    html = (await admin_client.get(admin_url("pin", error="pin-weak"))).text
    assert "too easy to guess" in html


@pytest.mark.parametrize(
    ("url", "tab"),
    [
        ("/admin?error=pin-invalid", "system"),
        ("/admin?error=import-already", "school"),
        ("/admin?sync=done", "google"),
        ("/admin?school_email=busy", "school"),
        ("/admin?weather_error=notfound", "display"),
    ],
)
async def test_older_links_without_a_tab_still_open_the_right_one(admin_client, url, tab):
    """/admin?error=...#section from before the tabs: the message picks the tab."""
    assert _current_tab((await admin_client.get(url)).text) == tab


async def test_hash_only_links_are_mapped_by_the_page_script(admin_client):
    """/admin#sync (the old sync-dot link, bookmarks) can't be seen by the
    server; the page carries the section map its script switches tabs with."""
    html = (await admin_client.get("/admin")).text
    assert '"sync": "google"' in html
    assert "location.replace('/admin?tab=' + tab + location.hash)" in html


# --- Parents (migration 2) ---


def _columns(path):
    with sqlite3.connect(path) as conn:
        return {row[1]: row for row in conn.execute("PRAGMA table_info(profiles)")}


async def test_migration_2_adds_parent_fields_without_touching_data(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:1])
    await database.init_db()  # a database as it was before migration 2
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE profiles SET school_year = 'Year 4', google_tasklist_id = 'gl' WHERE name = 'Riley'")
        before = conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    assert "is_parent" not in _columns(path)

    monkeypatch.undo()
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()

    columns = _columns(path)
    # PRAGMA table_info: (cid, name, type, notnull, default, pk)
    assert columns["is_parent"][2:5] == ("INTEGER", 1, "0")
    assert columns["email"][2:4] == ("TEXT", 0)
    with sqlite3.connect(path) as conn:
        after = conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    assert [row[: len(before[0])] for row in after] == before  # old columns unchanged
    assert {row[len(before[0]) : len(before[0]) + 2] for row in after} == {(0, None)}  # is_parent, email
    assert 2 in versions


async def _profile(db, name):
    row = await (await db.execute("SELECT * FROM profiles WHERE name = ?", (name,))).fetchone()
    return dict(row)


async def test_parent_and_email_save_and_show(db, admin_client):
    mum = await _profile(db, "Mum")
    r = await admin_client.post(
        f"/admin/profiles/{mum['id']}/details",
        data={"school_year": "", "is_parent": "true", "email": "  mum@example.co.uk "},
    )
    assert r.status_code == 303 and r.headers["location"] == admin_url("family")
    saved = await _profile(db, "Mum")
    assert (saved["is_parent"], saved["email"]) == (1, "mum@example.co.uk")

    html = (await admin_client.get("/admin?tab=family")).text
    assert 'value="mum@example.co.uk"' in html
    assert '<span class="admin-badge">Parent</span>' in html

    # Unticked and blank: back to not a parent, no email.
    await admin_client.post(f"/admin/profiles/{mum['id']}/details", data={"school_year": "", "email": ""})
    saved = await _profile(db, "Mum")
    assert (saved["is_parent"], saved["email"]) == (0, None)


@pytest.mark.parametrize(
    "bad",
    [
        "mum",
        "mum@",
        "@example.com",
        "mum@example",
        "a b@example.com",
        "mum@example.com, dad@example.com",
        "Mum <mum@example.com>",
        "x" * 250 + "@example.com",
        # control characters, anywhere
        "mu\x00m@example.com",
        "mum@exa\x01mple.com",
        "mum@example.com\x7f",
        "mu\x1bm@example.com",
    ],
)
async def test_a_bad_email_changes_nothing(db, admin_client, bad):
    mum = await _profile(db, "Mum")
    r = await admin_client.post(
        f"/admin/profiles/{mum['id']}/details", data={"school_year": "Year 9", "is_parent": "true", "email": bad}
    )
    assert r.headers["location"] == admin_url("family", error="profile-email")
    assert await _profile(db, "Mum") == mum
    html = (await admin_client.get(r.headers["location"].split("#")[0])).text
    assert "That email address doesn" in html


async def test_profile_details_need_admin(client, db):
    mum = await _profile(db, "Mum")
    r = await client.post(f"/admin/profiles/{mum['id']}/details", data={"is_parent": "true"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert await _profile(db, "Mum") == mum


def test_tab_labels_match_the_spec():
    assert list(admin_tabs.TABS.values()) == ["Family", "School", "Display", "Google & Sync", "Assistant", "System"]


# --- Signing in goes back to the tab that asked ---


async def test_signed_out_tab_link_comes_back_to_its_tab_and_section(client, db):
    # The sync dot's link, signed out: the tab rides along as ?next= (the
    # browser keeps #sync on the login page, whose script posts it).
    resp = await client.get("/admin?tab=google")
    assert (resp.status_code, resp.headers["location"]) == (303, "/admin/login?next=%2Fadmin%3Ftab%3Dgoogle")
    page = (await client.get(resp.headers["location"])).text
    assert '<input type="hidden" name="next" value="/admin?tab=google">' in page

    wrong = await client.post("/admin/login", data={"pin": "0000", "next": "/admin?tab=google", "section": "sync"})
    assert wrong.status_code == 401  # a typo keeps both for the next try
    assert 'name="next" value="/admin?tab=google"' in wrong.text and 'name="section" value="sync"' in wrong.text
    await database.set_setting(db, "pin_lockout", "{}")  # skip the backoff the typo started
    await db.commit()

    resp = await client.post("/admin/login", data={"pin": "1234", "next": "/admin?tab=google", "section": "sync"})
    assert (resp.status_code, resp.headers["location"]) == (303, "/admin?tab=google#sync")


async def test_plain_admin_needs_no_next(client):
    assert (await client.get("/admin")).headers["location"] == "/admin/login"
    assert (await client.get("/admin?tab=nope")).headers["location"] == "/admin/login"
    resp = await client.post("/admin/login", data={"pin": "1234", "section": "backups"})
    assert resp.headers["location"] == "/admin?tab=system#backups"  # an old /admin#backups link


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/admin",
        "//evil.example/admin",
        "/\\evil.example",
        "/admin/../x",
        "/adminx",
        "/admin/login",
        "/admin?tab=evil",
        "/admin?tab=google&x=1",
        "/admin?tab=google&tab=system",
        "/admin?tab=google#sync",
        "javascript:alert(1)",
        "http:/admin",
    ],
)
async def test_login_never_redirects_off_admin(client, next_url):
    resp = await client.post("/admin/login", data={"pin": "1234", "next": next_url, "section": "sync"})
    assert resp.headers["location"] == "/admin"
    page = (await client.get("/admin/login", params={"next": next_url})).text
    assert 'name="next"' not in page


@pytest.mark.parametrize(
    ("next_url", "section", "expected"),
    [
        ("/admin?tab=google", "", "/admin?tab=google"),
        ("/admin?tab=google", "backups", "/admin?tab=google"),  # not a section on that tab
        ("/admin?tab=google", "constructor", "/admin?tab=google"),
        ("", "__proto__", "/admin"),
        ("", "sync", "/admin?tab=google#sync"),
        ("/admin", "pin", "/admin?tab=system#pin"),
    ],
)
def test_login_return(next_url, section, expected):
    assert admin_tabs.login_return(next_url, section) == expected


async def test_the_forced_new_pin_screen_still_comes_first(client, db):
    await database.set_setting(db, "pin_is_default", "1")
    await db.commit()
    resp = await client.post("/admin/login", data={"pin": "1234", "next": "/admin?tab=google", "section": "sync"})
    assert resp.headers["location"] == "/admin?tab=google#sync"
    assert (await client.get("/admin?tab=google")).headers["location"] == "/admin/new-pin"
