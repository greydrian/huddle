"""
Recipe cards (spec 10.7): fetch a recipe page server-side and build a clean
card from the schema.org `Recipe` JSON-LD most recipe sites publish.

The URL is one a parent saved in Admin, but the fetch runs on the server
inside the family's network, so it is guarded against being pointed
inward (SSRF):
- http/https only, no user:password@, ports 80/443 only;
- the host name is resolved here and **every** address must be public
  (`ipaddress.is_global`); the request then goes to that checked address,
  with the real name in the Host header and TLS SNI (so the certificate is
  still checked against the name), so DNS can't answer differently between
  the check and the connection;
- redirects are followed by hand, at most MAX_REDIRECTS, each re-checked;
- only HTML is read, at most MAX_BYTES, within TIMEOUT.
Failures raise RecipeError with a log-safe code; nothing here logs a URL.
"""

import asyncio
import html
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from app import http_client

MAX_URL = 500
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
TIMEOUT = httpx.Timeout(8.0, connect=4.0)
PORTS = {"http": 80, "https": 443}
USER_AGENT = "Huddle family display (recipe card)"

MAX_INGREDIENTS = 60
MAX_STEPS = 40
MAX_TEXT = 500


class RecipeError(Exception):
    """A recipe couldn't be read. `code`: bad_url, blocked, offline,
    http_error, not_html, too_big, too_many_redirects, no_recipe."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def clean_url(url: str) -> str | None:
    """The URL as saved, or None if it isn't a plain http(s) link."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in PORTS or not parts.hostname or parts.username or parts.password:
        return None
    if port is not None and port != PORTS[parts.scheme]:
        return None
    return url


async def _resolve(host: str, port: int) -> list[str]:
    """Every address the name resolves to (tests replace this)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _checked_target(url: str) -> tuple[str, str, str]:
    """(URL to request, Host header, SNI name) for a public address."""
    if clean_url(url) is None:
        raise RecipeError("bad_url")
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or PORTS[parts.scheme]
    try:
        addresses = [host] if _is_ip(host) else await _resolve(host, port)
    except OSError:
        raise RecipeError("offline") from None
    if not addresses or not all(_public(a) for a in addresses):
        raise RecipeError("blocked")
    address = addresses[0]
    literal = f"[{address}]" if ":" in address else address
    path = parts.path or "/"
    target = f"{parts.scheme}://{literal}:{port}{path}" + (f"?{parts.query}" if parts.query else "")
    host_header = host if parts.port is None else f"{host}:{port}"
    return target, host_header, host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def fetch_page(url: str) -> str:
    """The page's HTML, following up to MAX_REDIRECTS checked redirects."""
    async with http_client.client(TIMEOUT) as client:
        for _ in range(MAX_REDIRECTS + 1):
            target, host_header, sni = await _checked_target(url)
            request = client.build_request(
                "GET",
                target,
                headers={"Host": host_header, "User-Agent": USER_AGENT, "Accept": "text/html"},
                extensions={"sni_hostname": sni},
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError:
                raise RecipeError("offline") from None
            try:
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise RecipeError("http_error")
                if "html" not in response.headers.get("content-type", "").lower():
                    raise RecipeError("not_html")
                body = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise RecipeError("too_big")
                except httpx.HTTPError:
                    raise RecipeError("offline") from None
                return body.decode(response.encoding or "utf-8", errors="replace")
            finally:
                await response.aclose()
    raise RecipeError("too_many_redirects")


# --- Parsing ---

_LD_JSON = re.compile(r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _text(value, limit: int = MAX_TEXT) -> str:
    text = html.unescape(_TAG.sub(" ", str(value or "")))
    return " ".join(text.split())[:limit]


def _is_recipe(node) -> bool:
    kind = node.get("@type")
    return kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind)


def _find_recipe(data):
    if isinstance(data, list):
        for item in data:
            found = _find_recipe(item)
            if found:
                return found
    elif isinstance(data, dict):
        if _is_recipe(data):
            return data
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data:
                found = _find_recipe(data[key])
                if found:
                    return found
    return None


def _steps(value) -> list[dict]:
    """[{"section": name or None, "text": ...}] from a string, a list of
    strings, HowToSteps or HowToSections."""
    steps: list[dict] = []

    def add(item, section=None):
        if len(steps) >= MAX_STEPS:
            return
        if isinstance(item, str):
            for line in re.split(r"\n+|(?<=\.)\s{2,}", html.unescape(item)):
                if _text(line):
                    steps.append({"section": section, "text": _text(line)})
        elif isinstance(item, list):
            for sub in item:
                add(sub, section)
        elif isinstance(item, dict):
            if "itemListElement" in item:
                add(item["itemListElement"], _text(item.get("name"), 80) or section)
            else:
                text = _text(item.get("text") or item.get("name"))
                if text:
                    steps.append({"section": section, "text": text})

    add(value)
    return steps


def _duration(value) -> str | None:
    """ISO 8601 "PT1H15M" as "1 h 15 min"."""
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", str(value or "").strip())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    hours += days * 24
    parts = ([f"{hours} h"] if hours else []) + ([f"{minutes} min"] if minutes else [])
    return " ".join(parts) or None


def _yield(value) -> str | None:
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and not v.strip().isdigit()), value[0] if value else None)
    text = _text(value, 60)
    return (f"Serves {text}" if text.isdigit() else text) or None


def parse_recipe(page: str) -> dict:
    """The card from a page's JSON-LD: {"title", "ingredients", "steps",
    "time", "prep", "cook", "serves"}. Raises RecipeError("no_recipe")."""
    for block in _LD_JSON.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        recipe = _find_recipe(data)
        if not recipe:
            continue
        ingredients = [_text(i) for i in (recipe.get("recipeIngredient") or recipe.get("ingredients") or [])]
        card = {
            "title": _text(recipe.get("name"), 120),
            "ingredients": [i for i in ingredients if i][:MAX_INGREDIENTS],
            "steps": _steps(recipe.get("recipeInstructions")),
            "time": _duration(recipe.get("totalTime")),
            "prep": _duration(recipe.get("prepTime")),
            "cook": _duration(recipe.get("cookTime")),
            "serves": _yield(recipe.get("recipeYield")),
        }
        if card["ingredients"] or card["steps"]:
            return card
    raise RecipeError("no_recipe")


async def fetch_card(url: str) -> dict:
    return parse_recipe(await fetch_page(url))
