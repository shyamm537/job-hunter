"""Ashby scraper — reads a company's public Ashby job board API.

Ashby (a modern ATS) exposes a public, unauthenticated JSON API:

    https://api.ashbyhq.com/posting-api/job-board/<org>

`<org>` is the public board token (the `ashby` in `jobs.ashbyhq.com/ashby`).
This is the project's third ATS source — same pattern as Greenhouse and Lever,
so one scraper unlocks every company hosted on Ashby.

Shape note: the response is `{"jobs": [...]}` like Greenhouse, but Ashby splits
remote out into a separate `isRemote` flag rather than baking it into the
location string. We fold that flag back into the location so the shared
"remote always passes" filter (src/ingestion/filtering.py) behaves the same as
it does for the other ATSs.
"""

import hashlib
from typing import List

from src.ingestion.base_scraper import BaseScraper
from src.ingestion.http_util import get_json
from src.storage.models import JobPost

API_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{org}"


class AshbyScraper(BaseScraper):
    source_name = "ashby"

    def __init__(self, org: str):
        self.org = org

    def _api_url(self) -> str:
        return API_TEMPLATE.format(org=self.org.strip())

    def scrape(self) -> List[JobPost]:
        data = get_json(self._api_url())
        jobs: List[JobPost] = []

        for entry in data.get("jobs", []):
            # Skip unlisted postings (Ashby flags them explicitly).
            if entry.get("isListed") is False:
                continue

            title = entry.get("title", "Untitled")
            url = entry.get("jobUrl") or entry.get("applyUrl") or ""
            # Same dedup convention as the other scrapers.
            job_board_id = f"ashby-{hashlib.sha1(url.encode()).hexdigest()[:10]}"

            location = entry.get("location") or ""
            # Ashby exposes "remote" as a boolean, not in the location string.
            # Fold it in so the shared remote-passes filter sees it.
            if entry.get("isRemote") and "remote" not in location.lower():
                location = f"{location} (Remote)" if location else "Remote"

            description = (
                entry.get("descriptionPlain") or entry.get("descriptionHtml") or ""
            )

            jobs.append(
                JobPost(
                    job_board_id=job_board_id,
                    title=title,
                    # Ashby's posting API has no clean company-name field; the
                    # org token IS the company, so use it (like Greenhouse/Lever).
                    company=self.org,
                    location=location,
                    description=description,
                    url=url,
                )
            )

        return jobs
