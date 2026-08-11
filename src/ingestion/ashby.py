# """Greenhouse scraper — reads a company's public Greenhouse job board.

# Many companies host their careers page on Greenhouse, which exposes a
# public, unauthenticated JSON API:

#     https://boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true

# `<board>` is the board token, e.g. the `stripe` in
# `boards.greenhouse.io/stripe`. No login, no API key, no anti-bot bypass —
# this is the same data the public board renders. That keeps it within the
# project's "public, non-authenticated" scope (see docs/scrapers.md), which
# LinkedIn/SEEK behind-auth scraping is deliberately not.

# This is the project's second scraper; its whole point is to prove the
# Strategy Pattern actually decouples — `BaseScraper`/the CLI didn't change
# shape to accommodate a completely different source (RSS vs. JSON API).
# """

import hashlib
from typing import List, Optional

from src.ingestion.base_scraper import BaseScraper
from src.ingestion.http_util import get_json
from src.storage.models import JobPost

API_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{board}"


class AshbyScraper(BaseScraper):
    source_name = "ashby"

    def __init__(self, board: str, title_contains: Optional[str] = None):
        self.board = board
        self.title_contains = title_contains

    def _api_url(self) -> str:
        return API_TEMPLATE.format(board=self.board.strip())

    def scrape(self) -> List[JobPost]:
        data = get_json(self._api_url())
        jobs: List[JobPost] = []

        for entry in data.get("jobs", []):
            title = entry.get("title", "Untitled")

            if (
                self.title_contains
                and self.title_contains.lower() not in title.lower()
            ):
                continue

            url = entry.get("jobUrl", "")
            # Same dedup convention as SeekScraper: "<source>-<sha1(url)[:10]>".
            job_board_id = f"{self.source_name}-{hashlib.sha1(url.encode()).hexdigest()[:10]}"

            location = (entry.get("location") or {})

            jobs.append(
                JobPost(
                    job_board_id=job_board_id,
                    title=title,
                    # Greenhouse's jobs endpoint doesn't carry a company name;
                    # the board token IS the company, so use it.
                    company=self.board,
                    location=location,
                    description=entry.get("content", ""),
                    url=url,
                )
            )

        return jobs
