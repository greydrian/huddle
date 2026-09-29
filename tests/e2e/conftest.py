"""Browser smoke tests: a real uvicorn + headless Chromium via Playwright.

Each test gets its own server on a free port with a throwaway DATA_DIR, seeded
straight into SQLite before the app starts (some settings, like the on-screen
keyboard, are read once at startup). No Google account is connected, the
weather cache is fresh, and Anthropic has no key, so nothing should call out;
as a backstop, outbound HTTP(S) goes to a dead proxy.

Run with `python -m pytest -m e2e` (after `python -m playwright install
chromium`). The plain `pytest -q` skips them via the marker in pytest.ini.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", ROOT / "test-results" / "e2e"))
VIEWPORT = {"width": 1280, "height": 800}
LOCATION = {"name": "Reading", "country": "United Kingdom", "latitude": 51.45, "longitude": -0.97}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server_env(data_dir: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "DATA_DIR": str(data_dir),
        # Set (even blank) so a developer's .env can't leak in: load_dotenv
        # never overrides a variable that's already present.
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "GOOGLE_PHOTOS_CLIENT_ID": "",
        "GOOGLE_PHOTOS_CLIENT_SECRET": "",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": "",
        # Anything that still tries the internet fails fast instead of leaking.
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def _seed(db_path: Path, *, keyboard: bool) -> None:
    now = datetime.now(timezone.utc)
    forecast = {
        "utc_offset_seconds": 0,
        "current": {"temperature": 14.0, "weather_code": 2},
        "daily": [
            {"date": (now + timedelta(days=n)).date().isoformat(), "weather_code": 2, "max": 16.0, "min": 7.0}
            for n in range(4)
        ],
    }
    settings = {
        "pin_is_default": "0",  # PIN is still 1234, but past the forced-change screen
        "calendar_timezone": "Europe/London",
        "weather_location": json.dumps(LOCATION),
        "weather_cache": json.dumps({
            "fetched_at": now.isoformat(),
            "latitude": LOCATION["latitude"],
            "longitude": LOCATION["longitude"],
            "forecast": forecast,
        }),
        "onscreen_keyboard": "1" if keyboard else "0",
    }
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            settings.items(),
        )
        profile_id = conn.execute("SELECT id FROM profiles ORDER BY sort_order LIMIT 1").fetchone()[0]
        conn.executemany(
            "INSERT INTO tasks (profile_id, title) VALUES (?, ?)",
            [(profile_id, "Feed the cat"), (profile_id, "Pack school bag")],
        )
        conn.execute("INSERT INTO shopping_items (title) VALUES ('Milk')")


class Server:
    def __init__(self, url: str, data_dir: Path, env: dict):
        self.url = url
        self.db_path = data_dir / "family_display.db"
        self._env = env

    def query(self, sql: str, params=()):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(sql, params).fetchall()

    def seed_banners(self, sound: bool = False):
        """Chore banners (spec 10.1) at (almost) any time of day, so a test
        doesn't depend on the clock: the Evening task group starts at 00:02
        and quiet hours are 00:00-00:02. Mum has two unfinished tasks
        (_seed); Riley and Jamie get one each, so three banners: "+1 more"."""
        settings = {
            "triggers": {kind: {"on": True, "sound": sound} for kind in ("events", "homework", "school", "chores")},
            "lead_minutes": 30, "quiet_start": "00:00", "quiet_end": "00:02",
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [("banner_settings", json.dumps(settings)),
                 ("task_group_after_school_start", "00:01"), ("task_group_evening_start", "00:02")],
            )
            conn.executemany("INSERT INTO tasks (profile_id, title) VALUES (?, ?)", [(3, "Bag"), (4, "Teeth")])

    def connect_google(self):
        """Store an unexpired Google token (encrypted with this server's key)
        so the calendar renders its month grid. Google itself stays out of
        reach (the dead proxy), so the grid shows its offline state."""
        code = (
            "import asyncio, time\n"
            "from app import database, google_oauth\n"
            "async def main():\n"
            "    async with database.get_db() as db:\n"
            "        await google_oauth.store_tokens(db, {'access_token': 'tok', 'refresh_token': 'r',"
            " 'expires_at': time.time() + 86400}, 'family@example.com')\n"
            "asyncio.run(main())\n"
        )
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=self._env, check=True)

    def seed_calendar(self):
        """A connected calendar with a saved copy of this month (seed_calendar.py).
        Google stays out of reach (the dead proxy), so every view is drawn
        from that copy with a "Last updated" note, and an add fails as offline."""
        subprocess.run([sys.executable, str(Path(__file__).with_name("seed_calendar.py"))],
                       cwd=ROOT, env=self._env, check=True)


@pytest.fixture
def start_server(tmp_path):
    """Factory: start_server(keyboard=False) -> Server. Stopped at teardown."""
    procs = []

    def start(*, keyboard: bool = False) -> Server:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        env = _server_env(data_dir)
        # Build the schema with the app's own init_db, then seed it directly.
        subprocess.run(
            [sys.executable, "-c", "import asyncio; from app import database; asyncio.run(database.init_db())"],
            cwd=ROOT, env=env, check=True,
        )
        _seed(data_dir / "family_display.db", keyboard=keyboard)

        port = _free_port()
        log = open(tmp_path / "uvicorn.log", "wb")  # closed at teardown
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        procs.append((proc, log))
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx.get(url + "/health", timeout=1, trust_env=False).status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                log.flush()
                pytest.fail("uvicorn didn't start:\n" + (tmp_path / "uvicorn.log").read_text(errors="replace"))
            time.sleep(0.2)
        return Server(url, data_dir, env)

    yield start

    for proc, log in procs:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture
async def page(request):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        # has_touch: the kiosk is a touchscreen, and Gridstack only wires up
        # its touch handlers when the browser reports touch support.
        context = await browser.new_context(viewport=VIEWPORT, has_touch=True)
        pg = await context.new_page()
        yield pg
        rep = getattr(request.node, "rep_call", None)
        if rep is not None and rep.failed:
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            await pg.screenshot(path=str(ARTIFACTS / f"{request.node.name}.png"))
        await browser.close()
