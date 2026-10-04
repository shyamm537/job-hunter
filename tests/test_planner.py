import pytest

from src.config import (
    AdzunaSource,
    AshbySource,
    ConfigError,
    Filters,
    GreenhouseSource,
    LeverSource,
    WorkableSource,
    WorkdaySource,
)
from src.ingestion.ashby import AshbyScraper
from src.ingestion.greenhouse import GreenhouseScraper
from src.ingestion.lever import LeverScraper
from src.ingestion.planner import plan_scrapes
from src.ingestion.workable import WorkableScraper
from src.ingestion.workday import WorkdayScraper


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


def _workday(tenant="cba", site="CommBank_Careers", dc="wd3"):
    return WorkdaySource(type="workday", tenant=tenant, datacenter=dc, site=site)


def test_workday_plan_carries_the_tenant_as_the_board_and_post_filters():
    (plan,) = plan_scrapes([_workday()], Filters(titles=["data analyst"]))
    assert isinstance(plan.scraper, WorkdayScraper)
    assert plan.board == ("workday", "cba")
    assert plan.label == "workday[cba/CommBank_Careers]"
    assert plan.post_filter is True
    assert (plan.scraper.tenant, plan.scraper.datacenter, plan.scraper.site) == (
        "cba", "wd3", "CommBank_Careers",
    )


def test_workday_scraper_receives_the_title_filters_as_detail_titles():
    titles = ["data analyst", "machine learning"]
    (plan,) = plan_scrapes([_workday()], Filters(titles=titles, locations=["Sydney"]))
    assert plan.scraper.detail_titles == titles
    (plan,) = plan_scrapes([_workday()], Filters())
    assert plan.scraper.detail_titles == []  # empty: the scraper fetches no details


def test_two_workday_sites_on_one_tenant_are_refused_case_insensitively():
    with pytest.raises(ConfigError) as exc:
        plan_scrapes([_workday(), _workday(tenant="CBA", site="Bankwest_Careers")], Filters())
    message = str(exc.value)
    assert "one site per tenant" in message
    assert "CommBank_Careers" in message and "Bankwest_Careers" in message


def test_workday_sources_on_different_tenants_and_other_boards_are_fine():
    plans = plan_scrapes(
        [_workday(), _workday(tenant="flinders", site="flinders_employment"),
         GreenhouseSource(type="greenhouse", board="cba")],  # same word, different source
        Filters(),
    )
    assert [p.board for p in plans] == [
        ("workday", "cba"), ("workday", "flinders"), ("greenhouse", "cba"),
    ]


def test_a_single_workday_source_is_never_refused():
    # validate plans one source at a time, so the duplicate check never trips it.
    assert len(plan_scrapes([_workday()], Filters())) == 1


def test_adzuna_plans_check_the_title_but_not_the_location():
    plans = plan_scrapes(
        [AdzunaSource(type="adzuna", country="au")],
        Filters(titles=["data analyst"], locations=["Adelaide"]),
        adzuna_auth=("id", "key"),
    )
    assert plans and all(p.post_filter is True for p in plans)
    assert all(p.check_location is False for p in plans)


def test_board_plans_check_the_location():
    plans = plan_scrapes(
        [
            GreenhouseSource(type="greenhouse", board="stripe"),
            LeverSource(type="lever", company="figma"),
            AshbySource(type="ashby", org="ramp"),
            WorkableSource(type="workable", account="squiz"),
            _workday(),
        ],
        Filters(),
    )
    assert len(plans) == 5 and all(p.check_location is True for p in plans)


def test_also_match_titles_never_become_adzuna_searches():
    base = dict(titles=["data analyst"], locations=["Adelaide"])
    plain = plan_scrapes(
        [AdzunaSource(type="adzuna", country="au")], Filters(**base), adzuna_auth=("id", "key")
    )
    extended = plan_scrapes(
        [AdzunaSource(type="adzuna", country="au")],
        Filters(**base, also_match_titles=["data engineer", "business analyst"],
                exclude_titles=["senior"], remote_regions=["Australia"]),
        adzuna_auth=("id", "key"),
    )
    assert [p.label for p in extended] == [p.label for p in plain]  # same searches, no extra calls
    assert len(extended) == 1


def test_workday_plan_fetches_details_for_wanted_titles_including_also_match_titles():
    filters = Filters(
        titles=["data analyst"], also_match_titles=["data engineer"], exclude_titles=["senior", "lead"],
    )
    (plan,) = plan_scrapes([_workday()], filters)
    assert plan.scraper.detail_titles == ["data analyst", "data engineer"]
    assert plan.scraper.skip_titles == ["senior", "lead"]


def test_workday_plan_with_no_titles_set_fetches_no_details():
    (plan,) = plan_scrapes([_workday()], Filters(also_match_titles=["data engineer"]))
    assert plan.scraper.detail_titles == []  # empty titles = no title filter; also is ignored


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
