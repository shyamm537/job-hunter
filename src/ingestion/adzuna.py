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
from urllib.parse import urlencode, urlsplit, urlunsplit

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


def _hash(value: str) -> str:
    """`adzuna-<sha1(value)[:10]>` — the shared `<source>-<hash>` id shape used
    by every scraper (see src/ingestion/*), so the source prefix and id length
    stay uniform regardless of which input we hashed."""
    return f"adzuna-{hashlib.sha1(value.encode()).hexdigest()[:10]}"


def id_dedup_key(ad_id: str) -> str:
    """Preferred dedup id: hash of Adzuna's own primary key (`id`).

    `id` is stable regardless of which search surfaced the ad and doesn't depend
    on the redirect URL's shape, so it's the robust dedup key. We hash it (rather
    than use it raw) purely so the id format matches the other scrapers'.
    """
    return _hash(str(ad_id))


def url_dedup_key(redirect_url: str) -> str:
    """Fallback dedup id from an Adzuna redirect URL (used when `id` is absent).

    Adzuna's redirect_url carries per-search tracking params (se, utm_*, v, ...)
    in the query string, so the SAME ad surfaced under two location-alias
    searches (e.g. 'bangalore' vs 'bengaluru', if both are ever queried) comes
    back with different URLs and would otherwise hash to two different ids. The
    job's identity lives in the PATH (.../land/ad/<id>), so we hash
    scheme+host+path only, dropping query and fragment, so those duplicates
    collapse to one job_board_id. The full URL is still stored on the JobPost.

    This is also the *legacy* key scheme: rows scraped before the switch to
    `id` are keyed this way, which is how the one-time re-key migration
    (src/storage/database.py) recognizes them.
    """
    parts = urlsplit(redirect_url)
    canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return _hash(canonical)


def adzuna_id_from_url(redirect_url: str) -> Optional[str]:
    """Extract Adzuna's ad id from a redirect URL path (`.../land/ad/<id>`).

    Returns None if the path doesn't carry one. Only used by the legacy->id
    dedup-key migration, which has the stored URL but not the original `id`.
    """
    parts = [p for p in urlsplit(redirect_url).path.split("/") if p]
    for i in range(len(parts) - 2):
        if parts[i] == "land" and parts[i + 1] == "ad":
            return parts[i + 2]
    return None


def dedup_key(entry: dict) -> str:
    """Stable per-job dedup id for one Adzuna search result.

    Prefer Adzuna's own `id` (parse-free and independent of the redirect URL's
    shape); fall back to hashing the URL path when `id` is missing or empty.
    Belt-and-suspenders: the fallback keeps dedup working even if a result ever
    omits `id`, and it complements rather than replaces the URL normalization.
    """
    ad_id = str(entry.get("id") or "").strip()
    if ad_id:
        return id_dedup_key(ad_id)
    return url_dedup_key(entry.get("redirect_url", ""))


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
                job_board_id = dedup_key(entry)
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
