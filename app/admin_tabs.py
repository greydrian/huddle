"""Admin's tabs (spec 10.0) and which tab shows each section.

Admin is one page, /admin?tab=<slug>, that renders only that tab's sections.
Every section keeps its anchor (#sync, #backups, ...), and every redirect back
to Admin goes through admin_url(), which adds the tab that shows the section.
Old links without a tab (/admin#sync, bookmarks) are sent to the right tab by
a small script in settings.html, which reads SECTIONS too.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit

TABS = {
    "family": "Family",
    "school": "School",
    "display": "Display",
    "google": "Google & Sync",
    "assistant": "Assistant",
    "system": "System",
}
DEFAULT_TAB = "family"

# Every section anchor on the Admin page -> the tab that shows it.
# tests/test_admin_tabs.py checks each one is rendered on its tab.
SECTIONS = {
    "family": "family",
    "tasks": "family",
    "homework": "family",
    "practice-words": "family",
    "classroom": "school",
    "inbox": "school",
    "school-email": "school",
    "weather": "display",
    "display": "display",  # Appearance (its anchor predates the tabs)
    "keyboard": "display",
    "device": "display",
    "google": "google",
    "calendars": "google",
    "task-lists": "google",
    "sync": "google",
    "assistant": "assistant",
    "backups": "system",
    "pin": "system",
}


def admin_url(section: str, **params: str) -> str:
    """/admin?tab=<its tab>[&params]#section. Raises KeyError for an unknown
    section, so a typo fails its test rather than landing on the wrong tab."""
    query = urlencode({"tab": SECTIONS[section], **params})
    return f"/admin?{query}#{section}"


def login_next(path: str, tab: str | None) -> str | None:
    """The `next` the login page carries for a signed-out visit to Admin:
    only /admin, with its tab if it's a known one."""
    if path != "/admin":
        return None
    return f"/admin?tab={tab}" if tab in TABS else "/admin"


def login_return(next_url: str | None, section: str | None = None) -> str:
    """Where a successful login goes. `next` counts only as /admin with at
    most ?tab=<known tab>; `section` (the page's #hash, which the login form
    posts) only if it's a known section on that tab. Anything else, such as
    another host or path, falls back to /admin: never an open redirect."""
    tab = None
    if next_url:
        parts = urlsplit(next_url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        valid = (not parts.scheme and not parts.netloc and parts.path == "/admin" and not parts.fragment
                 and len(query) <= 1 and all(k == "tab" and v in TABS for k, v in query))
        if not valid:
            return "/admin"
        tab = query[0][1] if query else None
    if section and section in SECTIONS and (tab is None or SECTIONS[section] == tab):
        return admin_url(section)
    return f"/admin?tab={tab}" if tab else "/admin"
