# Family Management Display — Specification Document

**Status:** DRAFT v0.9 — Docker deployment sub-decisions settled (Section 9.7). Tablet placement (kitchen), idle screen behaviour, notification triggers confirmed. Chore rewards/points removed (tasks are purely functional). Only Google Classroom (pending school confirmation) and meal planning UI remain open.
**Target hardware:** GMKtec G10 (Ryzen 5 3500U) as local Docker server + Samsung Galaxy Tab A9+ as wall-mounted display in the kitchen, connected over Wi-Fi — see Section 3 and Section 9.7

---

## 1. Purpose

A shared, always-on touchscreen display for the household that shows calendar events,
upcoming appointments, per-person daily tasks, and a shared shopping list — with sync
to a Google account and parent-only admin controls.

---

## 2. Reference Products Considered

Cozyla and Dragon Touch smart family displays were reviewed for feature comparison —
customisable dashboards, chore rewards/points, meal planning, photo screensaver, weather,
and companion phone apps all originate from that comparison (see Section 4.5–4.6).

---

## 3. Hardware & Platform — **Decided: split architecture**

The compute (server) and the display are now two separate devices, connected over Wi-Fi rather
than a wired HDMI+USB touch monitor. This was chosen once Docker + Home Assistant came into scope
(Section 9.7) — it decouples the family display's reliability from the home automation stack's,
and avoids having to mount a mini PC behind a wall-mounted monitor.

| Item | Decision |
|---|---|
| Server | **GMKtec G10 (AMD Ryzen 5 3500U, 16GB RAM)** — runs the FastAPI backend, SQLite, and **Home Assistant, both in Docker containers**, with room to add more containers later. Can live anywhere on the home network (cupboard, shelf) — no longer needs to be near the display. |
| Display | **Samsung Galaxy Tab A9+ (11", LCD)**, wall-mounted, connecting to the server over Wi-Fi only |
| Display software | **Fully Kiosk Browser** (Android) — fullscreen, no browser chrome, pointed at the server's dashboard URL, autostart on boot |
| Display mount | Purpose-made wall mount for the Tab A9+ (e.g. The 3D Room, UK), routing a single USB-C power cable into the wall — no video/data cable needed |
| App type | **Browser-based web app — confirmed over building a native Android app.** Fully Kiosk Browser already gives an app-like fullscreen experience with no browser chrome; a native app would mean a second codebase/tech stack to maintain in parallel with zero functional benefit for this use case. A lightweight PWA manifest is a possible small follow-up polish item (fullscreen launch without depending on Fully Kiosk Browser specifically), not required. |
| Network | Home Wi-Fi — both server and tablet on the same local network |
| Always-on display | Yes, with scheduled screen dim/sleep overnight (via Fully Kiosk Browser's own scheduling) |
| Server OS | Standard Ubuntu/Debian (x86) |

---

## 4. Core Screens & Features

### 4.1 Home Screen
- Calendar view, upcoming events, per-person tickable task lists, shared shopping list
- Customisable, resizable widget grid (anyone can rearrange, no PIN required)

### 4.2 Calendar Sync
- Google Calendar API + OAuth, fully editable from the screen
- Supports shared and/or individual per-person calendars, colour-coded

### 4.3 Family Member Task Lists
- Recurring and one-off tasks, ticked via touch with a checkmark animation
- Synced to Google Tasks; one-offs cross out then auto-remove at end of day
- **Purely functional — no points/rewards system** (confirmed; reverses an earlier draft decision
  that had tasks as points-based). Visual identity per person (name, colour, avatar).

### 4.4 Shopping List
- Single shared list, bidirectional sync with Google Tasks

### 4.5 Customisation
- Anyone can rearrange and resize widgets (grid-based dashboard, Gridstack.js)
- Additional widgets: weather, Google Photos (manual re-pick via Picker API — automatic
  full-library sync is no longer available), Google Classroom homework (status: unconfirmed,
  pending check with school on third-party API access), manual lists, meal planning
- **Idle screen behaviour: admin-configurable**, choosing between (a) rotating photo slideshow,
  (b) stay on the calendar/tasks view, or (c) dim the screen — not a single fixed behaviour, a
  setting in Admin
- **Notification banner** triggers on: task due-soon alerts and calendar reminders

### 4.6 Admin / Parent Controls
- Single shared PIN, both parents use it, with exponential backoff on failed attempts
- Manage family members, tasks, calendar/account connections
- Manage available widgets/integrations (layout rearranging itself is open to everyone)
- Set idle screen behaviour (photo slideshow / stay on calendar-tasks / dim screen — see 4.5)

---

## 5. Non-Functional Requirements
- Must run reliably unattended, 24/7
- Should recover gracefully from network loss / reconnect automatically
- Touch-first UI, legible from a few feet away
- Local data should persist across reboots/power loss
- Should be maintainable/updatable without needing to rebuild the SD card / boot media

## 6. Out of Scope (for now)
- Voice control
- Multiple physical displays / multi-device sync
- Notifications to phones beyond what Google's own apps already provide

---

## 7. Decisions Confirmed So Far
- Calendar sync: Google only (Workspace account), editable, colour-coded, shared and/or individual
- Family members: 3–4, each with own task list + colour
- Task lists: recurring and one-off, checkmark animation, synced to Google Tasks, **purely
  functional — no points/rewards system**
- Shopping list: single shared list, bidirectional Google Tasks sync
- Customisation: anyone can rearrange and resize widgets
- Admin access: single shared PIN
- Google Photos: manual re-pick via Picker API (no automatic full-library sync)
- **Tablet wall placement: kitchen** (confirmed)
- **Idle screen behaviour: admin-configurable** — choice of photo slideshow, stay on
  calendar/tasks view, or dim screen (not a single fixed behaviour)
- **Notification banner: triggers on task due-soon alerts and calendar reminders**
- Hardware: **split architecture — GMKtec G10 (Ryzen 5 3500U) as server, Samsung Galaxy Tab A9+
  as wall-mounted display**, connected over Wi-Fi (see Section 3). Chosen over both "Pi 5 doing
  everything" and "mini PC mounted behind a wired touch monitor" once Docker + Home Assistant
  came into scope — decouples display reliability from the home automation stack, and avoids
  mounting a PC behind the wall monitor.
- **Docker + shared box: confirmed.** Home Assistant and the family display backend both run in
  Docker containers on the G10, with room to add other containers later as the household's needs
  grow. Deployment decisions (Home Assistant Container, Caddy, bind mounts, shared `.env`) are
  settled — see Section 9.7.
- Display app type: **browser-based web app in Fully Kiosk Browser, not a native Android app** —
  confirmed after considering the maintenance cost of a second codebase against the negligible
  UX benefit for this use case
- App framework: Lightweight web app — Python backend (FastAPI) + HTMX/Alpine.js + Gridstack.js,
  no React/build pipeline, chosen for long-term maintainability over a full React SPA
- Hosting model: fully local, no external server — Google's own apps (Tasks, Calendar) already
  provide "add from anywhere" without needing a companion cloud component

## 8. Open Questions Still to Resolve

**Google Photos / Classroom**
1. Google Classroom: still needs confirming with the school whether third-party Classroom API
   access is allowed; fallback is parsing the weekly Guardian email summary.

**Other previously raised, still open**
2. Meal planning widget — wanted, mechanics/UI not yet fully specified

---

## 9. Technical Architecture

### 9.1 App framework — **Decided**
Lightweight web app: Python (FastAPI) backend + HTMX/Alpine.js + Gridstack.js frontend,
no build step, vendored JS libraries pinned at fixed versions. Chosen over a full React SPA
for long-term maintainability (no npm dependency churn to manage over years).

### 9.2 Backend & hosting model — **Decided**
Fully local, running on the device itself. No external/cloud component — Google Tasks and
Google Calendar already provide "add from anywhere" via their own phone apps, so a custom
companion app or hosted backend isn't needed.

### 9.3 Local data storage — **Decided**
SQLite (WAL mode). Sufficient for a single household's dataset (layout config, cached
calendar/task data, manual lists, points balances).

### 9.4 Real-time updates — **Decided**
Polling (every 1–5 minutes) rather than WebSockets/push. Matches how often calendar/task
data actually needs to refresh; avoids added complexity not needed for a single display.

### 9.5 Google OAuth / integrations management — **Decided**
Single Workspace account OAuth for Calendar + Tasks. Google Photos (Picker API) and Google
Classroom (per-child student OAuth) are separate flows. All tokens stored locally on-device,
gated behind the admin PIN.

### 9.6 Widget layout engine — **Decided**
Gridstack.js grid, drag/resize/collision handled out of the box, layout state persisted to SQLite.

### 9.7 Containerization (Docker) — **DECIDED**

Home Assistant and the family display backend both run as **Docker containers on the G10**,
alongside room for other containers as the household's needs grow. This was confirmed once the
display was decoupled from the server (Section 3) — the G10 can now be treated as a proper small
home server rather than something living behind a wall-mounted screen.

**Deployment decisions — now settled:**

| Decision | Answer | Why |
|---|---|---|
| Home Assistant install method | **Home Assistant Container** | Fits directly into the same `docker-compose.yml` as the family display backend. USB Zigbee/Z-Wave passthrough (if adopted later) is still possible via a `devices:` entry — more manual than Home Assistant OS's auto-detection, but not a blocker for a "maybe later." |
| Reverse proxy | **Caddy** | Automatic HTTPS, minimal config for 2–3 services. Traefik's extra power (dynamic service discovery via Docker labels) isn't needed at this scale. |
| Persistent data | **Bind mounts** | Plain folders (`./data/family-display`, `./data/homeassistant`) that can be browsed, backed up, or `rsync`'d directly — more visible than named volumes, matches the project's maintainability priority. |
| Secrets | **One shared `.env`** at the Compose project root, with prefixed variable names (`DISPLAY_...`, `HA_...`) to avoid collisions | Per-service env files only pay off with many services; unnecessary overhead for two or three containers. |

One general note that still holds regardless of the above: Docker deployment means updates are
`docker compose pull` + recreate the container, rather than `git pull` + restart a systemd
service directly — worth having in the README once the Compose setup is written.

**9.7.1 Resolved: same box, confirmed.** Home Assistant and the family display backend share the
G10, both in Docker, using the decisions in the table above.

**Nothing above changes the application code itself** (Section 9.1's FastAPI/HTMX/Gridstack app
containerizes without modification) — these are deployment/infrastructure decisions layered on
top.

---

Section 9.7 is now fully decided. Nothing here blocks starting the Docker Compose setup.
