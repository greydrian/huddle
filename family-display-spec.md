# Family Management Display ("Huddle"): Specification

**Status:** v1.0, as built, 28 September 2026. Sections 1–9 describe the app as it stands and the
decisions behind it. **Section 10 is the v1.1 plan** (next iteration) and is still open for
decisions.
**Target hardware:** GMKtec G10 (Ryzen 5 3500U) as the local Docker server, and a Samsung Galaxy
Tab A9+ wall-mounted in the kitchen as the display, connected over Wi-Fi.

This document is the product source of truth. `CLAUDE.md` covers how the code is organised, and
`README.md` covers running, deploying and restoring it. Check this document before adding or
changing a feature.

---

## 1. Purpose

A shared, always-on touchscreen display for the household. It shows the family calendar,
per-person daily chores, a shared shopping list, the week's meals, the weather, and the children's
homework and handwriting practice. Everything syncs with the family's Google account, and settings
sit behind parent-only (PIN) controls.

## 2. Reference products considered

Cozyla, Dragon Touch, Skylight and DAKboard-style displays were reviewed. The customisable
dashboard, meal planning, photo screensaver and weather ideas come from that comparison. The
**chore points/rewards idea was rejected**: tasks are purely functional. A September 2026 review
compared building our own with Home Assistant dashboards, MagicMirror², commercial displays, a
React SPA and Django. Building our own is still justified: none of those does per-child chores
with a daily reset, handwriting practice, or an approval inbox for school homework.

## 3. Hardware & platform (decided: split architecture)

| Item | Decision |
|---|---|
| Server | **GMKtec G10** (Ryzen 5 3500U, 16 GB) running the app and Home Assistant in Docker. Can live anywhere on the LAN. |
| Display | **Samsung Galaxy Tab A9+** (11", about 1280×800 CSS px landscape), wall-mounted in the kitchen, Wi-Fi only. |
| Display software | **Fully Kiosk Browser**: fullscreen, autostart, pointed at the dashboard URL. Its scheduled dim/sleep and remote cache-clear are relied on. |
| App type | **Browser-based web app, not a native Android app.** One codebase; no extra UX benefit from native. |
| Network | Home Wi-Fi; the server and tablet share the LAN. |

---

## 4. Features (v1.0, built)

### 4.1 Dashboard
- A Gridstack grid of widgets. **Anyone can move and resize widgets**, with no PIN. Drag only by
  the widget's header (`.drag-handle`), so swiping a card scrolls instead of dragging. There are
  no buttons inside a drag handle, because Gridstack swallows touch taps on them.
- The grid area scrolls; the top bar stays fixed. The top bar shows:
  - a live date that rolls over at midnight in the family's timezone
  - the sync status dot
  - the Admin button
- **Widgets refresh themselves.** Every 30 s the tablet asks `/api/rev` which widgets changed and
  re-fetches only those. It never swaps a widget while someone is using it: typing, keyboard open,
  a "done" fold recently opened, a finger down, a drag in progress, or a tick animating.
- Widgets:

| Widget | What it does |
|---|---|
| Calendar | Google Calendar month grid with multi-day bars, plus a day view. **Read-only** except for approved school events (4.8). Kept in a local cache, so during an internet outage it shows the last copy with "Last updated …" instead of an empty grid. |
| Tasks | Per-person chores, ticked by touch with a bouncy tick. Finished tasks fold under "✓ N done", so a mis-tap can be undone. Recurring tasks can repeat on chosen days. A daily reset runs in the family's timezone. |
| Shopping | One shared list with add, tick and remove. |
| Meals | The week's meal plan, editable inline. |
| Weather | Open-Meteo for the location set in Admin, with illustrated icons. It shows a cached forecast (up to 24 h old) when offline. |
| Homework | Per-child homework with due, due-today, overdue and done states. |
| Practice words | Each child's current handwriting word list in Playwrite GB (semi-joined or joined), with a daily "Practised today" toggle. Deliberately no points. |
| Photos | **Placeholder ("coming soon"); see 10.2.** |

### 4.2 Calendar sync
- Google Calendar (Workspace account). The calendars to show are chosen in Admin, each in its own
  colour.
- Event times keep Google's own offsets, and "today" always means the family's timezone, never
  the container's UTC.

### 4.3 Family members and tasks
- 3–4 family members, each with a name, a colour (used for their name pill; ink contrast is chosen
  automatically), and optionally a **school year group** (used by the school import).
- **Tasks are created, renamed and deleted in Google Tasks** (one list per person). They sync in
  within a minute. Admin only sets **which days a task repeats**, which Google Tasks can't store,
  and can move a task to another person.
- Two-way sync with conflict handling:
  - last write wins
  - reassigning a task leaves a tombstone, so it never appears twice
  - an outage pauses sync without losing queued changes

### 4.4 Shopping list
- A single shared list with two-way Google Tasks sync.

### 4.5 Appearance
- The "Calm modern" design. **Night mode switches on automatically from 19:00 to 07:00** in the
  family's timezone, with no reload needed. Admin → Appearance can set Auto, Always light or
  Always dark.
- All text meets WCAG AA contrast in both modes. Minimum text size is 1 rem, and 0.8 rem for
  secondary text.
- Fonts (Figtree, Playwrite GB) and icons (Lucide, Meteocons) are bundled with the app. Nothing
  is fetched at runtime.

### 4.6 On-screen keyboard
- An optional in-page keyboard (simple-keyboard), including a PIN pad. It exists because the
  Android keyboard is unreliable in Fully Kiosk. The Android keyboard is suppressed only once
  ours has actually started, so the kiosk can never be left without a keyboard.

### 4.7 Admin (behind a PIN)
Admin covers:
- family members and year groups
- task schedules
- homework and practice words
- the school inbox and school email
- the Google account (calendars, Tasks lists)
- weather location, appearance, on-screen keyboard
- backups, sync status
- changing the PIN

PIN rules and protection:
- A single shared PIN of 4–8 digits. It can't be one repeated digit or a straight run of digits.
- **A PIN change is forced while the PIN is still the default 1234.**
- Backoff after failed attempts, then a 15-minute lockout after 10 failures in a row. Failures
  older than 24 h are forgotten.
- Logout and PIN changes end every session.
- A forgotten PIN is reset with `docker compose exec family-display python -m app.reset_pin`.

### 4.8 School homework import
- **Add from Classroom:** the parent screenshots or copies a Google Classroom post, such as the
  weekly spellings, and uploads or pastes it in Admin.
- **Daily school email check:** by default at 18:00 (also twice daily, weekly, or off). It reads
  only mail from the school senders on the Admin allowlist:
  - `office@greshamprimary.school`: the weekly newsletter PDF, Half Term on a Page, letters
  - `*@gresham.croydon.sch.uk`: class teachers
- **Claude extracts** spelling lists, homework and key dates into the **School inbox**. Nothing
  reaches the wall or the calendar until a parent approves it. Approved dates are added to a
  chosen Google calendar.
- Privacy and safety:
  - `sen@gresham.croydon.sch.uk` (private SENDCo conversations) is **never fetched**, and any
    email that mentions it is dropped whole.
  - Quoted reply history is stripped.
  - Senders that fail DMARC/SPF/DKIM are skipped.
  - Documents are treated as untrusted: the model can only suggest inbox items.
  - No email content is logged.
  - Caps: 25 emails per check, and 60 Claude reads a day across the whole inbox.
- Setup steps: `docs/school-import-setup.md`.

## 5. Non-functional requirements (all met in v1.0)
- **Runs unattended 24/7.** A single uvicorn process with an in-process scheduler (sync every
  60 s, daily reset, backup, calendar cache, school email).
- **Survives network loss.** Google and Open-Meteo failures degrade to cached or offline states
  and never produce an error page. Page loads wait at most 6 s for the calendar.
- **Visible health.**
  - Top-bar sync dot: hidden when healthy, amber after 5 minutes of failures, red when a
    reconnect is needed.
  - Admin → Sync.
  - `/health` for Docker.
  - Logs warn once per outage and record "recovered" once.
- **Data safety.** Nightly backups taken with SQLite `VACUUM INTO` and integrity-checked:
  - keeps 14 daily plus 8 weekly
  - includes the token encryption key
  - Admin shows the latest backup and has "Back up now"
  - the restore guide is in the README
- **Touch-first and legible** from 1–3 m (see 4.5).
- **Updates without reflashing:** `git pull` then `docker compose up -d --build`. Static files are
  revalidated on every load, so the tablet picks up new CSS/JS without clearing its cache.

## 6. Out of scope (for now)
- Voice control.
- Multiple physical displays or multi-device sync.
- Phone notifications beyond what Google's own apps provide.
- Points or rewards for chores.
- A companion phone app. Google Tasks and Calendar are the "add from anywhere" apps.

## 7. Key decisions (with reasons)
- **Google only** for calendar and tasks (Workspace account). Tasks and shopping sync with Google
  Tasks because Google Keep has no usable API.
- **Admin is schedule-only for tasks.** Editing happens in Google Tasks, where the family already
  works.
- **Split hardware:** the G10 server and the tablet display. This decouples display reliability
  from the home-automation box.
- **Web app in Fully Kiosk, not native.**
- **Stack: FastAPI + Jinja2 + HTMX + Gridstack + SQLite, no JS build step** (see 9.1). Alpine.js
  was dropped because it was unused.
- **Classroom:** API access needs the school's approval (not yet asked). Until then, spellings
  come in via screenshot upload. Two possible upgrades:
  - Classroom guardian email summaries, which give homework titles and due dates only
  - a read-only Classroom API app, which would make spellings fully automatic
- **Claude for extraction.** The family consented to sending school emails and uploads, and only
  those, to the Anthropic API. Structured tool output, validated server-side, approval-gated.
- **OAuth app set to "Internal"** (Workspace), so the restricted Gmail scope needs no Google
  verification and there's no 7-day token expiry.

## 8. Open questions
1. **Classroom:** ask the school about guardian summaries and/or read-only API access (see 7).
2. **Meal planning:** the basic weekly plan is built. Links to recipes or the shopping list are
   not decided.
3. **HTTPS and hostname via Caddy** (9.7) are not deployed yet. Until they are, reconnecting
   Google must be done from `localhost` on the G10 or through an SSH tunnel.
4. **Checks to do on the tablet:**
   - Does Fully Kiosk suppress the Android keyboard when ours is on?
   - Does a touch tap on the shopping "Add" button work?

---

## 9. Technical architecture

### 9.1 App framework (decided)
Python 3.14, FastAPI, Jinja2 server-rendered fragments, HTMX 2 (each widget re-renders itself),
and Gridstack.js. JS libraries are vendored at pinned versions, with no npm or build step.
Structure:
- `app/routers/`: thin HTTP layer
- `app/services/`: domain logic
- `app/widgets.py`: registry; adding a widget means a registry entry plus `DEFAULT_LAYOUT`
- `app/auth.py`: Admin sessions

### 9.2 Hosting (decided)
Fully local on the G10. The only external services are Google (Calendar, Tasks, Gmail),
Open-Meteo, and the Anthropic API (the school import, optional).

### 9.3 Storage (decided)
SQLite in WAL mode, with `synchronous=NORMAL` on every connection. Migrations are idempotent
blocks in `init_db()`. It holds:
- layout, local task and list mirrors, the sync queue and status
- a calendar cache
- homework and word lists
- the import inbox
- settings
- Fernet-encrypted Google tokens

### 9.4 Refresh model (decided)
Polling, not WebSockets:
- widget revisions every 30 s
- calendar every 180 s
- weather every 900 s
- sync dot every 60 s
- day/night and date switch timers set from the server

### 9.5 Google OAuth (decided)
- One account, one OAuth client, Internal consent screen.
- Scopes: `calendar.readonly`, `tasks`, `gmail.readonly`, `calendar.events`, `openid email`.
- Features check the scopes actually granted, so an older connection keeps working until it's
  reconnected.
- Tokens are encrypted at rest. Only `invalid_grant` disconnects.
- Google Photos (v1.1) will need the Photos Picker scope and a reconnect.

### 9.6 Layout engine (decided)
- Gridstack, 12 columns. Every drag or resize persists to `layout_state`, which validates each
  item and clamps it to the grid.
- New widgets are added to existing installs without moving the others.
- `layout_state.is_visible` exists, but **nothing in the UI sets it yet** (see 10.3).

### 9.7 Containerisation (decided)
- The display backend and Home Assistant run in Docker Compose on the G10, with a bind-mounted
  `./data/family-display` and one shared `.env`.
- **Docker log rotation is not configured yet** (see 10.4).
- **Caddy** as the reverse proxy with automatic HTTPS remains the plan, but is not yet deployed
  (see 8.3).

---

## 10. Next iteration (v1.1)

Chosen themes, 28 September 2026: **widget visibility**, a **notification banner**, and an **idle
screen with photos**. Each subsection lists the intended behaviour and the decisions still needed
before building. Suggested build order is 10.3 → 10.1 → 10.2, from smallest to largest; 10.2 also
needs a Google reconnect.

### 10.1 Notification banner
A slim banner under the top bar that tells the family what's coming up without anyone opening a
widget. It must not cover the grid's drag handles or steal taps.

**Triggers (proposed):**
- **Calendar events starting soon**, e.g. "Swimming at 16:30 (in 25 min)". Lead time comes from
  the event's own Google reminder if it has one, otherwise a default (30 min). Only for calendars
  selected in Admin; all-day events excluded.
- **Homework due**: "due tomorrow" from 16:00 the day before, and "due today" in the morning.
- **Today's school events** from approved school-inbox items, e.g. "Non-uniform day".
- **Chores still open late in the day** (e.g. after 18:00). This is optional and may be too naggy.

**Behaviour:**
- At most 2 banners shown, with "+N more".
- Tapping one dismisses it for that occurrence.
- They clear themselves once the event starts or the item is done.
- Quiet overnight: no banners in night mode, or on a configurable schedule.
- Uses the existing 30 s refresh; no push.

**Decisions needed:**
1. Which triggers, and their lead times?
2. Should the banner **play a sound**? Fully Kiosk can; the default proposal is no.
3. Per-person filtering (e.g. only the children's homework), or everything for everyone?
4. Should Admin let each trigger be turned on or off? Proposed: yes, one toggle each.

### 10.2 Idle screen and Google Photos
When nobody has touched the tablet for a while, show something calm. Admin chooses the behaviour
(spec'd since v0.9):

- **(a) Photo slideshow:** full-screen family photos with a small clock and date, plus the next
  event and today's weather.
- **(b) Stay on the dashboard:** today's behaviour.
- **(c) Dim:** lower the brightness, or use a dark overlay if Fully Kiosk's brightness control
  isn't available.

**Behaviour:**
- Starts after N minutes idle (default 5).
- **The first tap only wakes the screen.** It never ticks a task or presses a button.
- The idle screen is never shown while the on-screen keyboard is open or during a drag.
- A night schedule can force (c), or screen-off via Fully Kiosk's own scheduler.

**Photos:**
- Google Photos' old library API is closed, so photos come through the **Photos Picker API**:
  1. A parent taps "Choose photos" in Admin, on a phone or PC.
  2. They pick photos or albums in Google's picker.
  3. Huddle **downloads resized copies into `data/`**, because picker links expire.
  4. Re-picking replaces or extends the set.
- The Photos widget, now a placeholder, becomes a small rotating photo.

**Decisions needed:**
1. Build the idle screen in the app (works on any browser; our own wake handling), or use Fully
   Kiosk's built-in screensaver pointed at an idle URL (less code; tied to Fully Kiosk)?
   Proposed: **in the app**, reusing Fully Kiosk only for the brightness and screen-off schedule.
2. How long before going idle, and how many photos to keep? Proposed: 5 min, and up to 300 photos
   at about 1600 px, roughly 100 MB.
3. What does the slideshow overlay show: clock, next event, weather?
4. A photo storage cap, and whether backups include photos (proposed: no; they can be re-picked).
5. Needs a Google reconnect for the Photos Picker scope, and enabling the Photos Picker API in
   Cloud Console.

### 10.3 Choosing which widgets are shown
Admin → **Widgets**: a list of every widget, each with a Show/Hide switch. It sets
`layout_state.is_visible`, which the dashboard already respects.

**Behaviour:**
- **Admin only.** The unauthenticated hide endpoint was removed on purpose.
- A hidden widget keeps its saved position. Showing it again puts it back there, or in the next
  free space if that's taken.
- The Photos placeholder is hidden by default until 10.2 ships.
- A hidden widget is excluded from `/api/rev` polling and its data isn't loaded.

**Decisions needed:**
1. Should the rest of the grid close the gap when a widget is hidden (Gridstack float off), or
   leave the gap as it is? Proposed: close it; widgets keep their order.
2. A time-based schedule (e.g. show Homework only on school days, Meals only after 15:00)? Proposed:
   not in v1.1; keep a simple on/off.

### 10.4 Carried over (smaller)
- Deploy Caddy with a hostname and HTTPS (8.3).
- Add Docker log rotation to `docker-compose.yml` (json-file, `max-size: 10m`, `max-file: 3`).
- Ask the school about Classroom (8.1).
- Do the tablet checks (8.4).
