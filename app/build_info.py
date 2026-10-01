"""Which build of Huddle is running, so the wall can reload itself after a
deploy (spec 11 / review 3.3).

The kiosk page is long-lived: widgets re-fetch their fragments through
/api/rev, and static files are revalidated on every load, but the page shell
(base.html, dashboard.html and the scripts they load) only changes on a full
reload. /api/rev returns BUILD_ID; static/js/fresh.js reloads the page, once
nobody is using it, when that differs from the id the page was served with.

The id is a hash of the app's own files (code, templates, static), computed
once at startup (main.py's lifespan; build_id() caches it). A file that
can't be read counts as a marker rather than failing /health or /api/rev. So it changes exactly when a deploy changes what the page
could look like, needs no git or build argument, and a plain restart of the
same image keeps it, so the wall doesn't reload for nothing.
"""

import hashlib
from functools import cache
from pathlib import Path

APP_DIR = Path(__file__).parent
# Generated or runtime files that aren't part of a build.
SKIPPED_DIRS = {"__pycache__"}


def compute(root: Path = APP_DIR) -> str:
    """A short hash of every file under `root` (paths and contents), in a
    stable order, so the same tree always gives the same id."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        if SKIPPED_DIRS.intersection(relative.parts) or path.suffix == ".pyc":
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:  # vanished or unreadable (an editor's temp file on bare metal)
            digest.update(b"\1unreadable")
        digest.update(b"\0")
    return digest.hexdigest()[:12]


@cache
def build_id() -> str:
    return compute()
