from src.config import (
    AshbySource,
    Filters,
    GreenhouseSource,
    LeverSource,
    SeekSource,
)
from src.ingestion.ashby import AshbyScraper
from src.ingestion.greenhouse import GreenhouseScraper
from src.ingestion.lever import LeverScraper
from src.ingestion.planner import plan_scrapes
from src.ingestion.seek import SeekScraper


def test_seek_expands_titles_by_locations():
    plans = plan_scrapes(
        [SeekSource()],
        Filters(titles=["data analyst", "data scientist"], locations=["Adelaide", "Sydney"]),
    )
    assert len(plans) == 4  # 2 titles x 2 locations
    assert all(isinstance(p.scraper, SeekScraper) for p in plans)
    assert all(p.post_filter is False for p in plans)


def test_seek_uses_default_location_when_none():
    plans = plan_scrapes([SeekSource()], Filters(titles=["x"]))
    assert len(plans) == 1
    assert plans[0].scraper.location == "All Australia"


def test_seek_skipped_without_titles():
    plans = plan_scrapes([SeekSource()], Filters())  # no titles
    assert plans == []


def test_seek_source_scopes_its_own_locations():
    # SEEK (AU/NZ-only) searches just its own locations; the global filter
    # locations (incl. non-AU) are NOT used to build SEEK searches.
    f = Filters(titles=["data analyst"], locations=["Adelaide", "Bangalore", "Remote"])
    plans = plan_scrapes([SeekSource(type="seek", locations=["Adelaide", "Sydney"])], f)
    searched = sorted(p.scraper.location for p in plans)
    assert searched == ["Adelaide", "Sydney"]
    assert "Bangalore" not in searched  # India location not sent to SEEK


def test_seek_falls_back_to_filter_locations():
    f = Filters(titles=["data analyst"], locations=["Adelaide", "Remote"])
    plans = plan_scrapes([SeekSource()], f)  # no per-source locations
    assert sorted(p.scraper.location for p in plans) == ["Adelaide", "Remote"]


def test_ats_sources_get_post_filter():
    plans = plan_scrapes(
        [
            GreenhouseSource(type="greenhouse", board="stripe"),
            LeverSource(type="lever", company="figma"),
            AshbySource(type="ashby", org="ashby"),
        ],
        Filters(titles=["data"]),
    )
    assert len(plans) == 3
    assert isinstance(plans[0].scraper, GreenhouseScraper)
    assert isinstance(plans[1].scraper, LeverScraper)
    assert isinstance(plans[2].scraper, AshbyScraper)
    assert all(p.post_filter is True for p in plans)
