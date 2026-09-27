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

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def client(timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    """Use as `async with http_client.client() as c: ...`."""
    return httpx.AsyncClient(timeout=timeout)


def describe(exc: Exception) -> str:
    """A log-safe summary of a failed request: the exception type, plus the
    status code for HTTP errors. Deliberately never the URL or message —
    some requests (token revoke) carry secrets in the query string."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__
