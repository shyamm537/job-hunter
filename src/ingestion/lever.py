"""Lever scraper — reads a company's public Lever postings API.

Like Greenhouse, Lever hosts many companies' job boards behind a public,
unauthenticated JSON API:

    https://api.lever.co/v0/postings/<company>?mode=json

`<company>` is the posting token, e.g. the `figma` in `jobs.lever.co/figma`.
No login, no API key. This is the project's third scraper and second ATS
(applicant-tracking-system) source — adding it took one new file, one config
model, and one factory branch, nothing else. That's the Strategy Pattern
paying off: one scraper per ATS unlocks every company hosted on it.
"""

import hashlib
from typing import List, Optional

from src.ingestion.base_scraper import BaseScraper
from src.ingestion.http_util import get_json
from src.storage.models import JobPost

API_TEMPLATE = "https://api.lever.co/v0/postings/{company}?mode=json"


class LeverScraper(BaseScraper):
    source_name = "lever"

    def __init__(self, company: str, title_contains: Optional[str] = None):
        self.company = company
        self.title_contains = title_contains

    def _api_url(self) -> str:
        return API_TEMPLATE.format(company=self.company.strip())

    def scrape(self) -> List[JobPost]:
        data = get_json(self._api_url())
        jobs: List[JobPost] = []

        # Lever returns a top-level JSON array of postings (not a dict).
        for entry in data:
            title = entry.get("text", "Untitled")

            if (
                self.title_contains
                and self.title_contains.lower() not in title.lower()
            ):
                continue

            url = entry.get("hostedUrl", "")
            # Same dedup convention as the other scrapers: "<source>-<sha1(url)[:10]>".
            job_board_id = f"lever-{hashlib.sha1(url.encode()).hexdigest()[:10]}"

            location = (entry.get("categories") or {}).get("location", "")
            description = entry.get("descriptionPlain") or entry.get("description", "")

            jobs.append(
                JobPost(
                    job_board_id=job_board_id,
                    title=title,
                    # Lever's API doesn't carry a display company name; the
                    # posting token IS the company, so use it.
                    company=self.company,
                    location=location,
                    description=description,
                    url=url,
                )
            )

        return jobs
