"""Maps a validated config source onto a concrete scraper.

This is the one place that knows which `type` string corresponds to which
`BaseScraper` subclass. Adding a third source means adding a branch here
and a config model in `src/config.py` — nothing in the CLI changes.
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
