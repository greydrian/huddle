"""Meal plan data (Section 4.5, spec 10.7): a text plan per day, plus
favourites and recipe cards.

Favourites are the meals planned before, by name (`meal_key`: lower case,
single spaces), most used first; starred ones lead and removed ones are
hidden. Their star, removal, recipe link and notes are the `meal_favourites`
row for that name (Admin → Family → Meals); how often and when a meal was
planned comes from `meal_plans` itself, so re-typing a day never inflates a
count.

A recipe card (app/recipes.py) is fetched from the meal's saved link on the
first tap and cached in `recipe_cache`: a card for CARD_DAYS, a failure for
FAILURE_HOURS, so a flaky site isn't asked on every tap.
"""

import json
import logging
from datetime import UTC, date, datetime, timedelta

from app import recipes
from app.database import family_today

logger = logging.getLogger(__name__)

MAX_MEAL = 120
MAX_NOTES = 1000
PICKER_SIZE = 25
CARD_DAYS = 7
FAILURE_HOURS = 1

ERROR_TEXT = {
    "bad_url": "That link isn't a web page address.",
    "blocked": "That link points inside the home network, so it isn't opened.",
    "offline": "Couldn't reach the recipe's website just now.",
    "http_error": "The recipe's website didn't return the page.",
    "not_html": "That link isn't a web page.",
    "too_big": "That page is too big to read.",
    "too_many_redirects": "That link redirects too many times.",
    "no_recipe": "That page has no recipe details Huddle can read.",
}


def meal_key(name: str) -> str:
    return " ".join((name or "").casefold().split())


async def get_week_meal_plan(db, start: date | None = None):
    """The next 7 days: {date, weekday, is_today, description, has_recipe}."""
    today = await family_today(db)
    start = start or today
    days = [start + timedelta(days=i) for i in range(7)]
    rows = await (
        await db.execute(
            "SELECT * FROM meal_plans WHERE date IN ({})".format(",".join("?" for _ in days)),
            [d.isoformat() for d in days],
        )
    ).fetchall()
    plan_by_date = {row["date"]: row["meal_description"] for row in rows}
    with_recipe = {
        r["name_key"]
        for r in await (
            await db.execute(
                "SELECT name_key FROM meal_favourites WHERE COALESCE(recipe_url, '') != '' OR COALESCE(notes, '') != ''"
            )
        ).fetchall()
    }
    return [
        {
            "date": d.isoformat(),
            "weekday": d.strftime("%A"),
            "is_today": d == today,
            "description": plan_by_date.get(d.isoformat(), ""),
            "has_recipe": meal_key(plan_by_date.get(d.isoformat(), "")) in with_recipe,
        }
        for d in days
    ]


async def set_meal(db, meal_date: date, description: str) -> None:
    await db.execute(
        """INSERT INTO meal_plans (date, meal_description) VALUES (?, ?)
           ON CONFLICT(date) DO UPDATE SET meal_description = excluded.meal_description""",
        (meal_date.isoformat(), " ".join(description.split())[:MAX_MEAL]),
    )
    await db.commit()


async def widget_context(db) -> dict:
    """What widgets/meals.html needs, from the dashboard and its own routes."""
    return {"days": await get_week_meal_plan(db), "meal_picker": await picker(db)}


# --- Favourites ---


async def favourites(db, include_hidden: bool = False) -> list[dict]:
    """Every meal planned before or saved in Admin: {"key", "name" (the
    latest spelling), "uses", "last_used", "starred", "hidden", "recipe_url",
    "notes"}. Starred first, then most used, then most recent."""
    found: dict[str, dict] = {}
    rows = await (
        await db.execute(
            "SELECT date, meal_description FROM meal_plans WHERE TRIM(meal_description) != '' ORDER BY date"
        )
    ).fetchall()
    for row in rows:
        key = meal_key(row["meal_description"])
        entry = found.setdefault(key, {"key": key, "uses": 0, "last_used": None})
        entry["name"] = " ".join(row["meal_description"].split())
        entry["uses"] += 1
        entry["last_used"] = row["date"]
    for row in await (await db.execute("SELECT * FROM meal_favourites")).fetchall():
        entry = found.setdefault(row["name_key"], {"key": row["name_key"], "uses": 0, "last_used": None})
        entry.setdefault("name", row["name"])
        entry.update(
            starred=bool(row["starred"]),
            hidden=bool(row["hidden"]),
            recipe_url=row["recipe_url"] or "",
            notes=row["notes"] or "",
        )
    result = []
    for entry in found.values():
        entry.setdefault("starred", False)
        entry.setdefault("hidden", False)
        entry.setdefault("recipe_url", "")
        entry.setdefault("notes", "")
        if include_hidden or not entry["hidden"]:
            result.append(entry)
    result.sort(key=lambda e: (not e["starred"], -e["uses"], _desc(e["last_used"]), e["name"].casefold()))
    return result


def _desc(iso: str | None) -> int:
    return -date.fromisoformat(iso).toordinal() if iso else 0


async def picker(db) -> list[str]:
    """The names the wall's favourites picker offers."""
    return [f["name"] for f in await favourites(db)][:PICKER_SIZE]


async def _upsert(db, name: str, **fields) -> None:
    key = meal_key(name)
    if not key:
        return
    await db.execute(
        "INSERT INTO meal_favourites (name_key, name) VALUES (?, ?) ON CONFLICT(name_key) DO NOTHING",
        (key, " ".join(name.split())[:MAX_MEAL]),
    )
    for column, value in fields.items():
        if column not in {"starred", "hidden", "recipe_url", "notes"}:
            raise ValueError(column)
        await db.execute(
            f"UPDATE meal_favourites SET {column} = ?, updated_at = datetime('now') WHERE name_key = ?",  # noqa: S608 - column from the fixed set above
            (value, key),
        )
    await db.commit()


async def set_starred(db, name: str, starred: bool) -> None:
    await _upsert(db, name, starred=int(starred))


async def set_hidden(db, name: str, hidden: bool) -> None:
    """Removed from favourites (or back): still planned days are untouched."""
    await _upsert(db, name, hidden=int(hidden))


async def set_recipe(db, name: str, url: str, notes: str) -> None:
    """Saves a meal's recipe link and notes. Raises ValueError("meal-link")
    for a link that isn't a plain http(s) address, ("meal-notes") for notes
    too long. Cards are cached by link, so a new link is read afresh."""
    url = url.strip()
    if url and recipes.clean_url(url) is None:
        raise ValueError("meal-link")
    notes = notes.strip()
    if len(notes) > MAX_NOTES:
        raise ValueError("meal-notes")
    await _upsert(db, name, recipe_url=url, notes=notes)


async def get_favourite(db, name: str) -> dict | None:
    key = meal_key(name)
    return next((f for f in await favourites(db, include_hidden=True) if f["key"] == key), None)


# --- Recipe cards ---


def _now() -> datetime:
    return datetime.now(UTC)


def _method(card: dict | None) -> list[dict]:
    """The card's steps as consecutive sections: [{"section", "steps": [text]}]."""
    parts: list[dict] = []
    for step in (card or {}).get("steps") or []:
        if not parts or parts[-1]["section"] != step.get("section"):
            parts.append({"section": step.get("section"), "steps": []})
        parts[-1]["steps"].append(step.get("text", ""))
    return parts


async def recipe_card(db, name: str) -> dict:
    """See _recipe_card; adds "method" (the steps by section) to its result."""
    result = await _recipe_card(db, name)
    result["method"] = _method(result["card"])
    return result


async def _recipe_card(db, name: str) -> dict:
    """What _recipe_card.html shows for a meal: {"name", "notes", "url",
    "card" (recipes.parse_recipe's dict) or None, "error" (text) or None}.
    Never raises for a network or page problem."""
    favourite = await get_favourite(db, name)
    result: dict = {
        "name": favourite["name"] if favourite else name,
        "notes": favourite["notes"] if favourite else "",
        "url": favourite["recipe_url"] if favourite else "",
        "card": None,
        "error": None,
        "method": [],
    }
    url: str = result["url"]
    if not url:
        return result
    row = await (await db.execute("SELECT * FROM recipe_cache WHERE url = ?", (url,))).fetchone()
    if row is not None:
        fetched = datetime.fromisoformat(row["fetched_at"])
        fresh_for = timedelta(days=CARD_DAYS) if row["status"] == "ok" else timedelta(hours=FAILURE_HOURS)
        if _now() - fetched < fresh_for:
            if row["status"] == "ok":
                result["card"] = json.loads(row["card_json"])
            else:
                result["error"] = ERROR_TEXT.get(row["status"], ERROR_TEXT["offline"])
            return result
    try:
        card = await recipes.fetch_card(url)
        status, stored = "ok", json.dumps(card)
        result["card"] = card
    except recipes.RecipeError as exc:
        logger.info("Recipe card not read: %s", exc.code)
        status, stored = exc.code, None
        result["error"] = ERROR_TEXT.get(exc.code, ERROR_TEXT["offline"])
    except Exception as exc:  # a parsing surprise must never 500 the wall
        logger.warning("Recipe card failed: %s", type(exc).__name__)
        status, stored = "offline", None
        result["error"] = ERROR_TEXT["offline"]
    await db.execute(
        """INSERT INTO recipe_cache (url, fetched_at, status, card_json) VALUES (?, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET fetched_at = excluded.fetched_at, status = excluded.status,
               card_json = excluded.card_json""",
        (url, _now().isoformat(), status, stored),
    )
    await db.commit()
    return result
