from unittest.mock import patch

from src.config import (
    AdzunaSource,
    Filters,
    GreenhouseSource,
    LeverSource,
    WorkableSource,
    WorkdaySource,
    source_to_line,
)
from src.ingestion import validate as V
from src.storage.models import JobPost


def _jobs(*specs):
    return [
        JobPost(job_board_id=f"j{i}", title=t, company="c", location=loc,
                description="d", url=f"u{i}")
        for i, (t, loc) in enumerate(specs)
    ]


F = Filters(titles=["data analyst"], locations=["Remote"])


def test_adzuna_is_live_without_a_network_call():
    # No scrape patched: if it tried to hit the network the test would fail.
    r = V.validate_source(AdzunaSource(type="adzuna", country="au"), F)
    assert r.live is True and r.status == "live" and r.total == 0


def test_live_board_with_a_matching_role():
    jobs = _jobs(("Senior Data Analyst", "Remote"), ("Chef", "Paris"))
    with patch("src.ingestion.greenhouse.GreenhouseScraper.scrape", return_value=jobs):
        r = V.validate_source(GreenhouseSource(type="greenhouse", board="acme"), F)
    assert r.status == "match"
    assert r.total == 2 and r.matched == 1 and r.live is True


def test_live_board_without_a_current_match():
    jobs = _jobs(("Chef", "Paris"), ("Welder", "Berlin"))
    with patch("src.ingestion.greenhouse.GreenhouseScraper.scrape", return_value=jobs):
        r = V.validate_source(GreenhouseSource(type="greenhouse", board="acme"), F)
    assert r.status == "live"  # live, but nothing matches right now
    assert r.total == 2 and r.matched == 0


def test_dead_board_is_caught_not_raised():
    with patch(
        "src.ingestion.lever.LeverScraper.scrape",
        side_effect=RuntimeError("404 Client Error"),
    ):
        r = V.validate_source(LeverSource(type="lever", company="gone"), F)
    assert r.live is False and r.status == "dead"
    assert "404" in r.error


def test_workable_board_validates_and_a_dead_account_is_reported_dead():
    jobs = _jobs(("Senior Data Analyst", "Remote"), ("Chef", "Paris"))
    with patch("src.ingestion.workable.WorkableScraper.scrape", return_value=jobs):
        r = V.validate_source(WorkableSource(type="workable", account="acme"), F)
    assert r.label == "workable[acme]" and r.status == "match" and r.matched == 1

    # An unknown Workable account is a 404, which get_json raises.
    with patch(
        "src.ingestion.workable.WorkableScraper.scrape",
        side_effect=RuntimeError("404 Client Error: Not Found"),
    ):
        r = V.validate_source(WorkableSource(type="workable", account="gone"), F)
    assert r.live is False and r.status == "dead" and "404" in r.error


def test_workday_board_validates_and_a_dead_tenant_is_reported_dead():
    source = WorkdaySource(type="workday", tenant="acme", datacenter="wd3", site="Careers")
    jobs = _jobs(("Senior Data Analyst", "Remote"), ("Chef", "Paris"))
    with patch("src.ingestion.workday.WorkdayScraper.scrape", return_value=jobs):
        r = V.validate_source(source, F)
    assert r.label == "workday[acme/Careers]" and r.status == "match" and r.matched == 1

    # An unknown tenant is a 422 and a wrong site a 404; both raise.
    with patch(
        "src.ingestion.workday.WorkdayScraper.scrape",
        side_effect=RuntimeError("422 Client Error"),
    ):
        r = V.validate_source(source, F)
    assert r.live is False and r.status == "dead" and "422" in r.error


def test_kept_filters_to_live_then_to_matching():
    live_match = V.ValidationResult(
        GreenhouseSource(type="greenhouse", board="a"), "a", True, 5, 2, None)
    live_only = V.ValidationResult(
        GreenhouseSource(type="greenhouse", board="b"), "b", True, 5, 0, None)
    dead = V.ValidationResult(
        GreenhouseSource(type="greenhouse", board="c"), "c", False, 0, 0, "err")
    adzuna = V.ValidationResult(
        AdzunaSource(type="adzuna", country="au"), "adzuna", True, 0, 0, None)
    results = [live_match, live_only, dead, adzuna]

    # Default: keep every live board + the search source, drop dead.
    kept = V._kept(results, require_match=False)
    assert {r.label for r in kept} == {"a", "b", "adzuna"}

    # require_match: only boards with a current match (+ the search source).
    kept = V._kept(results, require_match=True)
    assert {r.label for r in kept} == {"a", "adzuna"}


def test_source_to_line_roundtrips():
    assert source_to_line(AdzunaSource(type="adzuna", country="in")) == "adzuna in"
    assert source_to_line(GreenhouseSource(type="greenhouse", board="stripe")) == "greenhouse stripe"
    assert source_to_line(LeverSource(type="lever", company="metabase")) == "lever metabase"
    assert source_to_line(WorkableSource(type="workable", account="Squiz")) == "workable Squiz"
    workday = WorkdaySource(type="workday", tenant="cba", datacenter="wd3", site="CommBank_Careers")
    assert source_to_line(workday) == "workday cba wd3 CommBank_Careers"


def test_workday_source_to_line_parses_back_to_the_same_source(tmp_path):
    from src.config import load_sources_file

    source = WorkdaySource(type="workday", tenant="flinders", datacenter="wd3", site="flinders_employment")
    path = tmp_path / "sources.txt"
    path.write_text(source_to_line(source) + "\n", encoding="utf-8")
    assert load_sources_file(str(path)) == [source]
