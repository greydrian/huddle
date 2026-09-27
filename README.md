# Family Display

A shared touchscreen dashboard for the household — calendar, per-person task
lists, a shared shopping list, meal planning, and a customisable widget grid.
Built per the project spec: split architecture with a **GMKtec G10 (x86,
Ubuntu/Debian) running Docker** as the server and a **Samsung Galaxy Tab A9+
wall-mounted display** running Fully Kiosk Browser over Wi-Fi, Python/FastAPI
backend, HTMX + Alpine.js + Gridstack.js frontend, SQLite, fully local (no
cloud component).

## What's working right now

- **Dashboard**: Gridstack widget grid, drag/resize by anyone, layout persists to SQLite
- **Task lists**: per-family-member, tick to complete with a checkmark animation,
  purely functional (no points/rewards — per spec), admin can add/remove tasks
- **Shopping list**: single shared list, add/tick/delete
- **Meal planning**: simple text-based plan for the next 7 days, inline editing
- **Calendar & Upcoming Events**: Google OAuth (read-only), Calendar shows today's
  agenda and Upcoming Events shows the next 7 days, both self-refresh every 3 minutes;
  falls back to an illustrated "not connected" state until Admin → Connect Google
  Account is used (see "Connecting Google Calendar" below)
- **Admin**: PIN-protected (default PIN is **1234** — change it immediately under
  Admin → Change Admin PIN), with exponential backoff on failed attempts,
  family member management, task management
- **Design system**: self-hosted fonts (Fraunces/Inter/JetBrains Mono, no
  external font CDN — this is fully offline-capable), warm "kitchen
  noticeboard" visual language with per-person colour tabs on each widget

## What's stubbed (next build phase)

These show a placeholder card on the dashboard rather than live data:

- **Weather** — needs the Open-Meteo integration (no API key required, just not wired up)
- **Photos** — needs Google Photos Picker OAuth + the local image-cache layer
- **Homework** — needs Gmail API access to parse the weekly Classroom guardian email

None of these need architecture decisions — they were already made in the spec.
They're just not built yet. See "Next steps" below.

## Running with Docker (recommended)

Requires Docker Desktop (Windows/Mac, for local dev) or Docker Engine (Linux —
this is what the G10 runs).

```bash
docker compose up --build
```

Open `http://localhost:8000/`. This builds on **Python 3.14** (the current
stable series as of 2026). The deployment target (GMKtec G10) is plain x86_64
Ubuntu/Debian, same architecture as most dev machines, so there's no
cross-platform wheel concern to track.

The database lives in a **bind-mounted host folder**, `./data/family-display`,
per the spec's deployment decisions (Section 9.7) — plain files you can
browse, back up, or `rsync` directly, rather than an opaque named volume. This
is safe here specifically because the G10 runs native Linux: the
Docker-Desktop-on-Windows/Mac caveat about bind mounts and SQLite WAL-mode
file locking (gRPC-FUSE/virtiofs not reliably supporting it) doesn't apply on
the actual server. If you're developing on Windows/Mac and hit "database is
locked" errors against the bind-mounted DB, that's that caveat surfacing in
dev — it's not expected to happen on the G10 itself.

Running `docker compose up` locally automatically layers in
`docker-compose.override.yml`, which adds `--reload` and bind-mounts the
`app/` folder for live code edits. For a clean run without hot-reload, use
`docker compose -f docker-compose.yml up --build`.

**⚠️ Honesty check:** I don't have Docker available in the environment I
built this in, so unlike the rest of the app — which I ran end-to-end against
a live server — I have *not* actually executed `docker build`/`docker compose
up` myself. What I have verified: the Dockerfile follows standard patterns,
every pinned dependency in `requirements.txt` explicitly lists Python 3.14
support on PyPI, and I re-ran the full test suite (login, tasks, shopping,
meals, layout) against the updated dependency versions in a plain venv with
no errors. But the actual container build is untested by me — please run
`docker compose up --build` yourself as the first real check, and let me know
if anything surfaces (dependency resolution issues are the most likely
failure mode, not application logic).

### Deploying to the G10

1. Install Docker on Ubuntu/Debian: `curl -fsSL https://get.docker.com | sh`
2. Copy this project to the G10 (or `git clone` it)
3. `docker compose -f docker-compose.yml up -d --build` — production mode, no hot-reload
4. Point the Galaxy Tab A9+'s Fully Kiosk Browser at `http://<g10-lan-address>:8000/`
   — see "Kiosk browser setup" below
5. The spec (9.7) also has Home Assistant and a Caddy reverse proxy sharing the
   G10 via Docker, with a prefixed shared `.env` (`DISPLAY_...`, `HA_...`).
   That's a separate deployment concern layered on top of this repo, not
   something `docker-compose.yml` here needs to own — this file stays scoped
   to the family display service.

## Local development (without Docker)

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/`. The SQLite database is created automatically
at `data/family_display.db` on first run, seeded with 4 placeholder family
members (Mum, Dad, Riley, Jamie) — rename/replace these in Admin.

**Default admin PIN is `1234`.** Change it immediately (Admin → Change Admin PIN)
before this runs anywhere other than your own laptop.

## Connecting Google Calendar

The app talks to Google's OAuth and Calendar REST endpoints directly (via `httpx` —
see `app/google_oauth.py`), no Google SDK. Calendar is **read-only** (`calendar.readonly`
scope) and renders as a month grid with a day view; the `tasks` scope drives shopping-list
and per-person task sync (`app/task_sync.py`). In-app event creation/editing is a separate
follow-up.

**1. Create a Google Cloud project and OAuth credentials** (one-time, in a browser):

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project
   (or reuse one).
2. **APIs & Services → Library** — enable the **Google Calendar API** and **Google Tasks API**.
3. **APIs & Services → OAuth consent screen** — User type **External**. Add your own
   Google account under **Test users**. **Warning:** while publishing status is
   **Testing**, Google expires refresh tokens after 7 days, so the connection silently
   drops weekly. For an always-on display, switch it to **In production** (an unverified
   app is usable for personal use; you'll see a "Google hasn't verified this app" screen
   when connecting) — otherwise plan on reconnecting in Admin every week.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID** —
   Application type **Web application**. Under **Authorized redirect URIs**, add both
   `http://localhost:8000/admin/google/callback` and
   `http://127.0.0.1:8000/admin/google/callback` (Google matches the string exactly, so
   whichever host you type in the browser must be registered).
   **For the G10:** Google rejects raw IP addresses (e.g. `192.168.x.x`) and requires
   HTTPS for anything that isn't localhost, so the callback can't point at the G10's LAN
   IP. Either put the app behind Caddy with a real hostname + HTTPS (spec 9.7) and register
   that URL, or do the one-time Connect step through an SSH tunnel
   (`ssh -L 8000:localhost:8000 <g10>`) so your browser reaches it as `localhost`.
5. Copy the **Client ID** and **Client Secret**.

**2. Configure the app:**

```bash
cp .env.example .env
```

Fill in `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env` (gitignored — never commit
real credentials). Restart the app (`docker compose up` picks up `.env` automatically via
Compose variable substitution; for bare-metal, `python-dotenv` loads it on startup).

**3. Connect the account:** Admin → Google Account → **Connect Google Account**, sign in,
approve. Tokens are encrypted at rest (`app/security.py`'s `encrypt_token_json`, keyed off
the same local secret file that signs admin session cookies) in the `auth_tokens` table.
**Disconnect** in the same panel revokes and clears them.

## Project structure

```
Dockerfile                Python 3.14 image, non-root user, /health check
docker-compose.yml         Bind-mounted /data, port 8000, restart policy
docker-compose.override.yml  Dev-only: --reload + bind-mounted app/ code
.dockerignore
app/
  main.py              FastAPI app entrypoint
  database.py          SQLite schema, WAL mode, seed data (DATA_DIR env var)
  security.py          PIN hashing, session tokens, lockout backoff, OAuth token encryption
  google_oauth.py      Google OAuth + Calendar REST client (httpx, no SDK)
  templating.py         Shared Jinja2 instance
  routers/
    dashboard.py       Home screen assembly + /health endpoint
    tasks.py           Task list + checkmark toggle
    shopping.py        Shopping list CRUD
    meals.py           Meal plan CRUD
    layout.py          Gridstack position persistence
    admin.py           PIN auth + family/task management
    calendar.py         Google OAuth connect/disconnect + Calendar/Upcoming Events widgets
  templates/           Jinja2 templates (base, dashboard, widgets/, admin/)
  static/
    css/style.css      Design system (fonts, palette, widget styling)
    fonts/             Self-hosted Fraunces/Inter/JetBrains Mono (woff2)
    vendor/            Pinned HTMX 2.0.10, Alpine 3.16.3, Gridstack 13.2.0
data/                  SQLite DB + secret key — local dev only; in Docker this
                       is a bind-mounted folder instead (see docker-compose.yml)
```

`DATA_DIR` controls where the database and secret key live — defaults to
`./data` for bare-metal use, set to `/data` inside the Docker image.

No JS build step anywhere — vendor files are committed as-is and referenced
directly by `<script>` tags, per the maintainability decision in the spec.

## Kiosk browser setup (the part Docker doesn't cover)

The server and display are now separate devices (spec Section 3) — the G10
just needs to be reachable on the LAN; everything below runs on the Galaxy
Tab A9+ instead:

1. **Install Fully Kiosk Browser** on the Galaxy Tab A9+ (Play Store), point
   its Start URL at `http://<g10-lan-address>:8000/`, and enable its
   fullscreen/kiosk mode (no Android chrome, no notification shade access).
2. **Autostart on boot** — Fully Kiosk Browser has a built-in "Start on Boot"
   setting; no separate compositor/window-manager autostart config needed
   (unlike the earlier Raspberry Pi plan, there's no Wayfire/systemd layer
   here — Fully Kiosk Browser owns the whole kiosk lifecycle on Android).
3. **Screen dim/sleep schedule** — use Fully Kiosk Browser's own scheduling
   (Settings → Screen) rather than a cron/systemd timer, matching the admin-
   configurable idle-screen behaviour from spec 4.5 (still to be wired up on
   the app side — see "Next steps").
4. **Cache/memory management** — Fully Kiosk Browser has a built-in
   auto-reload/cache-clear timer (Settings → Other), replacing the old
   Chromium-tmpfs-on-SD-card plan entirely; there's no SD card in this
   architecture.

## Next steps (in rough priority order)

1. **Google Tasks sync** — Calendar is wired up (read-only, see "Connecting Google
   Calendar"); the shopping/task lists are still local-only. Switch them over to
   actually pushing/pulling Google Tasks using the `sync_queue` table that's already
   in the schema (rows are being written, nothing consumes them yet — that's the
   background sync worker to build), reusing the OAuth token plumbing in
   `app/google_oauth.py`.
2. **End-of-day task reset job** — `archive_completed_one_off_tasks()` and
   `reset_recurring_tasks()` are written in `tasks.py` but nothing calls them
   yet; needs a scheduler (APScheduler, per the spec's system architecture).
3. **Weather widget** — Open-Meteo, no auth needed, probably the quickest win.
4. **Notifications banner logic** — the `<div id="notification-banner">` exists
   in the dashboard template but nothing populates it yet; needs the
   due-soon-task and calendar-reminder logic from Section 4.6 of the spec.
5. **On-screen keyboard** — currently relies on whatever virtual keyboard the
   OS provides on focus; the spec calls for an embedded JS keyboard
   (simple-keyboard) for a more reliable kiosk experience.
6. **Google Photos + Classroom** — lower priority, both have the API caveats
   documented in the spec (Picker-only access, Guardian-email-first for
   homework).
7. **Idle screen behaviour** — spec 4.5/4.6 calls for an admin-configurable
   choice between photo slideshow, staying on the calendar/tasks view, or
   dimming the screen; not built yet (no `app_settings` key for it, no
   dashboard-side idle detection).
