"""Small shared HTTP helper for scrapers.

`SeekScraper` makes a single feed request and uses feedparser directly, so
it doesn't need this. Anything that hits a JSON/HTTP API (like the
Greenhouse scraper) goes through `get_json()` for one consistent place to
handle timeouts, a polite User-Agent, and a couple of retries with
backoff. This is the "rate limiting / politeness" seam noted as missing in
docs/scrapers.md.
"""

import time
from typing import Any

import requests

USER_AGENT = "job-hunter-ai/0.1 (+https://github.com/your/repo)"


def get_json(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 2,
    backoff: float = 1.0,
) -> Any:
    """GET a URL and return parsed JSON, retrying on transient failures.

    Retries on connection errors, timeouts, and 5xx responses. A 4xx is
    treated as a hard error and raised immediately — retrying a bad request
    or a missing board won't help.
    """
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url, timeout=timeout, headers={"User-Agent": USER_AGENT}
            )
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
