from unittest.mock import patch

from src.config import Filters, GreenhouseSource, LeverSource
from src.ingestion import discover as D
from src.ingestion.validate import ValidationResult


def test_slug_variants_strips_legal_suffixes():
    assert D.slug_variants("Acme Pty Ltd") == ["acme"]
    assert D.slug_variants("Stripe, Inc.") == ["stripe"]


def test_slug_variants_multiword_gives_joined_and_hyphenated():
    assert D.slug_variants("Foo Bar Labs") == ["foobar", "foo-bar"]


def test_slug_variants_empty_or_all_stopwords():
    assert D.slug_variants("") == []
    assert D.slug_variants("The Group Ltd") == []


def test_candidates_cover_three_ats_per_slug():
    cands = D.candidates_for_company("Acme")
    assert {c.type for c in cands} == {"greenhouse", "lever", "ashby"}
    assert all(D._token_of(c) == "acme" for c in cands)


def test_new_candidates_skips_already_configured():
    existing = [GreenhouseSource(type="greenhouse", board="acme")]
    already = D.existing_tokens(existing)
    cands = D.new_candidates(["Acme"], already)
    # greenhouse:acme already configured → only lever + ashby proposed
    assert {c.type for c in cands} == {"lever", "ashby"}


def test_new_candidates_dedups_across_companies():
    cands = D.new_candidates(["Acme", "Acme Pty Ltd"], set())
    keys = [f"{c.type}:{D._token_of(c)}" for c in cands]
    assert len(keys) == len(set(keys))  # no duplicates


def test_discover_keeps_only_live_results():
    F = Filters(titles=["data"], locations=["Remote"])

    def fake_validate(source, filters):
        # greenhouse tokens "live"; everything else dead
        if source.type == "greenhouse":
            return ValidationResult(source, source.board, True, 3, 1, None)
        return ValidationResult(source, "x", False, 0, 0, "404")

    with patch("src.ingestion.discover.validate_source", side_effect=fake_validate):
        live = D.discover(["Acme"], [], F)

    assert len(live) == 1
    assert live[0].source.type == "greenhouse"
    assert live[0].status == "match"


def test_proposal_lines_are_commented_and_match_first():
    gh = GreenhouseSource(type="greenhouse", board="acme")
    lv = LeverSource(type="lever", company="beta")
    results = [
        ValidationResult(gh, "acme", True, 5, 0, None),   # live, no match
        ValidationResult(lv, "beta", True, 5, 2, None),   # match
    ]
    lines = D._proposal_lines(results)
    body = [ln for ln in lines if ln.startswith("# greenhouse") or ln.startswith("# lever")]
    # match (beta) listed before live-no-match (acme); both commented out
    assert body[0].startswith("# lever beta")
    assert body[1].startswith("# greenhouse acme")
