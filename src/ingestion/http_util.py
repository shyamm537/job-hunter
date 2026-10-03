"""Small shared HTTP helper for scrapers.

Anything that hits a JSON/HTTP API (like the Greenhouse scraper) goes through
`get_json()` (or `post_json()` for APIs that take a POST body) for one
consistent place to handle timeouts, a polite User-Agent, and a couple of
retries with backoff. `REQUEST_DELAY` is the politeness pause for scrapers that
make many requests in a row (see docs/scrapers.md).
"""

import time
from typing import Any, Callable, Dict, Optional

import requests

USER_AGENT = "job-hunter-ai/0.1 (+https://github.com/shyamm537/job-hunter)"

# Seconds a scraper sleeps between requests when it pages through a board or
# fetches one page per posting. The same value as DELAY in check_links.py.
REQUEST_DELAY = 0.5


def _headers(extra: Optional[Dict[str, str]]) -> Dict[str, str]:
    """The polite User-Agent plus any extra headers (which may override it)."""
    return {"User-Agent": USER_AGENT, **(extra or {})}


def _request_json(
    send: Callable[[], Any], *, retries: int, backoff: float
) -> Any:
    """Call `send()` for a response and return its parsed JSON, with retries.

    Retries on connection errors, timeouts, and 5xx responses. A 4xx is
    treated as a hard error and raised immediately — retrying a bad request
    or a missing board won't help.
    """
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        try:
            resp = send()
            if 500 <= resp.status_code < 600:
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            # Don't retry client errors (4xx) — they're not transient.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                raise
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * (2**attempt))

    assert last_exc is not None
    raise last_exc


def get_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    retries: int = 2,
    backoff: float = 1.0,
) -> Any:
    """GET a URL and return parsed JSON, retrying on transient failures.

    Retries on connection errors, timeouts, and 5xx responses. A 4xx is
    treated as a hard error and raised immediately — retrying a bad request
    or a missing board won't help. `headers` are added to the User-Agent.
    """
    return _request_json(
        lambda: requests.get(url, timeout=timeout, headers=_headers(headers)),
        retries=retries,
        backoff=backoff,
    )


def post_json(
    url: str,
    body: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    retries: int = 2,
    backoff: float = 1.0,
) -> Any:
    """POST `body` as JSON and return parsed JSON, with the same retry rules
    as get_json(): retry connection errors, timeouts and 5xx; raise at once on 4xx.
    """
    return _request_json(
        lambda: requests.post(
            url, json=body, timeout=timeout, headers=_headers(headers)
        ),
        retries=retries,
        backoff=backoff,
    )
