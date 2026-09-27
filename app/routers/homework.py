"""
Homework and handwriting practice words (phase 1: entered by hand in
Admin). Ticking homework done and marking a word list "practised today"
are PIN-free kiosk taps, like tasks; nothing here is synced to Google and
there are no points or rewards.

"Today" is always family_today(): due-date labels, when done homework
drops off, which word lists are active, and which day a practice counts for.
"""

import re
from datetime import date, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.database import family_timezone, family_today, get_db, get_setting
from app.templating import templates

router = APIRouter()

HANDWRITING_SETTING = "handwriting_style"
# value -> Admin label. Playwrite GB S is "Playwrite England SemiJoined" and
# GB J "Playwrite England Joined"; Google has no fully unjoined GB variant.
HANDWRITING_STYLES = {
    "semijoined": "Unjoined letters (Playwrite GB S, semi-joined)",
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


# --- Validation helpers (shared with the Admin routes) ---

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


async def get_handwriting_style(db) -> str:
    style = await get_setting(db, HANDWRITING_SETTING)
    return style if style in HANDWRITING_STYLES else DEFAULT_HANDWRITING


# --- Homework widget ---

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
    """Done homework stays (struck through) until the end of its due day, or
    of the day it was ticked if that's later — so ticking an overdue item
    doesn't make it vanish under the child's finger."""
    if not item["done"]:
        return True
    last_day = max(d for d in (item["due_date"], item["done_on"]) if d)
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
        # done_at is stamped in the family's timezone, so its date part is the local day.
        item["done_on"] = item["done_at"][:10] if item["done"] and item["done_at"] else None
        if item["done"] and not item["done_on"]:
            item["done_on"] = today.isoformat()
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


@router.get("/widgets/homework", response_class=HTMLResponse)
async def homework_widget(request: Request):
    async with get_db() as db:
        homework_groups = await get_homework_groups(db)
    return templates.TemplateResponse(request, "widgets/homework.html", {"homework_groups": homework_groups})


@router.post("/api/homework/{homework_id}/toggle", response_class=HTMLResponse)
async def toggle_homework(request: Request, homework_id: int):
    async with get_db() as db:
        row = await (await db.execute(
            "SELECT done FROM homework WHERE id = ? AND archived = 0", (homework_id,)
        )).fetchone()
        if row is None:
            return HTMLResponse(status_code=404, content="Homework not found")
        done = not row["done"]
        done_at = datetime.now(await family_timezone(db)).isoformat() if done else None
        await db.execute(
            "UPDATE homework SET done = ?, done_at = ?, updated_at = datetime('now') WHERE id = ?",
            (int(done), done_at, homework_id),
        )
        await db.commit()
        homework_groups = await get_homework_groups(db)
    return templates.TemplateResponse(request, "widgets/homework.html", {"homework_groups": homework_groups})


# --- Practice words widget ---

def is_active(word_list: dict, today: date) -> bool:
    day = today.isoformat()
    return (not word_list["starts_on"] or word_list["starts_on"] <= day) and (
        not word_list["ends_on"] or day <= word_list["ends_on"]
    )


async def get_practice_lists(db) -> list[dict]:
    today = (await family_today(db)).isoformat()
    rows = await (await db.execute(
        """SELECT l.*, p.name AS profile_name, p.colour_hex,
                  EXISTS (SELECT 1 FROM practice_log g WHERE g.list_id = l.id AND g.practised_on = ?)
                      AS practised_today
           FROM practice_word_lists l JOIN profiles p ON p.id = l.profile_id
           WHERE l.archived = 0
             AND (l.starts_on IS NULL OR l.starts_on <= ?)
             AND (l.ends_on IS NULL OR l.ends_on >= ?)
           ORDER BY p.sort_order, l.created_at, l.id""",
        (today, today, today),
    )).fetchall()
    lists = []
    for row in rows:
        word_list = dict(row)
        word_list["word_items"] = [w for w in word_list["words"].split("\n") if w]
        lists.append(word_list)
    return lists


async def _practice_context(db) -> dict:
    return {
        "practice_lists": await get_practice_lists(db),
        "handwriting_style": await get_handwriting_style(db),
    }


@router.get("/widgets/practice-words", response_class=HTMLResponse)
async def practice_words_widget(request: Request):
    async with get_db() as db:
        context = await _practice_context(db)
    return templates.TemplateResponse(request, "widgets/practice_words.html", context)


@router.post("/api/practice-words/{list_id}/practised", response_class=HTMLResponse)
async def toggle_practised(request: Request, list_id: int):
    async with get_db() as db:
        if not await (await db.execute(
            "SELECT 1 FROM practice_word_lists WHERE id = ? AND archived = 0", (list_id,)
        )).fetchone():
            return HTMLResponse(status_code=404, content="Word list not found")
        today = (await family_today(db)).isoformat()
        deleted = await db.execute(
            "DELETE FROM practice_log WHERE list_id = ? AND practised_on = ?", (list_id, today)
        )
        if deleted.rowcount == 0:
            await db.execute(
                "INSERT INTO practice_log (list_id, practised_on) VALUES (?, ?)", (list_id, today)
            )
        await db.commit()
        context = await _practice_context(db)
    return templates.TemplateResponse(request, "widgets/practice_words.html", context)
