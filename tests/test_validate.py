from datetime import datetime
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


def _workday_rows(titles):
    return [
        {
            "title": title,
            "externalPath": f"/job/Sydney/Role-{n}_R{n:06d}",
            "locationsText": "Remote",
            "bulletFields": [f"R{n:06d}"],
        }
        for n, title in enumerate(titles)
    ]


def _validate_workday(details):
    source = WorkdaySource(type="workday", tenant="acme", datacenter="wd3", site="Careers")
    rows = _workday_rows(["Senior Data Analyst", "Chef"])
    page = {"total": len(rows), "jobPostings": rows}
    with patch("src.ingestion.workday.post_json", return_value=page), \
         patch("src.ingestion.workday.get_json", return_value={"jobPostingInfo": {}}) as get, \
         patch("src.ingestion.workday.time.sleep"):
        result = V.validate_source(source, F, details=details)
    return result, get


def test_validate_source_fetches_workday_details_by_default():
    result, get = _validate_workday(details=True)
    assert result.live and result.total == 2 and result.matched == 1
    assert get.call_count == 1  # one detail call, for the title match


def test_details_false_makes_no_detail_calls_but_reports_the_same_matches():
    result, get = _validate_workday(details=False)
    assert get.call_count == 0
    assert result.live and result.total == 2 and result.matched == 1


def test_details_false_does_not_change_the_callers_filters():
    filters = Filters(titles=["data analyst"], also_match_titles=["data engineer"])
    jobs = _jobs(("Data Engineer", "Remote"))
    with patch("src.ingestion.greenhouse.GreenhouseScraper.scrape", return_value=jobs):
        result = V.validate_source(
            GreenhouseSource(type="greenhouse", board="acme"), filters, details=False
        )
    assert result.matched == 1  # still counted with the real filters
    assert filters.titles == ["data analyst"] and filters.also_match_titles == ["data engineer"]


def _run_main(tmp_path, monkeypatch, sources_inline, sources_file_text, argv):
    """Run validate's main() with every board reported live, and return the
    lines written to the --out file."""
    import src.config as config_module

    sources_path = tmp_path / "sources.txt"
    sources_path.write_text(sources_file_text, encoding="utf-8")
    cfg = config_module.Config.model_validate(
        {
            "sources": sources_inline,
            "sources_file": str(sources_path),
            "companies_file": "companies.txt",
            "board_map_file": str(tmp_path / "board_map.yaml"),
        }
    )
    monkeypatch.setattr(V, "load_config", lambda: cfg)
    monkeypatch.setattr(
        V,
        "validate_source",
        lambda source, filters, details=True: V.ValidationResult(
            source, source.type, True, 5, 1, None
        ),
    )
    out = tmp_path / "out.txt"
    V.main([*argv, "--out", str(out)])
    return [
        line for line in out.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]


def test_out_writes_pinned_sources_only_never_mapped_boards(tmp_path, monkeypatch):
    from src.ingestion import board_map as bm

    mapped = LeverSource(type="lever", company="mapped")
    bm.save_map(
        bm.merge_check(
            bm.BoardMap(), "Mapped Co", [bm.CheckResult(mapped, True, 4, 1)],
            datetime(2026, 10, 4),
        ),
        str(tmp_path / "board_map.yaml"),
    )
    lines = _run_main(
        tmp_path, monkeypatch,
        sources_inline=[{"type": "adzuna", "country": "au"}],
        sources_file_text="greenhouse pinned\n",
        argv=[],
    )
    assert lines == ["adzuna au", "greenhouse pinned"]


def test_validate_still_checks_the_mapped_boards(tmp_path, monkeypatch):
    from src.ingestion import board_map as bm

    mapped = LeverSource(type="lever", company="mapped")
    bm.save_map(
        bm.merge_check(
            bm.BoardMap(), "Mapped Co", [bm.CheckResult(mapped, True, 4, 1)],
            datetime(2026, 10, 4),
        ),
        str(tmp_path / "board_map.yaml"),
    )
    checked = []
    import src.config as config_module

    sources_path = tmp_path / "sources.txt"
    sources_path.write_text("greenhouse pinned\n", encoding="utf-8")
    cfg = config_module.Config.model_validate(
        {
            "sources_file": str(sources_path),
            "companies_file": "companies.txt",
            "board_map_file": str(tmp_path / "board_map.yaml"),
        }
    )
    monkeypatch.setattr(V, "load_config", lambda: cfg)

    def fake(source, filters, details=True):
        checked.append(source.type)
        return V.ValidationResult(source, source.type, True, 5, 1, None)

    monkeypatch.setattr(V, "validate_source", fake)
    V.main([])
    assert checked == ["greenhouse", "lever"]


def test_out_with_an_input_file_writes_that_files_sources(tmp_path, monkeypatch):
    candidates = tmp_path / "cands.txt"
    candidates.write_text("greenhouse cand\nlever cand\n", encoding="utf-8")
    lines = _run_main(
        tmp_path, monkeypatch,
        sources_inline=[{"type": "adzuna", "country": "au"}],
        sources_file_text="greenhouse pinned\n",
        argv=[str(candidates)],
    )
    assert lines == ["greenhouse cand", "lever cand"]
