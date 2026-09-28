# Family Display

A shared touchscreen dashboard for the household: calendar, per-person task
lists, a shared shopping list, meals, weather, homework and a customisable
widget grid. It uses a split architecture. A **GMKtec G10 (x86, Ubuntu/Debian)
running Docker** is the server, and a **Samsung Galaxy Tab A9+** on the wall
runs Fully Kiosk Browser over Wi-Fi. The backend is Python/FastAPI, the
frontend is HTMX + Gridstack.js with server-rendered Jinja2 templates, and the
data lives in SQLite. Everything runs locally. The only outside services are
Google (Calendar + Tasks) and Open-Meteo (weather).

`family-display-spec.md` is the product source of truth.

## What's working

- **Dashboard**: Gridstack widget grid. Anyone can drag a widget by its
  header or resize it (no PIN), and the layout persists to SQLite. The area
  under the top bar scrolls, so a tall layout still fits the kiosk.
- **Calendar** (Google, read-only): month grid with a tap-through day view,
  a calendar picker in Admin, and a refresh every 3 minutes. It shows a "not
  connected" state until an account is connected, and an offline notice when
  Google can't be reached.
- **Tasks**: one list per family member, ticked with a checkmark animation.
  Finished tasks fold away under a "done" toggle. There are no points or
  rewards, per the spec.
- **Two-way Google Tasks sync** for the shopping list and each person's
  tasks (`app/task_sync.py`). Pick the lists in Admin → Google Account. Local
  changes are queued and pushed every 60s, then each list is reconciled with
  last-write-wins. Changes made on a phone show up within a minute, and
  changes made while offline stay queued until Google is reachable.
- **Admin task schedules**: tasks are added, renamed and deleted in Google
  Tasks. Admin → Task Schedules only sets which days a recurring chore
  repeats, using weekday chips (Mon–Sun). A chore appears only on its days.
- **Daily reset**: once per family-local day (the calendar's timezone, not
  the container's UTC), recurring tasks untick and finished one-offs are
  archived (`tasks.run_daily_reset_if_due`, checked by the scheduler every
  5 minutes).
- **Shopping list**: single shared list with add, tick and delete, synced
  as above.
- **Meals**: a plain-text plan for the next 7 days, edited inline on the
  display.
- **Weather** ([Open-Meteo](https://open-meteo.com), no API key): current
  conditions plus a 4-day forecast. Set the location in Admin → Weather
  location. The last good forecast is cached, so an outage shows a slightly
  stale reading rather than nothing.
- **Homework + practice words** (phase 1, entered by hand in Admin): kids
  tick homework done and mark a word list "practised today" on the display.
  Practice words render in a handwriting font (style chosen in Admin). Not
  synced anywhere.
- **On-screen keyboard** (simple-keyboard), off by default. Turn it on in
  Admin → On-screen Keyboard. While it is on, text inputs get
  `inputmode="none"` so Android's own keyboard stays hidden.
- **Calm day/night appearance**: light rounded cards by day and a low-glare
  night palette from 19:00 to 07:00 in the family's timezone. The open kiosk
  page switches without a reload. Admin → Appearance can pin it to light or
  dark. Fonts (Figtree, Playwrite) and icons (Lucide, Meteocons) are
  self-hosted, so no CDN is needed.
- **Admin**: PIN-protected (default **1234**, so change it straight away
  under Admin → Change Admin PIN). Failed attempts back off exponentially.
  Admin manages family members, task schedules, homework, practice words,
  Google account + list links, weather location, appearance and the
  keyboard.

**Photos** is a placeholder card ("coming soon"). See "Next steps".

## Running with Docker (recommended)

You need Docker Desktop (Windows/Mac, for local dev) or Docker Engine (Linux,
which is what the G10 runs).

```bash
cp .env.example .env      # optional: only needed for Google (see below)
docker compose up --build
```

Open `http://localhost:8000/`. The image is built on Python 3.14. CI runs
`docker build`, the linter and the tests on every PR.

The database and secret key live in a **bind-mounted host folder**,
`./data/family-display`, per spec 9.7. They are plain files you can browse,
back up or `rsync`. SQLite's WAL mode is safe on a bind mount on the G10's
native Linux. With Docker Desktop on Windows/Mac, "database is locked" errors
against the bind-mounted DB are a known Docker Desktop file-locking caveat
that affects dev only.

`docker compose up` automatically layers in `docker-compose.override.yml`,
which adds `--reload` and bind-mounts `app/` for live code edits. Rebuild
whenever `requirements.txt` changes. For a clean run without hot-reload, use
`docker compose -f docker-compose.yml up --build`.

Run **a single uvicorn process** (no `--workers`): the Google sync and daily
reset run on an in-process scheduler.

### Deploying to the G10

1. Install Docker on Ubuntu/Debian: `curl -fsSL https://get.docker.com | sh`
2. Copy this project to the G10 (or `git clone` it), and create `.env` from
   `.env.example`.
3. `docker compose -f docker-compose.yml up -d --build` (production mode, no hot-reload)
4. Point the Galaxy Tab A9+'s Fully Kiosk Browser at `http://<g10-lan-address>:8000/`.
   See "Kiosk browser setup" below.
5. The spec (9.7) also puts Home Assistant and a Caddy reverse proxy on the
   G10. That is a separate deployment layer, not something this repo's
   `docker-compose.yml` owns. Behind a reverse proxy, pass the `Host` header
   through: the app refuses non-GET requests whose `Origin` doesn't match its
   host.

## Local development (without Docker)

```bash
python3.14 -m venv venv     # match the Docker image and CI
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

ruff check app tests
python -m pytest -q
```

Open `http://localhost:8000/`. The SQLite database is created automatically
at `data/family_display.db` on first run. It is seeded with 4 placeholder
family members (Mum, Dad, Riley, Jamie), which you can rename or replace in
Admin. `DATA_DIR` controls where the database and secret key live. It
defaults to `./data` and is set to `/data` inside the Docker image. `.env` is
loaded automatically via `python-dotenv`.

The tests use a fresh temporary database each and mock every Google call
with `respx`.

## Connecting Google (Calendar + Tasks)

The app calls Google's OAuth, Calendar and Tasks REST endpoints directly
with `httpx`, without a Google SDK (`app/google_oauth.py`, `app/google_calendar.py`,
`app/google_tasks.py`). The scopes are `calendar.readonly` and `tasks`.
Calendar is read-only; the Tasks scope drives the shopping and task sync.

**1. Create a Google Cloud project and OAuth credentials** (one-time, in a browser):

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and
   create a project (or reuse one).
2. **APIs & Services → Library**: enable the **Google Calendar API** and the
   **Google Tasks API**.
3. **APIs & Services → OAuth consent screen**: choose User type
   **External** and add your own Google account under **Test users**.
   **Warning:** while the publishing status is **Testing**, Google expires
   refresh tokens after 7 days, so the connection silently drops every week.
   For an always-on display, switch it to **In production**. An unverified
   app is fine for personal use; you'll see a "Google hasn't verified this
   app" screen when connecting.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   choose Application type **Web application**. Under **Authorized redirect
   URIs**, add both `http://localhost:8000/admin/google/callback` and
   `http://127.0.0.1:8000/admin/google/callback`. Google matches the string
   exactly, so whichever host you type in the browser must be registered.
   **For the G10:** Google rejects raw IP addresses (e.g. `192.168.x.x`) and
   requires HTTPS for anything that isn't localhost, so the callback can't
   point at the G10's LAN IP. You have two options:
   - Put the app behind Caddy with a real hostname + HTTPS (spec 9.7) and
     register that URL.
   - Do the one-time Connect step through an SSH tunnel
     (`ssh -L 8000:localhost:8000 <g10>`) so your browser reaches the app as
     `localhost`.
5. Copy the **Client ID** and **Client Secret**.

**2. Configure the app:** copy `.env.example` to `.env` and fill in
`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. `.env` is gitignored, so never
commit real credentials. Then restart the app.

**3. Connect the account:** go to Admin → Google Account → **Connect Google
Account**, sign in and approve. Then pick which calendars to show, the
shopping list's Google Tasks list, and each family member's Tasks list.
Tokens are encrypted at rest in the `auth_tokens` table, keyed off the local
secret file in `DATA_DIR` that also signs admin sessions. **Disconnect** in
the same panel revokes and clears them. If the scopes ever change,
disconnect and reconnect.

## Project structure

```
Dockerfile                   Python 3.14 image, non-root user, /health check
docker-compose.yml           Bind-mounted /data, port 8000, restart policy, .env keys
docker-compose.override.yml  Dev-only: --reload + bind-mounted app/
.env.example                 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
.github/workflows/ci.yml     ruff + pytest + docker build
pyproject.toml, pytest.ini   ruff and pytest config
requirements.txt             Runtime deps (requirements-dev.txt adds test/lint tools)
family-display-spec.md       Product spec (source of truth)
app/
  main.py            FastAPI entrypoint: lifespan (DB init + scheduler), same-origin check, static files
  database.py        SQLite schema + idempotent migrations, WAL, seed data, settings, family_today()
  security.py        PIN hashing, session cookies, lockout backoff, OAuth token encryption
  appearance.py      Day/night palettes and /api/appearance
  templating.py      Shared Jinja2 instance + template context helpers
  recurrence.py      Weekday rules ("Mon,Wed,Fri") for recurring tasks
  http_client.py     Shared httpx client factory + timeout, log-safe error summaries
  google_oauth.py    Google OAuth, token storage/refresh, list pager, calendar-picker settings (httpx, no SDK)
  google_calendar.py Calendar event fetch, month grid bar packing, day view
  google_tasks.py    Google Tasks API client
  task_sync.py       Two-way Tasks sync: push sync_queue, then reconcile each list
  scheduler.py       APScheduler: sync every 60s, daily-reset check every 5 min
  routers/
    dashboard.py     Home screen assembly + /health
    layout.py        Gridstack position persistence (/api/layout)
    calendar.py      Google connect/callback/disconnect, calendar picker, month grid + day view
    tasks.py         Tasks widget, tick toggle, daily reset
    shopping.py      Shopping list add/tick/delete
    meals.py         7-day meal plan
    weather.py       Open-Meteo geocoding, forecast + cache, Weather widget
    homework.py      Homework and practice-words widgets and their kiosk taps
    admin.py         PIN login + all Admin settings
  templates/         base, dashboard, _icons, _keyboard_assets, widgets/, admin/
  static/
    css/             style.css (design system), homework.css, keyboard.css
    js/keyboard.js   On-screen keyboard behaviour (loaded only when enabled)
    fonts/           Self-hosted Figtree (UI) + Playwrite (handwriting), OFL
    icons/           Lucide sprite (ISC) + Meteocons weather icons (MIT)
    vendor/          Pinned HTMX 2.0.10, Gridstack 13.2.0, simple-keyboard 3.8.192
tests/               pytest suite (temp DB per test, Google mocked with respx)
data/                SQLite DB + secret key (local dev; bind-mounted in Docker), gitignored
```

There is no JS build step. Vendor files are committed as-is and referenced
directly by `<script>` tags, per the maintainability decision in the spec.

## Kiosk browser setup (the part Docker doesn't cover)

The server and display are separate devices (spec Section 3). The G10 just
needs to be reachable on the LAN; everything below runs on the Galaxy Tab
A9+:

1. **Install Fully Kiosk Browser** on the tablet (Play Store), point its
   Start URL at `http://<g10-lan-address>:8000/`, and enable its
   fullscreen/kiosk mode (no Android chrome, no notification shade access).
2. **Autostart on boot**: use Fully Kiosk Browser's built-in "Start on
   Boot" setting.
3. **Screen dim/sleep schedule**: use Fully Kiosk Browser's own scheduling
   (Settings → Screen). The app itself has no idle-screen behaviour yet.
4. **Cache/memory management**: use Fully Kiosk Browser's built-in
   auto-reload/cache-clear timer (Settings → Other).

## Next steps

1. **Sync health**: show in Admin (and subtly on the dashboard) when the
   last successful Google sync happened and whether changes are stuck in
   the queue.
2. **Backups**: scheduled copies of the SQLite database (and the secret key)
   out of `DATA_DIR`.
3. **PIN hardening**: stop shipping a default PIN that works forever, e.g.
   force a change on first login.
4. **Gmail/Classroom homework import**: replace manual homework entry by
   parsing the weekly Classroom guardian email (spec: Guardian-email-first).
5. **Photos**: Google Photos Picker plus a local image cache, replacing the
   "coming soon" card.
