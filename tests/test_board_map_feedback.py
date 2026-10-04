"""Tests for what a scrape tells the board map (KAN-41): is_definite_miss() and
apply_scrape_outcomes() in src/ingestion/board_map.py. No network, no database."""

from datetime import datetime

import pytest
import requests

from src.config import GreenhouseSource, LeverSource
from src.ingestion import board_map as bm
from src.ingestion.board_map import BoardMap, CheckResult
from src.ingestion.workday import WorkdayError

NOW = datetime(2026, 10, 4, 3, 0, 0)
DAY1 = datetime(2026, 10, 5, 9, 0, 0)
DAY2 = datetime(2026, 10, 6, 9, 0, 0)
DAY3 = datetime(2026, 10, 7, 9, 0, 0)

GH = GreenhouseSource(type="greenhouse", board="acme")
LV = LeverSource(type="lever", company="acme")
GH_KEY = ("greenhouse", "acme")


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Error", response=response)


def _mapped():
    results = [CheckResult(GH, True, 10, 2), CheckResult(LV, True, 4, 1)]
    return bm.merge_check(BoardMap(), "Acme", results, NOW)


def _acme(board_map):
    return board_map.find_company("Acme")


def _gh(board_map):
    return _acme(board_map).find_board(GH)


def _apply(board_map, outcomes, now, stale_after=3, pinned=()):
    return bm.apply_scrape_outcomes(board_map, outcomes, stale_after, now, pinned)


# --- is_definite_miss --------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (0, True),
        (12, False),
        (_http_error(404), True),
        (_http_error(410), True),
        (_http_error(422), True),
        (_http_error(400), False),
        (_http_error(403), False),
        (_http_error(429), False),
        (_http_error(500), False),
        (_http_error(503), False),
        (requests.HTTPError("no response attached"), False),
        (requests.Timeout("slow"), False),
        (requests.ConnectionError("down"), False),
        (WorkdayError("list capped at 2000"), False),
        (RuntimeError("anything else"), False),
    ],
)
def test_is_definite_miss(outcome, expected):
    assert bm.is_definite_miss(outcome) is expected


# --- apply_scrape_outcomes ---------------------------------------------------


def test_postings_reset_the_miss_counters_and_update_the_count():
    board_map = _mapped()
    _gh(board_map).misses, _gh(board_map).last_miss_on = 2, "2026-10-04"
    updated, retire = _apply(board_map, [(GH_KEY, 9)], DAY1)
    board = _gh(updated)
    assert (board.misses, board.last_miss_on, board.postings) == (0, None, 9)
    assert board.last_live == "2026-10-05T09:00:00"
    assert retire == []


def test_a_definite_miss_counts_one():
    updated, retire = _apply(_mapped(), [(GH_KEY, _http_error(404))], DAY1)
    board = _gh(updated)
    assert (board.misses, board.last_miss_on, board.status) == (1, "2026-10-05", "live")
    assert retire == []


def test_zero_postings_counts_as_a_miss():
    updated, _ = _apply(_mapped(), [(GH_KEY, 0)], DAY1)
    assert _gh(updated).misses == 1


def test_three_misses_on_one_day_count_once():
    board_map = _mapped()
    for hour in (9, 12, 15):
        board_map, retire = _apply(
            board_map, [(GH_KEY, _http_error(404))], datetime(2026, 10, 5, hour)
        )
        assert retire == []
    board = _gh(board_map)
    assert (board.misses, board.status) == (1, "live")


def test_gone_needs_three_separate_days_and_flags_the_company():
    board_map = _mapped()
    board_map, retire = _apply(board_map, [(GH_KEY, _http_error(404))], DAY1)
    board_map, retire = _apply(board_map, [(GH_KEY, 0)], DAY2)
    assert retire == [] and _gh(board_map).status == "live"
    assert _acme(board_map).needs_check is False

    board_map, retire = _apply(board_map, [(GH_KEY, _http_error(422))], DAY3)
    board = _gh(board_map)
    assert (board.status, board.misses, board.postings) == ("gone", 3, 0)
    assert retire == [GH]
    assert _acme(board_map).needs_check is True
    assert bm.live_sources(board_map) == [LV]  # no longer scraped
    assert _acme(board_map).find_board(LV).status == "live"  # the other board is untouched


def test_stale_after_is_configurable():
    board_map = _mapped()
    board_map, _ = _apply(board_map, [(GH_KEY, 0)], DAY1, stale_after=2)
    board_map, retire = _apply(board_map, [(GH_KEY, 0)], DAY2, stale_after=2)
    assert _gh(board_map).status == "gone" and retire == [GH]


def test_a_success_between_misses_starts_the_count_again():
    board_map = _mapped()
    board_map, _ = _apply(board_map, [(GH_KEY, 0)], DAY1)
    board_map, _ = _apply(board_map, [(GH_KEY, 5)], DAY2)
    board_map, retire = _apply(board_map, [(GH_KEY, 0)], DAY3)
    assert (_gh(board_map).misses, _gh(board_map).status) == (1, "live")
    assert retire == []


@pytest.mark.parametrize(
    "outcome",
    [
        requests.Timeout("slow"),
        requests.ConnectionError("down"),
        _http_error(500),
        _http_error(503),
        _http_error(429),
        WorkdayError("capped"),
        RuntimeError("scraper bug"),
    ],
)
def test_a_failure_that_is_not_a_definite_miss_counts_nothing(outcome):
    board_map = _mapped()
    _gh(board_map).misses, _gh(board_map).last_miss_on = 2, "2026-10-04"
    updated, retire = _apply(board_map, [(GH_KEY, outcome)], DAY1, stale_after=3)
    board = _gh(updated)
    assert (board.misses, board.last_miss_on, board.status) == (2, "2026-10-04", "live")
    assert retire == []
    assert updated == board_map


def test_a_platform_outage_across_many_runs_never_retires_a_board():
    board_map = _mapped()
    for _ in range(10):
        board_map, retire = _apply(board_map, [(GH_KEY, requests.Timeout("down"))], DAY1)
        assert retire == []
    assert _gh(board_map).status == "live"


def test_pinned_boards_are_never_touched():
    board_map = _mapped()
    updated, retire = _apply(board_map, [(GH_KEY, 0)], DAY1, stale_after=1, pinned={GH_KEY})
    assert updated == board_map and retire == []


def test_only_mapped_boards_are_touched():
    board_map = _mapped()
    outcomes = [(("greenhouse", "unknown"), 0), (("workday", "nobody"), 0)]
    updated, retire = _apply(board_map, outcomes, DAY1, stale_after=1)
    assert updated == board_map and retire == []


def test_the_outcome_key_ignores_token_case():
    updated, _ = _apply(_mapped(), [(("greenhouse", "ACME"), 0)], DAY1)
    assert _gh(updated).misses == 1


def test_empty_and_gone_boards_are_not_touched():
    board_map = bm.merge_check(BoardMap(), "Acme", [CheckResult(GH, True, 0, 0)], NOW)
    assert _gh(board_map).status == "empty"
    updated, retire = _apply(board_map, [(GH_KEY, 0)], DAY1, stale_after=1)
    assert updated == board_map and retire == []


def test_one_board_mapped_for_two_companies_is_retired_once():
    board_map = bm.merge_check(BoardMap(), "Acme", [CheckResult(GH, True, 5, 0)], NOW)
    board_map = bm.merge_check(board_map, "Acme Pty Ltd", [CheckResult(GH, True, 5, 0)], NOW)
    updated, retire = _apply(board_map, [(GH_KEY, 0)], DAY1, stale_after=1)
    assert retire == [GH]
    assert all(c.needs_check for c in updated.companies)


def test_apply_scrape_outcomes_does_not_change_its_input():
    board_map = _mapped()
    snapshot = board_map.model_copy(deep=True)
    _apply(board_map, [(GH_KEY, 0)], DAY1, stale_after=1)
    assert board_map == snapshot


def test_a_gone_board_that_resolve_finds_again_goes_live_with_fresh_counters():
    board_map, _ = _apply(_mapped(), [(GH_KEY, 0)], DAY1, stale_after=1)
    assert _gh(board_map).status == "gone"
    back = bm.merge_check(board_map, "Acme", [CheckResult(GH, True, 7, 1)], DAY2)
    board = _gh(back)
    assert (board.status, board.misses, board.last_miss_on) == ("live", 0, None)
    assert _acme(back).needs_check is False
