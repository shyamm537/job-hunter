from unittest.mock import patch

import pytest

from src.config import AdzunaSource, ConfigError, Filters
from src.ingestion.adzuna import (
    AdzunaScraper,
    adzuna_id_from_url,
    country_of,
    dedup_key,
    id_dedup_key,
    url_dedup_key,
)
from src.ingestion.planner import plan_scrapes


def _result(title="Data Analyst", company="Acme", loc="Adelaide", url="u1", id=None):
    entry = {
        "title": title,
        "company": {"display_name": company},
        "location": {"display_name": loc},
        "description": "snippet...",
        "redirect_url": url,
    }
    if id is not None:
        entry["id"] = id
    return entry


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


def test_dedup_key_ignores_query_params():
    # The same ad surfaced under two location-alias searches comes back with
    # different per-search tracking query strings; the dedup id must collapse
    # them (job identity is in the path), while the stored link keeps the full
    # URL so the redirect/tracking still works.
    base = "https://www.adzuna.in/land/ad/123456"
    p1 = {"results": [_result(url=f"{base}?se=AAA&utm_source=api&where=bangalore")]}
    p2 = {"results": [_result(url=f"{base}?se=BBB&utm_source=api&where=bengaluru")]}
    with patch("src.ingestion.adzuna.get_json", return_value=p1):
        j1 = AdzunaScraper("data", "Bengaluru", "in", "id", "key").scrape()[0]
    with patch("src.ingestion.adzuna.get_json", return_value=p2):
        j2 = AdzunaScraper("data", "Bengaluru", "in", "id", "key").scrape()[0]
    assert j1.job_board_id == j2.job_board_id
    assert j1.url != j2.url  # full URL preserved, only the dedup hash collapses


def test_dedup_key_distinguishes_different_paths():
    # Different ads (different path ids) must NOT collapse to one id.
    a = {"results": [_result(url="https://www.adzuna.in/land/ad/111?se=X")]}
    b = {"results": [_result(url="https://www.adzuna.in/land/ad/222?se=X")]}
    with patch("src.ingestion.adzuna.get_json", return_value=a):
        ja = AdzunaScraper("data", "Bengaluru", "in", "id", "key").scrape()[0]
    with patch("src.ingestion.adzuna.get_json", return_value=b):
        jb = AdzunaScraper("data", "Bengaluru", "in", "id", "key").scrape()[0]
    assert ja.job_board_id != jb.job_board_id


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


# --- id-based dedup key ----------------------------------------------------


def test_dedup_key_prefers_id_over_url():
    # Adzuna's own `id` is the robust key: the SAME ad fetched under two
    # different searches (different redirect URLs) must collapse to one id,
    # and that must work even when the URLs differ in PATH too (which the
    # url-only key can't handle).
    e1 = _result(url="https://www.adzuna.in/land/ad/111?se=A", id="999")
    e2 = _result(url="https://www.adzuna.au/jobs/land/ad/222?se=B", id="999")
    assert dedup_key(e1) == dedup_key(e2) == id_dedup_key("999")
    # Distinct ids stay distinct.
    assert dedup_key(_result(id="999")) != dedup_key(_result(id="1000"))


def test_dedup_key_with_id_is_uniform_format():
    key = dedup_key(_result(id="129698749"))
    assert key.startswith("adzuna-") and len(key) == 17


def test_dedup_key_falls_back_to_url_when_id_missing_or_empty():
    url = "https://www.adzuna.in/land/ad/123?se=X"
    assert dedup_key(_result(url=url)) == url_dedup_key(url)  # no id key at all
    assert dedup_key(_result(url=url, id="")) == url_dedup_key(url)  # empty id
    assert dedup_key(_result(url=url, id=None)) == url_dedup_key(url)


def test_scrape_uses_id_when_present():
    payload = {"results": [_result(url="https://adzuna/x/1", id="555")]}
    with patch("src.ingestion.adzuna.get_json", return_value=payload):
        jobs = AdzunaScraper("data", "Adelaide", "au", "id", "key").scrape()
    assert jobs[0].job_board_id == id_dedup_key("555")
    assert jobs[0].url == "https://adzuna/x/1"  # full URL still stored


def test_adzuna_id_from_url_extracts_path_id():
    assert adzuna_id_from_url("https://www.adzuna.in/land/ad/123456?se=X") == "123456"
    assert adzuna_id_from_url("http://adzuna.co.uk/jobs/land/ad/789") == "789"
    # No land/ad segment -> None (caller leaves such a row untouched).
    assert adzuna_id_from_url("https://example.com/jobs/789") is None
    assert adzuna_id_from_url("") is None
