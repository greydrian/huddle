"""
Which days a recurring task appears on.

Stored in tasks.recurrence_rule as canonical "Mon,Wed,Fri"; NULL (or a
rule covering all seven days) means every day. Parsing is tolerant of the
older free-text admin input ("mon wed", "Monday, Friday", "weekdays") so
rules typed before the day-checkbox UI still work.
"""

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # index == date.weekday()

_ALIASES = {
    "weekdays": {0, 1, 2, 3, 4},
    "weekends": {5, 6},
    "daily": set(range(7)),
    "everyday": set(range(7)),
}


def parse_rule(rule: str | None) -> set[int] | None:
    """Weekday numbers (Mon=0) a rule covers, or None for every day.
    Unrecognised words are ignored; a rule with no recognisable day falls
    back to every day rather than silently hiding the task forever."""
    if not rule:
        return None
    days: set[int] = set()
    for token in rule.replace(",", " ").split():
        word = token.strip().lower()
        if word in _ALIASES:
            days |= _ALIASES[word]
        elif len(word) >= 3 and word[:3].capitalize() in WEEKDAYS:
            days.add(WEEKDAYS.index(word[:3].capitalize()))
    if not days or len(days) == 7:
        return None
    return days


def normalize_rule(days: list[str]) -> str | None:
    """Canonical storage form from checkbox values; None means every day."""
    parsed = parse_rule(" ".join(days))
    if parsed is None:
        return None
    return ",".join(WEEKDAYS[i] for i in sorted(parsed))


def is_due(rule: str | None, weekday: int) -> bool:
    days = parse_rule(rule)
    return days is None or weekday in days


def describe(rule: str | None) -> str:
    """Human label for Admin: 'every day', 'weekdays', 'Mon, Wed, Fri'."""
    days = parse_rule(rule)
    if days is None:
        return "every day"
    if days == _ALIASES["weekdays"]:
        return "weekdays"
    if days == _ALIASES["weekends"]:
        return "weekends"
    return ", ".join(WEEKDAYS[i] for i in sorted(days))
