"""Adzuna scraper — reads Adzuna's official public job-search API.

Adzuna replaces SEEK as the search/aggregator source (SEEK's public RSS went
dead). Unlike SEEK it's a sanctioned API with a free tier, and it's per-country,
so it can reach India as well as Australia:

    https://api.adzuna.com/v1/api/jobs/<country>/search/<page>
        ?app_id=...&app_key=...&what=<title>&where=<location>&results_per_page=50

`<country>` is a path segment (au, in, gb, ...). Credentials (app_id/app_key)
come from the top-level `adzuna:` config block; register free at
developer.adzuna.com. Like SEEK, it's a *search* source: each (title, location)
is one query, so the planner expands filters into queries and does NOT
post-filter (the query already narrowed it).

One real limitation: Adzuna returns only a *snippet* of each job description,
not the full text. Generated cover letters / cold emails and the contact lookup
have less to work with than on the ATS boards. That's a property of the API.
"""

import hashlib
from typing import List, Optional
from urllib.parse import urlencode

from src.ingestion.base_scraper import BaseScraper
from src.ingestion.http_util import get_json
from src.storage.models import JobPost

API_BASE = "https://api.adzuna.com/v1/api/jobs"

# Which Adzuna country index a location belongs to. Used by the planner to pair
# each filters.location with the right country source (Adelaide under `au`,
# Mumbai under `in`) instead of querying every city under every country. A
# location not listed here (incl. "remote") returns None → treated as
# region-agnostic and run under whichever Adzuna country sources you configured.
COUNTRY_OF = {
    # Australia
    "adelaide": "au", "brisbane": "au", "sydney": "au", "melbourne": "au",
    "perth": "au", "canberra": "au", "hobart": "au", "darwin": "au",
    "gold coast": "au", "newcastle": "au", "all australia": "au",
    # India
    "bangalore": "in", "bengaluru": "in", "mumbai": "in", "bombay": "in",
    "pune": "in", "gurugram": "in", "gurgaon": "in", "delhi": "in",
    "new delhi": "in", "hyderabad": "in", "chennai": "in", "kolkata": "in",
    "noida": "in", "ahmedabad": "in", "jaipur": "in",
}


def country_of(location: str) -> Optional[str]:
    """Adzuna country code for a location, or None if region-agnostic/unknown."""
    return COUNTRY_OF.get((location or "").strip().lower())


class AdzunaScraper(BaseScraper):
    source_name = "adzuna"

    def __init__(
        self,
        what: str,
        where: str,
        country: str,
        app_id: str,
        app_key: str,
        *,
        results_per_page: int = 50,
        max_pages: int = 2,
    ):
        self.what = what
        self.where = where
        self.country = country
        self.app_id = app_id
        self.app_key = app_key
        self.results_per_page = results_per_page
        self.max_pages = max_pages

    def _api_url(self, page: int) -> str:
        params = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "results_per_page": self.results_per_page,
            "what": self.what,
            "content-type": "application/json",
        }
        if self.where:
            params["where"] = self.where
        return f"{API_BASE}/{self.country}/search/{page}?{urlencode(params)}"

    def scrape(self) -> List[JobPost]:
        jobs: List[JobPost] = []
        for page in range(1, self.max_pages + 1):
            data = get_json(self._api_url(page))
            results = data.get("results", []) if isinstance(data, dict) else []
            for entry in results:
                url = entry.get("redirect_url", "")
                job_board_id = f"adzuna-{hashlib.sha1(url.encode()).hexdigest()[:10]}"
                company = (entry.get("company") or {}).get("display_name", "Unknown")
                location = (entry.get("location") or {}).get("display_name", "")
                jobs.append(
                    JobPost(
                        job_board_id=job_board_id,
                        title=entry.get("title", "Untitled"),
                        company=company,
                        location=location,
                        # Adzuna returns only a snippet here (documented limit).
                        description=entry.get("description", ""),
                        url=url,
                    )
                )
            # Last page reached when the API returns a short page.
            if len(results) < self.results_per_page:
                break
        return jobs
