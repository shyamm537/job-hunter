"""Apply the global title/location filters to scraped postings.

Used for ATS sources (Greenhouse/Lever/Ashby), which return a company's whole
board. Adzuna isn't post-filtered — its search query already did the filtering.

Matching is lenient on purpose:
- Empty filter list => everything matches.
- Title: case-insensitive substring, against any wanted title.
- Location: case-insensitive substring against any wanted location, BUT a
  posting whose location mentions "remote" always passes — you rarely want to
  drop remote roles from a job hunt.
- Board locations: if `filters.board_locations` is set it is used INSTEAD of
  `filters.locations` for board postings, and its entries match as whole words
  (see `location_matches`). Adzuna never reads it.
"""

import re
from typing import List

from src.config import Filters
from src.storage.models import JobPost


def title_matches(title: str, titles: List[str]) -> bool:
    if not titles:
        return True
    haystack = (title or "").lower()
    return any(want.lower() in haystack for want in titles)


def _whole_word(entry: str, location: str) -> bool:
    """Does `entry` appear in `location`, not touching a letter or digit?

    "SA" matches "SA Western Area" and "Support Office SA" but not "San
    Francisco", "USA" or "Mount Pleasant". Case-insensitive; the entry is taken
    literally (no regex characters).
    """
    pattern = r"(?<![A-Za-z0-9])" + re.escape(entry) + r"(?![A-Za-z0-9])"
    return re.search(pattern, location, re.IGNORECASE) is not None


def location_matches(
    location: str, locations: List[str], whole_word: bool = False
) -> bool:
    """Substring match by default; `whole_word=True` for short place codes."""
    if not locations:
        return True
    haystack = (location or "").lower()
    if "remote" in haystack:
        return True
    if whole_word:
        return any(_whole_word(want, location or "") for want in locations)
    return any(want.lower() in haystack for want in locations)


def job_matches(job: JobPost, filters: Filters) -> bool:
    board_locations = [w.strip() for w in filters.board_locations if w and w.strip()]
    if board_locations:
        location_ok = location_matches(job.location, board_locations, whole_word=True)
    else:
        location_ok = location_matches(job.location, filters.locations)
    return title_matches(job.title, filters.titles) and location_ok
