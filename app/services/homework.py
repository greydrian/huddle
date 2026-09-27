"""
Homework and handwriting practice words (phase 1: entered by hand in
Admin). Nothing here is synced to Google and there are no points or rewards.

"Today" is always family_today(): due-date labels, when done homework
drops off, which word lists are active, and which day a practice counts for.
"""

import re
from datetime import date, datetime

from app.database import family_timezone, family_today, get_setting

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
    except (TypeError, ValueError):
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


async def homework_fields(db, profile_id, subject, title, details, due_date) -> tuple:
    """Validated (profile_id, subject, title, details, due_date) for an Admin
    homework form. Raises ValidationError."""
    return (
        await parse_profile_id(db, profile_id),
        clean_text(subject, "Subject", MAX_SUBJECT),
        clean_text(title, "Title", MAX_TITLE, required=True),
        clean_text(details, "Details", MAX_DETAILS) or None,
        parse_optional_date(due_date, "Due date"),
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
    profiles = [dict(r) for r in await (await db.execute(
        "SELECT id, name, colour_hex FROM profiles ORDER BY sort_order"
    )).fetchall()]
    rows = await (await db.execute(
        """SELECT * FROM homework WHERE archived = 0
           ORDER BY due_date IS NULL, due_date, created_at, id"""
    )).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        if not is_visible(item, today):
            continue
        due = date.fromisoformat(item["due_date"]) if item["due_date"] else None
        item["overdue"] = bool(due and due < today and not item["done"])
        item["label"] = None if item["done"] and due and due < today else due_label(due, today)
        item["due_soon"] = bool(due and 0 <= (due - today).days <= 1 and not item["done"])
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
    done = not row["done"]
    done_at = datetime.now(await family_timezone(db)).isoformat() if done else None
    await db.execute(
        "UPDATE homework SET done = ?, done_at = ?, updated_at = datetime('now') WHERE id = ?",
        (int(done), done_at, homework_id),
    )
    await db.commit()
    return True


async def get_admin_homework(db, today: date) -> tuple[list[dict], list[dict]]:
    """Admin's homework panel: (current, finished). Archived homework, or
    done and already off the widget, is tucked away so the panel doesn't
    grow all term."""
    homework_items, finished_homework = [], []
    for row in await (await db.execute(
        "SELECT homework.*, profiles.name AS profile_name FROM homework "
        "JOIN profiles ON profiles.id = homework.profile_id "
        "ORDER BY homework.done, homework.due_date IS NULL, homework.due_date, homework.id"
    )).fetchall():
        item = dict(row)
        (homework_items if is_visible(item, today) else finished_homework).append(item)
    return homework_items, finished_homework


# --- Practice words ---

def is_active(word_list: dict, today: date) -> bool:
    """Whether a word list belongs on the widget: not archived, and today
    within its (optionally open-ended) date range."""
    day = today.isoformat()
    return not word_list["archived"] and (not word_list["starts_on"] or word_list["starts_on"] <= day) and (
        not word_list["ends_on"] or day <= word_list["ends_on"]
    )


async def get_practice_lists(db) -> list[dict]:
    today = await family_today(db)
    rows = await (await db.execute(
        """SELECT l.*, p.name AS profile_name, p.colour_hex,
                  EXISTS (SELECT 1 FROM practice_log g WHERE g.list_id = l.id AND g.practised_on = ?)
                      AS practised_today
           FROM practice_word_lists l JOIN profiles p ON p.id = l.profile_id
           WHERE l.archived = 0
           ORDER BY p.sort_order, l.created_at, l.id""",
        (today.isoformat(),),
    )).fetchall()
    lists = []
    for row in rows:
        word_list = dict(row)
        if is_active(word_list, today):
            word_list["word_items"] = [w for w in word_list["words"].split("\n") if w]
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
    today = today.isoformat()
    deleted = await db.execute(
        "DELETE FROM practice_log WHERE list_id = ? AND practised_on = ?", (list_id, today)
    )
    if deleted.rowcount == 0:
        await db.execute(
            "INSERT INTO practice_log (list_id, practised_on) VALUES (?, ?)", (list_id, today)
        )
    await db.commit()
    return True


async def get_admin_word_lists(db) -> list[dict]:
    return [dict(r) for r in await (await db.execute(
        "SELECT practice_word_lists.*, profiles.name AS profile_name FROM practice_word_lists "
        "JOIN profiles ON profiles.id = practice_word_lists.profile_id "
        "ORDER BY practice_word_lists.archived, profiles.sort_order, practice_word_lists.id"
    )).fetchall()]
