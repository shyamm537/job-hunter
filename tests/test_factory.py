from src.config import GreenhouseSource, SeekSource
from src.ingestion.factory import build_scraper
from src.ingestion.greenhouse import GreenhouseScraper
from src.ingestion.seek import SeekScraper


def test_build_scraper_returns_seek():
    scraper = build_scraper(SeekSource(title="data analyst", location="Sydney"))
    assert isinstance(scraper, SeekScraper)
    assert scraper.search_terms == "data analyst"
    assert scraper.location == "Sydney"


def test_build_scraper_returns_greenhouse():
    scraper = build_scraper(GreenhouseSource(type="greenhouse", board="stripe"))
    assert isinstance(scraper, GreenhouseScraper)
    assert scraper.board == "stripe"
