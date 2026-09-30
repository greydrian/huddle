#!/usr/bin/env bash
# Deploy a release to the live family display on the G10.
#
#   scripts/deploy.sh v1.2.0        # a tag (the normal case)
#   scripts/deploy.sh main          # a branch, e.g. to test a fix before tagging
#   scripts/deploy.sh --list        # the newest tags on origin
#
# It does what the README's "Deploying to the G10" section does by hand, in
# order, and stops at the first problem:
#   1. refuses to run with local changes in the checkout, or without .env
#   2. fetches origin and checks out the requested tag or branch
#   3. takes a backup through the running app (Admin's "Back up now"), so a
#      rollback always has a fresh copy; the app also takes its own verified
#      snapshot at startup whenever database migrations are pending
#   4. rebuilds the image and restarts the container in PRODUCTION mode
#      (docker-compose.yml only: no --reload, no bind-mounted app/)
#   5. waits for /health, and shows the log and the rollback command if the
#      app doesn't come up (a failed migration leaves the database untouched,
#      so rolling back is just deploying the previous ref again)
#
# Run it from the G10 as the user that runs docker (or with sudo).
# DRY_RUN=1 prints the commands instead of running them.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.yml)
SERVICE=family-display
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
HEALTH_WAIT_SECONDS="${HEALTH_WAIT_SECONDS:-90}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }
run() {
  if [ "${DRY_RUN:-0}" = "1" ]; then
    printf '  (dry run) %s\n' "$*"
  else
    "$@"
  fi
}

REF="${1:-}"
case "$REF" in
  "" | -h | --help)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  --list)
    git fetch --quiet --tags origin
    git tag --list 'v*' --sort=-version:refname | head -n 10
    exit 0
    ;;
esac

# --- 1. Preconditions -------------------------------------------------------
say "Checking the checkout"
command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose (v2) is not available"
[ -f .env ] || die ".env is missing: copy .env.example and fill it in (README, Running with Docker)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no
  die "the checkout has local changes; commit, stash or discard them first"
fi
PREVIOUS="$(git rev-parse --short HEAD)"
PREVIOUS_NAME="$(git describe --tags --always 2>/dev/null || echo "$PREVIOUS")"
echo "Currently deployed: $PREVIOUS_NAME ($PREVIOUS)"

# --- 2. Fetch and check out the release -------------------------------------
say "Fetching origin and checking out $REF"
run git fetch --quiet --tags origin
if git show-ref --verify --quiet "refs/tags/$REF"; then
  run git checkout --quiet --detach "refs/tags/$REF"
elif git show-ref --verify --quiet "refs/remotes/origin/$REF"; then
  run git checkout --quiet --detach "refs/remotes/origin/$REF"
else
  die "no tag or origin branch called '$REF' (scripts/deploy.sh --list shows the newest tags)"
fi
echo "Deploying: $(git describe --tags --always) ($(git rev-parse --short HEAD))"

# --- 3. Backup through the running app --------------------------------------
say "Backing up the database through the running app"
if "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx "$SERVICE"; then
  run "${COMPOSE[@]}" exec -T "$SERVICE" python -c \
    'import asyncio, sys; from app import backup; sys.exit(0 if asyncio.run(backup.create_backup()) else 1)' \
    || die "the backup failed (see the app log); not deploying without one"
  echo "Backup written to data/family-display/backups/"
else
  echo "The app is not running, so no backup could be taken now."
  echo "If the database has pending migrations the app snapshots it itself at startup."
fi

# --- 4. Build and restart ---------------------------------------------------
say "Building the image and restarting the container (production mode)"
run "${COMPOSE[@]}" up -d --build

# --- 5. Wait for /health ----------------------------------------------------
say "Waiting up to ${HEALTH_WAIT_SECONDS}s for $HEALTH_URL"
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "  (dry run) skipped"
  exit 0
fi
for ((i = 1; i <= HEALTH_WAIT_SECONDS; i++)); do
  if curl -sf --max-time 3 "$HEALTH_URL" >/dev/null; then
    echo "Healthy after ${i}s."
    if "${COMPOSE[@]}" logs --since 10m "$SERVICE" 2>/dev/null | grep -q "Snapshot .* taken before migration"; then
      echo "The app took a pre-migration snapshot; it is in data/family-display/backups/."
    fi
    say "Deployed $(git describe --tags --always). Previous: $PREVIOUS_NAME."
    exit 0
  fi
  sleep 1
done

echo
echo "The app did not answer /health. Last 60 log lines:"
"${COMPOSE[@]}" logs --tail 60 "$SERVICE" || true
echo
if "${COMPOSE[@]}" logs --tail 200 "$SERVICE" 2>/dev/null | grep -q MigrationError; then
  echo "A database migration (or its snapshot) failed. The database is unchanged."
fi
echo "To go back to what was running before:"
echo "    scripts/deploy.sh $PREVIOUS_NAME"
echo "(README: \"If the app won't start after an update\")"
exit 1
