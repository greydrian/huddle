"""Shared by the Admin package: the error table every section redirects
through, the registry of per-tab context loaders that page.render_admin()
fills from, and the Google list fetch two tabs need.

Route modules call common.family_today(db) / common.family_timezone(db)
rather than importing them, so a test can patch one name for all of Admin.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi.responses import RedirectResponse

from app import google_oauth, google_tasks, school_email
from app.admin_tabs import admin_url
from app.database import family_timezone, family_today
from app.services import banners, extraction, term_dates

__all__ = [
    "ADMIN_ERRORS",
    "MAX_SCHOOL_YEAR",
    "TAB_CONTEXT",
    "admin_error",
    "family_timezone",
    "family_today",
    "form_error",
    "google_lists",
    "tab_context",
]

MAX_SCHOOL_YEAR = 30  # "Year 4", "Reception", "Year 6 (Oak Class)"

# A form that can't be saved redirects back to its Admin section with
# ?error=<code> (like ?weather_error=), and the page shows that section's
# message. Only these fixed messages are shown, never text from the URL.
ADMIN_ERRORS = {
    "task-missing": ("tasks", "That task no longer exists. It may have been deleted in Google Tasks."),
    "task-unknown-person": ("tasks", "That family member no longer exists."),
    "task-no-list": (
        "tasks",
        "That family member has no Google list linked yet, so the task can't move to "
        "them. Link one under Google & Sync → Task Sync first.",
    ),
    "task-group": ("tasks", "Pick Any time, Morning, After school or Evening."),
    "task-group-times": (
        "tasks",
        "Use times like 12:00, with After school starting after midnight and before Evening. Nothing was changed.",
    ),
    "homework-missing": ("homework", "That homework no longer exists. It may have just been deleted."),
    "words-missing": ("practice-words", "That word list no longer exists. It may have just been deleted."),
    "handwriting-style": ("practice-words", "Pick one of the handwriting styles."),
    "appearance": ("display", "Pick one of the appearance options."),
    "idle-mode": ("idle", "Pick what the wall does when idle, by day and at night."),
    "idle-numbers": (
        "idle",
        "Use whole numbers: an idle delay of 1 to 120 minutes, dimming to 1 to 50% and "
        "5 to 300 seconds a photo. Nothing was changed.",
    ),
    "idle-night": (
        "idle",
        "Give night both a start and an end, like 21:00 and 07:00 (different times), or "
        "leave both blank to follow Appearance. Nothing was changed.",
    ),
    "photos-signin": ("photos", "Signing in to the Photos account didn't finish. Try Choose photos again."),
    "photos-offline": ("photos", "Couldn't reach Google Photos just now. Try again in a minute."),
    "photos-busy": (
        "photos",
        "Photos are being picked or copied right now. Wait for that to finish, or Cancel it first.",
    ),
    "widget-missing": ("widgets", "That widget no longer exists. Nothing was changed."),
    "banner-lead": (
        "banners",
        f"The lead time must be a whole number of minutes from {banners.MIN_LEAD} to "
        f"{banners.MAX_LEAD}. Nothing was changed.",
    ),
    "banner-quiet": (
        "banners",
        "Quiet hours need a start and an end like 21:00 and 07:00, and they can't be the same. Nothing was changed.",
    ),
    "pin-invalid": ("pin", "A PIN must be 4 to 8 digits, numbers only. Your PIN hasn't changed."),
    "backup-failed": ("backups", "The backup didn't complete. Check the logs (docker compose logs)."),
    "pin-weak": (
        "pin",
        "That PIN is too easy to guess: avoid one digit repeated (like 0000) or a "
        "straight run (like 1234 or 9876). Your PIN hasn't changed.",
    ),
    "pin-mismatch": ("pin", "The two PINs didn't match. Your PIN hasn't changed."),
    "profile-missing": ("family", "That family member no longer exists."),
    "profile-year": ("family", f"A year group can be at most {MAX_SCHOOL_YEAR} characters."),
    "profile-email": (
        "family",
        "That email address doesn't look right. Use one address, like "
        "name@example.com, or leave it blank. Nothing was changed.",
    ),
    "avatar-kind": ("family", "Pick Initial, Emoji or Photo."),
    "avatar-emoji": (
        "family",
        "Pick one emoji from the grid, or type exactly one emoji (no letters or spaces). Nothing was changed.",
    ),
    "avatar-no-photo": ("family", "Upload a photo first, then pick Photo."),
    "avatar-empty": ("family", "Choose a photo to upload first."),
    "avatar-too-big": ("family", "That photo is too big: at most 8 MB and 60 megapixels. Try a smaller one."),
    "avatar-bad-type": (
        "family",
        "That file isn't a photo Huddle can read. Use a JPEG, PNG, WebP, GIF or HEIC (iPhone) photo.",
    ),
    "import-empty": ("classroom", "Add a screenshot, a PDF or some pasted text first."),
    "import-too-big": (
        "classroom",
        "That's too big to read: at most 15 MB a file and 22 MB in all. Try a smaller screenshot or fewer pages.",
    ),
    "import-too-many": ("classroom", f"Add at most {extraction.MAX_ATTACHMENTS} files at a time."),
    "import-bad-type": (
        "classroom",
        "That file isn't one the inbox can read. Use a screenshot or photo (PNG, JPEG, WebP or HEIC) or a PDF.",
    ),
    "import-already": ("inbox", "That's already in the School inbox below."),
    "import-missing": (
        "inbox",
        "That item is no longer waiting in the inbox. It may have just been approved or discarded.",
    ),
    "import-event": ("inbox", "Pick the calendar school events go to (School email panel), then approve it."),
    "import-calendar-scope": (
        "inbox",
        "Reconnect to allow adding to your calendar: Disconnect, then Connect "
        "Google Account. The event is still waiting here.",
    ),
    "import-calendar-offline": (
        "inbox",
        "Couldn't reach Google Calendar, so the event wasn't added. It's still waiting here; try again in a minute.",
    ),
    "import-calendar-missing": (
        "inbox",
        "The calendar for school events can't be found any more. Pick another "
        "in the School email panel. The event is still waiting here.",
    ),
    "import-calendar-failed": (
        "inbox",
        "Google Calendar didn't accept the event, so it wasn't added. It's still waiting here.",
    ),
    "school-senders": (
        "school-email",
        f"Each sender must be an email address or *@domain, one per line "
        f"(at most {school_email.MAX_ENTRIES}). Nothing was changed.",
    ),
    "school-schedule": ("school-email", "Pick one of the schedule options and a time like 18:00."),
    "school-calendar": ("school-email", "Pick a calendar this Google account can add events to."),
    "calendar-family": (
        "calendar-options",
        "Pick a calendar that's shown on the wall and that this Google account can add events to. Nothing was changed.",
    ),
    "calendar-view": ("calendar-options", "Pick Month, Week or Agenda."),
    "import-busy": ("inbox", "That's still being read. Try again in a moment."),
    "import-pdf-pages": (
        "classroom",
        f"That PDF has more than {extraction.MAX_PDF_PAGES} pages. Try just the pages you need.",
    ),
    "import-term-none": ("inbox", "Tick at least one period to add, or Discard the term dates."),
    "term-kind": ("term-dates", "Pick what kind of period it is: term, half term, holiday, INSET day or closure."),
    "term-dates": (
        "term-dates",
        "Enter a start and an end date, with the end on or after the start. Nothing was saved.",
    ),
    "term-length": (
        "term-dates",
        "That's too long for its kind (at most: term "
        f"{term_dates.MAX_DAYS['term']} days, half term {term_dates.MAX_DAYS['half_term']}, "
        f"holiday {term_dates.MAX_DAYS['holiday']}, INSET {term_dates.MAX_DAYS['inset']}, "
        f"closure {term_dates.MAX_DAYS['closure']}). Check the dates; nothing was saved.",
    ),
    "term-label": ("term-dates", f"A name can be at most {term_dates.MAX_LABEL} characters. Nothing was saved."),
    "term-overlap": (
        "term-dates",
        "That overlaps a period it can't: only half terms, INSET days and closures "
        "may fall inside a term, and INSET days inside a holiday. Nothing was saved.",
    ),
    "term-missing": ("term-dates", "That period no longer exists. It may have just been deleted."),
}


def admin_error(code: str) -> RedirectResponse:
    """Back to the section the error belongs to, with ?error=<code>."""
    section, _ = ADMIN_ERRORS[code]
    return RedirectResponse(url=admin_url(section, error=code), status_code=303)


def form_error(code: str | None) -> dict | None:
    if code not in ADMIN_ERRORS:
        return None
    section, message = ADMIN_ERRORS[code]
    return {"section": section, "message": message}


# Each tab module registers the loader for its tab's template context:
# async (db, base, extra) -> dict, where `base` is what every tab gets
# (profiles, google_account, ...) and `extra` the *_form / *_error kwargs of
# render_admin after a failed save. Only the requested tab's loader runs.
TabContext = Callable[[Any, dict, dict], Awaitable[dict]]
TAB_CONTEXT: dict[str, TabContext] = {}


def tab_context(tab: str) -> Callable[[TabContext], TabContext]:
    def register(loader: TabContext) -> TabContext:
        TAB_CONTEXT[tab] = loader
        return loader

    return register


async def google_lists(db, google_account: str | None, tasklists: bool = True) -> dict:
    """The Google account's calendars and task lists, for the Google & Sync
    tab (the School tab's calendar picker skips the task lists). Never raises on a Google
    error: google_offline / tasklists_error say what went wrong."""
    found: dict = {
        "available_calendars": [],
        "available_tasklists": [],
        "tasklists_error": False,
        "google_offline": False,
    }
    if not google_account:
        return found
    try:
        access_token = await google_oauth.get_valid_access_token(db)
    except httpx.HTTPError:
        access_token, found["google_offline"] = None, True
    if not access_token:
        return found
    try:
        found["available_calendars"] = await google_oauth.fetch_calendar_list(access_token)
    except httpx.HTTPError:
        found["google_offline"] = True
    if not tasklists:
        return found
    try:
        found["available_tasklists"] = await google_tasks.fetch_tasklists(access_token)
    except httpx.HTTPStatusError as exc:
        # 403 = this account connected before the `tasks` scope
        # existed — settings.html shows a reconnect prompt.
        if exc.response.status_code == 403:
            found["tasklists_error"] = True
        else:
            found["google_offline"] = True
    except httpx.HTTPError:
        found["google_offline"] = True
    return found
