from unittest.mock import patch

from src.config import (
    Filters,
    GreenhouseSource,
    LeverSource,
    SeekSource,
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


def test_seek_is_live_without_a_network_call():
    # No scrape patched: if it tried to hit the network the test would fail.
    r = V.validate_source(SeekSource(), F)
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


def test_kept_filters_to_live_then_to_matching():
    live_match = V.ValidationResult(
        GreenhouseSource(type="greenhouse", board="a"), "a", True, 5, 2, None)
    live_only = V.ValidationResult(
        GreenhouseSource(type="greenhouse", board="b"), "b", True, 5, 0, None)
    dead = V.ValidationResult(
        GreenhouseSource(type="greenhouse", board="c"), "c", False, 0, 0, "err")
    seek = V.ValidationResult(SeekSource(), "seek", True, 0, 0, None)
    results = [live_match, live_only, dead, seek]

    # Default: keep every live board + seek, drop dead.
    kept = V._kept(results, require_match=False)
    assert {r.label for r in kept} == {"a", "b", "seek"}

    # require_match: only boards with a current match (+ seek).
    kept = V._kept(results, require_match=True)
    assert {r.label for r in kept} == {"a", "seek"}


def test_source_to_line_roundtrips():
    assert source_to_line(SeekSource()) == "seek"
    assert source_to_line(GreenhouseSource(type="greenhouse", board="stripe")) == "greenhouse stripe"
    assert source_to_line(LeverSource(type="lever", company="metabase")) == "lever metabase"
