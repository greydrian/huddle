"""Schools (spec 11.2): each school has its own term dates
(services/term_dates.py) and its own school email senders
(app/school_email.py), and each family member optionally belongs to one
(`profiles.school_id`). A child with no school has no school days of their
own and gets nothing from a school's email.

Migration 10 made the first school (Gresham) from the household-wide term
dates and senders that came before, so one school behaves as before. A
nursery can be a school too, with its own dates.
"""

from app import school_email

MAX_NAME = 60
MAX_SCHOOLS = 10


class SchoolError(ValueError):
    """A school form that can't be saved; str() is its ADMIN_ERRORS code."""


def _senders(raw: str | None) -> list[str]:
    try:
        return school_email.parse_entries(raw or "")
    except school_email.InvalidEntry:  # a hand-edited row: read nothing rather than query something odd
        return []


def _clean_name(name: str) -> str:
    text = " ".join((name or "").split())
    if not text or len(text) > MAX_NAME:
        raise SchoolError("school-name")
    return text


async def list_schools(db) -> list[dict]:
    """Every school, oldest first: {"id", "name", "senders" (list), "children" (names)}."""
    rows = await (await db.execute("SELECT id, name, senders FROM schools ORDER BY id")).fetchall()
    members = await (
        await db.execute("SELECT school_id, name FROM profiles WHERE school_id IS NOT NULL ORDER BY sort_order")
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "senders": _senders(r["senders"]),
            "children": [m["name"] for m in members if m["school_id"] == r["id"]],
        }
        for r in rows
    ]


async def names(db) -> dict[int, str]:
    return {r["id"]: r["name"] for r in await (await db.execute("SELECT id, name FROM schools")).fetchall()}


async def exists(db, school_id) -> bool:
    try:
        school_id = int(school_id)
    except TypeError, ValueError:
        return False
    return await (await db.execute("SELECT 1 FROM schools WHERE id = ?", (school_id,))).fetchone() is not None


async def default_id(db) -> int | None:
    """The oldest school: what a question about "the" school means when
    nobody says which (and the only one, for most families)."""
    row = await (await db.execute("SELECT id FROM schools ORDER BY id LIMIT 1")).fetchone()
    return row["id"] if row else None


async def of_profile(db, profile_id) -> int | None:
    row = await (await db.execute("SELECT school_id FROM profiles WHERE id = ?", (profile_id,))).fetchone()
    return row["school_id"] if row else None


async def family_ids(db) -> list[int]:
    """The schools the family's children go to; every school if nobody is
    linked to one yet (so term dates entered before linking still count)."""
    rows = await (
        await db.execute("SELECT DISTINCT school_id FROM profiles WHERE school_id IS NOT NULL ORDER BY school_id")
    ).fetchall()
    if rows:
        return [r["school_id"] for r in rows]
    return [r["id"] for r in await (await db.execute("SELECT id FROM schools ORDER BY id")).fetchall()]


async def all_senders(db) -> list[str]:
    """Every school's senders, for the one Gmail search."""
    found: list[str] = []
    for school in await list_schools(db):
        found += [s for s in school["senders"] if s not in found]
    return found


async def for_sender(db, address: str) -> int | None:
    """The school whose senders list this From address (the oldest, if two do)."""
    for school in await list_schools(db):
        if any(school_email.matches(address, entry) for entry in school["senders"]):
            return school["id"]
    return None


async def add(db, name: str) -> int:
    clean = _clean_name(name)
    (count,) = await (await db.execute("SELECT COUNT(*) FROM schools")).fetchone()
    if count >= MAX_SCHOOLS:
        raise SchoolError("school-full")
    cursor = await db.execute("INSERT INTO schools (name) VALUES (?)", (clean,))
    await db.commit()
    return cursor.lastrowid or 0


async def update(db, school_id: int, name: str, senders: str) -> None:
    """Renames a school and replaces its senders. Raises SchoolError."""
    clean = _clean_name(name)
    try:
        entries = school_email.parse_entries(senders)
    except school_email.InvalidEntry:
        raise SchoolError("school-list-senders") from None
    cursor = await db.execute(
        "UPDATE schools SET name = ?, senders = ? WHERE id = ?", (clean, "\n".join(entries), school_id)
    )
    await db.commit()
    if not cursor.rowcount:
        raise SchoolError("school-missing")


async def delete(db, school_id: int) -> None:
    """Deletes the school and its term dates; its children keep no school."""
    await db.execute("DELETE FROM schools WHERE id = ?", (school_id,))
    await db.commit()


async def set_profile_school(db, profile_id: int, school_id: int | None) -> None:
    """No commit: the caller saves the rest of the profile with it."""
    await db.execute("UPDATE profiles SET school_id = ? WHERE id = ?", (school_id, profile_id))
