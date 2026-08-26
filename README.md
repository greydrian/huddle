# Family Display

A shared touchscreen dashboard for the household — calendar, per-person task
lists with points/rewards, a shared shopping list, meal planning, and a
customisable widget grid. Built per the project spec: Raspberry Pi 5,
Python/FastAPI backend, HTMX + Alpine.js + Gridstack.js frontend, SQLite,
fully local (no cloud component).

## What's working right now

- **Dashboard**: Gridstack widget grid, drag/resize by anyone, layout persists to SQLite
- **Task lists**: per-family-member, tick to complete with a checkmark animation, points
  awarded automatically, admin can add/remove tasks and set point values
- **Shopping list**: single shared list, add/tick/delete
- **Meal planning**: simple text-based plan for the next 7 days, inline editing
- **Admin**: PIN-protected (default PIN is **1234** — change it immediately under
  Admin → Change Admin PIN), with exponential backoff on failed attempts,
  family member management, task management, reward management
- **Design system**: self-hosted fonts (Fraunces/Inter/JetBrains Mono, no
  external font CDN — this is fully offline-capable), warm "kitchen
  noticeboard" visual language with per-person colour tabs on each widget

## What's stubbed (next build phase)

These show a placeholder card on the dashboard rather than live data:

- **Calendar** and **Upcoming Events** — needs Google Calendar OAuth
- **Weather** — needs the Open-Meteo integration (no API key required, just not wired up)
- **Photos** — needs Google Photos Picker OAuth + the local image-cache layer
- **Homework** — needs Gmail API access to parse the weekly Classroom guardian email

None of these need architecture decisions — they were already made in the spec.
They're just not built yet. See "Next steps" below.

## Running with Docker (recommended)

Requires Docker Desktop (Windows/Mac) or Docker Engine (Linux/Pi).

```bash
docker compose up --build
```

Open `http://localhost:8000/`. This builds on **Python 3.14** (the current
stable series as of 2026 — confirmed via PyPI that every dependency here,
including uvicorn's uvloop/httptools, publishes ARM64 wheels for it, so the
same image runs unmodified on the Pi 5 later with no separate build).

The database lives in a **named Docker volume**, not a bind mount — this
matters if you're on Windows or Mac. Docker Desktop's file-sharing layer
(gRPC-FUSE/virtiofs) doesn't reliably support the file locking that SQLite's
WAL mode depends on, and bind-mounting the database file there risks
"database is locked" errors or silent corruption under concurrent writes. A
named volume is a real Linux filesystem inside Docker's own VM, so it behaves
the same way it will later on the actual Pi. Don't change this to a bind
mount for `/data` unless you understand that trade-off.

Running `docker compose up` locally automatically layers in
`docker-compose.override.yml`, which adds `--reload` and bind-mounts the
`app/` folder (bind-mounting *code* is fine on Windows/Mac — the WAL caveat
above is specifically about the database file). For a clean run without
hot-reload, use `docker compose -f docker-compose.yml up --build`.

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

### Deploying to the Raspberry Pi with Docker

1. Install Docker on Raspberry Pi OS: `curl -fsSL https://get.docker.com | sh`
2. Copy this project to the Pi (or `git clone` it)
3. `docker compose -f docker-compose.yml up -d --build` — production mode, no hot-reload
4. Point the kiosk browser at `http://localhost:8000/`, same as before — Chromium-in-kiosk-mode and the Wayfire autostart setup from the original plan are unaffected by containerizing the backend, since those are OS/browser-level concerns outside the container
5. The nightly maintenance timer and Chromium tmpfs caching from the spec still apply — those aren't part of this container

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

## Project structure

```
Dockerfile                Python 3.14 image, non-root user, /health check
docker-compose.yml         Named volume for /data, port 8000, restart policy
docker-compose.override.yml  Dev-only: --reload + bind-mounted app/ code
.dockerignore
app/
  main.py              FastAPI app entrypoint
  database.py          SQLite schema, WAL mode, seed data (DATA_DIR env var)
  security.py          PIN hashing, session tokens, lockout backoff
  templating.py         Shared Jinja2 instance
  routers/
    dashboard.py       Home screen assembly + /health endpoint
    tasks.py           Task list + checkmark toggle + points
    shopping.py        Shopping list CRUD
    meals.py           Meal plan CRUD
    layout.py          Gridstack position persistence
    admin.py           PIN auth + family/task/reward management
  templates/           Jinja2 templates (base, dashboard, widgets/, admin/)
  static/
    css/style.css      Design system (fonts, palette, widget styling)
    fonts/             Self-hosted Fraunces/Inter/JetBrains Mono (woff2)
    vendor/            Pinned HTMX 2.0.10, Alpine 3.16.3, Gridstack 13.2.0
data/                  SQLite DB + secret key — local dev only; in Docker this
                       is a named volume instead (see docker-compose.yml)
```

`DATA_DIR` controls where the database and secret key live — defaults to
`./data` for bare-metal use, set to `/data` inside the Docker image.

No JS build step anywhere — vendor files are committed as-is and referenced
directly by `<script>` tags, per the maintainability decision in the spec.

## Kiosk browser setup (the part Docker doesn't cover)

Containerizing the backend only replaces "how the FastAPI app runs" — the
kiosk browser itself is still an OS-level concern, unchanged from the
original plan:

1. **Boot straight into a kiosk browser** pointed at `http://localhost:8000/`:
   Raspberry Pi OS Bookworm's default compositor is Wayfire (Wayland). Autostart
   Chromium with `--kiosk --noerrdialogs --disable-infobars` against that URL.
2. **Nightly maintenance timer** (systemd timer, ~3am): clear Chromium's cache,
   restart the Chromium process to avoid long-session memory creep — not yet
   scripted, flagged in the spec as a reliability item.
3. **tmpfs for Chromium's cache dir**, to keep frequent small writes off the SD
   card — an `/etc/fstab` line, not application code.

## Next steps (in rough priority order)

1. **Google OAuth for Calendar + Tasks** — standard in-browser consent flow
   (not the QR/device-flow approach — see spec for why), wire up the real
   Calendar and Upcoming Events widgets, and switch the shopping/task lists
   over from local-only to actually pushing/pulling Google Tasks using the
   `sync_queue` table that's already in the schema (rows are being written,
   nothing consumes them yet — that's the background sync worker to build).
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
7. **Rewards redemption UI** — rewards can be created/deleted in Admin, but
   there's no "redeem points against a reward" flow on the dashboard yet.
