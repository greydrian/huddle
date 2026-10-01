"""Recipe cards (app/recipes.py): the guarded fetch and the JSON-LD parser."""

import json

import httpx
import pytest

from app import recipes

PUBLIC = "93.184.216.34"
PAGE_URL = "https://www.example.com/recipes/lasagne"
TARGET = f"https://{PUBLIC}:443/recipes/lasagne"

RECIPE = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": "WebPage", "name": "Lasagne"},
        {
            "@type": ["Recipe", "NewsArticle"],
            "name": "Classic &amp; easy lasagne",
            "recipeIngredient": ["500g beef mince", "<b>1</b> onion", "", "9 lasagne sheets"],
            "recipeInstructions": [
                {
                    "@type": "HowToSection",
                    "name": "For the sauce",
                    "itemListElement": [
                        {"@type": "HowToStep", "text": "Fry the onion."},
                        {"@type": "HowToStep", "text": "Add the mince."},
                    ],
                },
                {"@type": "HowToStep", "text": "Layer and bake."},
            ],
            "totalTime": "PT1H15M",
            "prepTime": "PT15M",
            "cookTime": "PT1H",
            "recipeYield": ["6", "6 servings"],
        },
    ],
}


def page(data=RECIPE):
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body>…</body></html>'


@pytest.fixture
def dns(monkeypatch):
    """Host name -> addresses, instead of real DNS."""
    table = {"www.example.com": [PUBLIC], "example.com": [PUBLIC]}

    async def resolve(host, port):
        if host not in table:
            raise OSError("no such host")
        return table[host]

    monkeypatch.setattr(recipes, "_resolve", resolve)
    return table


# --- URLs ---


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bbcgoodfood.com/recipes/easy-lasagne",
        "http://example.com/a?b=c",
        "https://example.com:443/x",
    ],
)
def test_clean_url_accepts_plain_links(url):
    assert recipes.clean_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://user:pass@example.com/",
        "https://example.com:8080/",
        "https://example.com:99999/",
        "https://exa mple.com/",
        "https:///nohost",
        "https://example.com/" + "a" * 600,
    ],
)
def test_clean_url_refuses_the_rest(url):
    assert recipes.clean_url(url) is None


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.9", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1",  # noqa: S104 - an address to refuse, not bind
     "fc00::1", "fe80::1", "::ffff:192.168.1.1", "224.0.0.1"],
)  # fmt: skip
def test_inward_addresses_are_not_public(address):
    assert not recipes._public(address)


def test_a_public_address_is(dns):
    assert recipes._public(PUBLIC) and recipes._public("2606:4700::1111")


# --- The fetch ---


async def test_fetches_by_the_checked_address_with_the_real_name(google, dns):
    route = google.get(TARGET).respond(200, html=page())
    card = await recipes.fetch_card(PAGE_URL)
    request = route.calls.last.request
    assert request.headers["host"] == "www.example.com"
    assert request.extensions["sni_hostname"] == "www.example.com"
    assert card == {
        "title": "Classic & easy lasagne",
        "ingredients": ["500g beef mince", "1 onion", "9 lasagne sheets"],
        "steps": [
            {"section": "For the sauce", "text": "Fry the onion."},
            {"section": "For the sauce", "text": "Add the mince."},
            {"section": None, "text": "Layer and bake."},
        ],
        "time": "1 h 15 min",
        "prep": "15 min",
        "cook": "1 h",
        "serves": "6 servings",
    }


@pytest.mark.parametrize("address", ["192.168.1.20", "127.0.0.1"])
async def test_a_name_that_points_inward_is_never_fetched(google, dns, address):
    dns["www.example.com"] = [PUBLIC, address]  # every address must be public
    with pytest.raises(recipes.RecipeError, match="blocked"):
        await recipes.fetch_card(PAGE_URL)
    assert not google.calls


async def test_redirects_are_rechecked(google, dns):
    dns["router.example.com"] = ["192.168.1.1"]
    google.get(TARGET).respond(302, headers={"Location": "http://router.example.com/admin"})
    with pytest.raises(recipes.RecipeError, match="blocked"):
        await recipes.fetch_card(PAGE_URL)
    assert len(google.calls) == 1

    google.get(TARGET).respond(301, headers={"Location": "/recipes/lasagne"})  # to itself, forever
    with pytest.raises(recipes.RecipeError, match="too_many_redirects"):
        await recipes.fetch_card(PAGE_URL)


async def test_a_good_redirect_is_followed(google, dns):
    google.get(f"http://{PUBLIC}:80/old").respond(301, headers={"Location": PAGE_URL})
    google.get(TARGET).respond(200, html=page())
    assert (await recipes.fetch_card("http://example.com/old"))["title"] == "Classic & easy lasagne"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(404, html="gone"), "http_error"),
        (httpx.Response(200, json={"a": 1}), "not_html"),
        (httpx.Response(200, html="<html>no recipe here</html>"), "no_recipe"),
    ],
)
async def test_pages_that_arent_recipes(google, dns, response, code):
    google.get(TARGET).mock(return_value=response)
    with pytest.raises(recipes.RecipeError, match=code):
        await recipes.fetch_card(PAGE_URL)


async def test_big_pages_and_outages(google, dns, monkeypatch):
    monkeypatch.setattr(recipes, "MAX_BYTES", 100)
    google.get(TARGET).respond(200, html=page())
    with pytest.raises(recipes.RecipeError, match="too_big"):
        await recipes.fetch_card(PAGE_URL)

    google.get(TARGET).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(recipes.RecipeError, match="offline"):
        await recipes.fetch_card(PAGE_URL)

    with pytest.raises(recipes.RecipeError, match="offline"):
        await recipes.fetch_card("https://unknown.example.org/x")  # DNS fails
    with pytest.raises(recipes.RecipeError, match="bad_url"):
        await recipes.fetch_card("ftp://example.com/x")


# --- Parsing ---


def test_parse_plain_string_steps_and_a_bare_recipe():
    data = {
        "@type": "Recipe",
        "name": "Toast",
        "recipeIngredient": ["Bread"],
        "recipeInstructions": "Toast the bread.\nButter it.",
        "recipeYield": 2,
        "totalTime": "P0DT0H10M",
    }
    card = recipes.parse_recipe(page(data))
    assert [s["text"] for s in card["steps"]] == ["Toast the bread.", "Butter it."]
    assert (card["serves"], card["time"], card["prep"]) == ("Serves 2", "10 min", None)


def test_parse_skips_broken_json_and_other_types():
    broken = '<script type="application/ld+json">{not json</script>'
    other = '<script type="application/ld+json">{"@type": "Organization", "name": "x"}</script>'
    assert recipes.parse_recipe(broken + other + page())["title"] == "Classic & easy lasagne"
    with pytest.raises(recipes.RecipeError, match="no_recipe"):
        recipes.parse_recipe(broken + other)


def test_parse_caps_long_recipes():
    data = {"@type": "Recipe", "name": "Big", "recipeIngredient": [f"thing {i}" for i in range(200)]}
    data["recipeInstructions"] = [{"@type": "HowToStep", "text": "x" * 2000}] * 100
    card = recipes.parse_recipe(page(data))
    assert len(card["ingredients"]) == recipes.MAX_INGREDIENTS and len(card["steps"]) == recipes.MAX_STEPS
    assert len(card["steps"][0]["text"]) == recipes.MAX_TEXT
