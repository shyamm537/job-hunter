from unittest.mock import patch

import pytest

from src.config import AdzunaSource, ConfigError, Filters
from src.ingestion.adzuna import AdzunaScraper, country_of
from src.ingestion.planner import plan_scrapes


def _result(title="Data Analyst", company="Acme", loc="Adelaide", url="u1"):
    return {
        "title": title,
        "company": {"display_name": company},
        "location": {"display_name": loc},
        "description": "snippet...",
        "redirect_url": url,
    }


def test_country_of_maps_known_cities():
    assert country_of("Adelaide") == "au"
    assert country_of("Mumbai") == "in"
    assert country_of("Bengaluru") == "in"
    assert country_of("Remote") is None  # region-agnostic


def test_scrape_maps_fields_and_id():
    payload = {"results": [_result(url="https://adzuna/x/1")]}
    with patch("src.ingestion.adzuna.get_json", return_value=payload):
        jobs = AdzunaScraper("data", "Adelaide", "au", "id", "key",
                             results_per_page=50).scrape()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Data Analyst"
    assert j.company == "Acme"
    assert j.location == "Adelaide"
    assert j.url == "https://adzuna/x/1"
    assert j.job_board_id.startswith("adzuna-") and len(j.job_board_id) == 17


def test_pagination_stops_on_short_page():
    # page 1 full (2 results == results_per_page) -> fetch page 2; page 2 short
    # (1 result) -> stop. So exactly 2 calls, 3 jobs.
    pages = [
        {"results": [_result(url="a"), _result(url="b")]},
        {"results": [_result(url="c")]},
    ]
    calls = {"n": 0}

    def fake(url):
        i = calls["n"]
        calls["n"] += 1
        return pages[i]

    with patch("src.ingestion.adzuna.get_json", side_effect=fake):
        jobs = AdzunaScraper("data", "Adelaide", "au", "id", "key",
                             results_per_page=2, max_pages=5).scrape()
    assert calls["n"] == 2
    assert len(jobs) == 3


def test_missing_company_and_location_dont_crash():
    payload = {"results": [{"title": "X", "redirect_url": "u"}]}
    with patch("src.ingestion.adzuna.get_json", return_value=payload):
        jobs = AdzunaScraper("data", "", "au", "id", "key").scrape()
    assert jobs[0].company == "Unknown"
    assert jobs[0].location == ""


def test_planner_pairs_locations_with_matching_country():
    filters = Filters(titles=["data"], locations=["Adelaide", "Mumbai", "Remote"])
    plans = plan_scrapes(
        [AdzunaSource(type="adzuna", country="au")], filters,
        adzuna_auth=("id", "key"),
    )
    wheres = sorted(p.scraper.where for p in plans)
    # Adelaide (au) + Remote (region-agnostic) run under au; Mumbai (in) skipped.
    assert wheres == ["Adelaide", "Remote"]
    assert all(p.post_filter is False for p in plans)
    assert all(p.scraper.country == "au" for p in plans)


def test_planner_adzuna_without_auth_errors():
    filters = Filters(titles=["data"], locations=["Adelaide"])
    with pytest.raises(ConfigError, match="adzuna"):
        plan_scrapes([AdzunaSource(type="adzuna", country="au")], filters)
