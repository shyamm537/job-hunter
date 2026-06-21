"""Plan concrete scrapes from sources + filters.

Sources say *where* to look; the global `filters` say *what* you're after.
This module combines them per source type, because the two kinds use the
intent differently:

- SEEK / Adzuna are search engines: each (title, location) pair becomes one
  search, so a single search source expands into len(titles) x len(locations)
  scrapers. Nothing to post-filter — the query already did it.
- An ATS board (Greenhouse, Lever, Ashby) returns a company's whole list, so we
  scrape it once and filter the postings afterwards (post_filter=True; see
  src/ingestion/filtering.py).

Adding a source type is a new branch here plus a config model — the CLI loops
over PlannedScrape objects and never learns the concrete types.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from src.config import DEFAULT_LOCATION, ConfigError, Filters, Source
from src.ingestion.adzuna import AdzunaScraper, country_of
from src.ingestion.ashby import AshbyScraper
from src.ingestion.base_scraper import BaseScraper
from src.ingestion.greenhouse import GreenhouseScraper
from src.ingestion.lever import LeverScraper
from src.ingestion.seek import SeekScraper

log = logging.getLogger("jobhunter.plan")


@dataclass
class PlannedScrape:
    scraper: BaseScraper
    label: str
    # Whether to apply the title/location filters to results after scraping.
    # True for ATS boards, False for SEEK/Adzuna (their query already filtered).
    post_filter: bool


def plan_scrapes(
    sources: List[Source],
    filters: Filters,
    adzuna_auth: Optional[Tuple[str, str]] = None,
) -> List[PlannedScrape]:
    titles = filters.titles
    locations = filters.locations or [DEFAULT_LOCATION]
    planned: List[PlannedScrape] = []

    for source in sources:
        if source.type == "seek":
            if not titles:
                log.warning(
                    "SEEK source skipped: no filters.titles defined to search for"
                )
                continue
            seek_locations = source.locations or locations
            for title in titles:
                for location in seek_locations:
                    planned.append(
                        PlannedScrape(
                            SeekScraper(search_terms=title, location=location),
                            f"seek[{title} @ {location}]",
                            post_filter=False,
                        )
                    )
        elif source.type == "adzuna":
            if not titles:
                log.warning(
                    "Adzuna source skipped: no filters.titles defined to search for"
                )
                continue
            if adzuna_auth is None:
                raise ConfigError(
                    "an 'adzuna' source needs credentials — add an 'adzuna:' "
                    "block with app_id and app_key to config.yaml "
                    "(register free at developer.adzuna.com)"
                )
            app_id, app_key = adzuna_auth
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
        elif source.type == "greenhouse":
            planned.append(
                PlannedScrape(
                    GreenhouseScraper(board=source.board),
                    f"greenhouse[{source.board}]",
                    post_filter=True,
                )
            )
        elif source.type == "lever":
            planned.append(
                PlannedScrape(
                    LeverScraper(company=source.company),
                    f"lever[{source.company}]",
                    post_filter=True,
                )
            )
        elif source.type == "ashby":
            planned.append(
                PlannedScrape(
                    AshbyScraper(org=source.org),
                    f"ashby[{source.org}]",
                    post_filter=True,
                )
            )
        else:
            raise ValueError(f"Unknown source type: {source.type!r}")

    return planned
