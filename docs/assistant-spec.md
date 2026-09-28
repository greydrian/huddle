# Huddle Assistant: specification

**Status:** v0.1, agreed in the spec review on 28 September 2026. Companion to
`family-display-spec.md`. That document describes the app. This one covers everything the app does
with an AI model (Claude, via the Anthropic API): what it may do, what it may read, and the rules
every assistant feature follows.

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
4. **Send the minimum.**
   - About the children: **first names and year groups only**.
   - Only mail from allowed sources, or mail a parent has labelled for Huddle, is ever read.
   - **`sen@gresham.croydon.sch.uk` (SENDCo) is never read or sent automatically.** The only
     exception is an email a parent deliberately labels "Huddle" (see A1).
   - Where an answer needs family data, such as Q&A or the digest, only the slice that's needed is
     sent (e.g. the next 14 days of event titles and times), never the whole database.
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
| **Phone share** | Pair each phone once by scanning a QR code shown in Admin. The phone can then upload photos, screenshots or PDFs to a small upload page on the home network, without the PIN. Pairings are listed in Admin and can be revoked. | A parent, in the Admin inbox |
| **Admin** | Existing "Add from Classroom" upload and paste, generalised to any document. | A parent, in the Admin inbox |
| **Scheduled** | Daily school email (built), and the weekly digest. | The digest is read-only |

**Voice (future).** Voice is out of scope for now. The tablet box is designed so that
speech-to-text can feed it later, with nothing else changing.

## 3. Capabilities

Build order (decided): **A1 → A2 → A3 → A4 → A5**.

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
parent's phone and on the display. The parent is picked when approving.

New surfaces delivered with A1: **phone pairing** and the **Gmail "Huddle" label**.

**The SENDCo exception (decided).** An email a parent deliberately labels "Huddle" **is read even
if it's from, or mentions, the SENDCo address**. Applying the label is the parent's explicit choice.
- The automatic daily school-email check still never fetches SENDCo mail.
- Labelled SENDCo mail follows every other rule: quoted history is stripped, nothing is logged, and
  only the approved results and a short quote are kept.

### A2. Quick add in plain words (tablet)
- Type "Dentist Tuesday 4pm for Alanna", "Milk, eggs, bread" or "Alanna tidy room every Saturday
  morning".
- The assistant turns it into the right items: a Family-calendar event, shopping items, or a task
  with a person, a time-of-day group and repeat days. Each item is shown on a confirm card.
- It uses the **cheaper, faster model**. What's sent is the typed text, today's date, family first
  names, and the names of the Family calendar and task lists.

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
- Delivered as:
  - **a card on the display**, shown until dismissed
  - **an email to both parents** (addresses set in Admin). This needs the **Gmail send**
    permission, which is fine for an Internal app, and a Google reconnect.
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

## 4. Budget, models and the activity log
- **A monthly spending cap set in Admin, in pounds (£).** When it's reached, the assistant pauses
  and the tablet and Admin say so. Manual features still work.
  - Existing safety limits stay underneath it: 25 emails per check, and 60 documents a day.
  - **All costs are shown in pounds**: the cap, each log entry, and the monthly total. Anthropic
    bills in US dollars, so Huddle converts at an exchange rate set in Admin (defaulting to a
    sensible current rate). The cap is enforced in pounds, which makes it approximate by the
    exchange-rate difference.
- **The model is chosen per task automatically:**
  - a cheaper, faster model for quick add and Q&A
  - the stronger model for PDFs, photos, the digest and meal planning

  Either can be overridden in Admin.
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
  could create by hand anyway (Family-calendar events, tasks, shopping), and it's rate-limited to
  stop runaway costs.
- **Scopes added over time:**
  - `gmail.readonly`: already granted; also covers the label
  - `gmail.send`: for the digest (A4)
  - `tasks`: already granted; parent tasks go through it

  Each is gated by the scopes actually granted, as today.

## 6. Decided in the review (28 Sep 2026)
1. A parent-applied "Huddle" label overrides the SENDCo exclusion (see A1). The automatic school
   check never does.
2. Budget and costs are shown and capped in pounds, converted at an exchange rate set in Admin.
3. Answers show for 30 seconds by default, changeable, and can be dismissed. Spoken answers are
   part of the future voice work.
