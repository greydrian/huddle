# Huddle Assistant: specification

**Status:** v0.3. v0.2 was agreed in the spec review on 28 September 2026; v0.3 applies the
decisions of the 30 September 2026 review (main spec section 11.6): the cap in US dollars, the
digest email deferred, voice still out of scope, and corrected data-retention facts. Companion to `family-display-spec.md`. That document
describes the app. This one covers everything the app does with an AI model (Claude, via the
Anthropic API): what it may do, what it may read, and the rules every assistant feature follows.

**Already built:** the school import (main spec 4.8). It covers "Add from Classroom" uploads, the
daily school-email check, and the School inbox where a parent approves each item. It is the first
assistant capability, and the model for the rest.

---

## 1. Principles (apply to every capability)

1. **Always ask first.** The assistant only ever *suggests*. Nothing reaches the wall, a Google
   calendar, a task list or an email until a person confirms it.
   - On the tablet, **whoever asked confirms** on a card, with Add / Edit / Cancel.
   - Everything else goes to the **Admin inbox** for a parent.
   - Kiosk limits still apply. For example, events added from the tablet only go into the
     designated Family calendar (main spec 10.5).
2. **Input is untrusted.** Emails, letters, photos and typed text are data, never instructions.
   The model returns structured suggestions, and Huddle re-checks every field before showing it.
   The model can't take any action itself.
3. **Keep only the results.** Originals (photos, PDFs, email bodies) are never stored. Huddle keeps
   what was approved, plus a short quote (up to 200 characters) so a parent can check where it came
   from.
   - **No quote is stored for a SENDCo-related email a parent labelled.** Only the approved item
     is kept, so no SENDCo text ends up in the database or backups.
4. **Send the minimum.**
   - About the children: **first names and year groups only**.
   - Only mail from allowed sources, or mail a parent has labelled for Huddle, is ever read.
   - **`sen@gresham.croydon.sch.uk` (SENDCo) is never read or sent automatically.** The only
     exception is an email a parent deliberately labels "Huddle" (see A1).
   - Where an answer needs family data, such as Q&A or the digest, only the slice that's needed is
     sent (e.g. the next 14 days of event titles and times), never the whole database.
   - What each capability sends is listed in section 4.
5. **Nothing is logged that shouldn't be.** No document content, email bodies, model output,
   tokens or API keys ever go in the logs. Logs hold codes and counts only.
6. **Degrade gracefully.** If the API is down, the key is missing or the budget is used up, the
   assistant says so plainly. Every manual form still works, and nothing blocks the kiosk.
7. **Budget-capped.** See section 4.

## 2. Ways in (surfaces)

| Surface | How it works | Who confirms |
|---|---|---|
| **Tablet box** | A "Ask or add…" box on the dashboard (on-screen keyboard supported). Type a request or a question. | Whoever typed it, on a card |
| **Gmail label** | Apply a **"Huddle"** label to any email, on a phone or PC. Huddle reads labelled mail on the same daily schedule as the school email check. | A parent, in the Admin inbox |
| **Phone upload page** | Pair each phone once by scanning a QR code shown in Admin. The phone can then upload photos, screenshots or PDFs to a small **upload page**, reached by paired phones on the home network, without the PIN. Pairings are listed in Admin and can be revoked. | A parent, in the Admin inbox |
| **Admin** | Existing "Add from Classroom" upload and paste, generalised to any document. | A parent, in the Admin inbox |
| **Scheduled** | Daily school email (built), and the weekly digest. | The digest is read-only |

**Why an upload page, not a share target.** An Android share-sheet target needs HTTPS and an
installed PWA, which the home-network-only setup doesn't have. So phones open the upload page
instead. The QR pairing stays.

**Voice (future).** Voice is out of scope for the next two releases (decided 30 Sep 2026). The
tablet box is designed so that speech-to-text can feed it later, with nothing else changing.

## 3. Capabilities

Build order (decided): **A1 → A2 → A3 → A4 → A5**.

**Parents.** A1's parent tasks and A4's digest recipients use the **`is_parent` flag and the email
addresses on family members** (main spec 10.0).

### A1. Any letter or photo (the school inbox, generalised)
Any document (a paper letter photo, a PDF, a forwarded or labelled email) is read into the inbox.
Beyond homework, word lists and events (already built), the assistant also extracts:
- **Reply/consent deadlines**, e.g. "Return the trip form by Friday". These become a **parent task**.
- **Payments**, e.g. "£8 for the trip by 12 Oct". These become a **parent reminder**. Huddle never
  pays or opens payment links for you.
- **Kit and things to bring**, e.g. "Wellies and a packed lunch on Thursday". These become a note
  on that day's event, and optionally **shopping items**.
- **Contact details**, e.g. a club or coach's phone number. These are saved as a note on the
  related event.

**Parent tasks and reminders** go to the **chosen parent's Google Tasks list**, so they're on that
parent's phone and on the display. The parent is picked when approving, from the family members
with the `is_parent` flag (main spec 10.0).

New surfaces delivered with A1: **phone pairing** with the upload page, and the **Gmail "Huddle"
label**.

**The Gmail label.**
- Huddle finds labelled mail with `q=label:Huddle`, which uses the label name.
- Huddle **can't remove the label** without the `gmail.modify` scope, so it **tracks processed
  message IDs** instead (the existing `import_sources` idempotency).
- A message matched by both the school check and the label follows the **label** rules.

**The SENDCo exception (decided).** An email a parent deliberately labels "Huddle" **is read even
if it's from, or mentions, the SENDCo address**. Applying the label is the parent's explicit choice.
- The automatic daily school-email check still never fetches SENDCo mail.
- Labelled SENDCo mail follows every other rule: quoted history is stripped and nothing is logged.
- **No quote is stored for it.** Only the approved item is kept, so no SENDCo text ends up in the
  database or backups (principle 3).

### A2. Quick add in plain words (tablet)
- Type "Dentist Tuesday 4pm for Alanna", "Milk, eggs, bread" or "Alanna tidy room every Saturday
  morning".
- The assistant turns it into the right items: a Family-calendar event, shopping items, or a task
  with a person, a time-of-day group and repeat days. Each item is shown on a confirm card.
- **A quick-added event for one person is saved as "Name: Title"** in the Family calendar, e.g.
  "Alanna: Dentist" (main spec 10.5).
- It uses the **cheaper, faster model**. What's sent is the typed text, today's date, family first
  names, and the names of the Family calendar and task lists.
- **Rate limit on the tablet box:** 30 requests an hour and 100 a day.
- A2's calendar part depends on the **designated Family calendar setting** from main spec 10.5,
  which is in v1.2. That setting will be pulled into whichever release first needs it.

### A3. Questions (tablet, read-only)
- Ask in the same box: "What's on Thursday?", "When is the school trip?", "What homework is due?"
- The answer comes from Huddle's own data: calendar, tasks, homework, and the approved school
  items. Only the slice needed for that question is sent.
- **Read-only.** Answers never change anything. An answer can offer a quick-add card, e.g. "Add a
  reminder?", which then needs confirming.
- **How long answers stay up.** An answer shows for **30 seconds** by default, changeable in Admin.
  It can be **dismissed** at any time with a close button or by tapping outside it. Touching the
  answer keeps it open.

### A4. Weekly family digest
- **Sunday at 18:00 by default** (changeable in Admin). A "Week ahead" summary covering:
  - events per day, and busy days or clashes
  - homework due and school events
  - payments and reply deadlines
  - anything still waiting in the inbox
- Delivered as **a card on the display**, shown until dismissed.
- *Deferred (30 Sep 2026):* **an email to both parents** (the `is_parent` family members, main
  spec 10.0). It needs the Gmail send permission and a Google reconnect, so it waits until a
  delivery channel is chosen: email, or a phone notification through Home Assistant (main spec
  11.5).
- Sends the coming week's slice of family data to Claude.

### A5. Meal planning helper
- **On request only.** A "Suggest meals" button proposes meals for a chosen day or week.
- It takes account of:
  - **Allergies** (a hard rule) and **dislikes** (a soft preference), per person, set in Admin
  - **Busy evenings**: it reads the calendar to suggest quick meals on nights with swimming or
    clubs
- Uses meal favourites (main spec 10.7). The suggestions can be approved into the meal plan, and
  the ingredients added to the shopping list.
- Depends on main spec 10.7, which is due in v1.2.

## 4. Budget, models, data and the activity log
- **A monthly spending cap set in Admin, in US dollars. The default cap is $12.** When it's
  reached, the assistant pauses and the tablet and Admin say so. Manual features still work.
  - Existing safety limits stay underneath it: 25 emails per check, and 60 documents a day.
  - **All costs are shown in US dollars**, the currency Anthropic bills in: the cap, each log
    entry, and the monthly total. No conversion and no exchange-rate setting, so the cap matches
    the bill exactly. (Changed 30 Sep 2026 from pounds.)
  - Costs are computed from the API's `usage` token counts.
  - **Before sending a document or photo, Huddle counts its tokens** (`count_tokens`) and refuses
    the request with a clear message if it alone would take the month past the cap. The cap is a
    hard line, not something noticed afterwards.
  - Typical estimate: about $3–6 a month.
- **The model is chosen per task automatically:**
  - the **cheap model is Claude Haiku** (currently `claude-haiku-4-5`), for quick add and Q&A
  - the **strong model is Claude Sonnet** (currently `claude-sonnet-5`, the code's default), for
    PDFs, photos, the digest and meal planning. Claude Sonnet 5.5 is the same price and is the
    candidate to move to once it has been checked on a few real letters.

  Both can be overridden in Admin. Opus was considered (Opus 5.5 is about 2× Sonnet's price) and
  not chosen.
- **Keeping costs down.** Use **prompt caching** across a multi-email run, and consider the
  **Batch API (50% off)** for the scheduled 18:00 check.
- **Data sent per capability:**

  | Capability | Data sent to Anthropic |
  |---|---|
  | School import / A1 | the document or email, children's first names and year groups, today's date |
  | A2 | the typed text, first names, calendar and list names, the date |
  | A3 | the question, plus the minimal relevant slice of events, tasks and homework |
  | A4 | the coming week's events, homework, school items and deadlines |
  | A5 | allergies, dislikes, busy evenings, meal favourites |

  **Anthropic does not retain API prompts or outputs by default** for the Sonnet and Haiku models,
  and **doesn't train on them**. Content flagged by its automated safety systems may be kept for
  up to 2 years. The 30-day retention requirement applies only to the Claude Fable and Mythos
  models, which Huddle does not use. Zero data retention is a contract arrangement and doesn't
  apply here. (Corrected 30 Sep 2026.) The family's consent is recorded in main spec 7.
- **Activity log in Admin.** Each request records:
  - when, which capability and which surface
  - a one-line summary of what was suggested (no document content)
  - what was approved
  - the approximate cost

  There's also a running monthly total against the cap. Log entries older than 12 months are
  deleted.

## 5. Security notes
- **Phone pairing.**
  - Admin shows a one-time QR code, valid for 5 minutes. The phone gets a long random device
    token, stored hashed on the server.
  - Uploads use the same upload guard as today: the token is checked before any of the body is
    read, and there are size and count caps.
  - Pairings can be revoked individually. A PIN change prompts whether to revoke them all.
- **Tablet box.** It's PIN-free, like the rest of the kiosk. It can only create what the kiosk
  could create by hand anyway (Family-calendar events, tasks, shopping), and it's rate-limited
  (30 requests an hour, 100 a day) to stop runaway costs. Each request is also capped at 300
  characters, so a child leaning on the keyboard can't send a wall of text 30 times.
- **Scopes added over time:**
  - `gmail.readonly`: already granted; also covers the label. It can't remove the label, so
    processed message IDs are tracked instead (A1).
  - `gmail.send`: for the digest email (A4), deferred
  - `tasks`: already granted; parent tasks go through it

  Each is gated by the scopes actually granted, as today.

## 6. Decided in the review (28 Sep 2026)
1. A parent-applied "Huddle" label overrides the SENDCo exclusion (see A1). The automatic school
   check never does.
2. Budget and costs are shown and capped in pounds, converted at an exchange rate set in Admin.
   *Superseded 30 Sep 2026: US dollars, no conversion (section 4).*
3. Answers show for 30 seconds by default, changeable, and can be dismissed. Spoken answers are
   part of the future voice work.
4. No quote is stored for a SENDCo-related email a parent labelled; only the approved item is kept.
5. Phones use an upload page on the home network, not an Android share-sheet target.
6. Labelled mail is found with `label:Huddle`; processed message IDs are tracked because the label
   can't be removed without `gmail.modify`. Label rules win when both the label and the school
   check match.
7. Parent tasks and the digest use the `is_parent` flag and email addresses on family members
   (main spec 10.0).
8. A quick-added event for one person is saved as "Name: Title". The tablet box is limited to 30
   requests an hour and 100 a day.
9. The default monthly cap is £10. Haiku is the cheap model and Sonnet the strong one; Opus was not
   chosen. Costs come from the API's `usage` counts, with prompt caching across multi-email runs.
   *Updated 30 Sep 2026: the default cap is $12.*

## 7. Decided in the review (30 Sep 2026)
1. The cap and all costs are in US dollars (section 4).
2. The digest ships as the display card; its email is deferred (A4).
3. Voice stays out of scope for the next two releases (section 2).
4. A document or photo that alone would breach the cap is refused before it is sent (section 4).
5. Tablet-box requests are capped at 300 characters (section 5).
6. Arbor and ParentMail messages come in by email if their notifications carry the content, else
   by screenshot or PDF upload; Huddle never logs in to them (main spec 11.3).

---

## Sources
Checked in the external-sources review, 28 Sep 2026.
- Gmail scopes: <https://developers.google.com/workspace/gmail/api/auth/scopes>
- Gmail `messages.list`:
  <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list>
- Google Calendar events: <https://developers.google.com/workspace/calendar/api/v3/reference/events>
- Google Tasks: <https://developers.google.com/workspace/tasks/reference/rest/v1/tasks>
- Workspace API controls: <https://support.google.com/a/answer/7281227>
- Anthropic pricing: <https://platform.claude.com/docs/en/about-claude/pricing>
- Anthropic data retention: <https://platform.claude.com/docs/en/manage-claude/api-and-data-retention>
