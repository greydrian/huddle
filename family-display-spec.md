# Family Management Display ("Huddle"): Specification

**Status:** v1.0, as built, 28 September 2026. Sections 1–9 describe the app as it stands and the
decisions behind it. **Section 10 is the v1.1 plan** (next iteration) and is still open for
decisions.
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
- **Updates without reflashing:** `git pull` then `docker compose up -d --build`. Static files are
  revalidated on every load, so the tablet picks up new CSS/JS without clearing its cache.

## 6. Out of scope (for now)
- Access from outside the home. The display is **home network only** (decided 28 Sep 2026): no
  VPN, no public exposure.
- Opening the dashboard on family phones or tablets. **The wall display is the only screen**;
  phones use the Google apps.
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

## 10. Next iterations (v1.1 and v1.2)

Chosen themes, 28 September 2026: **widget visibility**, a **notification banner**, and an **idle
screen with photos**. A spec review the same day added improvements to tasks, the calendar,
meals, the shopping list, avatars, homework and practice, school term dates, and Home Assistant.
Everything below is **decided** unless it's marked *open*.

**Releases** (decided):

| Release | Contents | Build order |
|---|---|---|
| **v1.1**: daily-use wins | 10.3 Widget visibility, 10.6 School term dates, 10.4 Tasks, 10.1 Notification banner, 10.2 Idle screen & Photos | 10.3 → 10.6 → 10.4 → 10.1 → 10.2 |
| **v1.2**: richer widgets | 10.5 Calendar, 10.7 Meals, 10.8 Shopping, 10.9 Avatars, 10.10 Homework & practice, 10.11 Home Assistant | 10.9 → 10.10 → 10.5 → 10.7 → 10.8 → 10.11 |

10.6 comes before 10.4 and 10.3's "school days" option because both depend on term dates. 10.2
needs a Google reconnect (the Photos Picker scope).

### 10.1 Notification banner
A slim banner under the top bar that tells the family what's coming up without anyone opening a
widget. It must not cover the grid's drag handles or steal taps.

**Triggers.** All four are on by default, and **each can be switched off in Admin**:
1. **Events starting soon**, e.g. "Swimming at 16:30 (in 25 min)".
   - Lead time comes from the event's own Google reminder. Without one, it uses an **Admin setting
     (default 30 min)**.
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
- **Night mode is quiet:** no banners and no sounds.
- Uses the existing 30 s refresh; no push.

### 10.2 Idle screen and Google Photos
**Built into the app.** Huddle draws the idle overlay and handles waking. Fully Kiosk is used only
for screen brightness and the overnight screen-off schedule.

**Modes and timing.** All of these are Admin settings:
- the idle delay (default 5 min)
- what happens when idle: **photo slideshow**, **stay on the dashboard**, or **dim**
- what happens at night (e.g. dim instead of the slideshow)
- the overnight screen-off times, passed to Fully Kiosk

**Waking.** The first tap only wakes the screen; it never ticks a task or presses a button. The
display never goes idle while the on-screen keyboard is open, during a drag, or while a banner
has just appeared.

**The slideshow** shows full-screen photos with a readable overlay:
- the **clock and date**
- the **next event** today
- the **weather**

Banners stay visible over it.

**Photos.**
- They come from the **Google Photos picker** as a snapshot. In Admin, a parent opens Google's
  picker (on a phone or PC), browses into an album and picks photos.
- Huddle downloads resized copies (about 1600 px) into `data/`, because picker links expire.
  Re-pick to change the set.
- **At most 30 photos are kept.** The slideshow **reshuffles its order weekly**.
- Photos are **not included in backups**; they can be re-picked.
- Needs the Photos Picker API enabled in Cloud Console and a Google reconnect.
- Live album sync, like a Nest Hub, isn't available to third-party apps: Google removed album
  access for other apps in March 2025. A Google Drive folder was considered as a live alternative
  and not chosen.

**The Photos widget is removed from the dashboard.** Photos appear on the idle screen only.

### 10.3 Choosing which widgets are shown
Admin → **Widgets**: every widget listed with a **Show/Hide** switch. It sets
`layout_state.is_visible`, which the dashboard already respects.

- **Admin only.** The unauthenticated hide endpoint was removed on purpose.
- **Hiding a widget closes the gap**: the widgets below it move up to fill the space and keep their
  order. Showing it again puts it back in its saved position, or the nearest free space.
  - This must only fill the hidden widget's space. It must never rearrange the rest of the
    family's layout. In particular, don't switch Gridstack's `float` for the whole grid.
- **"School days only"** per widget: an optional switch that shows the widget only on school days,
  using the term dates (10.6). Useful for Homework and Practice words.
- A hidden widget is excluded from `/api/rev` polling, and its data isn't loaded.

### 10.4 Tasks
- **Where a task lives.** Tasks synced to Google show no marker. **Local-only tasks** (a person
  with no linked Google Tasks list) get a small "on this display only" icon, because they won't
  appear on phones.
- **Adding tasks.** Google Tasks stays the main place. The Tasks widget gains a PIN-free
  **"+ Add"** for quick one-offs. The task syncs if that person has a linked list, and is
  local-only otherwise.
- **Time of day.** Each task can be set to **Morning**, **After school** or **Evening** (default:
  no group). This is stored locally, like repeat days, because Google Tasks can't hold it.
  - The widget shows all groups, with **the current group first and highlighted**. Earlier groups'
    unfinished tasks stay visible.
  - Group boundaries are an Admin setting (proposed: Morning until 12:00, After school
    12:00–18:00, Evening after 18:00).
- **Missed one-offs.** A one-off not done by the end of its day **carries over, marked late**
  ("from yesterday", or "from Mon"), until it's done or deleted.
- **School days.** A new repeat option alongside the Mon–Sun chips. "School days" follows the
  school's term dates, so school-morning chores skip holidays and INSET days. Term dates are
  described in 10.6.

### 10.5 Calendar
- **Adding events.** A PIN-free "+" on the calendar adds a simple event: title, day, optional
  start and end time. It always goes into **one designated shared calendar** (for example,
  "Family"), chosen in Admin, and never into personal or work calendars. Editing and deleting
  stay in Google Calendar.
- **More views.** A **week view** (7 columns with times) and a **today/agenda list** join the
  month grid and day view. **Admin picks the default view**, and the widget returns to it after
  a few minutes idle.
- **Filter by person.** Tap a person's name pill to show only their events.
  - Ownership is decided by **calendar first**: Admin links each Google calendar to a family
    member, and a shared calendar counts for everyone.
  - If no calendar is linked, the fallback is the **person's name appearing in the event title**.

### 10.6 School term dates
- The school inbox learns a new item type, **term dates**: term start and end, half-terms and
  INSET days. Claude extracts them from the school's term-dates letter, and a parent approves them.
- Admin can add or edit term dates by hand too.
- They drive the "school days" repeat option (10.4) and each widget's "school days only" option
  (10.3).
- **They appear on the Huddle calendar** as subtle all-day bars for holidays, half-terms and INSET
  days. These are local only, **not written to Google**.

### 10.7 Meals
- **Favourites.** Past meals are remembered. When planning, pick from favourites (most used first)
  instead of typing. A meal can be starred or removed from favourites.
- **Recipe link.** Each meal can have a link or short notes. The widget shows a small icon.
  - Tapping it on the tablet shows the notes with **both options**: a **QR code** to open the recipe
    on a phone, and **"Open here"** to show the page full-screen on the tablet with a large Back
    button.
  - Some recipe sites refuse to open inside another page. For those, "Open here" falls back to
    the QR code.

### 10.8 Shopping list
- **Quantities**, e.g. "Milk ×2". Stored in the Google Tasks item (title suffix or notes, *open:*
  pick whichever phones display best) so phones see it too.
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
  - *Open:* stylus palm rejection on the Tab A9+.

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
- **Home Assistant widget on the display.** Admin chooses the entities, using an HA long-lived
  access token stored encrypted like the Google tokens:
  - **Lights and scenes:** tap to toggle a light or run a scene (e.g. "Bedtime").
  - **Heating:** current temperature, plus boost or up/down on chosen thermostats.
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
