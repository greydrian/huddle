# Family Management Display ("Huddle"): Specification

**Status:** v1.0 as built + v1.1/v1.2 plan, reviewed 28 Sep 2026. Sections 1–9 describe the app
as it stands and the decisions behind it. **Section 10 is the v1.1/v1.2 plan** (next iterations).
It was checked against external sources on 28 Sep 2026 and again on 30 Sep 2026; they're listed
at the end. **Section 11 records the decisions of the 30 Sep 2026 review**; where it changes an
earlier section, that section says so.
**Target hardware:** GMKtec G10 (Ryzen 5 3500U) as the local Docker server, and a Samsung Galaxy
Tab A9+ wall-mounted in the kitchen as the display, connected over Wi-Fi.

This document is the product source of truth. AI-assisted features have their own companion spec,
`docs/assistant-spec.md`. `CLAUDE.md` covers how the code is organised, and
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
| Display | **Samsung Galaxy Tab A9+** (11", about 1280×800 CSS px landscape) today, being replaced by a **Lenovo Idea Tab Plus 12.1"** (2560×1600, with pen). See below. Wall-mounted in the kitchen, Wi-Fi only. |
| Display software | **Fully Kiosk Browser**: fullscreen, autostart, pointed at the dashboard URL. Its scheduled dim/sleep and remote cache-clear are relied on. |
| Fully Kiosk licence | **Fully Kiosk PLUS is required** (€7.90 one-off per device). The JavaScript interface, screensaver, screen-off timer, scheduled wake/sleep and motion detection all need PLUS. |
| Tablet battery | Turn on Samsung **"Protect battery"** (charge limit about 85%). The tablet is on charge 24/7, and batteries kept at 100% can swell. |
| Server power | A **small UPS for the G10 is recommended**. No app changes needed. |
| App type | **Browser-based web app, not a native Android app.** One codebase; no extra UX benefit from native. |
| Network | Home Wi-Fi; the server and tablet share the LAN. |

**New display tablet (decided 28 Sep 2026): Lenovo Idea Tab Plus 12.1"**, with the Lenovo Tab Pen
in the box. It replaces the Tab A9+.
- **Budget:** under £350 all-in. That's about £290 for the tablet (Argos), £35 for The 3D Room mount
  (the current supplier) and about £20 for a USB-C wall socket. Fully PLUS is extra.
- **Why:** it gives a real active pen for handwriting practice (10.10) and a bigger 12.1" screen
  (2560×1600) for the calendar. Security updates run to about 2029.
- **Other options considered:**
  - a Galaxy Tab S10 FE+: about £600 all-in, too expensive
  - a refurbished Galaxy Tab S9 FE: about £250–315 with the S Pen, but no bigger than today's screen
    and updates end in late 2028
  - keeping the A9+
- **Test these within the Argos return period, before relying on it:**
  1. The pen reports `pointerType: "pen"` in Chrome and Fully Kiosk. Check with a pointer-events
     demo page, or Huddle's pen-test page if it's built first. If it reports `"touch"`, the pen gives
     no palm rejection, and the choice needs revisiting.
  2. Pressure is reported, and a resting hand is ignored while the pen is down.
  3. A **charge limit** exists in Settings (Lenovo "Battery Protection Mode", or similar), for 24/7
     power. Without one, battery swelling is a long-term risk.
  4. Fully Kiosk runs, wakes and dims correctly. That needs PLUS, so move the licence or buy one.
  5. The pen's AAAA battery life is reasonable. Note to replace it occasionally.
- The Tab A9+ stays in use until the new tablet has passed these checks.

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
| Photos | **Placeholder; removed in v1.1** (10.2, migration 5). |

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
  Always dark. *Changing to sunset-based, with 19:00–07:00 as the fallback: see 11.4.*
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
  - The school also uses **Arbor** and **ParentMail**; see 11.3 for how their messages get in.
  - *One school today; becoming per-child: see 11.2.*
- **Claude extracts** spelling lists, homework and key dates into the **School inbox**. Nothing
  reaches the wall or the calendar until a parent approves it. Approved dates are added to a
  chosen Google calendar.
- Privacy and safety:
  - **The automatic daily school-email check never fetches SENDCo mail**
    (`sen@gresham.croydon.sch.uk`, private SENDCo conversations), and any email that mentions it
    is dropped whole. This is as built.
  - **A parent-labelled "Huddle" email is the only exception**; see `docs/assistant-spec.md`, A1.
  - Quoted reply history is stripped.
  - Senders that fail DMARC/SPF/DKIM are skipped.
  - Documents are treated as untrusted: the model can only suggest inbox items.
  - No email content is logged.
  - Caps: 25 emails per check, and 60 Claude reads a day across the whole inbox.
- Setup steps: `docs/school-import-setup.md`.
- The school import is the first **assistant** capability. What the assistant does next (letters
  and photos, quick add, questions, weekly digest, meal ideas) and the rules they all follow are in
  **`docs/assistant-spec.md`**.

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
- **Updates without reflashing:** `scripts/deploy.sh <tag>` on the G10 (backup through the
  running app, check out the release, rebuild in production mode, wait for `/health`). The app
  also takes a verified snapshot at startup whenever migrations are pending. Static files are
  revalidated on every load, so the tablet picks up new CSS/JS without clearing its cache.

## 6. Out of scope (for now)
- Access from outside the home. The display is **home network only** (decided 28 Sep 2026): no
  VPN, no public exposure.
- Opening the dashboard on family phones or tablets. **The wall display is the only screen**;
  phones use the Google apps. The one exception: **paired phones can use a small upload page** on
  the home network (see `docs/assistant-spec.md`). It's not a dashboard.
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
- **Claude for extraction.** Structured tool output, validated server-side, approval-gated.
- **Consent to the Anthropic API.** The family consents to sending the following to the Anthropic
  API:
  - school emails and uploads (built)
  - later, per assistant capability: typed requests, calendar event titles and times, task titles,
    homework, children's first names and year groups, and **allergies and dislikes** (health data,
    for meal ideas)

  **Anthropic does not retain API prompts or outputs by default** for the models Huddle uses
  (Sonnet and Haiku), and **doesn't train on them**. Content flagged by its automated safety
  systems may be kept for **up to 2 years**. A 30-day retention requirement applies only to the
  Claude Fable and Mythos models, which Huddle does not use. Zero data retention is a contract
  arrangement and doesn't apply here. (Corrected 30 Sep 2026; the earlier text said 30 days by
  default.) What each capability sends is in the assistant spec's data table
  (`docs/assistant-spec.md`, section 4).
- **OAuth app set to "Internal"** (Workspace), so the restricted Gmail scope needs no Google
  verification and there's no 7-day token expiry.

## 8. Open questions
1. **Classroom:** ask the school about guardian summaries and/or read-only API access (see 7).
2. **Meal planning:** the basic weekly plan is built. Links to recipes or the shopping list are
   not decided.
3. **HTTPS and hostname via Caddy** (9.7) are not deployed yet. Until they are, reconnecting
   Google must be done from `localhost` on the G10 or through an SSH tunnel.
   - Google needs HTTPS on a real public domain for OAuth callbacks, so Caddy needs a **real
     domain with a DNS-01 certificate, pointing at the LAN IP**. A `.lan` name won't work.
   - Still deferred.
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
blocks in `init_db()` today; v1.1 moves to numbered migrations (9.8). It holds:
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
- One account, one OAuth client, Internal consent screen. *Extending to several accounts for
  calendars: see 11.1.*
- Scopes: `calendar.readonly`, `tasks`, `gmail.readonly`, `calendar.events`, `openid email`.
- Features check the scopes actually granted, so an older connection keeps working until it's
  reconnected.
- Tokens are encrypted at rest. Only `invalid_grant` disconnects.
- Google Photos (v1.1) uses its **own separate "Photos account" sign-in** with a second OAuth
  client (10.2). It needs no reconnect of this connection.

### 9.6 Layout engine (decided)
- Gridstack, 12 columns. Every drag or resize persists to `layout_state`, which validates each
  item and clamps it to the grid.
- New widgets are added to existing installs without moving the others.
- `layout_state.is_visible` and `school_days_only` are set in Admin → Display → Widgets (10.3,
  `app/services/layout.py`).

### 9.7 Containerisation (decided)
- The display backend and Home Assistant run in Docker Compose on the G10, with a bind-mounted
  `./data/family-display` and one shared `.env`.
- **Docker log rotation is not configured yet** (see 10.12).
- **Caddy** as the reverse proxy with automatic HTTPS remains the plan, but is not yet deployed
  (see 8.3).

### 9.8 Platform work (v1.1, first)
Done first in v1.1, as part of 10.0:
- **Numbered DB migrations.** A `schema_migrations` table; each change is applied once and
  recorded. The existing idempotent `init_db()` blocks become migration 1.
- **A dependency lock file with hashes** (uv or pip-tools), plus **Dependabot or Renovate** update
  PRs.
- **Playwright browser tests in CI.** A small smoke suite covering drag vs scroll, the on-screen
  keyboard and tasks folding, and later the banner and idle screen.
- **Type checking in CI** (pyright or mypy), introduced gradually.
- New interactive pieces (idle screen, banner, handwriting canvas) are **small plain-JavaScript
  modules**. Still no build step.
- **Pin the Docker base image by digest.**

**Stay on Python** (decided). A Go rewrite was considered and rejected: there's no performance
need, the rewrite and the test suite would be a large cost, and Go's image/HEIC libraries are
weaker.

---

## 10. Next iterations (v1.1 and v1.2)

Chosen themes, 28 September 2026: **widget visibility**, a **notification banner**, and an **idle
screen with photos**. A spec review the same day added improvements to tasks, the calendar,
meals, the shopping list, avatars, homework and practice, school term dates, and Home Assistant.
An external-sources review the same day added platform work (10.0) and corrected several details
below. Everything below is **decided** unless it's marked *open*.

**Releases** (decided):

| Release | Contents | Build order |
|---|---|---|
| **v1.1**: daily-use wins | 10.0 Platform work & Admin tabs, 10.6 School term dates, 10.3 Widget visibility, 10.4 Tasks, 10.1 Notification banner, 10.2 Idle screen & Photos | 10.0 → 10.6 → 10.3 → 10.4 → 10.1 → 10.2 |
| **v1.2**: richer widgets | 10.5 Calendar, 10.7 Meals, 10.8 Shopping, 10.9 Avatars, 10.10 Homework & practice, 10.11 Home Assistant | 10.9 → 10.10 → 10.5 → 10.7 → 10.8 → 10.11 |

**v1.1 build order:**
1. **10.0 Platform work and Admin tabs**
2. 10.6 Term dates: manual entry and bank holidays first; Claude extraction second
3. 10.3 Visibility
4. 10.4 Tasks
5. 10.1 Banner
6. 10.2 Idle + Photos

10.6 comes before 10.4 and 10.3's "school days" option because both depend on term dates. 10.2
needs its own Photos sign-in, not a reconnect of the main Google connection.

**Effort and risk:**

| Item | Effort / risk |
|---|---|
| Admin tabs | S–M / low |
| 10.3 | M / med |
| 10.6 | M / med |
| 10.4 | L / med |
| 10.1 | M / med |
| 10.2 | L / **high** |
| 10.5 | L / med |
| 10.7 | M / low |
| 10.8 | M / low–med |
| 10.9 | S–M / low |
| 10.10 | M / med |
| 10.11 | M–L / med |

**Before building 10.2, confirm or buy Fully PLUS and test waking behaviour on the tablet.**

### 10.0 Platform work and Admin tabs
- Everything in 9.8.
- **Admin is split into tabs** before more sections are added: Family · School · Display ·
  Google & Sync · Assistant · System. `settings.html` is currently about 507 lines with 14
  sections, and v1.1–v1.2 add about 12 more.
- Family members gain an **`is_parent` flag and an email address**. They're needed by assistant
  A1 (parent tasks) and A4 (digest email).
- **Touch targets are at least 48 px**, with generous spacing, because young children use the
  display. For reference, WCAG 2.5.8 (AA) asks for 24 px and 2.5.5 (AAA) for 44 px.

### 10.1 Notification banner
A slim banner under the top bar that tells the family what's coming up without anyone opening a
widget. It must not cover the grid's drag handles or steal taps.

**Triggers.** All four are on by default, and **each can be switched off in Admin**:
1. **Events starting soon**, e.g. "Swimming at 16:30 (in 25 min)".
   - Lead time is the **smallest `popup` reminder override** on the event. If the event uses the
     calendar's defaults (`useDefault`), Huddle uses the `defaultReminders` returned by
     `events.list` (readable with `calendar.readonly`). Otherwise it uses an **Admin setting
     (default 30 min)**.
   - **The lead time is capped at 2 h.** Otherwise a day-before reminder would show a banner for
     24 h.
   - Only calendars selected in Admin; all-day events are excluded.
2. **Homework due**: "Due tomorrow" from 16:00 the day before, and "Due today" in the morning.
3. **Today's school events**, from approved school-inbox dates, e.g. "Non-uniform day".
4. **Chores left late in the day**, e.g. once the Evening group starts: "3 chores still to do".

**Behaviour.**
- **Every banner shows the person's coloured name pill** (or "Everyone"). All banners are visible
  to the whole family.
- **Sound can be switched on or off per trigger in Admin** (default off).
- At most 2 banners at once, with "+N more".
- Tapping one dismisses it for that occurrence.
- They clear themselves once the event starts or the item is done.
- **Quiet hours:** no banners and no sounds. Quiet hours are **separate from night mode**: default
  **21:00–07:00**, set in Admin. So a 19:30 club still gets a banner, even though night colours
  start at 19:00.
- Uses the existing 30 s refresh; no push.

### 10.2 Idle screen and Google Photos
**Huddle owns idle behaviour.** Huddle draws the idle overlay and handles waking.
- In Fully Kiosk, **turn off its own screensaver and screen-off timer**.
- Huddle **dims** for idle, using `fully.setScreenBrightness` when `typeof fully` is available, and
  a dark overlay otherwise.
- **Overnight screen-off uses Fully's own wake/sleep schedule, set by hand** in Fully (needs
  PLUS). **A screen Fully has turned off does not wake on a tap**, so screen-off is for overnight
  only.

**Modes and timing.** All of these are Admin settings:
- the idle delay (default 5 min)
- what happens when idle: **photo slideshow**, **stay on the dashboard**, or **dim**
- what happens at night (e.g. dim instead of the slideshow)

The overnight screen-off times are not a Huddle setting; they're set in Fully (see above).

**Waking.** The first tap only wakes the screen; it never ticks a task or presses a button. The
display never goes idle while the on-screen keyboard is open, during a drag, or while a banner
has just appeared.

**The slideshow** shows full-screen photos with a readable overlay:
- the **clock and date**
- the **next event** today
- the **weather**

Banners stay visible over it.

**Photos account (decided).** Photos come from a **separate "Photos account" sign-in**. The
family's photos are in a **personal Google account**, but the main OAuth app is Internal to the
Workspace organisation and can't reach personal accounts.
- A **second OAuth client** in an External/Testing Cloud project, with the personal account added
  as a test user.
- Its only scope is `https://www.googleapis.com/auth/photospicker.mediaitems.readonly`.
- The token is only needed while picking and downloading, so Testing mode's 7-day expiry doesn't
  matter. Signing in again when re-picking is acceptable.
- It needs **no reconnect of the main Workspace connection.**

**Picker flow.** In Admin, a parent picks photos:
1. `sessions.create` with `pickingConfig.maxItemCount=30`.
2. Admin shows the `pickerUri` as a **QR code or link**, to open on a phone signed in to the
   personal account. It can't be put in an iframe.
3. Huddle polls the session using `pollingConfig`.
4. `mediaItems.list`.
5. Huddle downloads each item using its `baseUrl` with a Bearer token, sized to the display panel:
   **`=w2560-h1600`** for the Idea Tab Plus (use `=w1920-h1200` on the Tab A9+). baseUrls expire after
   60 min. Copies are kept in `data/`.
6. `sessions.delete`.

**Photos.**
- A snapshot, not a live album. Re-pick to change the set.
- **At most 30 photos are kept.** The slideshow **reshuffles its order weekly**.
- Photos are **not included in backups**; they can be re-picked.
- Needs the Photos Picker API enabled in the second Cloud project.
- Live album sync, like a Nest Hub, isn't available to third-party apps: Google removed album
  access for other apps in March 2025. A Google Drive folder was considered as a live alternative
  and not chosen.

**The Photos widget is removed from the dashboard.** Photos appear on the idle screen only. The
removal is a migration: delete its `layout_state` row and its registry entry (see 10.3).

### 10.3 Choosing which widgets are shown
Admin → **Widgets**: every widget listed with a **Show/Hide** switch. It sets
`layout_state.is_visible`. The dashboard already leaves hidden widgets out of the grid, but that
alone doesn't close the gap or skip their data (see below).

- **Admin only.** The unauthenticated hide endpoint was removed on purpose.
- **Hiding a widget closes the gap**: the widgets below it move up to fill the space and keep their
  order. Showing it again puts it back in its saved position, or the nearest free space.
  - The grid uses Gridstack `float: true`, so **closing the gap needs custom server-side
    shifting** of only the widgets below the hidden one.
  - This must only fill the hidden widget's space. It must never rearrange the rest of the
    family's layout. In particular, don't switch Gridstack's `float` for the whole grid.
- **"School days only"** per widget: an optional switch that shows the widget only on school days,
  using the term dates (10.6). Useful for Homework and Practice words.
- A hidden widget is excluded from `/api/rev` polling, and **its loader is skipped**. Today
  `dashboard.py` loads every widget's data up front.
- The Photos widget removal (10.2) is a migration here: its `layout_state` row and its registry
  entry.

### 10.4 Tasks
- **Where a task lives.** Tasks synced to Google show no marker. **Local-only tasks** (a person
  with no linked Google Tasks list) get a small "on this display only" icon, because they won't
  appear on phones.
- **Adding tasks.** Google Tasks stays the main place. The Tasks widget gains a PIN-free
  **"+ Add"** for quick one-offs. The task syncs if that person has a linked list, and is
  local-only otherwise.
- **Time of day.** Each task can be set to **Morning**, **After school** or **Evening** (default:
  no group). This is stored locally, like repeat days, because Google Tasks can't hold it: the
  Tasks API's `due` field is date-only, and it discards any time of day on write.
  - The widget shows all groups, with **the current group first and highlighted**. Earlier groups'
    unfinished tasks stay visible.
  - Group boundaries are an Admin setting (proposed: Morning until 12:00, After school
    12:00–18:00, Evening after 18:00).
- **Missed one-offs.** A one-off not done by the end of its day **carries over, marked late**
  ("from yesterday", or "from Mon"), until it's done or deleted.
- **School days.** A new repeat option alongside the Mon–Sun chips. "School days" follows the
  school's term dates, so school-morning chores skip holidays and INSET days. Term dates are
  described in 10.6.
  - **When term dates are missing**, school days are Mon–Fri minus bank holidays, and Admin shows
    a warning such as "Term dates missing for 2026–27".

### 10.5 Calendar
- **Adding events.** A PIN-free "+" on the calendar adds a simple event: title, day, optional
  start and end time. It always goes into **one designated shared calendar** (for example,
  "Family"), chosen in Admin, and never into personal or work calendars. Editing and deleting
  stay in Google Calendar.
  - **A quick-added event for one person is saved with a name prefix**, e.g. "Alanna: Dentist".
    It's clear on phones, and the person filter's name matching picks it up.
- **More views.** A **week view** (7 columns with times) and a **today/agenda list** join the
  month grid and day view. **Admin picks the default view**, and the widget returns to it after
  a few minutes idle.
- **Filter by person.** Tap a person's name pill to show only their events.
  - Ownership is decided by **calendar first**: Admin links each Google calendar to a family
    member, and a shared calendar counts for everyone.
  - If no calendar is linked, the fallback is the **person's name appearing in the event title**.
    The name-in-title fallback applies inside shared calendars too.

### 10.6 School term dates
- The school inbox learns a new item type, **term dates**: term start and end, half-terms and
  INSET days.
- **Sources, in order:**
  1. **manual Admin entry** (add or edit by hand)
  2. the **GOV.UK bank-holidays feed** (`https://www.gov.uk/bank-holidays.json`, England and
     Wales)
  3. **Claude extraction from the school's term-dates PDF**, which a parent approves
- Croydon Council and Gresham publish term dates as **PDF only, with no iCal feed**. Gresham's is
  a colour-coded grid, so extraction needs vision and a careful parent check. The council's dates
  don't include INSET days. **Check them once a year** after approving.
- They drive the "school days" repeat option (10.4) and each widget's "school days only" option
  (10.3).
- **They appear on the Huddle calendar** as subtle all-day bars for holidays, half-terms and INSET
  days. These are local only, **not written to Google**.

### 10.7 Meals
- **Favourites.** Past meals are remembered. When planning, pick from favourites (most used first)
  instead of typing. A meal can be starred or removed from favourites.
- **Recipe link.** Each meal can have a link or short notes. The widget shows a small icon.
  - Tapping it on the tablet shows a **recipe card**. Huddle fetches the page server-side and
    builds a clean card (ingredients, steps, time) from the **schema.org `Recipe` JSON-LD** that
    most recipe sites publish (checked: BBC Good Food, Jamie Oliver, RecipeTin Eats).
  - A **QR code** to open the recipe on a phone is the fallback when no recipe data is found.
  - Why not show the site itself: 9 of 12 major UK recipe sites checked forbid being shown inside
    another page (X-Frame-Options/CSP), and navigating the kiosk away to the site would be a kiosk
    escape.

### 10.8 Shopping list
- **Quantities**, e.g. "Milk ×2". **Stored in the Google Tasks title as a suffix**, so phones see
  it too. Google Tasks notes only show as a truncated preview, so notes weren't chosen. Huddle
  parses `x2`, `×2` and `2x`.
- **Admin display options.** Choose between a **simple list** (today's) and a **grouped by
  category/aisle** layout, e.g. fruit & veg, dairy, bakery, frozen, household.
  - Categories are assigned automatically from a built-in word list, can be corrected per item,
    and are remembered for next time.
  - **The order of the groups is set in Admin**: drag the categories into the order of your usual
    supermarket.

### 10.9 Family members and avatars
- Each person's avatar can be **a coloured initial** (today's), **an emoji**, or **a photo**,
  chosen per person in Admin → Family Members.
- Photos are uploaded in Admin, cropped round and resized small (about 256 px). They're **stored
  in the database**, so the nightly backup includes them: backups only copy the database and the
  key, not other files in `data/`.
- The avatar appears wherever the name pill does: tasks, homework, practice words and banners.
  The contrast rules from 4.5 still apply.

### 10.10 Homework and handwriting practice
- **Subject icons and colours.** Maths, English, Reading, Science, Topic and Other each get an icon
  and a colour tint, so children can spot them quickly. The subject is picked in Admin or by the
  school inbox, and "Other" is the fallback.
- **Done history.** Children can already tick homework done on the tablet (v1.0). v1.2 adds a
  record of **when** each item was ticked, shown in Admin, so parents can see what was done and
  when.
- **Reading log.** A per-child "Read tonight ✓" daily tick, like "Practised today". Admin shows a
  simple history, e.g. the last 4 weeks, to help with the school reading record. No points.
- **Write-on area for practice words.** Tapping a word opens a large handwriting area:
  - lined, like school paper, with the word shown above in Playwrite GB
  - the child writes it freehand with a finger or stylus
  - Clear, and Next word
  - no marking or scoring, and nothing is saved
  - It must not trigger Gridstack drags; it opens as a full-screen overlay.
- **Pen input.** The target is the **Lenovo Tab Pen on the Idea Tab Plus** (3):
  - When `pointerType: "pen"` is seen, draw only with the pen and **ignore touch pointers while the
    pen is down**. This is how a web page does palm rejection; Android doesn't do it for browsers.
  - Use `touch-action: none` on the canvas, and use pressure for stroke width if it's reported.
  - **Fallback** for finger or rubber-tip stylus use (the Tab A9+, or if the pen turns out to report
    `"touch"`): lock to the first pointer and reject contacts with a large `width`/`height`.
  - *Open:* confirm the pen's browser behaviour on the device (3, test 1).

### 10.11 Home Assistant
Home Assistant already runs on the G10.
- **Huddle status to HA.** A token-protected `GET /api/ha/status` returns JSON:
  - sync state
  - chores left per person
  - homework due today or tomorrow
  - school inbox items awaiting approval
  - last backup age

  HA reads it as REST sensors (about every minute) for its own automations, e.g. flash a light when
  sync needs attention. The token is generated in Admin and can be regenerated.
  - The status feed stays REST: HA's `rest:` integration supports headers and several sensors from
    one resource. An HA webhook trigger is an optional later addition for instant alerts. MQTT
    isn't needed.
- **Home Assistant widget on the display.** Admin chooses the entities. Huddle uses a
  **non-admin HA user's** long-lived access token, stored encrypted like the Google tokens:
  - **Lights and scenes:** tap to toggle a light or run a scene (e.g. "Bedtime").
  - **Heating:** current temperature, plus boost or up/down on chosen thermostats. The family's
    heating is **Hive**. There's no generic HA "boost", so boost uses Hive's
    `hive.boost_heating_on` service, or the widget runs **HA scripts** (`script.turn_on`) that the
    family sets up. Up/down uses `climate.set_temperature`.
  - **Status tiles, read-only:** doors or windows open, bin day, washing machine done, and so on.
  - **Locks and the alarm are not included.** If they're ever added, each action needs the Admin
    PIN.
  - If HA is unreachable, the widget shows an offline state and never an error page. Tiles update
    on the normal refresh; HA's websocket push is *open* as a later improvement.

### 10.12 Carried over (smaller)
- Deploy Caddy with a hostname and HTTPS (8.3).
- Add Docker log rotation to `docker-compose.yml` (json-file, `max-size: 10m`, `max-file: 3`).
- Ask the school about Classroom (8.1).
- Do the tablet checks (8.4).
- **Check Fully PLUS** (Settings → About, or the licence page), and **buy it if missing**.
- *Open:* **pull a CI-built image instead of building on the G10.** A release workflow would
  push the image to GHCR on each tag and `scripts/deploy.sh` would `docker compose pull` it, so
  the G10 runs exactly the image CI tested and needs no PyPI or Docker Hub access. Decided 30 Sep
  2026 to keep building on the G10 for now; the pull stays manual either way, because a deploy
  runs migrations.

### 10.13 Cross-cutting requirements for v1.1/v1.2
- **Offline behaviour, per feature:**
  - banners work from the calendar cache
  - the HA widget shows an offline state
  - the assistant shows "unavailable", and manual forms still work
  - photos show from the local copies
- **Tests:** respx mocks for the Photos Picker and HA; frozen clocks for banners and quiet hours;
  Playwright for idle, wake and the banner.
- **Performance on the tablet:** slideshow crossfades use CSS opacity only; the handwriting canvas
  size is capped.
- **Backups:** new data lives in the DB, including the HA and status tokens (encrypted), avatars
  and term dates. Photos are excluded.
- **Photos Picker sessions** are cleaned up with `sessions.delete`.
- **Migrations** go through 9.8's numbered migrations.

### Things to know up front
- **Cost:** about $3–6 a month is typical for the assistant. The cap is set and enforced in US
  dollars, the currency Anthropic bills in (11.6). Fully PLUS is about US$10–11 one-off per
  device, depending on region.
- **Reconnects:** none planned. A4's digest email (which needed Gmail send and a reconnect) is
  deferred (11.6). **Photos use their own separate sign-in.**
- **Not bought yet (30 Sep 2026):** the Lenovo tablet and Fully PLUS. Home Assistant is not set up
  yet either; it is a separate project (11.5).
- **Privacy:** see 7.
- **Hardware:** a new Lenovo Idea Tab Plus 12.1" with pen, under £350 all-in. Run the 5 checks in 3
  within the Argos return period. A screen Fully has turned off won't wake on a tap.
- **Workspace admin:** keep Huddle trusted under Security → API controls ("Trust internal apps").
- **Term dates** come from PDFs; check them once a year.

---

## 11. Decisions from the 30 Sep 2026 review

A spec review against the code and external sources on 30 Sep 2026 asked 15 questions. These are
the answers and what they change. Items marked *proposed* are the design that follows from an
answer; they're decided in outline and settled in detail when built.

### 11.1 More than one Google account (changes 9.5 and 10.5)
- **Decided:** Huddle can read calendars from **more than one Google account**, for when a
  parent's or child's calendars live in another account.
- **Decided:** each child gets **their own Google calendar**, plus a general **Family** calendar.
  Calendar-to-person linking (10.5) then does the work; the name-in-title fallback stays for
  events in the Family calendar.
- *Proposed design:*
  - The existing connection stays the **primary account**. Tasks, shopping, school email, the
    Family calendar that "+" adds to, and approved school events all stay on it.
  - **Extra accounts are calendar-read-only** (`calendar.readonly` only). Admin lists them, each
    with its own Connect/Disconnect and calendar picker. Their calendars join the same
    selection, colours, person links, cache and offline rules as the primary account's.
  - Each account's token is its own `auth_tokens` row; sync status and the top-bar dot report
    per account.
  - **An account outside the Workspace** (e.g. a personal gmail.com) can't use the Internal
    OAuth client. It needs a second, External client, like Photos (10.2), whose Testing-mode
    refresh tokens expire after 7 days — too short for a calendar that must stay connected. So
    the recommended setup for a non-Workspace account is to **share its calendars into the
    primary account**, which needs no code and no second client. Extra-account connections are
    for Workspace accounts.
  - *Open:* are the other accounts inside the Workspace? That decides whether the extra-account
    code is needed at all, or calendar sharing covers it.

### 11.2 Two children, one school, a second later (changes 4.3, 4.8 and 10.6)
- **Decided:** support several children properly. Today: two children, one in Year 4 at
  Gresham, one not yet at school.
- *Proposed design:* a **school** is its own record (name, sender allowlist, term dates, whether
  it has Classroom). Each child optionally belongs to one school; a child with no school has no
  "school days" and gets no school import. A nursery can be a school too, with its own dates.
  - Term dates (10.6) belong to a school, not to the whole family.
  - The "School days" repeat option (10.4) follows **the task owner's** school; a task for a
    parent or a child without a school falls back to Mon–Fri minus bank holidays.
  - A widget's "school days only" (10.3) shows when **any** child with a school has school that
    day.
  - The school email check reads each school's allowlist, and the inbox pre-fills the child from
    the sender's school.
  - One migration moves today's term dates and senders onto a Gresham school record and links the
    Year 4 child to it; nothing else changes for the family until a second school is added.

### 11.3 Arbor and ParentMail (changes 4.8 and assistant A1)
- **Decided:** the school sends through **Arbor**, **ParentMail** and ordinary email.
- *Proposed design:*
  - Both apps send email notifications. Whether a notification carries the letter itself (text or
    PDF) or only a link to the app decides the route. *Open:* forward one real example of each to
    check.
  - If the email carries the content, add the app's sender to the school's allowlist, and the
    existing email check reads it.
  - If it carries only a link, the route is a **screenshot or PDF** uploaded through Admin now, and
    the phone upload page (assistant A1) later. Huddle will not log in to Arbor or ParentMail on
    the family's behalf: no public API for either was found, and storing a parent's password
    there would be a new and larger risk.

### 11.4 Sunset-based night mode (changes 4.5)
- **Decided:** Auto appearance follows **sunset and sunrise** for the weather location, with
  **19:00–07:00 as the fallback**.
- *Proposed design:*
  - Sunrise and sunset come from Open-Meteo's daily forecast for the weather location, fetched with
    the forecast Huddle already makes, and cached with it.
  - Night starts at sunset and ends at sunrise. The fallback applies when there is no weather
    location, no cached times for today, or the times are unusable (missing, or sunset before
    sunrise).
  - **Every fallback is logged once per day with its reason**, as a code: `no_location`,
    `no_forecast`, `stale_forecast`, `bad_times`. Admin → Appearance shows today's source
    ("Sunset 18:42 from the forecast" or "Fixed 19:00–07:00: no forecast yet") and the most recent
    fallback reason.
  - The existing timers switch day and night without a reload, so nothing on the tablet changes.
  - Quiet hours for banners (10.1) stay a separate fixed setting.

### 11.5 Home Assistant is a later, separate project (changes 10.11)
- **Decided:** notifications to parents' phones through Home Assistant's companion app are
  wanted, for Huddle being unreachable, sync needing attention, and a stale backup.
- Home Assistant is **not set up yet** and is its own project. 10.11 stays as specified; its
  status feed (`GET /api/ha/status`) is what those phone notifications will read. Presence-based
  screen wake using Fully's REST API also waits for it.

### 11.6 Assistant: dollars, no digest email yet, no voice (changes the assistant spec)
- **Decided:** the monthly assistant cap is **set and enforced in US dollars**, the currency
  Anthropic bills in. No exchange-rate setting. (Assistant spec 4 and decision 2 there.)
- **Decided:** the digest's **email is deferred** (assistant A4). The digest ships as the
  on-display card first; email or phone delivery is decided later. No Gmail send scope and no
  Google reconnect for now.
- **Decided:** **voice stays out of scope** for the next two releases.

### 11.7 Deferred, with no change now
- **HTTPS and a hostname** (8.3) stay deferred; reconnecting Google keeps using `localhost` on the
  G10 or an SSH tunnel. When it's built, `docs/https-setup.md` gives the step-by-step setup
  (domain, DNS-01 certificate, Caddy, the new OAuth redirect URI).
- **Off-box backups** (README, "Keep a copy off the box") stay a manual job for now.
- **Tablet and Fully PLUS** aren't bought yet. The five checks in 3 still apply before relying on
  the pen (10.10), and 10.2 downloads photos at the Tab A9+ size until the new tablet arrives.
- **Clubs and activities** aren't in Google Calendar yet. Adding them as recurring events in each
  child's calendar is setup, not code, and banners, the agenda and the week view pick them up.
- **Build order** after v1.2: not decided yet.

### 11.8 Small features added from the review
- **"Next up" strip** (built). Under the banner bar, one item per person with something still to
  come today: their pill, the event, its time and "in 25 min"; events for no one in particular
  show as Everyone. Read from `calendar_cache` only, so it works offline. Whose event it is follows
  the calendar's person filter (10.5): a calendar linked to the person, else their name in the
  title. All-day events and term-date bars are left to the calendar and banners. It wraps onto a
  second line rather than scrolling, and like the banner bar its refresh waits while the wall is
  in use. Admin → Display → Banners has an on/off switch (default on).
- **Countdowns** (built). "Half term in 21 days", "Trip to Gran in 3 days": after the day's events
  in the same strip, under a "Counting down" label, the nearest three, each optionally one
  person's (their pill). Two sources: dates added in Admin → Display → Countdowns (a name up to 60
  characters, a date from today to two years ahead, optionally whose), and the next half term or
  holiday from the term dates (10.6) once it's under 60 days away, which can be switched off
  there. A date that has passed stops showing; Admin lists it greyed until someone deletes it.
  Countdowns show even when the next-up events are switched off. Once schools are per child (11.2),
  the school break becomes one per school.

---

## Sources
Checked in the external-sources review, 28 Sep 2026.
- Google Photos Picker: <https://developers.google.com/photos/picker/guides/sessions>;
  <https://developers.google.com/photos/picker/guides/media-items>;
  <https://developers.google.com/photos/support/updates>
- Fully Kiosk: <https://www.fully-kiosk.com/en/#websiteintegration>;
  <https://license.fully-kiosk.com/license/single>
- Google Calendar events and reminders:
  <https://developers.google.com/workspace/calendar/api/v3/reference/events>
- Google Tasks: <https://developers.google.com/workspace/tasks/reference/rest/v1/tasks>
- Workspace API controls: <https://support.google.com/a/answer/7281227>
- Home Assistant: <https://developers.home-assistant.io/docs/api/rest/>;
  <https://www.home-assistant.io/integrations/rest/>; Hive:
  <https://www.home-assistant.io/integrations/hive/>
- WCAG target size: <https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html>
- Anthropic pricing: <https://platform.claude.com/docs/en/about-claude/pricing>; data retention:
  <https://platform.claude.com/docs/en/manage-claude/api-and-data-retention>
- Term dates: <https://www.gresham.croydon.sch.uk/parent-info/term-dates/>;
  <https://www.gov.uk/bank-holidays.json>

Added in the review of 30 Sep 2026 (section 11). Google's and Fully's own pages were blocked from
the environment that review ran in, so those facts were confirmed through search summaries and the
secondary pages listed.
- Anthropic data retention (read in full):
  <https://platform.claude.com/docs/en/manage-claude/api-and-data-retention>
- Google Tasks `due` is date-only: <https://issuetracker.google.com/issues/129591245>;
  <https://wadih.systemesmw.com/2026/07/07/google-tasks-api-creating-tasks-works-due-times-do-not/>
- Google Photos Library API changes:
  <https://developers.googleblog.com/en/google-photos-picker-api-launch-and-library-api-updates/>;
  Picker `baseUrl` expiry: <https://github.com/NicoPietrusco/HicPicNunc/issues/23>
- Google Classroom third-party access: <https://support.google.com/edu/classroom/answer/6250906>
- Fully Kiosk PLUS price and REST API: <https://license.fully-kiosk.com/license/single>;
  <https://kleypot.com/fully-kiosk-rest-api-integration-in-home-assistant/>
- Lenovo Idea Tab pen: <https://mynexttablet.com/lenovo-idea-tab-review/>
- Open-Meteo daily sunrise and sunset: <https://open-meteo.com/en/docs>
