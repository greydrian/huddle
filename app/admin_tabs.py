"""Admin's tabs (spec 10.0) and which tab shows each section.

Admin is one page, /admin?tab=<slug>, that renders only that tab's sections.
Every section keeps its anchor (#sync, #backups, ...), and every redirect back
to Admin goes through admin_url(), which adds the tab that shows the section.
Old links without a tab (/admin#sync, bookmarks) are sent to the right tab by
a small script in settings.html, which reads SECTIONS too.
"""

from urllib.parse import urlencode

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
