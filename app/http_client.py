"""
Shared outbound HTTP settings for the Google and Open-Meteo clients.

A fresh AsyncClient per call rather than one long-lived module client:
traffic is tiny (a sync every 60s plus widget renders), a per-call client
needs no lifespan wiring, and it never outlives the event loop it was made
on — pytest runs each test on its own loop, and respx patches the transport
either way. What *is* shared is the timeout, so nothing waits on httpx's
bare default.

(Named http_client, not http, so running a file from inside app/ can never
shadow the stdlib `http` package.)
"""

import logging

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def client(timeout: httpx.Timeout = DEFAULT_TIMEOUT, trust_env: bool = True) -> httpx.AsyncClient:
    """Use as `async with http_client.client() as c: ...`. trust_env=False
    ignores proxy settings from the environment (app/recipes.py, which
    connects to a checked address itself)."""
    return httpx.AsyncClient(timeout=timeout, trust_env=trust_env)


# Upstreams currently failing, by key. Callers retry on every sync cycle,
# poll and page load, so a long outage would otherwise log a WARNING each
# time — log the transition into failure once, and the recovery once.
_failing: set[str] = set()


def report_failure(logger: logging.Logger, key: str, message: str, *args, exc_info=None) -> None:
    """WARNING the first time `key` fails; silent while it stays failing.
    exc_info adds the traceback — only for unexpected (non-HTTP) errors: an
    httpx traceback can include the request URL."""
    if key not in _failing:
        _failing.add(key)
        logger.warning(message, *args, exc_info=exc_info)


def report_success(logger: logging.Logger, key: str) -> None:
    """INFO once when a failing `key` works again; silent otherwise."""
    if key in _failing:
        _failing.discard(key)
        logger.info("%s recovered", key)


def reset_failures() -> None:
    """Forget all outage state (tests)."""
    _failing.clear()


def describe(exc: BaseException) -> str:
    """A log-safe summary of a failed request: the exception type, plus the
    status code for HTTP errors. Deliberately never the URL or message —
    some requests (token revoke) carry secrets in the query string."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__
