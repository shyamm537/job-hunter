"""Deprecated shim. Scraper selection moved to src/ingestion/planner.py when
sources and filters were split. Import `plan_scrapes` / `PlannedScrape` from
planner instead. (This file is kept only because it can't be removed here;
`git rm src/ingestion/factory.py` to drop it for good.)
"""

from src.config import Source
from src.ingestion.base_scraper import BaseScraper
from src.ingestion.greenhouse import GreenhouseScraper
from src.ingestion.seek import SeekScraper


def build_scraper(source: Source) -> BaseScraper:
    if source.type == "seek":
        return SeekScraper(search_terms=source.title, location=source.location)
    if source.type == "greenhouse":
        return GreenhouseScraper(
            board=source.board, title_contains=source.title_contains
        )
    raise ValueError(f"Unknown source type: {source.type!r}")
