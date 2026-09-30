"""
The single shared shopping list (Section 4.4). Every change is written to
SQLite straight away and queued for app/task_sync.py, which syncs the list
both ways with the Google Tasks list linked in Admin.

Spec 10.8:
- A quantity lives in the title as a suffix ("Milk ×2"), so phones see it
  in Google Tasks too. `parse_title` reads "x2", "×2" and "2x" (either end);
  titles from Google are shown as they are, never rewritten.
- The widget is a simple list or grouped by aisle (Admin → Display →
  Shopping). An item's category is a remembered correction for its name
  (`shopping_categories`), else the built-in word list, else "Other". The
  aisle order is set in Admin.
"""

import json
import re

from app.database import get_setting, set_setting
from app.task_sync import queue_sync

MAX_QUANTITY = 99
MAX_TITLE = 200

VIEW_SETTING = "shopping_view"  # "simple" (default) or "grouped"
ORDER_SETTING = "shopping_category_order"  # JSON list of CATEGORIES keys
VIEWS = {"simple": "Simple list", "grouped": "Grouped by aisle"}

CATEGORIES = {
    "fruit_veg": "Fruit & veg",
    "bakery": "Bakery",
    "dairy": "Dairy & eggs",
    "meat_fish": "Meat & fish",
    "chilled": "Chilled",
    "frozen": "Frozen",
    "cupboard": "Cupboard",
    "drinks": "Drinks",
    "snacks": "Snacks & sweets",
    "household": "Household",
    "toiletries": "Health & beauty",
    "baby": "Baby",
    "other": "Other",
}
OTHER = "other"

# The built-in word list: whole words or phrases, matched on the item's
# name (lower case, simple plurals folded); guess_category says which match
# wins.
WORDS: dict[str, tuple[str, ...]] = {
    "frozen": ("frozen", "ice cream", "ice lolly", "ice lollies", "fish fingers", "oven chips", "ice", "sorbet"),
    "baby": ("nappy", "nappies", "wipes", "baby food", "formula", "calpol"),
    "toiletries": (
        "shampoo",
        "conditioner",
        "toothpaste",
        "toothbrush",
        "soap",
        "shower gel",
        "deodorant",
        "plasters",
        "paracetamol",
        "ibuprofen",
        "razor",
        "sun cream",
        "suncream",
        "tampon",
        "sanitary",
        "moisturiser",
        "cotton wool",
        "floss",
        "mouthwash",
        "vitamin",
    ),
    "household": (
        "toilet roll",
        "toilet paper",
        "kitchen roll",
        "bin bag",
        "washing up liquid",
        "washing powder",
        "washing liquid",
        "detergent",
        "fabric softener",
        "bleach",
        "cleaner",
        "sponge",
        "foil",
        "cling film",
        "battery",
        "batteries",
        "light bulb",
        "dishwasher tablet",
        "tissues",
        "candle",
        "matches",
        "sandwich bag",
    ),
    "dairy": (
        "milk",
        "cheese",
        "cheddar",
        "mozzarella",
        "parmesan",
        "butter",
        "yoghurt",
        "yogurt",
        "cream",
        "egg",
        "creme fraiche",
        "custard",
        "margarine",
        "fromage frais",
    ),
    "meat_fish": (
        "chicken",
        "beef",
        "mince",
        "pork",
        "lamb",
        "bacon",
        "sausage",
        "ham",
        "turkey",
        "steak",
        "salmon",
        "cod",
        "tuna steak",
        "prawn",
        "fish",
        "gammon",
        "chorizo",
        "salami",
        "haddock",
        "mackerel",
    ),
    "chilled": ("hummus", "houmous", "pesto", "fresh pasta", "coleslaw", "dip", "tofu", "quiche", "pizza"),
    "bakery": (
        "bread",
        "loaf",
        "roll",
        "bagel",
        "croissant",
        "baguette",
        "wrap",
        "tortilla",
        "crumpet",
        "muffin",
        "pitta",
        "naan",
        "brioche",
        "cake",
        "scone",
        "teacake",
    ),
    "fruit_veg": (
        "apple",
        "banana",
        "orange",
        "pear",
        "grape",
        "strawberry",
        "strawberries",
        "raspberry",
        "raspberries",
        "blueberry",
        "blueberries",
        "lemon",
        "lime",
        "melon",
        "mango",
        "pineapple",
        "kiwi",
        "plum",
        "peach",
        "satsuma",
        "clementine",
        "avocado",
        "tomato",
        "tomatoes",
        "potato",
        "potatoes",
        "onion",
        "garlic",
        "carrot",
        "broccoli",
        "cauliflower",
        "cabbage",
        "lettuce",
        "cucumber",
        "pepper",
        "courgette",
        "aubergine",
        "mushroom",
        "spinach",
        "salad",
        "celery",
        "leek",
        "sweetcorn",
        "pea",
        "bean sprout",
        "green bean",
        "ginger",
        "herb",
        "coriander",
        "parsley",
        "basil",
        "sweet potato",
        "parsnip",
        "swede",
        "beetroot",
        "spring onion",
        "rocket",
        "cherry",
        "cherries",
        "berries",
        "fruit",
        "veg",
    ),
    "drinks": (
        "juice",
        "squash",
        "water",
        "lemonade",
        "cola",
        "coke",
        "beer",
        "wine",
        "cider",
        "tea",
        "coffee",
        "hot chocolate",
        "smoothie",
        "fizzy",
    ),
    "snacks": (
        "crisps",
        "chocolate",
        "biscuit",
        "sweets",
        "popcorn",
        "cereal bar",
        "flapjack",
        "nuts",
        "raisins",
        "crackers",
        "rice cakes",
    ),
    "cupboard": (
        "pasta",
        "rice",
        "flour",
        "sugar",
        "cereal",
        "porridge",
        "oats",
        "tin",
        "tinned",
        "baked beans",
        "beans",
        "soup",
        "stock",
        "gravy",
        "sauce",
        "ketchup",
        "mayonnaise",
        "mayo",
        "mustard",
        "vinegar",
        "oil",
        "salt",
        "spice",
        "honey",
        "jam",
        "marmalade",
        "peanut butter",
        "noodles",
        "lentils",
        "chickpeas",
        "tuna",
        "coconut milk",
        "passata",
        "chopped tomatoes",
        "tinned tomatoes",
        "stock cubes",
        "yeast",
        "baking powder",
        "cornflakes",
        "weetabix",
        "spaghetti",
        "couscous",
        "nutella",
    ),
}


# --- Quantities ---

_SUFFIX = re.compile(
    r"^(?P<name>.*?\S)(?:\s+[x×]\s*|\s*×\s*)(?P<n>\d{1,3})$|^(?P<name2>.*?\S)\s+(?P<n2>\d{1,3})\s*[x×]$", re.I
)
_PREFIX = re.compile(r"^(?P<n>\d{1,3})\s*[x×]\s+(?P<name>\S.*)$", re.I)


def parse_title(title: str) -> tuple[str, int]:
    """(name, quantity) from a title: "Milk ×2", "milk x 2", "2x milk" and
    "Milk 2x" are ("Milk"/"milk", 2); anything else is (title, 1)."""
    text = " ".join((title or "").split())
    match = _SUFFIX.match(text)
    if match:
        name, n = match["name"] or match["name2"], match["n"] or match["n2"]
    else:
        match = _PREFIX.match(text)
        if not match:
            return text, 1
        name, n = match["name"], match["n"]
    quantity = int(n)
    if not 1 <= quantity <= MAX_QUANTITY:
        return text, 1
    return name, quantity


def format_title(name: str, quantity: int) -> str:
    """The title Huddle writes: "Milk", or "Milk ×2"."""
    return name if quantity <= 1 else f"{name} ×{min(quantity, MAX_QUANTITY)}"


def item_key(name: str) -> str:
    """What a category correction is remembered by: the name without its
    quantity, lower case, single spaces."""
    return " ".join(parse_title(name)[0].casefold().split())


# --- Categories ---


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("oes") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _normal(text: str) -> str:
    words = re.findall(r"[a-z]+", text.casefold().replace("é", "e"))
    return " " + " ".join(_singular(w) for w in words) + " "


_NORMAL_WORDS = {category: tuple(_normal(w) for w in words) for category, words in WORDS.items()}


def guess_category(name: str) -> str:
    """The built-in word list's category for an item name, else "other".
    The phrase ending last wins (English puts the thing itself last:
    "orange juice" is a drink), then the longest ("coconut milk" over
    "milk"), then the earlier category; "frozen ..." is always Frozen."""
    text = _normal(parse_title(name)[0])
    if text.startswith(" frozen "):
        return "frozen"
    best: tuple[int, int, int] | None = None
    best_category = OTHER
    for rank, (category, words) in enumerate(_NORMAL_WORDS.items()):
        for word in words:
            at = text.rfind(word)
            if at < 0:
                continue
            score = (at + len(word), len(word), -rank)
            if best is None or score > best:
                best, best_category = score, category
    return best_category


async def remembered_categories(db) -> dict[str, str]:
    rows = await (await db.execute("SELECT item_key, category FROM shopping_categories")).fetchall()
    return {r["item_key"]: r["category"] for r in rows if r["category"] in CATEGORIES}


async def set_category(db, item_id: int, category: str) -> bool:
    """Corrects an item's category and remembers it for its name. False if
    the item or the category doesn't exist."""
    if category not in CATEGORIES:
        return False
    row = await (await db.execute("SELECT title FROM shopping_items WHERE id = ?", (item_id,))).fetchone()
    if row is None:
        return False
    key = item_key(row["title"])
    if not key:
        return False
    await db.execute(
        """INSERT INTO shopping_categories (item_key, category) VALUES (?, ?)
           ON CONFLICT(item_key) DO UPDATE SET category = excluded.category""",
        (key, category),
    )
    await db.commit()
    return True


async def get_view(db) -> str:
    value = await get_setting(db, VIEW_SETTING, "simple")
    return value if value in VIEWS else "simple"


async def set_view(db, view: str) -> None:
    if view not in VIEWS:
        raise ValueError(view)
    await set_setting(db, VIEW_SETTING, view)
    await db.commit()


async def get_order(db) -> list[str]:
    """Every category once, in the family's aisle order (new ones at the
    end, "Other" last unless moved)."""
    try:
        saved = json.loads(await get_setting(db, ORDER_SETTING) or "[]")
    except ValueError:
        saved = []
    order = [c for c in saved if isinstance(c, str) and c in CATEGORIES]
    order = list(dict.fromkeys(order))
    return order + [c for c in CATEGORIES if c not in order]


async def move_category(db, category: str, step: int) -> bool:
    """Moves a category one place up (-1) or down (+1). False if unknown."""
    order = await get_order(db)
    if category not in order or step not in (-1, 1):
        return False
    i = order.index(category)
    j = i + step
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
        await set_setting(db, ORDER_SETTING, json.dumps(order))
        await db.commit()
    return True


# --- The list ---


async def get_shopping_items(db) -> list[dict]:
    """Every item, unticked first: its row plus "name", "quantity" and
    "category" (and its label)."""
    rows = await (await db.execute("SELECT * FROM shopping_items ORDER BY is_checked, created_at")).fetchall()
    remembered = await remembered_categories(db)
    items = []
    for row in rows:
        item = dict(row)
        item["name"], item["quantity"] = parse_title(item["title"])
        item["category"] = remembered.get(item_key(item["title"])) or guess_category(item["title"])
        item["category_label"] = CATEGORIES[item["category"]]
        items.append(item)
    return items


async def widget_context(db) -> dict:
    """What widgets/shopping.html needs, from the dashboard and its own routes."""
    items = await get_shopping_items(db)
    view = await get_view(db)
    groups = []
    if view == "grouped":
        order = await get_order(db)
        unticked = [i for i in items if not i["is_checked"]]
        groups = [
            {"key": c, "label": CATEGORIES[c], "items": [i for i in unticked if i["category"] == c]} for c in order
        ]
        groups = [g for g in groups if g["items"]]
    return {
        "items": items,
        "shopping_view": view,
        "shopping_groups": groups,
        "shopping_ticked": [i for i in items if i["is_checked"]],
        "shopping_categories": CATEGORIES,
    }


async def add_item(db, title: str) -> None:
    """Add an item (blank titles are ignored). Adding something already on
    the list, unticked, adds to its quantity instead ("Milk" twice is
    "Milk ×2"); a ticked one is unticked with the new quantity."""
    name, quantity = parse_title(title.strip()[:MAX_TITLE])
    if not name:
        return
    key = item_key(name)
    for row in await (await db.execute("SELECT id, title, is_checked FROM shopping_items")).fetchall():
        if item_key(row["title"]) != key:
            continue
        old_name, old_quantity = parse_title(row["title"])
        total = quantity if row["is_checked"] else old_quantity + quantity
        await db.execute(
            "UPDATE shopping_items SET title = ?, is_checked = 0, updated_at = datetime('now') WHERE id = ?",
            (format_title(old_name, total), row["id"]),
        )
        await queue_sync(db, "shopping", {"item_id": row["id"]})
        await db.commit()
        return
    cursor = await db.execute("INSERT INTO shopping_items (title) VALUES (?)", (format_title(name, quantity),))
    await queue_sync(db, "shopping", {"item_id": cursor.lastrowid})
    await db.commit()


async def toggle_item(db, item_id: int) -> bool:
    """Check or uncheck an item. False if there's no such item."""
    cursor = await db.execute("SELECT * FROM shopping_items WHERE id = ?", (item_id,))
    item = await cursor.fetchone()
    if item is None:
        return False

    new_state = 0 if item["is_checked"] else 1
    await db.execute(
        "UPDATE shopping_items SET is_checked = ?, updated_at = datetime('now') WHERE id = ?",
        (new_state, item_id),
    )
    await queue_sync(db, "shopping", {"item_id": item_id, "is_checked": bool(new_state)})
    await db.commit()
    return True


async def delete_item(db, item_id: int) -> None:
    # Capture google_task_id before deleting — the row (and this
    # column) won't exist anymore by the time the sync worker drains
    # this queue entry and needs to know what to delete on Google's side.
    cursor = await db.execute("SELECT google_task_id FROM shopping_items WHERE id = ?", (item_id,))
    existing = await cursor.fetchone()
    google_task_id = existing["google_task_id"] if existing else None

    await db.execute("DELETE FROM shopping_items WHERE id = ?", (item_id,))
    await queue_sync(db, "shopping", {"action": "delete", "item_id": item_id, "google_task_id": google_task_id})
    await db.commit()
