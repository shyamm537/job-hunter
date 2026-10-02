"""SEEK scraper — reads SEEK's public RSS search feed.

No login, no authentication, no stealth/anti-detection patches. This only
parses a feed SEEK already publishes for public consumption. See the
README's "Scraping: scope and ethics" section for why this project doesn't
go further than that.
"""

import hashlib
from typing import List

import feedparser

from src.ingestion.base_scraper import BaseScraper
from src.storage.models import JobPost


class SeekScraper(BaseScraper):
    source_name = "seek"

    def __init__(self, search_terms: str, location: str = "All Australia"):
        self.search_terms = search_terms
        self.location = location

    def _feed_url(self) -> str:
        query = self.search_terms.strip().replace(" ", "-").lower()
        loc = self.location.strip().replace(" ", "-").lower()
        return f"https://au.seek.com/{query}-jobs/in-{loc}?rss=true"

    def scrape(self) -> List[JobPost]:
        feed = feedparser.parse(self._feed_url())
        jobs: List[JobPost] = []

        for entry in feed.entries:
            link = getattr(entry, "link", "")
            job_board_id = f"seek-{hashlib.sha1(link.encode()).hexdigest()[:10]}"

            jobs.append(
                JobPost(
                    job_board_id=job_board_id,
                    title=getattr(entry, "title", "Untitled"),
                    company=getattr(entry, "author", "Unknown"),
                    location=self.location,
                    description=getattr(entry, "summary", ""),
                    url=link,
                )
            )

        return jobs
