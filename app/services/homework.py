"""
Homework, the reading log and handwriting practice words (entered in Admin
or approved from the School inbox). Nothing here is synced to Google and
there are no points or rewards (spec 10.10).

"Today" is always family_today(): due-date labels, when done homework
drops off, which word lists are active, and which day a practice or a
night's reading counts for.
"""

import re
from datetime import date, datetime, timedelta

from app import avatars
from app.database import family_timezone, family_today, get_setting
from app.services import term_dates

HANDWRITING_SETTING = "handwriting_style"
# value -> Admin label. Playwrite GB S is "Playwrite England SemiJoined" and
# GB J "Playwrite England Joined"; Google has no fully unjoined GB variant.
HANDWRITING_STYLES = {
    "semijoined": "Semi-joined (Playwrite GB S)",
    "joined": "Joined-up (Playwrite GB J)",
    "plain": "Plain (standard font)",
}
DEFAULT_HANDWRITING = "semijoined"

MAX_SUBJECT = 60
MAX_TITLE = 120
MAX_DETAILS = 1000
MAX_WORDS = 40
MAX_WORD_LENGTH = 40


class ValidationError(ValueError):
    """A friendly message for the Admin form, never a 500."""


# --- Validation helpers (used by the Admin routes) ---


def clean_text(value: str | None, field: str, max_length: int, required: bool = False) -> str:
    value = (value or "").strip()
    if required and not value:
        raise ValidationError(f"{field} can't be blank.")
    if len(value) > max_length:
        raise ValidationError(f"{field} is too long (max {max_length} characters).")
    return value


def parse_optional_date(value: str | None, field: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValidationError(f"{field} isn't a valid date — use the date picker or YYYY-MM-DD.") from None


async def parse_profile_id(db, value) -> int:
    try:
        profile_id = int(value)
    except TypeError, ValueError:
        raise ValidationError("Choose a family member.") from None
    if not await (await db.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,))).fetchone():
        raise ValidationError("That family member no longer exists.")
    return profile_id


def normalise_words(raw: str | None) -> list[str]:
    """Split on newlines or commas, trim, drop blanks and case-insensitive
    repeats (first spelling wins), and cap the count and each word's length."""
    words, seen = [], set()
    for part in re.split(r"[\n,]", raw or ""):
        word = " ".join(part.split())[:MAX_WORD_LENGTH].strip()
        if word and word.casefold() not in seen:
            seen.add(word.casefold())
            words.append(word)
    return words[:MAX_WORDS]


async def homework_fields(db, profile_id, subject, title, details, due_date, subject_key=None) -> tuple:
    """Validated (profile_id, subject, title, details, due_date, subject_key)
    for an Admin or School inbox homework form. A blank subject_key means
    "match the subject text" (subject_key_for). Raises ValidationError."""
    subject = clean_text(subject, "Subject", MAX_SUBJECT)
    return (
        await parse_profile_id(db, profile_id),
        subject,
        clean_text(title, "Title", MAX_TITLE, required=True),
        clean_text(details, "Details", MAX_DETAILS) or None,
        parse_optional_date(due_date, "Due date"),
        parse_subject_key(subject_key, subject),
    )


async def word_list_fields(db, profile_id, title, words, starts_on, ends_on) -> tuple:
    """Validated (profile_id, title, words, starts_on, ends_on) for an Admin
    word-list form. Raises ValidationError."""
    profile = await parse_profile_id(db, profile_id)
    title = clean_text(title, "Title", MAX_TITLE, required=True)
    word_items = normalise_words(words)
    if not word_items:
        raise ValidationError("Add at least one word.")
    starts = parse_optional_date(starts_on, "Start date")
    ends = parse_optional_date(ends_on, "End date")
    if starts and ends and ends < starts:
        raise ValidationError("The end date is before the start date.")
    return profile, title, "\n".join(word_items), starts, ends


async def get_handwriting_style(db) -> str:
    style = await get_setting(db, HANDWRITING_SETTING)
    return style if style in HANDWRITING_STYLES else DEFAULT_HANDWRITING


# --- Subjects (spec 10.10) ---
# A fixed set, each with a bundled Lucide icon; the colour tints are CSS
# (.subj-<key> in static/css/homework.css, day and night). The free-text
# subject (typed in Admin, or read from a school post) is kept for display;
# subject_key is what picks the icon and colour.

SUBJECTS = {
    # key: (label, Lucide icon id)
    "maths": ("Maths", "calculator"),
    "english": ("English", "spell-check"),
    "reading": ("Reading", "book-open-text"),
    "science": ("Science", "flask-conical"),
    "topic": ("Topic", "globe"),
    "other": ("Other", "shapes"),
}
OTHER = "other"

# Phrases (lower case, whole words) that mean each subject, checked against
# the free-text subject in three passes:
# 1. A language subject ("French", "MFL") is Other, whatever else it says.
# 2. SUBJECT_SYNONYMS: the earliest match in the text wins, and the longest
#    phrase on a tie, so "Reading comprehension" is Reading, "Read Write Inc"
#    is English and "Maths: number bonds" is Maths.
# 3. WEAK_SYNONYMS, only if nothing else matched: "Read chapter 3" is Reading.
# Anything unmatched is Other ("Homework book").
LANGUAGE_WORDS = ("french", "spanish", "german", "mfl", "languages", "modern foreign languages")
SUBJECT_SYNONYMS = {
    "maths": (
        "maths",
        "math",
        "mathematics",
        "numeracy",
        "number",
        "numbers",
        "number bonds",
        "times tables",
        "times table",
        "arithmetic",
        "mental maths",
        "fractions",
        "sumdog",
        "mathletics",
        "numbots",
        "tt rockstars",
        "ttrockstars",
        "ttrs",
        "times tables rock stars",
    ),
    "english": (
        "english",
        "spelling",
        "spellings",
        "phonics",
        "grammar",
        "spag",
        "gps",
        "punctuation",
        "writing",
        "handwriting",
        "literacy",
        "vocabulary",
        "creative writing",
        "read write inc",
        "rwi",
    ),
    "reading": (
        "reading",
        "reader",
        "library",
        "reading book",
        "reading record",
        "guided reading",
        "comprehension",
        "reading comprehension",
        "bug club",
        "oxford owl",
    ),
    "science": ("science", "biology", "chemistry", "physics", "experiment", "investigation"),
    "topic": ("topic", "project", "history", "geography", "humanities", "topic work"),
}
WEAK_SYNONYMS = {"reading": ("read",)}


def _words(text: str | None) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (text or "").casefold()).split())


def _first_match(text: str, table: dict) -> str | None:
    best = None  # (position, -length, key): the smallest wins
    for key, phrases in table.items():
        for phrase in phrases:
            at = text.find(f" {phrase} ")
            if at >= 0 and (best is None or (at, -len(phrase), key) < best):
                best = (at, -len(phrase), key)
    return best[2] if best else None


def subject_key_for(subject: str | None) -> str:
    """The fixed subject a free-text subject means ("Spelling" -> english),
    or "other"."""
    text = f" {_words(subject)} "
    if _first_match(text, {OTHER: LANGUAGE_WORDS}):
        return OTHER
    return _first_match(text, SUBJECT_SYNONYMS) or _first_match(text, WEAK_SYNONYMS) or OTHER


def parse_subject_key(value: str | None, subject: str | None) -> str:
    """An explicit pick from the Admin / inbox select, else the mapping of
    the subject text. An unknown value counts as no pick."""
    value = (value or "").strip()
    return value if value in SUBJECTS else subject_key_for(subject)


def subject_info(key: str | None, subject: str | None = None) -> dict:
    """{key, label, icon, text} for a homework row's subject chip: `text` is
    the typed subject when there is one ("Spellings"), else the label."""
    key = key if key in SUBJECTS else OTHER
    label, icon = SUBJECTS[key]
    return {"key": key, "label": label, "icon": icon, "text": (subject or "").strip() or label}


# --- Homework ---


def due_label(due: date | None, today: date) -> str | None:
    if due is None:
        return None
    days = (due - today).days
    if days < 0:
        return "Overdue"
    if days == 0:
        return "Due today"
    if days == 1:
        return "Due tomorrow"
    if days < 7:
        return f"Due {due.strftime('%A')}"
    return f"Due {due.day} {due.strftime('%b')}"


def is_visible(item: dict, today: date) -> bool:
    """Whether a homework row belongs on the widget. Done homework stays
    (struck through) until the end of its due day, or of the day it was
    ticked if that's later — so ticking an overdue item doesn't make it
    vanish under the child's finger."""
    if item["archived"]:
        return False
    if not item["done"]:
        return True
    # done_at is stamped in the family's timezone, so its date part is the local day.
    done_on = item["done_at"][:10] if item["done_at"] else today.isoformat()
    last_day = max(d for d in (item["due_date"], done_on) if d)
    return today.isoformat() <= last_day


async def get_homework_groups(db) -> list[dict]:
    today = await family_today(db)
    profiles = [
        avatars.attach(dict(r))
        for r in await (
            await db.execute(f"SELECT id, name, colour_hex, {avatars.COLUMNS} FROM profiles ORDER BY sort_order")
        ).fetchall()
    ]
    rows = await (
        await db.execute(
            """SELECT * FROM homework WHERE archived = 0
           ORDER BY due_date IS NULL, due_date, created_at, id"""
        )
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        if not is_visible(item, today):
            continue
        due = date.fromisoformat(item["due_date"]) if item["due_date"] else None
        item["overdue"] = bool(due and due < today and not item["done"])
        item["label"] = None if item["done"] and due and due < today else due_label(due, today)
        item["due_soon"] = bool(due and 0 <= (due - today).days <= 1 and not item["done"])
        item["subj"] = subject_info(item["subject_key"], item["subject"])
        items.append(item)
    groups = []
    for profile in profiles:
        profile["homework"] = [i for i in items if i["profile_id"] == profile["id"]]
        if profile["homework"]:
            groups.append(profile)
    return groups


async def toggle_homework(db, homework_id: int) -> bool:
    """Tick or untick homework. False if it isn't on the widget."""
    row = await (await db.execute("SELECT * FROM homework WHERE id = ?", (homework_id,))).fetchone()
    # Only what the widget shows can be tapped: a stale page can't revive dropped-off homework.
    if row is None or not is_visible(dict(row), await family_today(db)):
        return False
    now = datetime.now(await family_timezone(db)).isoformat(timespec="seconds")
    # Flip in one statement and record what it became, so two racing taps
    # each log the state they actually left (never two "done"s).
    flipped = await (
        await db.execute(
            """UPDATE homework SET done = 1 - done,
               done_at = CASE WHEN done = 0 THEN ? END,
               updated_at = datetime('now')
           WHERE id = ? RETURNING done""",
            (now, homework_id),
        )
    ).fetchone()
    if flipped is None:  # deleted in between
        await db.rollback()
        return False
    # The done history (spec 10.10): when, not who (a kiosk tap is anonymous).
    await db.execute(
        "INSERT INTO homework_events (homework_id, done, at) VALUES (?, ?, ?)", (homework_id, flipped[0], now)
    )
    await db.commit()
    return True


def event_time_label(at: str | None) -> str:
    """ "Tue 16:40" for a family-tz ISO timestamp. Its own offset is kept (no
    astimezone), so it reads in the family's time whatever the container's."""
    if not at:
        return ""
    try:
        moment = datetime.fromisoformat(at)
    except ValueError:
        return ""
    return f"{moment.strftime('%a')} {moment.day} {moment.strftime('%b')}, {moment.strftime('%H:%M')}"


async def get_recent_homework_events(db, limit: int = 20) -> list[dict]:
    """Admin's "Recently ticked" list, newest first. A deleted homework's
    events go with it (ON DELETE CASCADE)."""
    rows = await (
        await db.execute(
            """SELECT e.done, e.at, h.subject, h.subject_key, h.title, p.name AS profile_name
           FROM homework_events e
           JOIN homework h ON h.id = e.homework_id
           JOIN profiles p ON p.id = h.profile_id
           ORDER BY e.id DESC LIMIT ?  -- insertion order: ISO text with mixed offsets (DST) sorts wrong""",
            (limit,),
        )
    ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["when"] = event_time_label(event["at"])
        event["subj"] = subject_info(event["subject_key"], event["subject"])
        events.append(event)
    return events


async def get_admin_homework(db, today: date) -> tuple[list[dict], list[dict]]:
    """Admin's homework panel: (current, finished). Archived homework, or
    done and already off the widget, is tucked away so the panel doesn't
    grow all term."""
    homework_items: list[dict] = []
    finished_homework: list[dict] = []
    for row in await (
        await db.execute(
            "SELECT homework.*, profiles.name AS profile_name FROM homework "
            "JOIN profiles ON profiles.id = homework.profile_id "
            "ORDER BY homework.done, homework.due_date IS NULL, homework.due_date, homework.id"
        )
    ).fetchall():
        item = dict(row)
        item["subj"] = subject_info(item["subject_key"], item["subject"])
        # The edit form's subject select shows "Match the subject" unless the
        # stored key was an explicit pick that differs from the text's.
        item["subject_pick"] = "" if item["subject_key"] == subject_key_for(item["subject"]) else item["subject_key"]
        item["done_when"] = event_time_label(item["done_at"]) if item["done"] else ""
        (homework_items if is_visible(item, today) else finished_homework).append(item)
    return homework_items, finished_homework


async def homework_context(db) -> dict:
    """Template context for widgets/homework.html (the dashboard include and
    its own routes): the homework, and the reading log row."""
    return {
        "homework_groups": await get_homework_groups(db),
        "reading_children": await get_reading_today(db),
    }


# --- Reading log (spec 10.10) ---
# One row per child per family date they read. A "child" here is a profile
# that isn't a parent AND has a year group (Admin → Family Members): new
# installs start with every profile is_parent = 0, so the year group is
# what tells a pupil from a grown-up nobody has flagged yet. Nobody
# qualifying means no reading row at all. Shown every day, school day or
# not: school reading records count weekends and holidays too, so hiding
# the tick then would lose real reading.

READING_WEEKS = 4
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_READER = "is_parent = 0 AND TRIM(COALESCE(school_year, '')) != ''"


async def _children(db) -> list[dict]:
    return [
        avatars.attach(dict(r))
        for r in await (
            await db.execute(
                f"SELECT id, name, colour_hex, {avatars.COLUMNS} FROM profiles WHERE {_READER} ORDER BY sort_order, id"
            )
        ).fetchall()
    ]


async def get_reading_today(db) -> list[dict]:
    today = (await family_today(db)).isoformat()
    read = {
        r[0]
        for r in await (await db.execute("SELECT profile_id FROM reading_log WHERE read_on = ?", (today,))).fetchall()
    }
    children = await _children(db)
    for child in children:
        child["read_today"] = child["id"] in read
    return children


async def toggle_reading(db, profile_id: int) -> bool:
    """Tick "Read tonight" for a child, or undo it. False if they aren't a
    child with a year group (or no longer exist)."""
    if not await (await db.execute(f"SELECT 1 FROM profiles WHERE id = ? AND {_READER}", (profile_id,))).fetchone():
        return False
    today = (await family_today(db)).isoformat()
    deleted = await db.execute("DELETE FROM reading_log WHERE profile_id = ? AND read_on = ?", (profile_id, today))
    if deleted.rowcount == 0:
        await db.execute("INSERT INTO reading_log (profile_id, read_on) VALUES (?, ?)", (profile_id, today))
    await db.commit()
    return True


async def get_reading_history(db, today: date, weeks: int = READING_WEEKS) -> dict:
    """Admin's reading grid: the last `weeks` whole weeks, Monday to Sunday,
    ending with this one, so it reads like a school reading record.

    {"weekdays": (...), "children": [{name, colour_hex, weeks: [[cell] * 7] * weeks}]}
    Each cell is {date, day, label, read, future, today, school_day}; days
    off school are shaded so the school week stands out."""
    start = today - timedelta(days=today.weekday() + 7 * (weeks - 1))
    days = [start + timedelta(days=n) for n in range(7 * weeks)]
    school = {d: await term_dates.is_school_day(db, d) for d in days}
    read = {
        (r[0], r[1])
        for r in await (
            await db.execute(
                "SELECT profile_id, read_on FROM reading_log WHERE read_on BETWEEN ? AND ?",
                (days[0].isoformat(), days[-1].isoformat()),
            )
        ).fetchall()
    }
    children = await _children(db)
    for child in children:
        cells = [
            {
                "date": d.isoformat(),
                "day": d.day,
                "label": f"{d.strftime('%a')} {d.day} {d.strftime('%b')}",
                "read": (child["id"], d.isoformat()) in read,
                "future": d > today,
                "today": d == today,
                "school_day": school[d],
            }
            for d in days
        ]
        child["weeks"] = [cells[i : i + 7] for i in range(0, len(cells), 7)]
    return {"weekdays": WEEKDAY_NAMES, "children": children}


# --- Practice words ---


def is_active(word_list: dict, today: date) -> bool:
    """Whether a word list belongs on the widget: not archived, and today
    within its (optionally open-ended) date range."""
    day = today.isoformat()
    return (
        not word_list["archived"]
        and (not word_list["starts_on"] or word_list["starts_on"] <= day)
        and (not word_list["ends_on"] or day <= word_list["ends_on"])
    )


async def get_practice_lists(db) -> list[dict]:
    today = await family_today(db)
    rows = await (
        await db.execute(
            f"""SELECT l.*, p.name AS profile_name, p.colour_hex, {avatars.columns("p")},
                  EXISTS (SELECT 1 FROM practice_log g WHERE g.list_id = l.id AND g.practised_on = ?)
                      AS practised_today
           FROM practice_word_lists l JOIN profiles p ON p.id = l.profile_id
           WHERE l.archived = 0
           ORDER BY p.sort_order, l.created_at, l.id""",
            (today.isoformat(),),
        )
    ).fetchall()
    lists = []
    for row in rows:
        word_list = dict(row)
        if is_active(word_list, today):
            word_list["word_items"] = [w for w in word_list["words"].split("\n") if w]
            avatars.attach(word_list, id_key="profile_id")
            lists.append(word_list)
    return lists


async def practice_context(db) -> dict:
    """Template context for widgets/practice_words.html."""
    return {
        "practice_lists": await get_practice_lists(db),
        "handwriting_style": await get_handwriting_style(db),
    }


async def toggle_practised(db, list_id: int) -> bool:
    """Mark a word list practised today, or undo it. False if the list
    isn't on the widget."""
    row = await (await db.execute("SELECT * FROM practice_word_lists WHERE id = ?", (list_id,))).fetchone()
    today = await family_today(db)
    if row is None or not is_active(dict(row), today):
        return False
    today_iso = today.isoformat()
    deleted = await db.execute("DELETE FROM practice_log WHERE list_id = ? AND practised_on = ?", (list_id, today_iso))
    if deleted.rowcount == 0:
        await db.execute("INSERT INTO practice_log (list_id, practised_on) VALUES (?, ?)", (list_id, today_iso))
    await db.commit()
    return True


async def get_admin_word_lists(db) -> list[dict]:
    return [
        dict(r)
        for r in await (
            await db.execute(
                "SELECT practice_word_lists.*, profiles.name AS profile_name FROM practice_word_lists "
                "JOIN profiles ON profiles.id = practice_word_lists.profile_id "
                "ORDER BY practice_word_lists.archived, profiles.sort_order, practice_word_lists.id"
            )
        ).fetchall()
    ]
