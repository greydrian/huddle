"""Numbered migrations (app/migrations.py).

The baseline tests pin MIGRATIONS to version 1 so they stay true after later
migrations are appended: they prove that a database built by the old
init_db() (tests/legacy_database.py, a frozen copy) is left exactly as it was.
"""

import asyncio
import sqlite3

import legacy_database
import pytest

from app import database, migrations
from app.migrations import Migration, MigrationError


def _schema(path):
    with sqlite3.connect(path) as conn:
        master = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name != 'schema_migrations' ORDER BY type, name"
        ).fetchall()
        columns = {
            name: conn.execute(f"PRAGMA table_xinfo({name})").fetchall()
            for (kind, name, _tbl, _sql) in master
            if kind == "table"
        }
    return master, columns


def _data(path):
    with sqlite3.connect(path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'schema_migrations' ORDER BY name"
            )
        ]
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}


def _versions(path):
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


@pytest.fixture
def baseline_only(monkeypatch):
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:1])


@pytest.fixture
async def legacy_db(tmp_path, monkeypatch):
    """A database as the family's live one is today: built by the old init_db()."""
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(legacy_database, "DB_PATH", path)
    await legacy_database.init_db()
    return path


@pytest.fixture
def use_db(monkeypatch):
    def use(path):
        monkeypatch.setattr(database, "DB_PATH", path)
        return path

    return use


def _populate(path):
    """Representative data in every kind of table the family uses."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("UPDATE profiles SET google_tasklist_id = 'gl-1', school_year = 'Year 4' WHERE name = 'Riley'")
        conn.execute("INSERT INTO profiles (name, colour_hex, sort_order) VALUES ('Nan', '#123456', 4)")
        conn.executemany(
            "INSERT INTO tasks (profile_id, google_task_id, title, is_recurring, recurrence_rule, is_completed,"
            " completed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "g1", "Bins out", 1, "MO,TH", 0, None, "2026-09-01 07:00:00", "2026-09-02T08:00:00+00:00"),
                (3, None, "Feed the cat", 0, None, 1, "2026-09-27", "2026-09-20 10:00:00", "2026-09-27T18:00:00Z"),
            ],
        )
        conn.execute("DELETE FROM tasks WHERE title = 'Feed the cat'")  # a gap in the ids
        conn.execute("INSERT INTO tasks (profile_id, title) VALUES (2, 'Mow lawn')")
        conn.execute("INSERT INTO shopping_items (title, is_checked, google_task_id) VALUES ('Milk ×2', 1, 's1')")
        conn.execute("INSERT INTO meal_plans VALUES ('2026-09-28', 'Lasagne')")
        conn.execute("UPDATE layout_state SET grid_x = 3, grid_y = 20, is_visible = 0 WHERE widget_id = 'photos'")
        conn.execute("INSERT INTO sync_queue (service, payload_json, retry_count) VALUES ('shopping', '{\"item_id\": 1}', 2)")
        conn.execute(
            "INSERT INTO auth_tokens (service_name, account_email, encrypted_token_json) "
            "VALUES ('google', 'family@example.com', 'gAAAA-encrypted')"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            [
                ("pin_is_default", "0"),
                ("calendar_timezone", "Europe/London"),
                ("google_selected_calendars", '["primary"]'),
                ("onscreen_keyboard", "1"),
                ("last_daily_reset", "2026-09-28"),
                ("pin_lockout", "{}"),
            ],
        )
        conn.execute("INSERT INTO homework (profile_id, subject, title, due_date) VALUES (3, 'Maths', 'Fractions', '2026-10-01')")
        conn.execute("INSERT INTO practice_word_lists (profile_id, title, words) VALUES (3, 'Week 4', 'because\nwhich')")
        conn.execute("INSERT INTO practice_log VALUES (1, '2026-09-27')")
        conn.execute(
            "INSERT INTO calendar_cache VALUES ('sel', '2026-08-31', '2026-10-12', 'primary', '[]', '2026-09-28T07:00:00+00:00')"
        )
        conn.execute("UPDATE sync_status SET connected = 1, last_success_at = '2026-09-28T07:00:00+00:00', queue_depth = 1")
        conn.execute(
            "INSERT INTO import_sources (kind, source_ref, subject, excerpt, status, attempts, sender_unverified)"
            " VALUES ('gmail', 'msg-1', 'Trip letter', 'Dear parents', 'extracted', 1, 0)"
        )
        conn.execute(
            "INSERT INTO import_candidates (source_id, kind, profile_id, payload_json, evidence, status, created_table,"
            " external_id, claim_calendar_id) VALUES (1, 'event', 3, '{}', 'Trip on Friday', 'approved',"
            " 'google_calendar', 'ev-1', 'cal-1')"
        )
        conn.execute("INSERT INTO gmail_skipped (message_id, code, internal_date) VALUES ('msg-2', 'sender', 1759000000000)")


def test_versions_are_unique_and_consecutive():
    versions = [m.version for m in migrations.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert len({m.name for m in migrations.MIGRATIONS}) == len(versions)
    migrations.check_versions(migrations.MIGRATIONS)
    noop = migrations.MIGRATIONS[0].apply
    for bad in ([1, 3], [1, 1], [2, 1], [0, 1]):
        with pytest.raises(MigrationError):
            migrations.check_versions([Migration(v, "x", noop) for v in bad])


async def test_fresh_db_reaches_the_latest_version(tmp_path, use_db):
    path = use_db(tmp_path / "fresh.db")
    await database.init_db()
    assert _versions(path) == {m.version for m in migrations.MIGRATIONS}


async def test_fresh_baseline_matches_the_old_init_db(tmp_path, use_db, legacy_db, baseline_only):
    path = use_db(tmp_path / "fresh.db")
    await database.init_db()

    assert _schema(path) == _schema(legacy_db)
    assert _versions(path) == {1}

    # Seeded data is the same too, apart from the salted PIN hash.
    new, old = _data(path), _data(legacy_db)
    new["app_settings"] = [row for row in new["app_settings"] if row[0] != "pin_hash"]
    old["app_settings"] = [row for row in old["app_settings"] if row[0] != "pin_hash"]
    assert new == old


async def test_upgrading_a_live_db_changes_nothing_and_records_the_baseline(legacy_db, use_db, baseline_only):
    _populate(legacy_db)
    schema_before, data_before = _schema(legacy_db), _data(legacy_db)
    assert all(data_before[table] for table in data_before if table != "sqlite_sequence")  # every table has rows

    use_db(legacy_db)
    await database.init_db()

    assert _schema(legacy_db) == schema_before
    assert _data(legacy_db) == data_before
    assert _versions(legacy_db) == {1}
    assert database.onscreen_keyboard_enabled is True


async def test_running_twice_is_a_no_op(tmp_path, use_db):
    path = use_db(tmp_path / "twice.db")
    await database.init_db()
    with sqlite3.connect(path) as conn:
        recorded = conn.execute("SELECT * FROM schema_migrations").fetchall()
    schema, data = _schema(path), _data(path)

    await database.init_db()

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM schema_migrations").fetchall() == recorded
    assert _schema(path) == schema
    assert _data(path) == data


async def _boom(db):
    await db.execute("CREATE TABLE half_done (id INTEGER)")
    await database.set_setting(db, "half_done", "1")
    raise ValueError("boom")


async def test_a_failing_migration_rolls_back_and_stops_startup(monkeypatch, use_db, legacy_db, caplog):
    use_db(legacy_db)
    await database.init_db()
    before = _data(legacy_db), _schema(legacy_db)
    monkeypatch.setattr(migrations, "MIGRATIONS", [*migrations.MIGRATIONS, Migration(len(migrations.MIGRATIONS) + 1, "boom", _boom)])

    with pytest.raises(MigrationError, match="boom"):
        await database.init_db()

    assert (_data(legacy_db), _schema(legacy_db)) == before
    assert _versions(legacy_db) == set(range(1, len(migrations.MIGRATIONS)))
    assert "rolled back" in caplog.text

    # Fixed and redeployed: the next start applies it.
    async def fixed(db):
        await db.execute("CREATE TABLE half_done (id INTEGER)")

    migrations.MIGRATIONS[-1] = Migration(len(migrations.MIGRATIONS), "fixed", fixed)
    await database.init_db()
    assert _versions(legacy_db) == set(range(1, len(migrations.MIGRATIONS) + 1))
    assert "half_done" in _data(legacy_db)


async def test_a_failing_baseline_on_a_fresh_db_leaves_nothing(tmp_path, monkeypatch, use_db):
    path = use_db(tmp_path / "fresh.db")

    async def baseline_then_fail(db):
        await migrations.m0001_baseline(db)
        raise ValueError("late failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", [Migration(1, "baseline", baseline_then_fail)])
    with pytest.raises(MigrationError):
        await database.init_db()
    assert _data(path) == {}  # only the empty schema_migrations table exists
    assert _versions(path) == set()


async def test_a_migration_that_commits_is_refused(tmp_path, monkeypatch, use_db):
    path = use_db(tmp_path / "fresh.db")
    await database.init_db()

    async def commits(db):
        await db.commit()

    monkeypatch.setattr(migrations, "MIGRATIONS", [*migrations.MIGRATIONS, Migration(len(migrations.MIGRATIONS) + 1, "commits", commits)])
    with pytest.raises(MigrationError, match="own transaction"):
        await database.init_db()
    assert _versions(path) == set(range(1, len(migrations.MIGRATIONS)))


async def test_concurrent_runs_in_one_process_apply_once(tmp_path, use_db):
    path = use_db(tmp_path / "fresh.db")
    await asyncio.gather(database.init_db(), database.init_db(), database.init_db())
    assert _versions(path) == {m.version for m in migrations.MIGRATIONS}
    assert len(_data(path)["profiles"]) == len(database.DEFAULT_PROFILES)
