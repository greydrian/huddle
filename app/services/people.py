"""
Who an event title is about (spec 10.5): the one place that matches family
members' names in text, shared by the notification banner (services/
banners.py) and the calendar's person filter (services/calendar_view.py).

- A **"Name: Title"** prefix, as the calendar's quick add saves an event for
  one person ("Alanna: Dentist"), names exactly that person.
- Otherwise, each family member whose **first name appears as a whole word**
  ("Riley's swimming", "Pick up Riley") is named. Case doesn't matter.

Profiles are dicts with at least "name" (and whatever else the caller needs
back, such as "id" and "colour_hex").
"""

import re


def first_name(name: str | None) -> str:
    """"Alanna Smith" -> "Alanna"; "" for a blank name."""
    parts = (name or "").split()
    return parts[0] if parts else ""


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def split_person(title: str, profiles: list[dict]) -> tuple[dict | None, str]:
    """"Alanna: Dentist" -> (Alanna's profile, "Dentist") when the part
    before the first colon is a family member's name (their full name or
    first name); otherwise (None, title), i.e. Everyone."""
    name, sep, rest = (title or "").partition(":")
    if sep and rest.strip() and name.strip():
        for profile in profiles:
            if _same(name, profile["name"]) or _same(name, first_name(profile["name"])):
                return profile, rest.strip()
    return None, title


def _whole_word(word: str) -> re.Pattern:
    # \w-bounded rather than \b, so a name ending in a non-word character
    # still matches, and "Riley's" counts as Riley.
    return re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)


def named_people(title: str, profiles: list[dict]) -> list[dict]:
    """The family members a title is about, in the profiles' order: the
    "Name:" prefix's person alone, else everyone whose first name is a
    whole word in it. Empty = nobody in particular."""
    prefixed, _ = split_person(title, profiles)
    if prefixed is not None:
        return [prefixed]
    found = []
    for profile in profiles:
        word = first_name(profile["name"])
        if word and _whole_word(word).search(title or ""):
            found.append(profile)
    return found
