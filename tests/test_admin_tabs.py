"""Admin tabs (spec 10.0, app/admin_tabs.py) and family members' Parent +
email fields (migration 2)."""

import re
import sqlite3
from pathlib import Path

import pytest

from app import admin_tabs, database, google_oauth, migrations
from app.admin_tabs import SECTIONS, TABS, admin_url
from app.routers import admin
from app.security import create_session_token

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


def _redirect_sections() -> set[str]:
    """Every section a router sends Admin back to: admin_url("...") calls
    and the ADMIN_ERRORS sections (via _admin_error)."""
    found = {section for section, _ in admin.ADMIN_ERRORS.values()}
    for path in (APP / "routers").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        found |= set(re.findall(r'admin_url\(\s*"([a-z-]+)"', source))
        # Nothing builds an Admin section link by hand any more.
        assert not re.search(r'["\']/admin[?#]', source), path.name
    return found


def test_redirects_and_links_only_use_known_sections():
    assert _redirect_sections() <= set(SECTIONS)
    for path in (APP / "templates").rglob("*.html"):
        for tab, section in re.findall(r'href="/admin\?tab=([a-z]+)#([a-z-]+)"', path.read_text(encoding="utf-8")):
            assert SECTIONS[section] == tab, (path.name, section)


async def test_every_redirect_target_opens_the_tab_with_its_anchor(admin_client, google_lists):
    targets = {admin_url(s) for s in _redirect_sections()}
    targets |= {admin_url(section, error=code) for code, (section, _) in admin.ADMIN_ERRORS.items()}
    targets.add(admin_url("sync", sync="done"))
    targets.add(admin_url("school-email", school_email="started"))
    targets.add(admin_url("weather", weather_error="offline"))
    for url in sorted(targets):
        path, _, anchor = url.partition("#")
        resp = await admin_client.get(path)
        assert resp.status_code == 200, url
        assert f'id="{anchor}"' in resp.text, url
        assert _current_tab(resp.text) == SECTIONS[anchor], url


async def test_error_messages_show_on_their_tab(admin_client):
    html = (await admin_client.get(admin_url("pin", error="pin-weak"))).text
    assert "too easy to guess" in html


@pytest.mark.parametrize(("url", "tab"), [
    ("/admin?error=pin-invalid", "system"),
    ("/admin?error=import-already", "school"),
    ("/admin?sync=done", "google"),
    ("/admin?school_email=busy", "school"),
    ("/admin?weather_error=notfound", "display"),
])
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
    assert [row[:-2] for row in after] == before  # old columns unchanged
    assert {row[-2:] for row in after} == {(0, None)}
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


@pytest.mark.parametrize("bad", ["mum", "mum@", "@example.com", "mum@example", "a b@example.com",
                                 "mum@example.com, dad@example.com", "Mum <mum@example.com>",
                                 "x" * 250 + "@example.com"])
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
