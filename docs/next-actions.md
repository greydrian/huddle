# Next actions

Where work paused on 1 October 2026, for the next session (human or Claude). Read this with
`family-display-spec.md` section 11 (the 30 Sep 2026 review decisions) and `CLAUDE.md`.

## Done and merged (deploy with `scripts/deploy.sh` once tagged)

Spec 11 groups 1–3, each reviewed by the `code-reviewer` agent and merged one at a time:

- Reload the wall after a deploy (build id in `/api/rev`).
- Night mode follows sunset and sunrise, with a logged 19:00–07:00 fallback (spec 11.4).
- "Next up" strip and countdowns under the banners (spec 11.8).
- Schools per child: per-school term dates and senders (spec 11.2, migration 10).
- Shopping list quantities, aisles and a grouped view (spec 10.8, migration 11).
- Meals: favourites and recipe cards (spec 10.7, migration 12).

After deploying, check **Admin → Family**: the Year 4 child should show Gresham as their school and
the younger child "No school" (migration 10 linked everyone with a year group).

## Next: assistant stage A1 (paused on purpose)

Spec: `family-display-spec.md` (assistant section) and the assistant spec v0.3. Stage A1 is:

1. **Gmail "Huddle" label**: anything the family labels `Huddle` in Gmail is read like a school
   email (same SENDCo exclusion, caps and log rules as `school_email.py`).
2. **Phone upload page**: a PIN-protected page to photograph a letter on a phone and send it to the
   School inbox (reuse `imports.ingest` and `upload_guard`).
3. **Letter extraction**: what's already in `services/extraction.py`, extended for A1's kinds.
4. **Arbor and ParentMail** (spec 11.3): first forward one real notification email of each to see
   whether it carries the letter or only a link. If it carries the content, add the sender to
   Gresham's senders (Admin → School → Schools); if only a link, the route is the phone upload page.

Open questions for the family before starting A1:

- Are the other Google accounts inside Google Workspace (affects multi-account calendars, 11.1)?
- Forward one Arbor and one ParentMail notification email.

## Later / optional (spec 11, group 5)

Notes board, bin day, weather warnings, birthdays, chore rotation; multi-account calendar code only
if needed; Home Assistant items once the separate HA project exists. HTTPS setup instructions
(`docs/https-setup.md`) when HTTPS is built.

## Housekeeping the family does by hand

- GitHub settings in `docs/github-settings.md` (branch protection, etc.) if not done yet.
- Push release tags (this environment can't push tags): see README "Releases".
- Delete merged `ccr-fad4cae3-n8q7ce*` branches on GitHub.
