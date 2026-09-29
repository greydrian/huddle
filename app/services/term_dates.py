"""
School term dates (spec 10.6): the periods a parent enters in Admin or
approves from the School inbox, plus the GOV.UK bank holidays
(app/bank_holidays.py keeps that table fresh).

One set per household (Gresham's dates are whole-school). A period is
inclusive at both ends. They answer "is this a school day?" for the
"school days" repeat option (10.4) and the "school days only" widget option
(10.3), and they appear on the calendar as local-only bars (never written
to Google).

School years run 1 September to 31 August and are labelled "2026–27".
When a date's school year has no term at all, school days fall back to
Monday–Friday minus bank holidays, and Admin warns that the year is missing.
"""

from datetime import date

KINDS = {
    "term": "Term",
    "half_term": "Half term",
    "holiday": "Holiday",
    "inset": "INSET day",
    "closure": "School closed",
}
SOURCES = ("manual", "import")
# Longest sensible period of each kind, in days (inclusive). A 15-week
# autumn term is about 110 days; the summer holiday about 45.
MAX_DAYS = {"term": 120, "half_term": 16, "holiday": 60, "inset": 2, "closure": 14}
# What may overlap a term: things that happen inside one. Anything else
# overlapping is a clash (two terms, a holiday across a term, an INSET day
# in a holiday...).
INSIDE_TERM = frozenset({"half_term", "inset", "closure"})
MAX_LABEL = 80
EARLIEST = date(2000, 1, 1)
LATEST = date(2099, 12, 31)

FALLBACK = "fallback"      # school_day_status source: Mon–Fri minus bank holidays
TERM_DATES = "term_dates"  # school_day_status source: the term dates decided it


class PeriodError(ValueError):
    """A period that can't be saved; `code` is an admin.ADMIN_ERRORS key."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- School years ---

def school_year_of(day: date) -> int:
    """The calendar year a school year starts in: 2026 for 2026–27."""
    return day.year if day.month >= 9 else day.year - 1


def school_year_label(start_year: int) -> str:
    return f"{start_year}–{(start_year + 1) % 100:02d}"


def school_year_span(start_year: int) -> tuple[date, date]:
    return date(start_year, 9, 1), date(start_year + 1, 8, 31)


# --- Validation ---

def _parse_date(value) -> date:
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise PeriodError("term-dates") from None
    if not EARLIEST <= parsed <= LATEST:
        raise PeriodError("term-dates")
    return parsed


def clean_period(kind, start_date, end_date, label="") -> dict:
    """A validated period {kind, start_date, end_date, label} (ISO dates), or
    PeriodError: an unknown kind, a missing or unreal date, end before start,
    too long for its kind, or too long a label. A blank label becomes the
    kind's own name."""
    kind = str(kind or "").strip()
    if kind not in KINDS:
        raise PeriodError("term-kind")
    start, end = _parse_date(start_date), _parse_date(end_date)
    if end < start:
        raise PeriodError("term-dates")
    if (end - start).days + 1 > MAX_DAYS[kind]:
        raise PeriodError("term-length")
    text = " ".join(str(label or "").split())
    if len(text) > MAX_LABEL:
        raise PeriodError("term-label")
    return {"kind": kind, "start_date": start.isoformat(), "end_date": end.isoformat(), "label": text or KINDS[kind]}


def overlap_allowed(kind_a: str, kind_b: str) -> bool:
    return (kind_a == "term" and kind_b in INSIDE_TERM) or (kind_b == "term" and kind_a in INSIDE_TERM)


def _overlaps(a: dict, b: dict) -> bool:
    return a["start_date"] <= b["end_date"] and b["start_date"] <= a["end_date"]


def same_period(a: dict, b: dict) -> bool:
    """Identical dates and kind (the label doesn't matter): "already added"."""
    return (a["kind"], a["start_date"], a["end_date"]) == (b["kind"], b["start_date"], b["end_date"])


def clash(period: dict, others) -> dict | None:
    """The first of `others` that `period` overlaps where it shouldn't."""
    return next(
        (o for o in others if _overlaps(period, o) and not overlap_allowed(period["kind"], o["kind"])), None
    )


# --- Storage ---

async def list_periods(db) -> list[dict]:
    return [dict(r) for r in await (await db.execute(
        "SELECT * FROM school_periods ORDER BY start_date, end_date, id"
    )).fetchall()]


async def get_period(db, period_id: int) -> dict | None:
    row = await (await db.execute("SELECT * FROM school_periods WHERE id = ?", (period_id,))).fetchone()
    return dict(row) if row else None


async def _check_clash(db, period: dict, exclude_id: int | None = None):
    others = [p for p in await list_periods(db) if p["id"] != exclude_id]
    if clash(period, others):
        raise PeriodError("term-overlap")


async def insert_period(db, period: dict, source: str) -> int:
    """Inserts a clean_period() result. No commit: callers own the transaction."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    cursor = await db.execute(
        "INSERT INTO school_periods (kind, start_date, end_date, label, source) VALUES (?, ?, ?, ?, ?)",
        (period["kind"], period["start_date"], period["end_date"], period["label"], source),
    )
    return cursor.lastrowid


async def add_period(db, kind, start_date, end_date, label="") -> int:
    """Admin's add form. Raises PeriodError (nothing saved)."""
    period = clean_period(kind, start_date, end_date, label)
    await _check_clash(db, period)
    period_id = await insert_period(db, period, "manual")
    await db.commit()
    return period_id


async def update_period(db, period_id: int, kind, start_date, end_date, label="") -> None:
    """Admin's edit form: the source is kept. Raises PeriodError."""
    if await get_period(db, period_id) is None:
        raise PeriodError("term-missing")
    period = clean_period(kind, start_date, end_date, label)
    await _check_clash(db, period, exclude_id=period_id)
    await db.execute(
        """UPDATE school_periods SET kind = ?, start_date = ?, end_date = ?, label = ?,
               updated_at = datetime('now') WHERE id = ?""",
        (period["kind"], period["start_date"], period["end_date"], period["label"], period_id),
    )
    await db.commit()


async def delete_period(db, period_id: int) -> None:
    await db.execute("DELETE FROM school_periods WHERE id = ?", (period_id,))
    await db.commit()


def group_by_school_year(periods: list[dict]) -> list[dict]:
    """[{"year": 2026, "label": "2026–27", "periods": [...]}], oldest first.
    A period sits in the school year it starts in."""
    groups: dict[int, list[dict]] = {}
    for period in periods:
        groups.setdefault(school_year_of(date.fromisoformat(period["start_date"])), []).append(period)
    return [{"year": y, "label": school_year_label(y), "periods": groups[y]} for y in sorted(groups)]


# --- Questions ---

async def has_terms(db, start_year: int) -> bool:
    """Whether any term falls in that school year."""
    first, last = school_year_span(start_year)
    row = await (await db.execute(
        "SELECT 1 FROM school_periods WHERE kind = 'term' AND start_date <= ? AND end_date >= ? LIMIT 1",
        (last.isoformat(), first.isoformat()),
    )).fetchone()
    return row is not None


async def is_bank_holiday(db, day: date) -> bool:
    row = await (await db.execute("SELECT 1 FROM bank_holidays WHERE date = ?", (day.isoformat(),))).fetchone()
    return row is not None


async def school_day_status(db, day: date) -> tuple[bool, str]:
    """(is it a school day, how that was decided). Source TERM_DATES: the
    day's school year has term dates, and it's a school day when it's a
    weekday inside a term, not a bank holiday, and not in a half term,
    holiday, INSET day or closure. Source FALLBACK: that year has no term
    dates, so any weekday that isn't a bank holiday counts."""
    weekday = day.weekday() < 5
    if not await has_terms(db, school_year_of(day)):
        return weekday and not await is_bank_holiday(db, day), FALLBACK
    if not weekday or await is_bank_holiday(db, day):
        return False, TERM_DATES
    iso = day.isoformat()
    kinds = {r["kind"] for r in await (await db.execute(
        "SELECT kind FROM school_periods WHERE start_date <= ? AND end_date >= ?", (iso, iso)
    )).fetchall()}
    return "term" in kinds and not (kinds - {"term"}), TERM_DATES


async def is_school_day(db, day: date) -> bool:
    return (await school_day_status(db, day))[0]


# The next school year's dates are only asked for once they're needed:
# from May, when schools have published them and September is close.
NEXT_YEAR_WARNING_MONTH = 5


async def missing_years(db, today: date) -> list[str]:
    """Labels of the school years Admin should warn about: the current one
    if it has no term dates, and (from May) the next one too."""
    current = school_year_of(today)
    years = [current]
    if NEXT_YEAR_WARNING_MONTH <= today.month < 9:
        years.append(current + 1)
    return [school_year_label(y) for y in years if not await has_terms(db, y)]


async def periods_between(db, start: date, end: date) -> list[dict]:
    """Everything touching [start, end] inclusive, for the calendar: the
    school periods, then each bank holiday as a one-day period of kind
    "bank_holiday" (source "bank_holiday")."""
    first, last = start.isoformat(), end.isoformat()
    periods = [dict(r) for r in await (await db.execute(
        """SELECT id, kind, start_date, end_date, label, source FROM school_periods
           WHERE start_date <= ? AND end_date >= ? ORDER BY start_date, id""",
        (last, first),
    )).fetchall()]
    for row in await (await db.execute(
        "SELECT date, title FROM bank_holidays WHERE date BETWEEN ? AND ? ORDER BY date", (first, last)
    )).fetchall():
        periods.append({"id": None, "kind": "bank_holiday", "start_date": row["date"], "end_date": row["date"],
                        "label": row["title"], "source": "bank_holiday"})
    return periods

