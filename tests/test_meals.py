"""Meals (spec 10.7, app/services/meals.py): favourites, the picker, recipe
links and notes, and the recipe card overlay."""

from datetime import date, timedelta

import pytest

from app import recipes
from app.routers import admin
from app.security import create_session_token
from app.services import meals

DAY = date(2026, 10, 5)
CARD = {
    "title": "Lasagne",
    "ingredients": ["Mince", "Pasta sheets"],
    "steps": [
        {"section": "For the sauce", "text": "Fry."},
        {"section": "For the sauce", "text": "Simmer."},
        {"section": None, "text": "Bake it."},
    ],
    "time": "1 h",
    "prep": None,
    "cook": None,
    "serves": "Serves 4",
}
LINK = "https://www.example.com/lasagne"


@pytest.fixture
def admin_client(client):
    client.cookies.set(admin.SESSION_COOKIE, create_session_token())
    return client


@pytest.fixture
def today(monkeypatch):
    async def fixed(db):
        return DAY

    monkeypatch.setattr(meals, "family_today", fixed)
    return DAY


@pytest.fixture
def fetches(monkeypatch):
    """Recipe fetches: the URLs asked for; set .result to a card or a RecipeError."""

    class Fetches(list):
        result: object = CARD

    calls = Fetches()

    async def fetch_card(url):
        calls.append(url)
        if isinstance(calls.result, Exception):
            raise calls.result
        return calls.result

    monkeypatch.setattr(recipes, "fetch_card", fetch_card)
    return calls


async def plan(db, *meals_by_day):
    for offset, name in enumerate(meals_by_day):
        if name:
            await meals.set_meal(db, DAY - timedelta(days=30) + timedelta(days=offset), name)


# --- Favourites ---


async def test_favourites_are_past_meals_most_used_first(db):
    await plan(db, "Pasta bake", "Fish pie", "pasta  BAKE", "Tacos", "Fish pie", "Pasta bake", "")
    names = [(f["name"], f["uses"]) for f in await meals.favourites(db)]
    assert names == [("Pasta bake", 3), ("Fish pie", 2), ("Tacos", 1)]

    # Re-typing one day doesn't count twice.
    await meals.set_meal(db, DAY - timedelta(days=30), "Pasta bake")
    assert (await meals.favourites(db))[0]["uses"] == 3


async def test_starred_lead_and_removed_ones_hide(db):
    await plan(db, "Pasta bake", "Pasta bake", "Fish pie", "Tacos")
    await meals.set_starred(db, "tacos", True)
    await meals.set_hidden(db, "Fish Pie", True)
    assert [f["name"] for f in await meals.favourites(db)] == ["Tacos", "Pasta bake"]
    assert await meals.picker(db) == ["Tacos", "Pasta bake"]
    everything = await meals.favourites(db, include_hidden=True)
    assert [f["name"] for f in everything if f["hidden"]] == ["Fish pie"]


async def test_a_favourite_can_be_added_before_it_is_planned(db):
    await meals.set_recipe(db, "Lasagne", LINK, "Double the sauce")
    (fav,) = await meals.favourites(db)
    assert (fav["name"], fav["uses"], fav["recipe_url"], fav["notes"]) == ("Lasagne", 0, LINK, "Double the sauce")


@pytest.mark.parametrize(
    ("url", "notes", "code"),
    [
        ("javascript:alert(1)", "", "meal-link"),
        ("http://x.example/a b", "", "meal-link"),
        ("", "x" * 1001, "meal-notes"),
    ],
)
async def test_bad_recipe_links_and_notes_are_refused(db, url, notes, code):
    with pytest.raises(ValueError, match=code):
        await meals.set_recipe(db, "Lasagne", url, notes)
    assert await meals.favourites(db) == []


# --- The widget ---


async def test_widget_has_a_picker_and_a_recipe_button(db, client, today):
    await plan(db, "Fish pie", "Lasagne")
    await meals.set_meal(db, DAY, "Lasagne")
    await meals.set_recipe(db, "lasagne", LINK, "")
    html = (await client.get("/widgets/meals")).text
    assert html.count('class="meal-picker-select"') == 6  # not on the planned day
    assert '<option value="Lasagne">Lasagne</option>' in html and '<option value="Fish pie">' in html
    assert html.count('class="meal-recipe"') == 1 and 'aria-label="Recipe for Lasagne"' in html

    # Picking a favourite plans it.
    tomorrow = (DAY + timedelta(days=1)).isoformat()
    resp = await client.post(f"/api/meals/{tomorrow}", data={"description": "Fish pie"})
    assert 'aria-label="Tuesday meal"' in resp.text and 'value="Fish pie" placeholder' in resp.text
    assert '<div id="wall-overlay"></div>' in (await client.get("/")).text


async def test_no_favourites_means_no_picker(db, client, today):
    assert "meal-picker" not in (await client.get("/widgets/meals")).text


# --- Recipe cards ---


async def test_recipe_card_reads_the_link_once_and_shows_a_qr_code(db, client, fetches):
    await meals.set_recipe(db, "Lasagne", LINK, "Double the sauce")
    html = (await client.get("/meals/recipe", params={"name": "lasagne"})).text
    assert 'role="dialog"' in html and "Pasta sheets" in html and "Bake it." in html
    assert "Total 1 h" in html and "Serves 4" in html and "Double the sauce" in html
    # A section is a heading over its own steps, numbered on: 1, 2, then 3.
    assert '<h4 class="recipe-section">For the sauce</h4>' in html
    assert '<ol class="recipe-steps" start="1"><li>Fry.</li><li>Simmer.</li></ol>' in html
    assert '<ol class="recipe-steps" start="3"><li>Bake it.</li></ol>' in html
    assert 'class="recipe-qr"' in html and "Open on a phone" in html
    await client.get("/meals/recipe", params={"name": "Lasagne"})
    assert fetches == [LINK]  # cached


async def test_an_unreadable_recipe_falls_back_to_the_qr_code(db, client, fetches, monkeypatch):
    fetches.result = recipes.RecipeError("no_recipe")
    await meals.set_recipe(db, "Lasagne", LINK, "")
    html = (await client.get("/meals/recipe", params={"name": "Lasagne"})).text
    assert "no recipe details Huddle can read" in html and "Scan the code" in html and 'class="recipe-qr"' in html

    # Failures are cached for an hour, then tried again.
    await client.get("/meals/recipe", params={"name": "Lasagne"})
    assert len(fetches) == 1
    later = meals._now() + timedelta(hours=2)
    monkeypatch.setattr(meals, "_now", lambda: later)
    fetches.result = CARD
    assert "Pasta sheets" in (await client.get("/meals/recipe", params={"name": "Lasagne"})).text
    assert len(fetches) == 2


async def test_a_surprise_in_the_fetch_never_500s(db, client, fetches):
    fetches.result = KeyError("boom")
    await meals.set_recipe(db, "Lasagne", LINK, "")
    resp = await client.get("/meals/recipe", params={"name": "Lasagne"})
    assert resp.status_code == 200 and "Couldn&#39;t reach" in resp.text


async def test_notes_only_and_unknown_meals(db, client, fetches):
    await meals.set_recipe(db, "Toast", "", "Butter first")
    html = (await client.get("/meals/recipe", params={"name": "Toast"})).text
    assert "Butter first" in html and 'class="recipe-qr"' not in html
    html = (await client.get("/meals/recipe", params={"name": "Mystery"})).text
    assert "No recipe saved for this meal yet" in html
    assert fetches == []


# --- Admin ---


async def test_admin_manages_favourites(db, admin_client):
    await plan(db, "Pasta bake", "Fish pie")
    resp = await admin_client.post("/admin/meals/recipe", data={"name": "Lasagne", "url": LINK, "notes": "Big dish"})
    assert resp.status_code == 303 and resp.headers["location"].endswith("tab=family#meals")
    resp = await admin_client.post("/admin/meals/recipe", data={"name": "Lasagne", "url": "ftp://nope"})
    assert resp.headers["location"].endswith("error=meal-link#meals")
    assert (
        (await admin_client.post("/admin/meals/recipe", data={"name": " "}))
        .headers["location"]
        .endswith("error=meal-name#meals")
    )

    await admin_client.post("/admin/meals/star", data={"name": "Fish pie", "starred": "true"})
    await admin_client.post("/admin/meals/hide", data={"name": "Pasta bake", "hidden": "true"})
    assert [f["name"] for f in await meals.favourites(db)] == ["Fish pie", "Lasagne"]

    page = (await admin_client.get("/admin?tab=family")).text
    assert 'id="meals"' in page and 'aria-label="Unstar Fish pie"' in page
    assert "Removed from favourites (1)" in page

    await admin_client.post("/admin/meals/hide", data={"name": "Pasta bake", "hidden": "false"})
    assert "Pasta bake" in [f["name"] for f in await meals.favourites(db)]
