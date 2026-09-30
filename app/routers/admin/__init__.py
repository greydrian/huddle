"""
Admin / Parent Controls (spec 4.7; tabs, spec 10.0): one module per tab,
so a change to one tab touches one file.

    page.py       GET /admin: the shared context, plus the tab's loader
    login.py      PIN page, lockout, log out, forced new PIN, Change PIN
    family.py     family members, avatars, task schedules, homework, practice words
    school.py     School inbox, term dates, school email
    display.py    widgets, weather, appearance, banners, keyboard
    google.py     calendar options, Google Tasks list links
    assistant.py  the Assistant tab's context
    system.py     backups
    common.py     ADMIN_ERRORS, admin_error(), the tab-context registry

Each module has its own router (prefix /admin), included here; each tab
module registers its context loader with common.tab_context. Every section
redirects back through admin_tabs.admin_url(). Older callers and tests
import SESSION_COOKIE, require_admin and ADMIN_ERRORS from here.
"""

from fastapi import APIRouter

from app.auth import SESSION_COOKIE, require_admin
from app.routers.admin import assistant, display, family, google, login, page, school, system
from app.routers.admin.common import ADMIN_ERRORS

__all__ = ["ADMIN_ERRORS", "SESSION_COOKIE", "require_admin", "router"]

router = APIRouter()
for _module in (page, login, family, school, display, google, assistant, system):
    router.include_router(_module.router)
