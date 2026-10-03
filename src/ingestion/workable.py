"""Workable scraper — reads a company's public Workable widget API.

Workable (an ATS popular with small and mid-size employers) exposes a public,
unauthenticated JSON endpoint behind its careers pages:

    https://apply.workable.com/api/v1/widget/accounts/<account>?details=true

`<account>` is the first path segment of the careers URL (the `squiz` in
`apply.workable.com/squiz`). It is case-sensitive: `SQUIZ` is a 404. An unknown
account is a 404 too, so a dead token raises and `make validate` catches it;
an account with no openings is a 200 with `"jobs": []`.

Shape notes (checked against live boards on 2026-10-03):

- `?details=true` puts the job description (HTML) on every job in the one list
  call, so there is no per-posting request.
- A posting with several locations appears ONCE PER LOCATION: the same
  `shortcode` and `url`, a different city each time. We merge those entries into
  one JobPost whose location lists every place, so the location filter sees all
  of them. (The site's own count equals the number of unique shortcodes.)
- Remote is a `telecommuting` boolean, not part of the location. Like Ashby we
  fold it into the location string so the shared "remote always passes" filter
  behaves the same for every source.
"""

import hashlib
import logging
from typing import Any, Dict, List

from src.ingestion.base_scraper import BaseScraper
from src.ingestion.dates import parse_posted_at
from src.ingestion.http_util import get_json
from src.ingestion.text_util import html_to_text
from src.storage.models import JobPost

API_TEMPLATE = "https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
POSTING_URL = "https://apply.workable.com/j/{shortcode}"

log = logging.getLogger("jobhunter.workable")


def _place(city: Any, region: Any, country: Any) -> str:
    """Non-empty parts of a place joined with ", " ("Sydney, New South Wales, Australia")."""
    return ", ".join(str(p).strip() for p in (city, region, country) if p and str(p).strip())


def _entry_places(entry: Dict[str, Any]) -> List[str]:
    """Places named by one list entry, skipping hidden ones.

    Uses the `locations` list when it has items; only when it is missing or empty
    does it fall back to the entry's top-level city/state/country. A location
    flagged `hidden` is left out and does not trigger the fallback.
    """
    items = entry.get("locations") or []
    if not items:
        place = _place(entry.get("city"), entry.get("state"), entry.get("country"))
        return [place] if place else []
    places: List[str] = []
    for item in items:
        if item.get("hidden"):
            continue
        place = _place(item.get("city"), item.get("region"), item.get("country"))
        if place:
            places.append(place)
    return places


class WorkableScraper(BaseScraper):
    source_name = "workable"

    def __init__(self, account: str):
        self.account = account

    def _api_url(self) -> str:
        # Case-sensitive on Workable's side: use the account exactly as given.
        return API_TEMPLATE.format(account=self.account.strip())

    def scrape(self) -> List[JobPost]:
        data = get_json(self._api_url())

        # One group per posting, in first-seen order: the widget repeats a
        # multi-location posting once per location.
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for entry in data.get("jobs", []):
            key = entry.get("shortcode") or entry.get("url") or entry.get("shortlink")
            if not key:
                log.warning("workable[%s]: skipping a job with no shortcode or url", self.account)
                continue
            groups.setdefault(key, []).append(entry)

        return [self._to_job(entries) for entries in groups.values()]

    def _to_job(self, entries: List[Dict[str, Any]]) -> JobPost:
        first = entries[0]
        shortcode = first.get("shortcode") or ""
        url = (
            first.get("url")
            or first.get("shortlink")
            or (POSTING_URL.format(shortcode=shortcode) if shortcode else "")
        )
        # Same dedup convention as the other scrapers.
        job_board_id = f"workable-{hashlib.sha1(url.encode()).hexdigest()[:10]}"

        places: List[str] = []
        for entry in entries:
            for place in _entry_places(entry):
                if place not in places:
                    places.append(place)
        location = "; ".join(places)
        # Workable exposes remote as a boolean, not in the location string.
        # Fold it in so the shared remote-passes filter sees it.
        if any(e.get("telecommuting") for e in entries) and "remote" not in location.lower():
            location = f"{location} (Remote)" if location else "Remote"

        raw_description = next(
            (e["description"] for e in entries if e.get("description")), ""
        )

        return JobPost(
            job_board_id=job_board_id,
            title=first.get("title") or "Untitled",
            # The account slug IS the company token (reconcile_board matches on
            # it), so use it rather than the display name in the response.
            company=self.account.strip(),
            location=location,
            description=html_to_text(raw_description),
            url=url,
            # `published_on` is a YYYY-MM-DD date; fall back to `created_at`.
            posted_at=parse_posted_at(first.get("published_on"))
            or parse_posted_at(first.get("created_at")),
        )
