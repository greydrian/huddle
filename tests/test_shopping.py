"""Shopping list quantities, aisles and the grouped view (spec 10.8,
app/services/shopping.py)."""

import pytest

from app.routers import admin
from app.security import create_session_token
from app.services import shopping


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


async def titles(db):
    return [i["title"] for i in await shopping.get_shopping_items(db)]


async def queued(db):
    return (await (await db.execute("SELECT COUNT(*) FROM sync_queue WHERE service = 'shopping'")).fetchone())[0]


# --- Quantities ---


@pytest.mark.parametrize(
    ("title", "parsed"),
    [
        ("Milk ×2", ("Milk", 2)),
        ("Milk x2", ("Milk", 2)),
        ("milk X 3", ("milk", 3)),
        ("Milk×2", ("Milk", 2)),
        ("2x milk", ("milk", 2)),
        ("2 x Milk", ("Milk", 2)),
        ("2× Milk", ("Milk", 2)),
        ("Milk 2x", ("Milk", 2)),
        ("Eggs x 12", ("Eggs", 12)),
        ("Milk", ("Milk", 1)),
        ("Box", ("Box", 1)),  # an x in a word isn't a quantity
        ("Milkx2", ("Milkx2", 1)),
        ("4x4 tyres", ("4x4 tyres", 1)),
        ("Milk x0", ("Milk x0", 1)),
        ("Milk x100", ("Milk x100", 1)),
        ("  Semi skimmed   milk  x2 ", ("Semi skimmed milk", 2)),
    ],
)
def test_parse_title(title, parsed):
    assert shopping.parse_title(title) == parsed


def test_format_title():
    assert [shopping.format_title("Milk", n) for n in (1, 2, 150)] == ["Milk", "Milk ×2", "Milk ×99"]


async def test_adding_from_the_wall_writes_the_suffix_and_merges_repeats(db):
    await shopping.add_item(db, "2x milk")
    await shopping.add_item(db, "Bread")
    assert await titles(db) == ["milk ×2", "Bread"]

    await shopping.add_item(db, "Milk")  # already there: one more
    await shopping.add_item(db, "bread x2")
    assert await titles(db) == ["milk ×3", "Bread ×3"]
    assert await queued(db) == 4  # every change goes to Google

    # A ticked one comes back unticked with just the new amount.
    (bread,) = [i for i in await shopping.get_shopping_items(db) if i["name"] == "Bread"]
    await shopping.toggle_item(db, bread["id"])
    await shopping.add_item(db, "Bread")
    items = {i["name"]: i for i in await shopping.get_shopping_items(db)}
    assert (items["Bread"]["title"], items["Bread"]["is_checked"]) == ("Bread", 0)


async def test_titles_from_google_are_read_not_rewritten(db):
    await db.execute("INSERT INTO shopping_items (title, google_task_id) VALUES ('Apples x 6', 'g1')")
    await db.commit()
    (item,) = await shopping.get_shopping_items(db)
    assert (item["title"], item["name"], item["quantity"]) == ("Apples x 6", "Apples", 6)


# --- Aisles ---


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("Bananas", "fruit_veg"),
        ("Cherry tomatoes x2", "fruit_veg"),
        ("Semi skimmed milk", "dairy"),
        ("Eggs", "dairy"),
        ("Frozen peas", "frozen"),
        ("Ice cream", "frozen"),
        ("Sliced bread", "bakery"),
        ("Chicken thighs", "meat_fish"),
        ("Toilet roll", "household"),
        ("Toothpaste", "toiletries"),
        ("Nappies", "baby"),
        ("Pasta", "cupboard"),
        ("Orange juice", "drinks"),  # the thing itself comes last
        ("Coconut milk", "cupboard"),  # the longer phrase
        ("Peanut butter", "cupboard"),
        ("Chocolate milk", "dairy"),
        ("Fish fingers", "frozen"),
        ("Tinned tomatoes", "cupboard"),
        ("Chicken soup", "cupboard"),
        ("Crisps", "snacks"),
        ("Birthday card", "other"),
    ],
)
def test_guess_category(name, category):
    assert shopping.guess_category(name) == category


async def test_a_corrected_aisle_is_remembered_for_the_name(db, client):
    await shopping.add_item(db, "Party rings")  # no idea: Other
    (item,) = await shopping.get_shopping_items(db)
    assert item["category"] == "other"
    resp = await client.post(f"/api/shopping/{item['id']}/category", data={"category": "snacks"})
    assert resp.status_code == 200
    assert (await shopping.get_shopping_items(db))[0]["category"] == "snacks"

    # Next week's "party rings x2" goes there too.
    await shopping.delete_item(db, item["id"])
    await shopping.add_item(db, "party rings x2")
    assert (await shopping.get_shopping_items(db))[0]["category"] == "snacks"

    assert (await client.post(f"/api/shopping/{item['id']}/category", data={"category": "snacks"})).status_code == 404
    new_id = (await shopping.get_shopping_items(db))[0]["id"]
    assert (await client.post(f"/api/shopping/{new_id}/category", data={"category": "nope"})).status_code == 404


async def test_aisle_order_moves_and_survives_odd_settings(db):
    order = await shopping.get_order(db)
    assert order == list(shopping.CATEGORIES)
    assert await shopping.move_category(db, "frozen", -1)
    assert (await shopping.get_order(db)).index("frozen") == order.index("frozen") - 1
    assert await shopping.move_category(db, "fruit_veg", -1)  # already first: stays
    assert (await shopping.get_order(db))[0] == "fruit_veg"
    assert not await shopping.move_category(db, "nope", 1)

    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        (shopping.ORDER_SETTING, '["dairy", "dairy", "gone", 7]'),
    )
    await db.commit()
    fixed = await shopping.get_order(db)
    assert fixed[0] == "dairy" and sorted(fixed) == sorted(shopping.CATEGORIES)


# --- The widget ---


async def test_simple_view_shows_quantities(db, client):
    await shopping.add_item(db, "Milk x2")
    html = (await client.get("/widgets/shopping")).text
    assert 'Milk <span class="shopping-qty">×2</span>' in html
    assert "shopping-group" not in html and "shopping-aisle" not in html


async def test_grouped_view_follows_the_aisle_order(db, client):
    await shopping.set_view(db, "grouped")
    for title in ("Toilet roll", "Bananas", "Milk", "Birthday card"):
        await shopping.add_item(db, title)
    await shopping.move_category(db, "household", -1)
    (milk,) = [i for i in await shopping.get_shopping_items(db) if i["name"] == "Milk"]
    await shopping.toggle_item(db, milk["id"])

    html = (await client.get("/widgets/shopping")).text
    headings = [part.split("</h3>")[0] for part in html.split('<h3 class="shopping-group-name">')[1:]]
    assert headings == ["Fruit &amp; veg", "Household", "Other", "In the basket"]
    assert html.count('class="shopping-aisle-select"') == 3  # the ticked milk has no picker
    assert '<option value="household" selected>Household</option>' in html
    assert f'hx-post="/api/shopping/{milk["id"]}/toggle"' in html.split("In the basket")[1]

    # The dashboard renders the same thing.
    assert "In the basket" in (await client.get("/")).text


# --- Admin ---


async def test_admin_sets_the_view_and_the_aisle_order(db, admin_client):
    resp = await admin_client.post("/admin/shopping/view", data={"view": "grouped"})
    assert resp.status_code == 303 and resp.headers["location"].endswith("tab=display#shopping")
    assert await shopping.get_view(db) == "grouped"

    resp = await admin_client.post("/admin/shopping/view", data={"view": "fancy"})
    assert resp.headers["location"].endswith("error=shopping-view#shopping")
    assert await shopping.get_view(db) == "grouped"

    await admin_client.post("/admin/shopping/order", data={"category": "household", "step": "-1"})
    assert (await shopping.get_order(db)).index("household") == list(shopping.CATEGORIES).index("household") - 1

    page = (await admin_client.get("/admin?tab=display")).text
    assert 'id="shopping"' in page and 'aria-label="Move Fruit &amp; veg up" disabled' in page
