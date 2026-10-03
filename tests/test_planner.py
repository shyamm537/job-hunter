from src.config import (
    AdzunaSource,
    AshbySource,
    Filters,
    GreenhouseSource,
    LeverSource,
    WorkableSource,
)
from src.ingestion.ashby import AshbyScraper
from src.ingestion.greenhouse import GreenhouseScraper
from src.ingestion.lever import LeverScraper
from src.ingestion.planner import plan_scrapes
from src.ingestion.workable import WorkableScraper


def test_ats_sources_get_post_filter():
    plans = plan_scrapes(
        [
            GreenhouseSource(type="greenhouse", board="stripe"),
            LeverSource(type="lever", company="figma"),
            AshbySource(type="ashby", org="ashby"),
            WorkableSource(type="workable", account="squiz"),
        ],
        Filters(titles=["data"]),
    )
    assert len(plans) == 4
    assert isinstance(plans[0].scraper, GreenhouseScraper)
    assert isinstance(plans[1].scraper, LeverScraper)
    assert isinstance(plans[2].scraper, AshbyScraper)
    assert isinstance(plans[3].scraper, WorkableScraper)
    assert all(p.post_filter is True for p in plans)


def test_board_plans_carry_source_and_token():
    plans = plan_scrapes(
        [
            GreenhouseSource(type="greenhouse", board="stripe"),
            LeverSource(type="lever", company="figma"),
            AshbySource(type="ashby", org="ramp"),
            WorkableSource(type="workable", account="squiz"),
        ],
        Filters(),
    )
    assert [p.board for p in plans] == [
        ("greenhouse", "stripe"), ("lever", "figma"), ("ashby", "ramp"),
        ("workable", "squiz"),
    ]
    assert plans[3].label == "workable[squiz]"
    assert plans[3].scraper.account == "squiz"


def test_adzuna_searches_use_locations_never_board_locations():
    # board_locations is for board post-filtering only; it must not add searches.
    both = plan_scrapes(
        [AdzunaSource(type="adzuna", country="au")],
        Filters(
            titles=["data analyst"],
            locations=["Adelaide"],
            board_locations=["Adelaide", "SA", "Bedford Park"],
        ),
        adzuna_auth=("id", "key"),
    )
    only = plan_scrapes(
        [AdzunaSource(type="adzuna", country="au")],
        Filters(titles=["data analyst"], locations=["Adelaide"]),
        adzuna_auth=("id", "key"),
    )
    assert [p.label for p in both] == [p.label for p in only]
    assert len(both) == 1
    assert "Bedford Park" not in both[0].label


def test_adzuna_plans_have_no_board():
    plans = plan_scrapes(
        [AdzunaSource(type="adzuna", country="au")],
        Filters(titles=["data analyst"], locations=["Adelaide"]),
        adzuna_auth=("id", "key"),
    )
    assert plans and all(p.board is None for p in plans)
