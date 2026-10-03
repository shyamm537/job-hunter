"""Plan concrete scrapes from sources + filters.

Sources say *where* to look; the global `filters` say *what* you're after.
This module combines them per source type, because the two kinds use the
intent differently:

- Adzuna is a search engine: each (title, location) pair becomes one
  search, so a single search source expands into len(titles) x len(locations)
  scrapers. Nothing to post-filter — the query already did it.
- An ATS board (Greenhouse, Lever, Ashby, Workable, Workday) returns a company's whole list, so we
  scrape it once and filter the postings afterwards (post_filter=True; see
  src/ingestion/filtering.py).

Adding a source type is a new branch here plus a config model — the CLI loops
over PlannedScrape objects and never learns the concrete types.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from src.config import DEFAULT_LOCATION, ConfigError, Filters, Source
from src.config import (
    AdzunaSource,
    AshbySource,
    GreenhouseSource,
    LeverSource,
    WorkableSource,
    WorkdaySource,
)
from src.ingestion.adzuna import AdzunaScraper, country_of
from src.ingestion.ashby import AshbyScraper
from src.ingestion.base_scraper import BaseScraper
from src.ingestion.greenhouse import GreenhouseScraper
from src.ingestion.lever import LeverScraper
from src.ingestion.workable import WorkableScraper
from src.ingestion.workday import WorkdayScraper

log = logging.getLogger("jobhunter.plan")


@dataclass
class PlannedScrape:
    scraper: BaseScraper
    label: str
    # Whether to apply the title/location filters to results after scraping.
    # True for ATS boards, False for Adzuna (its query already filtered).
    post_filter: bool
    # (source, token) for a whole-board scrape (Greenhouse/Lever/Ashby/Workable/Workday). The
    # scrape CLI uses it to compare the board's stored rows with what the
    # board still lists (reconcile_board). None for Adzuna: a search result
    # isn't a complete list, so a missing posting proves nothing.
    board: Optional[Tuple[str, str]] = None


def plan_adzuna(
    source: AdzunaSource,
    titles: List[str],
    locations: List[str],
    adzuna_auth: Optional[Tuple[str, str]] = None,
) -> List[PlannedScrape]:
    if not titles:
        log.warning("Adzuna source skipped: no filters.titles defined to search for")
        return []
    if adzuna_auth is None:
        raise ConfigError(
            "an 'adzuna' source needs credentials — add an 'adzuna:' "
            "block with app_id and app_key to config.yaml "
            "(register free at developer.adzuna.com)"
        )
    app_id, app_key = adzuna_auth
    planned: List[PlannedScrape] = []
    for title in titles:
        for location in locations:
            loc_country = country_of(location)
            # Skip a location that belongs to a *different* country index
            # (don't search Adelaide under `in`). Region-agnostic
            # locations (remote/unknown -> None) run under this country.
            if loc_country is not None and loc_country != source.country:
                continue
            planned.append(
                PlannedScrape(
                    AdzunaScraper(
                        what=title,
                        where=location,
                        country=source.country,
                        app_id=app_id,
                        app_key=app_key,
                    ),
                    f"adzuna[{source.country}: {title} @ {location}]",
                    post_filter=False,
                )
            )
    return planned


def plan_greenhouse(
    source: GreenhouseSource,
    titles: List[str],
    locations: List[str],
    adzuna_auth: Optional[Tuple[str, str]] = None,
) -> List[PlannedScrape]:
    return [
        PlannedScrape(
            GreenhouseScraper(board=source.board),
            f"greenhouse[{source.board}]",
            post_filter=True,
            board=("greenhouse", source.board),
        )
    ]


def plan_lever(
    source: LeverSource,
    titles: List[str],
    locations: List[str],
    adzuna_auth: Optional[Tuple[str, str]] = None,
) -> List[PlannedScrape]:
    return [
        PlannedScrape(
            LeverScraper(company=source.company),
            f"lever[{source.company}]",
            post_filter=True,
            board=("lever", source.company),
        )
    ]


def plan_ashby(
    source: AshbySource,
    titles: List[str],
    locations: List[str],
    adzuna_auth: Optional[Tuple[str, str]] = None,
) -> List[PlannedScrape]:
    return [
        PlannedScrape(
            AshbyScraper(org=source.org),
            f"ashby[{source.org}]",
            post_filter=True,
            board=("ashby", source.org),
        )
    ]


def plan_workable(
    source: WorkableSource,
    titles: List[str],
    locations: List[str],
    adzuna_auth: Optional[Tuple[str, str]] = None,
) -> List[PlannedScrape]:
    return [
        PlannedScrape(
            WorkableScraper(account=source.account),
            f"workable[{source.account}]",
            post_filter=True,
            board=("workable", source.account),
        )
    ]


def plan_workday(
    source: WorkdaySource,
    titles: List[str],
    locations: List[str],
    adzuna_auth: Optional[Tuple[str, str]] = None,
) -> List[PlannedScrape]:
    # The title filters double as "which postings are worth a detail request"
    # (see WorkdayScraper): descriptions are fetched for title matches only.
    return [
        PlannedScrape(
            WorkdayScraper(
                tenant=source.tenant,
                datacenter=source.datacenter,
                site=source.site,
                detail_titles=titles,
            ),
            f"workday[{source.tenant}/{source.site}]",
            post_filter=True,
            board=("workday", source.tenant),
        )
    ]


SOURCE_PLANNERS = {
    "adzuna": plan_adzuna,
    "greenhouse": plan_greenhouse,
    "lever": plan_lever,
    "ashby": plan_ashby,
    "workable": plan_workable,
    "workday": plan_workday,
}


def _check_one_workday_site_per_tenant(sources: List[Source]) -> None:
    """Raise ConfigError if two Workday sources share a tenant.

    reconcile_board identifies a board by (source, company) and company is the
    tenant, so two sites on one tenant would be reconciled as one board and each
    scrape would mark the other site's rows "gone from board". Tenants compare
    case-insensitively because reconcile_board lowercases company.
    """
    sites_by_tenant: dict = {}
    for source in sources:
        if source.type != "workday":
            continue
        tenant = source.tenant.strip().lower()
        if tenant in sites_by_tenant:
            raise ConfigError(
                f"two workday sources share the tenant {source.tenant!r} (sites "
                f"{sites_by_tenant[tenant]!r} and {source.site!r}): only one site per "
                "tenant is supported, because a board is identified by its tenant and "
                "the two sites would mark each other's postings as gone. Keep the one "
                "you want."
            )
        sites_by_tenant[tenant] = source.site


def plan_scrapes(
    sources: List[Source],
    filters: Filters,
    adzuna_auth: Optional[Tuple[str, str]] = None,
) -> List[PlannedScrape]:
    titles = filters.titles
    locations = filters.locations or [DEFAULT_LOCATION]
    planned: List[PlannedScrape] = []

    _check_one_workday_site_per_tenant(sources)

    for source in sources:
        try:
            planner = SOURCE_PLANNERS[source.type]
        except KeyError:
            raise ValueError(f"Unknown source type: {source.type!r}") from None
        planned.extend(planner(source, titles, locations, adzuna_auth))

    return planned