"""Apply the global title/location filters to scraped postings.

Used for ATS sources (Greenhouse/Lever), which return a company's whole board.
SEEK isn't post-filtered — its search query already did the filtering.

Matching is lenient on purpose:
- Empty filter list => everything matches.
- Title: case-insensitive substring, against any wanted title.
- Location: case-insensitive substring against any wanted location, BUT a
  posting whose location mentions "remote" always passes — you rarely want to
  drop remote roles from a job hunt.
"""

from typing import List

from src.config import Filters
from src.storage.models import JobPost


def title_matches(title: str, titles: List[str]) -> bool:
    if not titles:
        return True
    haystack = (title or "").lower()
    return any(want.lower() in haystack for want in titles)


def location_matches(location: str, locations: List[str]) -> bool:
    if not locations:
        return True
    haystack = (location or "").lower()
    if "remote" in haystack:
        return True
    return any(want.lower() in haystack for want in locations)


def job_matches(job: JobPost, filters: Filters) -> bool:
    return title_matches(job.title, filters.titles) and location_matches(
        job.location, filters.locations
    )
