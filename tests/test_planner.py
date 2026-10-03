from src.config import (
    AdzunaSource,
    AshbySource,
    Filters,
    GreenhouseSource,
    LeverSource,
)
from src.ingestion.ashby import AshbyScraper
from src.ingestion.greenhouse import GreenhouseScraper
from src.ingestion.lever import LeverScraper
from src.ingestion.planner import plan_scrapes


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


def test_board_plans_carry_source_and_token():
    plans = plan_scrapes(
        [
            GreenhouseSource(type="greenhouse", board="stripe"),
            LeverSource(type="lever", company="figma"),
            AshbySource(type="ashby", org="ramp"),
        ],
        Filters(),
    )
    assert [p.board for p in plans] == [
        ("greenhouse", "stripe"), ("lever", "figma"), ("ashby", "ramp"),
    ]


def test_adzuna_plans_have_no_board():
    plans = plan_scrapes(
        [AdzunaSource(type="adzuna", country="au")],
        Filters(titles=["data analyst"], locations=["Adelaide"]),
        adzuna_auth=("id", "key"),
    )
    assert plans and all(p.board is None for p in plans)
