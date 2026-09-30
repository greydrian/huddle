# GitHub settings (done by hand, once)

Repository settings can't be changed from a PR, so these steps are done in
the browser at https://github.com/greydrian/huddle/settings. Everything here
is free for a public repository. They were written on 30 Sep 2026 alongside
the CI hardening PRs (#53 to #57); do them in this order, and step 3 after
the first CI run of #53, so its new check names exist to pick from. Keep this
file current when a setting changes: it is the record of how the repository
is configured.

## 1. Merge behaviour (Settings → General → Pull Requests)

- **Allow squash merging**: on. "Default commit message": *Pull request
  title and description*.
- **Allow merge commits** and **Allow rebase merging**: off. Squash is what
  the history already uses; one commit per PR keeps `git bisect` and the
  generated release notes clean.
- **Automatically delete head branches**: on. This is what stops merged
  branches piling up again.
- Optional: **Always suggest updating pull request branches**: on.

## 2. Delete the stale branches (once)

Every remote branch other than `main` (and the `ccr-…` branches of the open
PRs) belongs to a PR that is merged or closed. From any clone:

```bash
git fetch --prune origin
git push origin --delete \
  chore/py314-target cleanup/leftovers-docs cleanup/templates-admin \
  docs/assistant-spec docs/production-deploy docs/school-import-setup \
  docs/spec-review-updates docs/spec-v1 docs/tablet-choice docs/v1.1-setup \
  feature/admin-tabs feature/admin-task-schedule feature/avatars feature/backups \
  feature/calendar-v12 feature/calm-redesign feature/fresh-wall feature/hide-done-tasks \
  feature/homework-extras feature/homework-inbox feature/homework-words feature/idle-photos \
  feature/notification-banner feature/pen-test feature/pin-hardening \
  feature/school-gmail-import feature/sync-health feature/task-improvements \
  feature/term-dates feature/widget-visibility fix/admin-scroll fix/dashboard-scroll \
  fix/input-validation fix/static-cache fix/time-dependent-tests \
  platform/migrations platform/tooling refactor/google-clients-logging \
  refactor/services-and-widget-registry test/coverage-gaps
```

39 of these were squash-merged (PRs #3 to #51). `fix/time-dependent-tests`
is PR #52, closed without merging: the same two tests were fixed another
way inside #49, so nothing on it is needed. The Branches page (Code →
Branches → "Stale") has a bin icon per branch if you'd rather not bulk
delete.

## 3. Branch ruleset for `main` (Settings → Rules → Rulesets → New branch ruleset)

- **Name**: `main`. **Enforcement status**: Active.
- **Bypass list**: empty. In an emergency, set the ruleset to "Disabled" for
  a minute; that is a deliberate act with an audit entry, which beats a
  standing bypass.
- **Target branches**: Add target → *Include default branch*.
- **Rules**:
  - *Restrict deletions*
  - *Require linear history*
  - *Require a pull request before merging*, required approvals **0** (a
    single committer can't approve their own PR; requiring one would block
    every merge). Tick *Dismiss stale pull request approvals when new
    commits are pushed* and *Require conversation resolution before merging*.
  - *Require status checks to pass*, with *Require branches to be up to date
    before merging* ticked (each merge then needs one "Update branch" click
    and a CI run, so the code that lands is the code that was tested). Add
    these checks, all from GitHub Actions:
    `test`, `typecheck`, `lock`, `conflict-markers`, `static`, `e2e`,
    `docker`, `pip-audit`. Once CodeQL (step 4) has run, add `CodeQL` too.
  - *Block force pushes*
- Leave *Require signed commits*, *Require deployments to succeed* and
  *Require code scanning results* off.

Stacked PRs (one based on another) merge in order: after each merge GitHub
retargets the next PR to `main`; press "Update branch" on it, let CI run,
merge.

## 4. Security (Settings → Advanced Security; "Code security" on some accounts)

- **Dependency graph**: on.
- **Dependabot alerts**: on. **Dependabot security updates**: on. These are
  separate from the weekly version-update PRs in `.github/dependabot.yml`:
  an advisory against a pinned package opens a PR the same day instead of
  waiting for Monday. The weekly `pip-audit` workflow catches the same
  advisories from the CI side.
- **Secret scanning**: on. **Push protection**: on. A push containing a key
  matching a known provider pattern (a Google client secret, an Anthropic
  key) is refused before it lands. The history was scanned before this was
  written and holds only the two fake test keys, so nothing needs rotating.
- **Code scanning → CodeQL analysis**: *Set up → Default*. Languages: Python,
  JavaScript/TypeScript, GitHub Actions. It runs on PRs and weekly and posts
  under Security → Code scanning.

## 5. Actions (Settings → Actions → General)

- **Actions permissions**: "Allow greydrian, and select non-greydrian,
  actions and reusable workflows", with "Allow actions created by GitHub"
  ticked. The workflows use only `actions/*`.
- **Workflow permissions**: *Read repository contents and packages
  permissions*. Both workflows set `permissions: contents: read` themselves;
  this covers any workflow added later.
- Leave the fork pull request approval settings as they are.

## 6. Tags and releases

Once the five PRs are merged, from an up-to-date `main`:

```bash
git checkout main && git pull
# v1.1 retrospectively, at the commit that closed it (the v1.1 setup checklist)
git tag -a v1.1.0 d47ef44 -m "v1.1.0: admin tabs, term dates, widget visibility, tasks, banner, idle screen and photos"
# what main is now: v1.2 features so far, on the hardened platform
git tag -a v1.2.0-beta.1 -m "v1.2.0-beta.1: avatars, homework extras, calendar views; CI hardening, deploy script, ruff format, Admin split"
git push origin v1.1.0 v1.2.0-beta.1
```

Then Releases → *Draft a new release* → choose the tag → *Generate release
notes* → tick *Set as a pre-release* for the beta → Publish. Deploy with
`scripts/deploy.sh v1.2.0-beta.1` on the G10 (README, "Deploying to the
G10"). The README's "Releases" section has the numbering rules.

Optional but cheap: Settings → Rules → Rulesets → **New tag ruleset**,
target `v*`, rules *Restrict deletions* and *Restrict updates*. The deploy
script checks out tags by name, so a moved tag would quietly change what a
version means.

## 7. Nothing to do

- `dependabot.yml`, both workflows, `app/static/vendor/SHA256SUMS` and
  `scripts/deploy.sh` are in the repository.
- The repository is public, so Actions minutes are not metered.
