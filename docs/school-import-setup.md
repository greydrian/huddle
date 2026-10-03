# School import: setup checklist

The code for the school import is merged. It covers the **School inbox**,
**Add from Classroom**, and the daily **School email** check. These steps
switch it on. Do them in order. Until they're done, Calendar and Tasks work
as normal, and the new Admin panels show "not set up" or "Reconnect Google…"
instead of errors.

- [ ] 1. Google Cloud: make the app Internal
- [ ] 2. Google Cloud: enable the Gmail API
- [ ] 3. Anthropic: create an API key and add it to `.env`
- [ ] 4. Deploy the latest code
- [ ] 5. Admin: reconnect Google
- [ ] 6. Admin: set the year group and the school calendar
- [ ] 7. Admin: run the first check and review the inbox

---

## 1. Google Cloud: make the app Internal

Go to [console.cloud.google.com](https://console.cloud.google.com), select the
project that holds Huddle's OAuth client (its Client ID is `GOOGLE_CLIENT_ID`
in `.env`), then open **APIs & Services → OAuth consent screen** (on newer
consoles, **Google Auth Platform → Audience**) and set **User type** to
**Internal**.

Why this matters:
- **Verification.** Reading Gmail is a "restricted" permission. For an
  Internal app, Google doesn't require its verification review.
- **The weekly drop-out.** Internal apps aren't subject to the 7-day token
  expiry that "Testing" mode has, so the Google connection stops dropping
  every week.

If **Internal** is greyed out, the project doesn't belong to your Google
Workspace organisation. Either move it into the organisation (**IAM & Admin →
Settings → Migrate**), or create a new project inside the organisation with a
new OAuth client. If you create a new one, put the new
`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`, and add the same
redirect URIs as before (see README, "Connecting Google").

## 2. Google Cloud: enable the Gmail API

In the same project, go to **APIs & Services → Library**, search for **Gmail
API**, and click **Enable**.

Leave the **Google Calendar API** and **Google Tasks API** enabled as they are.

## 3. Anthropic: API key

1. Go to [console.anthropic.com](https://console.anthropic.com). Add a payment
   method and create an API key (**API Keys → Create key**).
2. On the G10, add it to `.env` in the repo folder:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
   `.env` is gitignored, so never commit it or paste the key anywhere else.
   `ANTHROPIC_MODEL` can stay blank, which uses the default model.
3. Optional: set a monthly spend limit in the Anthropic console. The app
   itself caps usage at 25 emails per check and 60 documents a day.

## 4. Deploy

From the repo folder on the G10:

```
git pull
docker compose -f docker-compose.yml up -d --build
```

Then reload the tablet. If Admin asks you to **Choose a new PIN**, do that
first. If you ever forget the PIN, run
`docker compose exec family-display python -m app.reset_pin`. It resets the
PIN to 1234 and brings back the "Choose a new PIN" screen.

## 5. Admin: reconnect Google

The new Gmail (read-only) and calendar-event permissions are only granted
when you connect again:

1. Admin → Google & Sync → **Google accounts** → **Edit** on the account
   the school writes to (or **Add account** for it), and tick **School
   email** and **Writing events**. School email is offered for a parent's or
   a Family account only.
2. Approve on Google's screen. It asks only for the newly ticked
   permissions (reading Gmail, managing calendar events).
3. Re-check the calendar selection and the Tasks lists in the same panel.
   Everything linked to the account is kept, but make sure.

Remember that Google only accepts the callback on `localhost`. Do this step in
a browser on the G10 itself, or through the SSH tunnel
(`ssh -L 8000:localhost:8000 <g10>`, then open `http://localhost:8000/admin`).
It won't work from the tablet's LAN address.

If Google says access is blocked by your organisation, check the Workspace
Admin console under **Security → Access and data control → API controls**. The
Huddle app must be allowed. Internal apps normally are.

## 6. Admin: year group and school calendar

- **Family Members**: set your daughter's year group to **Year 4**. Claude uses
  it to work out which child each item belongs to. Add your younger child's year
  group when they start school.
- **School email → School events go to calendar**: pick the Google calendar
  approved school dates should go into.
- **School email**: the schedule defaults to **daily at 18:00**. Change it if
  you like: twice daily, weekly (Friday), or off.

## 7. First check and weekly routine

1. **School email → Check now**. The first run looks back 14 days and reads
   up to 25 emails. If there are more, the rest follow about 15 minutes later.
   The status line shows progress, and when the check is running it says
   "Checking…".
2. **School inbox**: review what was found. Each item shows a short quote from
   the email. **Approve** puts homework and word lists on the wall and dates in
   the calendar. **Discard** drops an item. Nothing appears without approval.
3. **Every week, for spellings**: in the Google Classroom app, screenshot the
   spellings post, or copy its text. Then go to Admin → **Add from Classroom**,
   upload or paste it, pick your daughter, submit, and approve it in the
   **School inbox**.

## Checking it's working

- The **School email** panel shows the last check, the next check, and how many
  emails and items were found.
- The dot in the top bar and Admin → **Sync** cover the Google connection as a
  whole.
- `docker compose logs --tail 50 family-display` shows one line per check with
  counts only. Email content is never logged.

## Later: ask the school

To avoid the weekly screenshot, ask Gresham Primary about either of these:
- **Guardian email summaries** from Google Classroom. The import reads these
  automatically, but they give homework titles and due dates only, not the
  spelling lists.
- Allowing a read-only third-party app to access your daughter's Classroom
  coursework through the **Classroom API**. That would make spellings fully
  automatic. It needs the school's IT admin to approve it.
