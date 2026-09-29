"""
Which days a recurring task appears on.

Stored in tasks.recurrence_rule as canonical "Mon,Wed,Fri"; NULL (or a
rule covering all seven days) means every day. Parsing is tolerant of the
older free-text admin input ("mon wed", "Monday, Friday", "weekdays") so
rules typed before the day-checkbox UI still work.

"school" (SCHOOL_DAYS) is its own rule: due on school days only, as
services/term_dates.is_school_day() decides (term dates, else Mon–Fri
minus bank holidays). That needs the database, so callers pass the answer
in; this module stays pure.
"""

SCHOOL_DAYS = "school"

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # index == date.weekday()

_ALIASES = {
    "weekdays": {0, 1, 2, 3, 4},
    "weekends": {5, 6},
    "daily": set(range(7)),
    "everyday": set(range(7)),
}


def is_school_days(rule: str | None) -> bool:
    return rule is not None and rule.strip().lower().replace(" ", "") in ("school", "schooldays")


def parse_rule(rule: str | None) -> set[int] | None:
    """Weekday numbers (Mon=0) a rule covers, or None for every day.
    Unrecognised words are ignored; a rule with no recognisable day falls
    back to every day rather than silently hiding the task forever. The
    school-days rule parses as Mon–Fri, the days it can ever fall on."""
    if not rule:
        return None
    if is_school_days(rule):
        return set(_ALIASES["weekdays"])
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
    """Canonical storage form from checkbox values; None means every day.
    "school" among them wins over any day chips."""
    if any(is_school_days(d) for d in days):
        return SCHOOL_DAYS
    parsed = parse_rule(" ".join(days))
    if parsed is None:
        return None
    return ",".join(WEEKDAYS[i] for i in sorted(parsed))


def is_due(rule: str | None, weekday: int, school_day: bool | None = None) -> bool:
    """`school_day` answers the school-days rule (term_dates.is_school_day
    for the same date); without it, that rule falls back to Mon–Fri."""
    if is_school_days(rule) and school_day is not None:
        return school_day
    days = parse_rule(rule)
    return days is None or weekday in days


def describe(rule: str | None) -> str:
    """Human label for Admin: 'every day', 'weekdays', 'Mon, Wed, Fri'."""
    if is_school_days(rule):
        return "school days"
    days = parse_rule(rule)
    if days is None:
        return "every day"
    if days == _ALIASES["weekdays"]:
        return "weekdays"
    if days == _ALIASES["weekends"]:
        return "weekends"
    return ", ".join(WEEKDAYS[i] for i in sorted(days))
