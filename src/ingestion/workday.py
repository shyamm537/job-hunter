"""Workday scraper — reads a tenant's public Workday careers API.

Workday hosts careers sites for many large employers (banks, universities,
retailers). Each site has a public JSON API behind its careers page:

    list    POST https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
            body {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""}
    detail  GET  https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site><externalPath>

`<tenant>`, `<dc>` (a data centre such as `wd3`) and `<site>` are read off the
careers URL; none can be guessed from a company name. The endpoint is not a
published contract and can change without notice.

What is known about it (checked against live tenants on 2026-10-03):

- `limit` above 20 is an HTTP 400, so we page 20 at a time.
- `total` is correct on the first page only; later pages report 0.
- A tenant's list cannot be read past 2,000 postings: `total` is capped at 2000
  and an `offset` of 2000 or more returns the first page again. We raise for such
  a board rather than return a list that merely looks complete.
- The list has no description. The detail call does (HTML), plus a clean
  location, `additionalLocations` and a `startDate` that is the posting date.
  Fetching one per posting is too many requests, so we fetch it only for
  postings whose title matches `detail_titles` (the configured title filters).
  Every other posting is still returned, without a description, because
  `reconcile_board` needs the whole board.
- `locationsText` can be "2 Locations" or, at some tenants, pack the job level
  and closing date after a "|". `postedOn` is display text ("Posted Today"), not
  a date, and is ignored.
- `externalPath` ends in `_<requisition id>`, sometimes with a `-N` repost
  marker (`JR0000017701-1`), and also contains location and title slugs that can
  change. The id is therefore built from the requisition id, not the URL.
"""

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

from src.ingestion.base_scraper import BaseScraper
from src.ingestion.dates import parse_posted_at
from src.ingestion.filtering import title_matches
from src.ingestion.http_util import REQUEST_DELAY, get_json, post_json
from src.ingestion.text_util import html_to_text
from src.storage.models import JobPost

# Workday answers HTTP 400 for any larger page size, so this must stay at 20.
PAGE_SIZE = 20
# `total` is capped here and offsets at or past it wrap to the first page, so a
# board this size cannot be listed completely.
LIST_CEILING = 2000

_JSON = {"Accept": "application/json"}

log = logging.getLogger("jobhunter.workday")


class WorkdayError(RuntimeError):
    """The board could not be read completely (raised so nothing is half-saved)."""


def _requisition_id(external_path: str, bullet_fields: List[Any]) -> str:
    """The posting's requisition id, or "" if the path carries none.

    The id is the text after the last "_" in the path's final segment. When one
    of the posting's `bulletFields` is that id without its repost marker (so
    `JR0000017701` for `JR0000017701-1`), that base id is used: the marker looks
    like a version counter, and an id that included it would change when the
    posting is refreshed. Otherwise the suffix is used exactly as it is. A
    trailing "-N" is never stripped by pattern, because an id such as `R-12345`
    would collapse to `R`.
    """
    segment = external_path.rstrip("/").rsplit("/", 1)[-1]
    if "_" not in segment:
        return ""
    suffix = segment.rsplit("_", 1)[-1]
    if not suffix:
        return ""
    matches = [
        b for b in bullet_fields
        if isinstance(b, str) and b and (suffix == b or suffix.startswith(b + "-"))
    ]
    return max(matches, key=len) if matches else suffix


class WorkdayScraper(BaseScraper):
    source_name = "workday"

    def __init__(
        self,
        tenant: str,
        datacenter: str,
        site: str,
        detail_titles: Optional[List[str]] = None,
    ):
        self.tenant = tenant
        self.datacenter = datacenter
        self.site = site
        self.detail_titles = detail_titles

    # -- URLs ------------------------------------------------------------------

    def _host(self) -> str:
        return f"https://{self.tenant.strip()}.{self.datacenter.strip()}.myworkdayjobs.com"

    def _list_url(self) -> str:
        return f"{self._host()}/wday/cxs/{self.tenant.strip()}/{self.site.strip()}/jobs"

    def _detail_url(self, external_path: str) -> str:
        return f"{self._host()}/wday/cxs/{self.tenant.strip()}/{self.site.strip()}{external_path}"

    def _public_url(self, external_path: str) -> str:
        # No locale segment: it opens the posting without one.
        return f"{self._host()}/{self.site.strip()}{external_path}"

    # -- list ------------------------------------------------------------------

    def _page(self, offset: int) -> Dict[str, Any]:
        body = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
        return post_json(self._list_url(), body, headers=_JSON)

    def _list_board(self) -> List[Dict[str, Any]]:
        """Every posting on the board, once each. Raises unless the list is complete."""
        first = self._page(0)
        total = first.get("total") or 0
        postings: Dict[str, Dict[str, Any]] = {}

        def take(page: Dict[str, Any]) -> int:
            rows = page.get("jobPostings") or []
            for row in rows:
                path = row.get("externalPath")
                if not path:
                    # No path means no URL and no id. Raise rather than skip: a
                    # skipped posting would be marked dead by reconcile_board.
                    raise WorkdayError(
                        f"{self._label()}: a posting has no externalPath "
                        f"(title {row.get('title')!r}); the site layout may have changed"
                    )
                postings.setdefault(path, row)  # a posting seen twice counts once
            return len(rows)

        if total >= LIST_CEILING:
            raise WorkdayError(
                f"{self._label()}: reports {total} postings, which is Workday's "
                f"listing ceiling ({LIST_CEILING}); a board this large cannot be listed "
                "completely, so it is not scraped"
            )
        if take(first) and not total:
            raise WorkdayError(
                f"{self._label()}: the first page has postings but no total; "
                "the site layout may have changed"
            )

        offset = PAGE_SIZE
        while offset < total:
            time.sleep(REQUEST_DELAY)
            if not take(self._page(offset)):
                break
            offset += PAGE_SIZE

        if len(postings) < total:
            raise WorkdayError(
                f"{self._label()}: listed {len(postings)} unique postings but the board "
                f"reports {total}; not returning a partial list"
            )
        return list(postings.values())

    def _label(self) -> str:
        return f"workday[{self.tenant}/{self.site}]"

    # -- scrape ----------------------------------------------------------------

    def scrape(self) -> List[JobPost]:
        rows = self._list_board()
        jobs = [self._from_list(row) for row in rows]

        # Two postings mapped to one id would be stored as one row. Fail loudly.
        seen: Dict[str, str] = {}
        for row, job in zip(rows, jobs):
            other = seen.setdefault(job.job_board_id, row["externalPath"])
            if other != row["externalPath"]:
                raise WorkdayError(
                    f"{self._label()}: two postings map to the id {job.job_board_id}: "
                    f"{other} and {row['externalPath']}; fix the id rule rather than "
                    "skipping the board"
                )

        # Empty means "no title filter" elsewhere (title_matches returns True),
        # but here it must mean "fetch nothing": it would otherwise fetch a
        # detail for every posting on the board.
        if not self.detail_titles:
            log.info("%s: no title filters set, so no descriptions are fetched", self._label())
            return jobs

        wanted = [(row, job) for row, job in zip(rows, jobs)
                  if title_matches(job.title, self.detail_titles)]
        for index, (row, job) in enumerate(wanted):
            if index:
                time.sleep(REQUEST_DELAY)
            self._add_detail(row["externalPath"], job)
        return jobs

    # -- mapping ---------------------------------------------------------------

    def _from_list(self, row: Dict[str, Any]) -> JobPost:
        path = row["externalPath"]
        url = self._public_url(path)

        req_id = _requisition_id(path, row.get("bulletFields") or [])
        # Not the URL: its location and title slugs can change while the posting
        # stays open. Fall back to it only when the path carries no id at all.
        key = f"{self.tenant}/{self.site}/{req_id}" if req_id else url
        job_board_id = f"workday-{hashlib.sha1(key.encode()).hexdigest()[:10]}"

        # Some tenants pack "<place> | <job level> | Closes <date>" into this.
        location = (row.get("locationsText") or "").split("|")[0].strip()

        return JobPost(
            job_board_id=job_board_id,
            title=row.get("title") or "Untitled",
            # The tenant IS the board token (reconcile_board matches on it).
            company=self.tenant,
            location=location,
            description="",
            url=url,
            posted_at=None,  # postedOn is display text; startDate comes with the detail
        )

    def _add_detail(self, external_path: str, job: JobPost) -> None:
        """Fill description, location and posted_at from the detail call.

        A failed call is logged and leaves the list values: one bad posting must
        not fail the scrape or drop the posting.
        """
        try:
            detail = get_json(self._detail_url(external_path), headers=_JSON)
            info = detail.get("jobPostingInfo") or {}
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("%s: no detail for %r: %s", self._label(), job.title, exc)
            return

        places = [info.get("location"), *(info.get("additionalLocations") or [])]
        location = "; ".join(p.strip() for p in places if isinstance(p, str) and p.strip())
        if location:
            job.location = location
        job.description = html_to_text(info.get("jobDescription"))
        job.posted_at = parse_posted_at(info.get("startDate"))
