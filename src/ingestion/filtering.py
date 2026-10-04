"""Apply the global title/location filters to scraped postings.

Used for every source's results: ATS boards (Greenhouse/Lever/Ashby/Workable/
Workday) return a company's whole board, and Adzuna's fuzzy search returns many
ads whose title is not one you asked for. Adzuna is checked by title only: its
query already narrowed the location.

Matching is lenient on purpose:
- Empty filter list => everything matches.
- Title: case-insensitive substring, against any wanted title (`titles`, plus
  `also_match_titles` when `titles` is set), and then none of `exclude_titles`
  may appear in it as a whole word.
- Location: case-insensitive substring against any wanted location, BUT a
  posting whose location mentions "remote" passes — you rarely want to drop
  remote roles from a job hunt. If `filters.remote_regions` is set, a remote
  posting passes only when it names no place ("Remote", "Global Remote") or
  names one of those regions; otherwise "remote" passes for any country.
- Board locations: if `filters.board_locations` is set it is used INSTEAD of
  `filters.locations` for board postings, and its entries match as whole words
  (see `location_matches`). Adzuna never reads it.
"""

import re
from typing import List, Optional

from src.config import Filters
from src.storage.models import JobPost

# Words that make a remote location region-agnostic when nothing else is named.
_AGNOSTIC_WORDS = {"remote", "global", "worldwide", "anywhere", "everywhere", "international"}


def title_matches(title: str, titles: List[str]) -> bool:
    if not titles:
        return True
    haystack = (title or "").lower()
    return any(want.lower() in haystack for want in titles)


def whole_word_in(entry: str, text: str) -> bool:
    """Does `entry` appear in `text`, not touching a letter or digit?

    "SA" matches "SA Western Area" and "Support Office SA" but not "San
    Francisco", "USA" or "Mount Pleasant"; "sr" matches "Sr. Analyst" but not
    "Srinivas". Case-insensitive; the entry is taken literally (no regex
    characters).
    """
    pattern = r"(?<![A-Za-z0-9])" + re.escape(entry) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _clean(entries: Optional[List[str]]) -> List[str]:
    """Entries stripped, with blanks dropped (a blank would match everything)."""
    return [e.strip() for e in (entries or []) if e and e.strip()]


def wanted_titles(filters: Filters) -> List[str]:
    """Titles a job may match: `titles` plus `also_match_titles`.

    Empty when `titles` is empty (no title filter), in which case
    `also_match_titles` is ignored rather than turning into the only filter.
    """
    titles = _clean(filters.titles)
    if not titles:
        return []
    return titles + _clean(filters.also_match_titles)


def title_excluded(title: str, exclude_titles: Optional[List[str]]) -> bool:
    """Does the title contain any excluded word, as a whole word?"""
    return any(whole_word_in(word, title or "") for word in _clean(exclude_titles))


def title_ok(title: str, filters: Filters) -> bool:
    """The title passes: it matches a wanted title and has no excluded word."""
    return title_matches(title, wanted_titles(filters)) and not title_excluded(
        title, filters.exclude_titles
    )


def _remote_region_ok(location: str, regions: List[str]) -> bool:
    """A remote location passes if it names no place, or names a wanted region."""
    words = re.findall(r"[a-z0-9]+", location.lower())
    if all(word in _AGNOSTIC_WORDS for word in words):
        return True  # "Remote", "Global Remote", "Remote - Anywhere"
    return any(whole_word_in(region, location) for region in regions)


def location_matches(
    location: str,
    locations: List[str],
    whole_word: bool = False,
    remote_regions: Optional[List[str]] = None,
) -> bool:
    """Substring match by default; `whole_word=True` for short place codes.

    A location that mentions "remote" passes, unless `remote_regions` is set:
    then it must also be region-agnostic or name one of those regions (a place
    in `locations` still passes it, e.g. "Remote - Sydney").
    """
    if not locations:
        return True
    regions = _clean(remote_regions)
    wanted = locations
    if regions:
        # With regions set, a literal "remote" entry would let every remote job
        # through by itself; remote jobs are judged by the region rule instead.
        wanted = [w for w in locations if w.strip().lower() != "remote"]
    haystack = (location or "").lower()
    if whole_word:
        direct = any(whole_word_in(want, location or "") for want in wanted)
    else:
        direct = any(want.lower() in haystack for want in wanted)
    if direct:
        return True
    if "remote" in haystack:
        return _remote_region_ok(location or "", regions) if regions else True
    return False


def job_matches(job: JobPost, filters: Filters, *, check_location: bool = True) -> bool:
    """Does the posting pass the title rules and (unless told not to) the location rules?

    `check_location=False` is for Adzuna, whose search already narrowed the
    location: only the title is checked.
    """
    if not title_ok(job.title, filters):
        return False
    if not check_location:
        return True
    board_locations = _clean(filters.board_locations)
    if board_locations:
        return location_matches(
            job.location, board_locations, whole_word=True,
            remote_regions=filters.remote_regions,
        )
    return location_matches(
        job.location, filters.locations, remote_regions=filters.remote_regions
    )
