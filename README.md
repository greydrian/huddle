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
- **Admin**: PIN-protected. The default **1234** only gets you as far as a
  "Choose a new PIN" screen. Failed attempts back off exponentially, up to
  15 minutes after 10 in a row. Logging out or changing the PIN signs out
  every device. Forgot the PIN? Run
  `docker compose exec family-display python -m app.reset_pin`. It puts the
  PIN back to 1234 (and asks for a new one at the next login), clears any
  lockout and signs everyone out.
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
but don't back up the live `.db` file by copying it: use the nightly backups
instead (see "Backups and restore" below). SQLite's WAL mode is safe on a bind mount on the G10's
native Linux. With Docker Desktop on Windows/Mac, "database is locked" errors
against the bind-mounted DB are a known Docker Desktop file-locking caveat
that affects dev only.

`docker compose up` automatically layers in `docker-compose.override.yml`,
which adds `--reload` and bind-mounts `app/` for live code edits. Rebuild
whenever the requirements change. For a clean run without hot-reload, use
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

## Backups and restore

The app backs up its database every night at about 03:30 in the family's
timezone (the connected calendar's; UTC if none). If the G10 was off at
03:30, the backup runs as soon as it's back up: the app checks every 10
minutes (and at startup) whether a backup exists since the most recent 03:30.
DST changes still give exactly one backup a night. Where clocks skip 03:30,
it runs at 04:30.
Admin → Backups shows the latest one and has a "Back up now" button.

- **Where**: `data/family-display/backups/` on the host (`/data/backups/` in
  the container), named `huddle-YYYYMMDD-HHMMSS.db` in family-local time.
  The time in the name decides which backup is newest, not the file's
  modification time, so copying old backups back in doesn't confuse retention.
- **How**: SQLite `VACUUM INTO`, which takes a consistent snapshot of the live
  database, including changes still in the WAL, without stopping the app. Each
  file is written under a temporary name, checked with `PRAGMA
  integrity_check`, and only then renamed into place. A copy that fails the
  check is deleted and logged as a WARNING. Don't `cp`/`rsync` the live
  `family_display.db`: without its `-wal` file the copy can be missing
  recent changes or be inconsistent.
- **Kept**: the 14 newest backups, plus the newest backup of each of the last
  8 weeks.
- **The secret key**: Google tokens in the database are encrypted with
  `data/family-display/.secret_key`, so a copy of that key is kept in the
  backups folder as `.secret_key` (owner-only permissions). If the key ever
  changes, the old copy is kept as `.secret_key.replaced-<time>`, since the
  older backups need it. `.env` is never copied.

**Security trade-off:** the backups folder holds the database *and* the key
that decrypts its Google tokens, so it is as sensitive as `data/` itself.
Anyone with a copy can use the family's Google Calendar/Tasks access until you
disconnect Google in Admin or revoke access at
https://myaccount.google.com/permissions. Keep off-box copies somewhere
private. Without the key, a backup still restores everything except the
Google connection (and signs you out of Admin), so you'd just reconnect Google.

The backups folder is inside `data/`, which is already in `.gitignore` and
`.dockerignore`, so backups are never committed or baked into the image.

### Restoring a backup

On the G10, from the project folder:

```bash
# 1. Stop the app so nothing writes to the database.
docker compose -f docker-compose.yml stop family-display

# 2. Put the chosen backup in place of the live database. (sudo: backups/ is
#    0700 and its files 0600, owned by the container's uid 1000.)
cd data/family-display
sudo cp family_display.db family_display.db.before-restore    # optional safety copy
sudo cp backups/huddle-YYYYMMDD-HHMMSS.db family_display.db

# 3. Remove the old database's WAL/shared-memory files. They belong to the
#    database you just replaced, and SQLite would try to apply them to the backup.
sudo rm -f family_display.db-wal family_display.db-shm

# 4. Put back the secret key the backup needs (see "Which key" below). For
#    the usual case, a backup newer than every .secret_key.replaced-* file:
sudo cmp .secret_key backups/.secret_key || sudo cp backups/.secret_key .secret_key

# 5. The container runs as uid 1000; keep the files owned by it.
sudo chown 1000:1000 family_display.db .secret_key
sudo chmod 600 .secret_key
cd ../..

# 6. Start the app again.
docker compose -f docker-compose.yml start family-display
```

**Which key**: a backup stamped B needs the key that was current at B. That
is the earliest `.secret_key.replaced-T` whose T is later than B. If there is
no such file, it needs the current `backups/.secret_key`. Both stamps are
family-local `YYYYMMDD-HHMMSS`, so compare them as text. For example, with
`.secret_key.replaced-20261103-033000` present, `huddle-20261101-033000.db`
needs that file (`sudo cp backups/.secret_key.replaced-20261103-033000
.secret_key`), and `huddle-20261104-033000.db` needs `backups/.secret_key`. With
the wrong key the restore still works, but Google shows as disconnected and
you'll need to reconnect it in Admin.

### If the app won't start after an update (a failed migration)

Updates to the database's structure are numbered migrations (`app/migrations.py`)
that run when the app starts. Before deploying an update, take a snapshot:
press Admin → Backups → "Back up now".

If a migration fails, the app does the following:
- It rolls that migration back completely, so the database is left as it was.
- It logs the error and exits.
- Docker (`restart: unless-stopped`) starts it again, and it fails the same way.

So the container keeps restarting, the display and `/health` don't respond, and
nothing is changed on each attempt. To confirm, run:

```bash
docker compose -f docker-compose.yml logs --tail 100 family-display | grep -B2 -A20 MigrationError
```

To recover:
1. Go back to the previous version of the code: `git checkout <previous commit>`, then
   `docker compose -f docker-compose.yml up -d --build`. The database needs no restore: a
   failed migration changes nothing. The older code runs happily on it.
2. Only if the database itself looks damaged, restore the pre-deploy snapshot with the steps in
   "Restoring a backup" above.

Restoring alone doesn't help: the new code would run the same failing migration again.

### Keep a copy off the box

The backups are on the same disk as the database, so if that disk fails you
lose both. Copy the backups folder somewhere else regularly. For example, a
nightly host cron job on the G10 (after the 03:30 backup) that syncs it to a
NAS share or a USB drive:

```bash
# crontab -e on the G10 (as root, or a user who can read data/family-display)
0 5 * * * rsync -a /path/to/huddle/data/family-display/backups/ /mnt/nas/huddle-backups/
```

Copying the finished `huddle-*.db` backups this way is safe, unlike the live
database. They're complete files that the app never changes. The destination
now holds the secret key too, so keep it private (see the trade-off above).

## Local development (without Docker)

```bash
python3.14 -m venv venv     # match the Docker image and CI
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install --require-hashes -r requirements-dev.txt   # includes requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

ruff check app tests
mypy                          # type check app/ ([tool.mypy] in pyproject.toml)
python -m pytest -q           # unit tests; skips the browser tests

python -m playwright install chromium   # once
python -m pytest -m e2e       # browser smoke tests (tests/e2e)
```

Open `http://localhost:8000/`. The SQLite database is created automatically
at `data/family_display.db` on first run. It is seeded with 4 placeholder
family members (Mum, Dad, Riley, Jamie), which you can rename or replace in
Admin. `DATA_DIR` controls where the database and secret key live. It
defaults to `./data` and is set to `/data` inside the Docker image. `.env` is
loaded automatically via `python-dotenv`.

The tests use a fresh temporary database each and mock every Google call
with `respx`.

The browser smoke tests (`tests/e2e`, marked `e2e`) start their own uvicorn
on a free port with a throwaway `DATA_DIR`, seed it straight into SQLite and
drive it with headless Chromium at 1280x800: every widget renders, a header
drag moves a widget while a touch swipe on a card scrolls, a ticked task
folds into "✓ N done", the on-screen keyboard opens, and Admin logs in and
scrolls. Nothing external is called. On failure a screenshot lands in
`test-results/e2e/` (a CI artifact).

Type checking is gradual: `mypy` runs at its default (lenient) level over
`app/` only, which is clean today, so CI fails on any new error. Widen it
in `pyproject.toml` a step at a time (add `tests`, then `check_untyped_defs`).
mypy rather than pyright because it is pure Python: pyright needs Node, and
its pip package's typeshed paths overflow Windows' 260-character limit
inside this OneDrive folder.

### Dependencies

`requirements.in` (runtime) and `requirements-dev.in` (tests and tools) are
the files you edit. `requirements.txt` and `requirements-dev.txt` are
generated from them by [uv](https://docs.astral.sh/uv/): every package,
including transitive ones, is pinned with its hashes, and the Docker image
and CI install with `pip install --require-hashes`, so a tampered or
substituted download fails the build.

The lock files are compiled **universally** (`--universal`): one file carries
environment markers and the hashes of every platform's wheels, so the same
file installs on the Linux image and CI and in a Windows dev venv. (Compiling
for Linux only would leave Windows without hashes for its own wheels, and
pip-tools can only resolve for the machine it runs on.)

To add or upgrade a dependency:

```bash
pip install uv
# 1. Edit requirements.in (or requirements-dev.in), e.g. bump a pin.
# 2. Recompile both files (uv keeps every other pin as it is):
uv pip compile requirements.in --universal --python-version 3.14 --generate-hashes -o requirements.txt
uv pip compile requirements-dev.in --universal --python-version 3.14 --generate-hashes -o requirements-dev.txt
#    To move a package that isn't pinned in a .in file: add -P <package>
#    (or --upgrade for everything).
# 3. Install, test, and commit the .in and .txt files together.
pip install --require-hashes -r requirements-dev.txt
```

CI's `lock` job recompiles and fails if the `.txt` files don't match their
`.in` files. Rebuild the Docker image after a change.

Dependabot (`.github/dependabot.yml`) opens update PRs: Python weekly
(minor and patch bumps grouped into one PR), GitHub Actions and the Docker
base image monthly. It uses the `uv` ecosystem, which re-runs `uv pip compile`
with the options in the file header; the `pip` ecosystem only knows pip-tools,
which would lose the universal hashes. The base image is pinned by digest in
the `Dockerfile` (`python:3.14-slim@sha256:...`), and Dependabot moves the
digest; it is told to stay on 3.14.

## Connecting Google (Calendar + Tasks)

The app calls Google's OAuth, Calendar, Tasks and Gmail REST endpoints
directly with `httpx`, without a Google SDK (`app/google_oauth.py`,
`app/google_calendar.py`, `app/google_tasks.py`, `app/google_gmail.py`). The
scopes are `calendar.readonly` (the month grid), `tasks` (shopping and task
sync), and for the school email import `gmail.readonly` and
`calendar.events` (see "School email import" below).

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

## School email import

**Setting it up?** Follow the step-by-step checklist in
[docs/school-import-setup.md](docs/school-import-setup.md).

Once a day (18:00 family time by default) the display reads the school's
emails from the connected Gmail account and puts what Claude finds in them
(spellings, homework, events) in Admin's **School inbox** to approve. Only
emails from the listed school senders are read, and only those are sent to
the Anthropic API. `sen@gresham.croydon.sch.uk` (private SENDCo
conversations) is never fetched, whatever the lists say, and an email that
forwards, quotes or mentions it is dropped whole. Quoted reply history is
stripped before anything is sent, and emails whose sender fails Gmail's
DMARC/SPF/DKIM checks are dropped (ones with no verdict are marked "Sender
not verified"). Approved events go into a Google calendar you pick. Code:
`app/school_email.py`, `app/google_gmail.py`, `app/services/school_events.py`.

Cost caps: at most 25 emails are sent to Claude per check (oldest first; the
rest follow 15 minutes later), and at most 60 documents a day across the
whole School inbox, uploads included. An email Claude can't read is retried
at the next checks and given up after 3 tries; Admin offers Retry.

Setup, once:

1. **Make the OAuth app Internal.** In the Cloud console, **APIs & Services
   → OAuth consent screen**, set User type to **Internal** (needs a Google
   Workspace account). `gmail.readonly` is a restricted scope: an Internal
   app needs no Google verification for it, and Internal apps don't have
   the 7-day refresh-token expiry of "Testing".
2. **Enable the Gmail API** in the same project (**APIs & Services →
   Library → Gmail API → Enable**). Without it Admin says "The Gmail API
   isn't enabled".
3. **Add `ANTHROPIC_API_KEY`** to `.env` (a key from console.anthropic.com),
   then `docker compose up -d`. Optional: `ANTHROPIC_MODEL`.
4. **Reconnect Google in Admin**: Google Account → **Disconnect**, then
   **Connect Google Account**, and approve the new Gmail and calendar
   permissions. Until then Calendar and Tasks keep working as before, and
   the School email panel says "Reconnect Google to enable school email
   import".
5. In Admin → **School email**, check the senders (defaults:
   `office@greshamprimary.school` and `*@gresham.croydon.sch.uk`), pick
   **School events go to calendar**, and press **Check now** once. The
   first check looks back 14 days. After that it runs on the schedule set
   there: daily at a time you choose, twice daily, weekly on Friday, or off.

Weekly spellings posted in Google Classroom aren't emailed: add those with
Admin → **Add from Classroom** (a screenshot or pasted text).

## Project structure

```
Dockerfile                   Python 3.14 image, non-root user, /health check
docker-compose.yml           Bind-mounted /data, port 8000, restart policy, .env keys
docker-compose.override.yml  Dev-only: --reload + bind-mounted app/
.env.example                 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / ANTHROPIC_API_KEY
.github/workflows/ci.yml     ruff + pytest, lock check, mypy, Playwright e2e, docker build
.github/dependabot.yml       Weekly Python, monthly Actions + base-image update PRs
pyproject.toml, pytest.ini   ruff, mypy and pytest config
requirements.in              Runtime deps you edit (requirements-dev.in adds test/lint tools)
requirements.txt             Generated: pinned + hashed (and requirements-dev.txt)
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
  google_gmail.py    Gmail reads: search, headers, body (HTML to text), attachments
  school_email.py    School email import: sender lists, schedule, checkpoint, feeds the School inbox
  scheduler.py       APScheduler: sync every 60s, daily-reset check every 5 min, nightly backup, school email
  backup.py          Nightly SQLite backups (VACUUM INTO + integrity check), retention, key copy
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
  e2e/               Playwright browser smoke tests (`pytest -m e2e`)
data/                SQLite DB + secret key + backups/ (local dev; bind-mounted in Docker), gitignored
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
3. **PIN hardening**: stop shipping a default PIN that works forever, e.g.
   force a change on first login.
5. **Photos**: Google Photos Picker plus a local image cache, replacing the
   "coming soon" card.
