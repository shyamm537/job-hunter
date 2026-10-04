"""Tests for the resolve step (src/ingestion/resolve.py). The board check is
replaced by a fake, so nothing touches the network."""

from datetime import datetime, timedelta

import pytest
import requests

from src.config import (
    AshbySource,
    Company,
    Config,
    ConfigError,
    GreenhouseSource,
    LeverSource,
    WorkableSource,
    WorkdaySource,
    source_key,
)
from src.ingestion import board_map as bm
from src.ingestion import resolve as R
from src.ingestion.board_map import BoardMap
from src.ingestion.validate import ValidationResult

NOW = datetime(2026, 10, 4, 3, 0, 0)
GH = GreenhouseSource(type="greenhouse", board="acme")
ALL_BOARDS = ["greenhouse", "lever", "ashby", "workable"]
WD = WorkdaySource(type="workday", tenant="cba", datacenter="wd3", site="CommBank_Careers")


def _types(sources):
    return sorted(s.type for s in sources)


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Error", response=response)


class FakeBoards:
    """Stands in for validate_source: `live` maps a source line to the number of
    postings it has; anything else is dead. Records every call."""

    def __init__(self, live=None, dead_status=404):
        self.live = live or {}
        self.dead_status = dead_status
        self.calls = []

    def __call__(self, source):
        from src.config import source_to_line

        line = source_to_line(source)
        self.calls.append(line)
        if line in self.live:
            n = self.live[line]
            samples = [(f"Role {i}", "Adelaide") for i in range(min(n, 3))]
            return ValidationResult(source, line, True, n, 1 if n else 0, None, samples)
        return ValidationResult(
            source, line, False, 0, 0, "404", exception=_http_error(self.dead_status)
        )


# --- candidates --------------------------------------------------------------


def test_candidates_are_every_board_times_every_token():
    company = Company(name="Foo Bar", aliases=["fb"])
    c = R.candidates_for(company, ALL_BOARDS, [], [])
    assert len(c.to_check) == 4 * 3  # 4 boards x (fb, foobar, foo-bar)
    assert {source_key(s)[1] for s in c.to_check} == {"fb", "foobar", "foo-bar"}
    assert _types(c.to_check) == sorted(ALL_BOARDS * 3)
    assert c.pinned == []


def test_only_the_enabled_boards_are_tried():
    c = R.candidates_for(Company(name="Acme"), ["greenhouse", "workable"], [], [])
    assert _types(c.to_check) == ["greenhouse", "workable"]


def test_a_known_board_is_checked_too_and_first():
    company = Company(name="Commonwealth Bank", aliases=["cba"], known=[WD])
    c = R.candidates_for(company, ALL_BOARDS, [], [])
    assert c.to_check[0] == WD
    assert "workday" not in _types(c.to_check[1:])  # workday is never guessed


def test_blocked_candidates_are_skipped():
    blocked = [AshbySource(type="ashby", org="amp")]
    c = R.candidates_for(Company(name="AMP"), ALL_BOARDS, blocked, [])
    assert ("ashby", "amp") not in {source_key(s) for s in c.to_check}
    assert _types(c.to_check) == ["greenhouse", "lever", "workable"]


def test_a_block_matches_the_token_in_any_case():
    blocked = [AshbySource(type="ashby", org="AMP")]
    c = R.candidates_for(Company(name="amp"), ["ashby"], blocked, [])
    assert c.to_check == []


def test_pinned_candidates_are_reported_and_not_checked():
    pinned = [GreenhouseSource(type="greenhouse", board="acme")]
    c = R.candidates_for(Company(name="Acme"), ALL_BOARDS, [], pinned)
    assert c.pinned == pinned
    assert "greenhouse" not in _types(c.to_check)


def test_an_alias_keeps_its_case_and_wins_over_an_equal_slug():
    c = R.candidates_for(Company(name="Squiz", aliases=["Squiz"]), ["workable"], [], [])
    assert [s.account for s in c.to_check] == ["Squiz"]


def test_a_name_with_no_usable_slug_has_only_its_aliases_and_known_boards():
    c = R.candidates_for(Company(name="The Group Ltd", aliases=["tgl"]), ["lever"], [], [])
    assert [s.company for s in c.to_check] == ["tgl"]
    none = R.candidates_for(Company(name="The Group Ltd"), ALL_BOARDS, [], [])
    assert none.to_check == []


# --- due selection -----------------------------------------------------------


def test_select_due_new_flagged_and_stale_companies():
    companies = [Company(name=n) for n in ("New", "Fresh", "Flagged", "Stale")]
    board_map = BoardMap()
    for name, when in (("Fresh", NOW), ("Flagged", NOW), ("Stale", NOW - timedelta(days=15))):
        board_map = bm.merge_check(board_map, name, [], when)
    board_map.find_company("Flagged").needs_check = True

    due = R.select_due(companies, board_map, NOW, 14)
    assert [c.name for c in due] == ["New", "Flagged", "Stale"]


def test_all_checks_every_company():
    companies = [Company(name="A"), Company(name="B")]
    board_map = bm.merge_check(BoardMap(), "A", [], NOW)
    assert [c.name for c in R.select_due(companies, board_map, NOW, 14, check_all=True)] == ["A", "B"]


def test_company_checks_one_even_if_not_due_and_ignores_case():
    companies = [Company(name="Acme"), Company(name="Other")]
    board_map = bm.merge_check(BoardMap(), "Acme", [], NOW)
    assert [c.name for c in R.select_due(companies, board_map, NOW, 14, only="acme")] == ["Acme"]


def test_limit_caps_the_number_of_companies():
    companies = [Company(name=n) for n in "ABCD"]
    assert [c.name for c in R.select_due(companies, BoardMap(), NOW, 14, limit=2)] == ["A", "B"]


# --- resolving ---------------------------------------------------------------


def _config(tmp_path, companies_text, pinned="", **resolve):
    companies = tmp_path / "companies.txt"
    companies.write_text(companies_text, encoding="utf-8")
    sources = tmp_path / "sources.txt"
    sources.write_text(pinned, encoding="utf-8")
    return Config.model_validate(
        {
            "filters": {"titles": ["data analyst"]},
            "sources_file": str(sources),
            "companies_file": str(companies),
            "board_map_file": str(tmp_path / "board_map.yaml"),
            "database": {"url": f"sqlite:///{tmp_path / 'jobs.db'}"},
            "resolve": resolve,
        }
    )


def _run(config, fake, **kw):
    kw.setdefault("now", NOW)
    return R.run_resolve(config, check=fake, sleep=lambda s: None, **kw)


def test_all_live_hits_are_kept_not_just_the_first(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    fake = FakeBoards({"greenhouse acme": 5, "lever acme": 2})
    outcome = _run(cfg, fake)
    lines = [b.line for b in outcome.board_map.find_company("Acme").boards]
    assert lines == ["greenhouse acme", "lever acme"]
    assert len(fake.calls) == 4  # every board was tried, in spite of the first hit


def test_the_map_is_saved_and_scrape_reads_it(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    _run(cfg, FakeBoards({"greenhouse acme": 5}))
    assert [s.board for s in cfg.resolved_sources] == ["acme"]


def test_dry_run_writes_nothing(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    outcome = _run(cfg, FakeBoards({"greenhouse acme": 5}), dry_run=True)
    assert not (tmp_path / "board_map.yaml").exists()
    assert outcome.board_map.find_company("Acme") is not None  # it is in the report


def test_a_company_checked_recently_is_not_checked_again(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    first = FakeBoards({"greenhouse acme": 5})
    _run(cfg, first)
    second = FakeBoards({"greenhouse acme": 5})
    outcome = _run(cfg, second, now=NOW + timedelta(days=1))
    assert second.calls == [] and outcome.checked == []
    third = FakeBoards({"greenhouse acme": 5})
    _run(cfg, third, now=NOW + timedelta(days=15))
    assert len(third.calls) == 4


def test_all_and_company_flags_force_a_check(tmp_path):
    cfg = _config(tmp_path, "Acme\nOther\n")
    _run(cfg, FakeBoards())
    forced = FakeBoards()
    _run(cfg, forced, check_all=True, now=NOW + timedelta(days=1))
    assert len(forced.calls) == 8
    one = FakeBoards()
    outcome = _run(cfg, one, only="other", now=NOW + timedelta(days=1))
    assert outcome.checked == ["Other"] and len(one.calls) == 4


def test_an_unknown_company_flag_is_an_error(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    with pytest.raises(ConfigError, match="--company 'Nope'"):
        _run(cfg, FakeBoards(), only="Nope")


def test_without_companies_file_it_is_an_error():
    cfg = Config.model_validate({"sources": [{"type": "adzuna", "country": "au"}]})
    with pytest.raises(ConfigError, match="companies_file is not set"):
        R.run_resolve(cfg)


def test_a_pinned_board_is_reported_pinned_and_not_added_to_the_map(tmp_path):
    cfg = _config(tmp_path, "Acme\n", pinned="greenhouse acme\n")
    fake = FakeBoards({"greenhouse acme": 5, "lever acme": 2})
    outcome = _run(cfg, fake)
    assert "greenhouse acme" not in fake.calls
    assert [b.line for b in outcome.board_map.find_company("Acme").boards] == ["lever acme"]
    assert any(r.status == "pinned" and r.line == "greenhouse acme" for r in outcome.rows)


def test_blocked_boards_are_never_checked_or_recorded(tmp_path):
    cfg = _config(tmp_path, "AMP\n- ashby amp\n")
    fake = FakeBoards({"ashby amp": 9, "greenhouse amp": 1})
    outcome = _run(cfg, fake)
    assert "ashby amp" not in fake.calls
    assert [b.line for b in outcome.board_map.find_company("AMP").boards] == ["greenhouse amp"]


def test_a_company_deleted_from_the_file_is_dropped_from_the_map(tmp_path):
    cfg = _config(tmp_path, "Acme\nOther\n")
    _run(cfg, FakeBoards({"greenhouse acme": 5, "lever other": 1}))
    (tmp_path / "companies.txt").write_text("Acme\n", encoding="utf-8")
    outcome = _run(cfg, FakeBoards(), now=NOW + timedelta(days=1))
    assert outcome.dropped == ["Other"]
    assert [c.name for c in bm.load_map(cfg.board_map_file).companies] == ["Acme"]


def test_a_known_url_board_is_recorded_when_live(tmp_path):
    cfg = _config(
        tmp_path,
        "Commonwealth Bank | cba, https://cba.wd3.myworkdayjobs.com/CommBank_Careers\n",
    )
    outcome = _run(cfg, FakeBoards({"workday cba wd3 CommBank_Careers": 167}))
    lines = [b.line for b in outcome.board_map.find_company("Commonwealth Bank").boards]
    assert lines == ["workday cba wd3 CommBank_Careers"]
    assert [s.type for s in cfg.resolved_sources if s.type == "workday"] == ["workday"]


def test_a_known_url_board_that_does_not_answer_is_reported(tmp_path):
    cfg = _config(tmp_path, "Commonwealth Bank | https://cba.wd3.myworkdayjobs.com/CommBank_Careers\n")
    outcome = _run(cfg, FakeBoards())
    assert [(r.line, r.flag) for r in outcome.rows] == [
        ("workday cba wd3 CommBank_Careers", "NOT FOUND")
    ]


def test_a_failure_part_way_keeps_the_companies_already_saved(tmp_path):
    cfg = _config(tmp_path, "Acme\nBoom\n")

    class Exploding(FakeBoards):
        def __call__(self, source):
            if "boom" in str(source):
                raise KeyboardInterrupt
            return super().__call__(source)

    with pytest.raises(KeyboardInterrupt):
        _run(cfg, Exploding({"greenhouse acme": 5}))
    assert [c.name for c in bm.load_map(cfg.board_map_file).companies] == ["Acme"]


def test_the_check_sleeps_between_candidates_but_not_before_the_first(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    pauses = []
    R.run_resolve(cfg, check=FakeBoards(), now=NOW, sleep=pauses.append)
    assert len(pauses) == 3  # 4 candidates
    assert all(p == R.REQUEST_DELAY for p in pauses)


# --- the report flags --------------------------------------------------------


def _rows(outcome):
    return {r.line: r for r in outcome.rows}


def test_new_boards_are_flagged_and_show_three_samples(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    rows = _rows(_run(cfg, FakeBoards({"greenhouse acme": 7, "lever acme": 0})))
    new = rows["greenhouse acme"]
    assert (new.flag, new.status, new.postings) == ("NEW", "live", 7)
    assert new.samples == [("Role 0", "Adelaide"), ("Role 1", "Adelaide"), ("Role 2", "Adelaide")]
    empty = rows["lever acme"]
    assert (empty.flag, empty.status) == ("EMPTY", "empty") and empty.samples == []


def test_a_board_that_died_is_flagged_gone_and_one_that_returned_back(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    _run(cfg, FakeBoards({"greenhouse acme": 7}))
    rows = _rows(_run(cfg, FakeBoards(), check_all=True, now=NOW + timedelta(days=20)))
    assert (rows["greenhouse acme"].flag, rows["greenhouse acme"].status) == ("GONE", "gone")
    back = _rows(
        _run(cfg, FakeBoards({"greenhouse acme": 4}), check_all=True, now=NOW + timedelta(days=40))
    )["greenhouse acme"]
    assert (back.flag, back.status) == ("BACK", "live")


def test_a_company_that_moved_boards_is_flagged_moved(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    _run(cfg, FakeBoards({"greenhouse acme": 7}))
    rows = _rows(
        _run(cfg, FakeBoards({"lever acme": 3}), check_all=True, now=NOW + timedelta(days=20))
    )
    assert rows["greenhouse acme"].flag == "GONE"
    assert rows["lever acme"].flag == "MOVED"
    assert rows["lever acme"].samples  # a moved board gets samples too


def test_an_unchanged_live_board_has_no_flag(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    _run(cfg, FakeBoards({"greenhouse acme": 7}))
    rows = _rows(_run(cfg, FakeBoards({"greenhouse acme": 7}), check_all=True, now=NOW + timedelta(days=20)))
    assert rows["greenhouse acme"].flag == "" and rows["greenhouse acme"].samples == []


def test_format_report_shows_flags_samples_and_a_summary(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    outcome = _run(cfg, FakeBoards({"greenhouse acme": 7}), dry_run=True)
    text = R.format_report(outcome, dry_run=True)
    assert "greenhouse acme" in text and "NEW" in text
    assert "- Role 0  (Adelaide)" in text
    assert "1 company(ies) checked; 1 NEW." in text
    assert "Dry run: nothing was saved." in text


def test_format_report_with_nothing_to_show(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    _run(cfg, FakeBoards())
    outcome = _run(cfg, FakeBoards(), now=NOW + timedelta(days=1))
    assert "No boards found or changed." in R.format_report(outcome)
    assert "0 company(ies) checked." in R.format_report(outcome)


def test_resolve_never_uses_workday_without_a_url(tmp_path):
    cfg = _config(tmp_path, "Commonwealth Bank | cba\n")
    fake = FakeBoards()
    _run(cfg, fake)
    assert all(not call.startswith("workday") for call in fake.calls)
    assert isinstance(R.RESOLVABLE["workable"]("x"), WorkableSource)
    assert isinstance(R.RESOLVABLE["lever"]("x"), LeverSource)
