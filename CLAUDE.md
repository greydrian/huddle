# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Huddle" / Family Display: a wall-mounted family dashboard. FastAPI + Jinja2 + HTMX + Gridstack.js + SQLite (WAL), no JS build step (vendored libs in `app/static/vendor/`). Deploy target is a GMKtec G10 running Docker; the display is a Galaxy Tab A9+ in Fully Kiosk Browser over Wi-Fi. `family-display-spec.md` is the product source of truth — check it before adding features (e.g. tasks are deliberately **not** points/rewards-based).

## Commands

```bash
# Dev (preferred) — docker-compose.override.yml auto-applies: --reload + ./app bind-mounted
docker compose up -d --build          # rebuild needed whenever the requirements change
docker compose logs --tail 50 family-display
docker compose exec family-display python -c "..."   # poke the live DB / app modules

# Production-style run (skips the override file)
docker compose -f docker-compose.yml up -d --build

# Bare metal (./venv is Python 3.14, like Docker and CI; Windows Git Bash shown)
# Rebuild: py -3.14 -m venv venv && venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt
#          venv/Scripts/pip install playwright && venv/Scripts/python -m playwright install chromium
source venv/Scripts/activate && pip install --require-hashes -r requirements-dev.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8010   # 8000 is usually taken by the container

python -c "import app.main"   # fastest syntax/import check

# Tests, lint, types — CI runs all of these plus a lock-file check and `docker build`
ruff check app tests          # config in pyproject.toml; DTZ rules flag naive dates on purpose
mypy                          # app/ only, lenient; [tool.mypy] in pyproject.toml — keep it at zero errors
python -m pytest -q           # skips tests/e2e (pytest.ini: -m "not e2e")
python -m pytest tests/test_task_sync.py::test_outage_keeps_queue_and_does_not_burn_retries
python -m pytest -m e2e       # Playwright smoke tests: own uvicorn + temp DATA_DIR, 1280x800

# Dependencies: edit requirements.in / requirements-dev.in, never the .txt files, then
# recompile both (universal = hashes for Linux and Windows wheels) and commit .in + .txt together.
uv pip compile requirements.in --universal --python-version 3.14 --generate-hashes -o requirements.txt
uv pip compile requirements-dev.in --universal --python-version 3.14 --generate-hashes -o requirements-dev.txt
```

Tests use a fresh temp SQLite DB per test (`tests/conftest.py`) and `respx` to mock every Google call — an unmocked outbound request fails the test. They go through `httpx.ASGITransport`, which skips the lifespan, so the scheduler never starts. For UI changes, also run the app and drive it with Playwright (installed in `./venv`). Admin PIN defaults to `1234`.

## Working with agents and tooling

- `.claude/agents/feature-implementer.md` — builds one feature end-to-end and opens a PR. Launch several at once with worktree isolation for parallel work; each uses its own port (8011–8019) and temp `DATA_DIR`. Brief it with the feature spec only — the repo conventions are in the agent file.
- `.claude/agents/code-reviewer.md` — read-only reviewer. Run it on every branch before merge, including agent-written ones.
- `.claude/skills/run-huddle` — how to launch an isolated or live instance and verify with Playwright.
- `.claude/settings.json` PostToolUse hook runs ruff on each edited `.py` file and reports problems back.
- Parallel branches most often conflict in `app/static/css/style.css`, `admin/settings.html` and `dashboard.py`/`dashboard.html`. Merge PRs one at a time and rebase the rest.

## Architecture

- **Routers** (`app/routers/`) each own one widget or area and stay thin: parse the request, call a service, render. Every widget has a server-rendered fragment in `app/templates/widgets/` whose root element has an id (`#widget-tasks`, `#widget-calendar`, …); mutations and navigation use `hx-post`/`hx-get` with `hx-target="#widget-x" hx-swap="outerHTML"` — the widget re-renders itself. Calendar and Weather also self-poll (`hx-trigger="every 180s"` / `"every 900s"`). The rest (roots marked `data-refresh` + `data-rev-key`) are re-fetched by `static/js/fresh.js` when `/api/rev` (`app/freshness.py`: a hash of each widget's loader output) changes — never while the widget is in use (focused input, on-screen keyboard, open `<details>`, pending tick, pointer down, Gridstack drag). The same script keeps the top-bar date on the family's day via `/api/today`.
- **Services** (`app/services/`: `tasks`, `shopping`, `homework`, `meals`, `weather`) hold the domain/data functions. Routers, `widgets.py`, Admin and `scheduler.py` import from services, never from another router.
- **Auth**: `app/auth.py` has `require_admin` (the Admin route dependency), `SESSION_COOKIE` and the session start/end helpers; PIN checking and lockout stay in `routers/admin.py`.
- **Dashboard assembly**: `app/widgets.py`'s `WIDGETS` registry maps each widget id to its template and an async context loader. `routers/dashboard.py` runs every loader and `dashboard.html` includes `"widgets/" ~ template` for each layout row (includes inherit the full context). A widget's template must work both from that include and from its own `/widgets/...` route, so a loader must return the same variable names the route uses. Adding a widget = registry entry + `DEFAULT_LAYOUT` in `database.py` (`tests/test_widget_registry.py` checks they agree).
- **Admin tasks are schedule-only**: tasks are added, renamed and deleted in Google Tasks (they sync in within a minute); Admin only sets which days a task repeats (`recurrence_rule`, see `recurrence.py`).
- **Admin tabs** (`app/admin_tabs.py`): `/admin?tab=<slug>` renders only that tab's sections (and `_render_admin` loads only its data). Every section anchor is in `SECTIONS`; redirects back to Admin use `admin_url(section, **params)`, never a hand-built `"/admin#..."`. `tests/test_admin_tabs.py` walks every Admin route, so a new route must be classified in its `ROUTES`. A new section = an `id` on its panel + a `SECTIONS` entry. Signing in returns to the tab (and `#section`) that asked, through the validated `admin_tabs.login_return()`.
- **Term dates** (spec 10.6): `services/term_dates.py` owns `school_periods` (one whole-school set; Admin → School → `#term-dates`, or approved from a `term_dates` inbox candidate) and answers `school_day_status()`/`is_school_day()` — a school year (Sep–Aug) with no term falls back to Mon–Fri minus bank holidays. `app/bank_holidays.py` keeps the `bank_holidays` table from the GOV.UK feed (England and Wales; weekly scheduler job, `run_if_due`). The calendar draws holidays/half terms/INSET days/closures as local-only bars (packed after Google's events) and never writes them to Google.
- **Layout**: Gridstack positions persist in `layout_state` via `POST /api/layout` on every drag/resize (unauthenticated by design — anyone can rearrange).
- **Migrations** are numbered, in `app/migrations.py` (`MIGRATIONS`), recorded in `schema_migrations`. **Append only: add a new numbered migration; never edit an applied one** (0001 `baseline` is the pre-v1.1 `init_db()`, frozen). Each runs once in its own transaction, recorded in that same transaction; a failure rolls back and stops startup. Migrations get a proxy connection whose `commit()`/`rollback()`/`executescript()` raise `MigrationError`. Never read live constants (`DEFAULT_LAYOUT`, `DEFAULT_PROFILES`, `LAYOUT_VERSION`) in a migration; freeze a copy, as 0001 does with `BASELINE_*`. **A future layout reset is a new migration, not a `LAYOUT_VERSION` bump.** A new widget needs no migration: `startup_checks` adds any missing `DEFAULT_LAYOUT` id. If a migration fails in production, the app exits at startup and Docker restarts it in a loop. It rolls back cleanly each time, and `/health` doesn't respond. Look for `MigrationError` in `docker compose logs` and go back to the previous code; README "If the app won't start after an update" has the steps. Stay idempotent where cheap (`IF NOT EXISTS`, `_add_column_if_missing()`) since a restored older backup re-runs later migrations. `init_db()` = pragmas, then migrations, then `startup_checks()` (every-boot data repairs such as re-seeding a deleted PIN or adding a new `DEFAULT_LAYOUT` widget's row). Connections use `aiosqlite.Row`, so `get_setting`/`set_setting` work in migrations. `tests/legacy_database.py` is a frozen copy of the old `init_db()` for the baseline tests; don't edit it.
- **Google integration** (no Google SDK — plain `httpx`):
  - `http_client.py`: every outbound call uses `http_client.client()` (per-call `AsyncClient` with the shared `DEFAULT_TIMEOUT`; weather passes its own tighter timeout). Log failures with `http_client.describe(exc)` — type/status only, never URLs, tokens or bodies — and for anything retried on a timer or page load use `report_failure`/`report_success` (one WARNING per outage, one INFO on recovery). Loggers are `logging.getLogger(__name__)`; `main.py` configures INFO once, keeps httpx (logs URLs incl. the revoke token) and APScheduler at WARNING, and strips the OAuth callback query from the access log.
  - `google_oauth.py`: OAuth flow, token storage (Fernet-encrypted in `auth_tokens`, keyed off `data/.secret_key` via `security.py`), refresh, the calendar-picker settings, and `get_all_pages()` (always use it for Google list endpoints — reconcile treats "missing" as "deleted"). `get_valid_access_token()` returns `None` for "not connected" (render the stub) but **raises `httpx.HTTPError` for "Google unreachable"** — that's offline, not disconnected; only an `invalid_grant` refresh deletes tokens. Request paths must never 500 on a Google error: catch `httpx.HTTPError`, log a WARNING and render an offline state (see `connect()` / the `offline` flag on the month grid). Changing `SCOPES` requires users to disconnect/reconnect in Admin.
  - `google_calendar.py`: event fetch (calendars fetched concurrently), month grid bar packing, day view. Request-path loads run under `CALENDAR_DEADLINE` (6s); calendars that answer render live, and each one that failed or timed out renders from `calendar_cache` (per calendar, keyed by a hash of account + selection; refreshed by a 5-min scheduler job, cleared on selection change/disconnect/reconnect) with a "Last updated" note. The offline state shows only for a failed calendar with nothing cached. Its timezone is `database.family_timezone()`, self-healed from the calendar's metadata when unset.
  - `google_tasks.py`: Tasks API client. `task_sync.py`: bidirectional sync — local mutations write rows to `sync_queue` (via `task_sync.queue_sync()`; payloads identify the row by id, and a hard-deleted shopping item's payload must carry its `google_task_id`), `run_sync()` pushes the queue then reconciles each configured list (last-write-wins on `updated_at`). Transient failures (network, 401/403/429/5xx) stop the cycle without counting retries; reconcile only runs after a clean push. Changing which Google list something is linked to must go through `relink_shopping`/`relink_profile`. `scheduler.py` runs sync every 60s via APScheduler, started in `main.py`'s lifespan — **single uvicorn process only**. `run_sync()` holds a lock (Admin's "Sync now" is a second caller) and records each cycle in the one-row `sync_status` table (`sync_status.py`: log-safe codes only); the same 401/permission 403 for 10 cycles becomes "needs attention" (rate-limit 403s never do). Unexpected non-HTTP errors are recorded as "error" and swallowed. The OAuth callback and Disconnect reset the row. That drives the top-bar dot (`GET /sync-status`, self-polling), Admin's `#sync` panel and `/health`'s detail — `/health` stays 200 through Google outages.
  - `main.py` refuses non-GET requests whose `Origin` isn't this host (the kiosk routes are PIN-free by design). Behind a reverse proxy, the `Host` header must be passed through.
  - `routers/calendar.py`: connect/callback/disconnect, calendar picker, month grid + day view (`/widgets/calendar/day/{date}`).
  - School email import (`school_email.py` + `google_gmail.py`, scheduled via `run_if_due` like the backup job) feeds school emails into `services/imports.ingest()`; approved events go to Google Calendar via `services/school_events.py` (claim, then `events.insert` with a deterministic event id). Gate features on `google_oauth.has_scope()` (granted scopes come from the token response), never on `SCOPES`. The SENDCo address (`ALWAYS_EXCLUDED`) must never reach Claude: Gmail query exclusion (headers and full text), a headers-only re-check before any body is fetched, and a drop of any email mentioning it; quoted history is stripped (`google_gmail.strip_quoted`). Caps are named constants (`MAX_MESSAGES_PER_RUN`, `imports.DAILY_CLAUDE_CAP`, `imports.MAX_ATTEMPTS`); a failed email is retried from `import_sources`, never by holding the checkpoint back. Log codes and counts only, never subjects, bodies or addresses.

## Gotchas learned the hard way

- The container runs in **UTC**. Never use naive `date.today()`/`datetime.now()` for "today" — use `database.family_today(db)` (the calendar's timezone, else UTC), and keep Google's own offsets on event times (don't `.astimezone()` them). The daily task reset (`scheduler.py` → `tasks.run_daily_reset_if_due`) depends on this too.
- `#dashboard-scroll` (under the top bar) is the dashboard's scroller; html/body are locked and Gridstack gives `#dashboard-grid` an inline height, so the grid itself never scrolls. Playwright `full_page` screenshots don't capture below the fold — scroll `#dashboard-scroll` instead.
- Gridstack drags only by `.drag-handle` (the widget's one header, from the `widget_header` macro in `widgets/_widget.html`, which also has `stub_empty` and the `tick_button` reveal-then-swap). Every widget template needs exactly one — with none, Gridstack falls back to the whole card and swallows every scroll swipe (`tests/test_dashboard_scroll.py` checks this). Put no buttons or other controls inside a handle: Gridstack eats touch taps on them (see the calendar header, where only icon + title are the handle).
- `.grid-stack-item-content` is forced `overflow: visible` so card shadows aren't clipped; each `.widget-body` scrolls itself. Flex children that should shrink need `min-height: 0`.
- Touch targets are at least 48 x 48 px (spec 10.0), checked on the dashboard and every Admin tab by `tests/e2e/test_touch_targets.py` (with a short, commented allowlist). Where a bigger visual would crowd the design, give the control `position: relative` and `--hit-x`/`--hit-y` for an invisible `::after` hit area ("Hit areas" in `style.css`).
- In Playwright, use native `page.click()` + `wait_for_selector(...)` on the expected result; `element.click()` via `evaluate` and `networkidle` waits give false negatives with HTMX. Automated clicks can also trigger Gridstack drags, which **persist** to `layout_state` — check it after UI automation.
- OAuth redirect URIs are exact-string matched (`localhost` ≠ `127.0.0.1`). Google rejects raw LAN IPs and non-localhost `http://`, and "Testing" consent screens expire refresh tokens after 7 days.
- Forgotten Admin PIN: `docker compose exec family-display python -m app.reset_pin` (back to 1234 + forced change, lockout cleared, all sessions ended).
- `docker compose config` prints the resolved `.env` secrets — don't run it where output is logged.
