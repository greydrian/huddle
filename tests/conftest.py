import os
import tempfile
import time

# Must be set before any app module is imported: google_oauth reads the
# client id at import time, and database/security derive paths from DATA_DIR.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="huddle-test-")
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id"
os.environ["GOOGLE_CLIENT_SECRET"] = "test-client-secret"

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from app import database, google_oauth, http_client, security  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
async def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own fresh SQLite file and secret key."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(security, "SECRET_KEY_PATH", tmp_path / ".secret_key")
    http_client.reset_failures()  # outage log state is module-level
    await database.init_db()
    async with database.get_db() as db:
        # Avoid a calendar-metadata network call just to learn the timezone.
        await database.set_setting(db, database.CALENDAR_TIMEZONE_SETTING, "Europe/London")
        # A fresh install forces a PIN change before Admin opens. Most tests
        # are about Admin itself, so start past that screen (the PIN hash is
        # still "1234"); test_admin_auth.py covers the forced change.
        await database.set_setting(db, "pin_is_default", "0")
        await db.commit()
    yield


@pytest.fixture
async def db():
    async with database.get_db() as conn:
        yield conn


@pytest.fixture
async def connected(db):
    """A stored, unexpired Google token — i.e. 'connected'."""
    await google_oauth.store_tokens(
        db,
        {"access_token": "tok", "refresh_token": "refresh", "expires_at": time.time() + 3600},
        "family@example.com",
    )


@pytest.fixture
def google():
    """Mocks every outbound httpx call; unmatched requests fail loudly."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


@pytest.fixture
async def client():
    # ASGITransport doesn't run the lifespan, so the scheduler never starts.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
