# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Huddle" / Family Display: a wall-mounted family dashboard. FastAPI + Jinja2 + HTMX + Gridstack.js + SQLite (WAL), no JS build step (vendored libs in `app/static/vendor/`). Deploy target is a GMKtec G10 running Docker; the display is a Galaxy Tab A9+ in Fully Kiosk Browser over Wi-Fi. `family-display-spec.md` is the product source of truth — check it before adding features (e.g. tasks are deliberately **not** points/rewards-based).

## Commands

```bash
# Dev (preferred) — docker-compose.override.yml auto-applies: --reload + ./app bind-mounted
docker compose up -d --build          # rebuild needed whenever requirements.txt changes
docker compose logs --tail 50 family-display
docker compose exec family-display python -c "..."   # poke the live DB / app modules

# Production-style run (skips the override file)
docker compose -f docker-compose.yml up -d --build

# Bare metal (a ./venv already exists; Windows Git Bash shown)
source venv/Scripts/activate && pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8010   # 8000 is usually taken by the container

python -c "import app.main"   # fastest syntax/import check

# Tests + lint (pip install -r requirements-dev.txt) — CI runs both plus `docker build`
ruff check app tests          # config in pyproject.toml; DTZ rules flag naive dates on purpose
python -m pytest -q
python -m pytest tests/test_task_sync.py::test_outage_keeps_queue_and_does_not_burn_retries
```

Tests use a fresh temp SQLite DB per test (`tests/conftest.py`) and `respx` to mock every Google call — an unmocked outbound request fails the test. They go through `httpx.ASGITransport`, which skips the lifespan, so the scheduler never starts. For UI changes, also run the app and drive it with Playwright (installed in `./venv`). Admin PIN defaults to `1234`.

## Architecture

- **Routers** (`app/routers/`) each own one widget or area. Every widget has a server-rendered fragment in `app/templates/widgets/` whose root element has an id (`#widget-tasks`, `#widget-calendar`, …); mutations and navigation use `hx-post`/`hx-get` with `hx-target="#widget-x" hx-swap="outerHTML"` — the widget re-renders itself. Calendar also self-polls (`hx-trigger="every 180s"`).
- **Dashboard assembly**: `routers/dashboard.py` fetches data for every widget up front and `dashboard.html` `{% include %}`s each widget template (includes inherit the full context). A widget's template must work both from that include and from its own `/widgets/...` route, so keep context variable names identical in both places.
- **Layout**: Gridstack positions persist in `layout_state` via `POST /api/layout` on every drag/resize (unauthenticated by design — anyone can rearrange).
- **Migrations** live in `database.init_db()` and must be idempotent: `CREATE TABLE IF NOT EXISTS` for new tables, `_add_column_if_missing()` for columns on shipped tables, and the `LAYOUT_VERSION` setting for one-time layout resets. `init_db()` uses `aiosqlite.Row`, so `get_setting`/`set_setting` work there.
- **Google integration** (no Google SDK — plain `httpx`):
  - `google_oauth.py`: OAuth flow, token storage (Fernet-encrypted in `auth_tokens`, keyed off `data/.secret_key` via `security.py`), refresh, Calendar reads, and `get_all_pages()` (always use it for Google list endpoints — reconcile treats "missing" as "deleted"). `get_valid_access_token()` returns `None` for "not connected" (render the stub) but **raises `httpx.HTTPError` for "Google unreachable"** — that's offline, not disconnected; only an `invalid_grant` refresh deletes tokens. Request paths must never 500 on a Google error: catch `httpx.HTTPError` and render an offline state (see `_connect()` / the `offline` flag on the month grid). Changing `SCOPES` requires users to disconnect/reconnect in Admin.
  - `google_tasks.py`: Tasks API client. `task_sync.py`: bidirectional sync — local mutations write rows to `sync_queue` (via `_queue_sync` in shopping/tasks/admin routers; payloads identify the row by id, and a hard-deleted shopping item's payload must carry its `google_task_id`), `run_sync()` pushes the queue then reconciles each configured list (last-write-wins on `updated_at`). Transient failures (network, 401/403/429/5xx) stop the cycle without counting retries; reconcile only runs after a clean push. Changing which Google list something is linked to must go through `relink_shopping`/`relink_profile`. `scheduler.py` runs sync every 60s via APScheduler, started in `main.py`'s lifespan — **single uvicorn process only**.
  - `main.py` refuses non-GET requests whose `Origin` isn't this host (the kiosk routes are PIN-free by design). Behind a reverse proxy, the `Host` header must be passed through.
  - `routers/calendar.py`: connect/callback/disconnect, calendar picker, month grid + day view (`/widgets/calendar/day/{date}`).

## Gotchas learned the hard way

- The container runs in **UTC**. Never use naive `date.today()`/`datetime.now()` for "today" — use `database.family_today(db)` (the calendar's timezone, else UTC), and keep Google's own offsets on event times (don't `.astimezone()` them). The daily task reset (`scheduler.py` → `tasks.run_daily_reset_if_due`) depends on this too.
- The pin/tab above each `.widget-card` pokes outside the card: `.widget-card` must not get `overflow: hidden`, and `.grid-stack-item-content` is forced `overflow: visible`. Flex children that should shrink need `min-height: 0`.
- `#dashboard-grid` scrolls internally, so Playwright `full_page` screenshots don't capture below the fold — scroll the element instead.
- In Playwright, use native `page.click()` + `wait_for_selector(...)` on the expected result; `element.click()` via `evaluate` and `networkidle` waits give false negatives with HTMX. Automated clicks can also trigger Gridstack drags, which **persist** to `layout_state` — check it after UI automation.
- OAuth redirect URIs are exact-string matched (`localhost` ≠ `127.0.0.1`). Google rejects raw LAN IPs and non-localhost `http://`, and "Testing" consent screens expire refresh tokens after 7 days.
- `docker compose config` prints the resolved `.env` secrets — don't run it where output is logged.
