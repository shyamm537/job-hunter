"""Tests for the resolve step's stage 2 behaviour (KAN-41): a failure that is not
"the board is not there" leaves a mapped board alone, and a company removed from
companies.txt has its stored postings retired. Reuses the fakes of test_resolve."""

from datetime import timedelta

import pytest
import requests
from sqlmodel import select

import src.storage.database as database
from src.config import GreenhouseSource
from src.ingestion import board_map as bm
from src.ingestion import resolve as R
from src.ingestion.board_map import BoardMap, CheckResult
from src.storage.models import JobPost
from tests.test_resolve import NOW, FakeBoards, _config, _http_error, _rows, _run

GH = GreenhouseSource(type="greenhouse", board="acme")


@pytest.fixture(autouse=True)
def _reset_database():
    database._database_url = None
    database._engine = None
    yield
    database._database_url = None
    database._engine = None


class Flaky(FakeBoards):
    """Every dead board fails with this exception instead of a 404."""

    def __init__(self, live=None, error=None):
        super().__init__(live)
        self.error = error

    def __call__(self, source):
        result = super().__call__(source)
        if not result.live:
            result.exception = self.error
        return result


# --- failures that are not "the board is not there" ---------------------------


@pytest.mark.parametrize(
    "error", [requests.Timeout("slow"), requests.ConnectionError("down"), _http_error(503)]
)
def test_a_timeout_or_server_error_does_not_mark_a_live_board_gone(tmp_path, error):
    cfg = _config(tmp_path, "Acme\n")
    _run(cfg, FakeBoards({"greenhouse acme": 7}))
    outcome = _run(cfg, Flaky(error=error), check_all=True, now=NOW + timedelta(days=20))
    company = outcome.board_map.find_company("Acme")
    assert company.boards[0].status == "live" and company.boards[0].postings == 7
    assert len(bm.live_sources(outcome.board_map)) == 1  # still scraped
    assert company.checked_at == "2026-10-24T03:00:00"


def test_a_guess_that_could_not_be_asked_is_not_recorded(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    outcome = _run(cfg, Flaky(error=requests.Timeout("slow")))
    assert outcome.board_map.find_company("Acme").boards == []


def test_an_unreachable_known_board_is_reported_as_unreachable_not_not_found(tmp_path):
    cfg = _config(tmp_path, "Bank | https://cba.wd3.myworkdayjobs.com/CommBank_Careers\n")
    outcome = _run(cfg, Flaky(error=requests.Timeout("slow")))
    assert [(r.line, r.flag) for r in outcome.rows] == [
        ("workday cba wd3 CommBank_Careers", "UNREACHABLE")
    ]


def test_an_existing_board_that_could_not_be_asked_is_flagged_unreachable(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    _run(cfg, FakeBoards({"greenhouse acme": 7}))
    outcome = _run(
        cfg, Flaky(error=requests.Timeout("slow")), check_all=True, now=NOW + timedelta(days=20)
    )
    row = _rows(outcome)["greenhouse acme"]
    assert (row.flag, row.status) == ("UNREACHABLE", "live")


@pytest.mark.parametrize("status", [404, 410, 422])
def test_a_definite_miss_still_marks_the_board_gone(tmp_path, status):
    cfg = _config(tmp_path, "Acme\n")
    _run(cfg, FakeBoards({"greenhouse acme": 7}))
    outcome = _run(
        cfg, FakeBoards(dead_status=status), check_all=True, now=NOW + timedelta(days=20)
    )
    assert outcome.board_map.find_company("Acme").boards[0].status == "gone"


# --- removing a company retires its stored postings ---------------------------


def _row(job_board_id, company):
    return dict(
        job_board_id=job_board_id, title="Data Analyst", company=company,
        location="Remote", description="d", url=f"http://x/{job_board_id}",
    )


def _store(tmp_path, *rows):
    database.set_database_url(f"sqlite:///{tmp_path / 'jobs.db'}")
    database.init_db()
    with database.get_session() as session:
        session.add_all(JobPost(**row) for row in rows)
        session.commit()


def _stored(tmp_path):
    database.set_database_url(f"sqlite:///{tmp_path / 'jobs.db'}")
    with database.get_session() as session:
        return {r.job_board_id: r for r in session.exec(select(JobPost)).all()}


def _remove_other(tmp_path):
    (tmp_path / "companies.txt").write_text("Acme\n", encoding="utf-8")


def test_a_removed_company_has_its_stored_postings_retired(tmp_path):
    cfg = _config(tmp_path, "Acme\nOther\n")
    _run(cfg, FakeBoards({"greenhouse acme": 5, "lever other": 1}))
    _store(
        tmp_path,
        _row("greenhouse-1", "acme"), _row("greenhouse-2", "acme"), _row("lever-3", "other"),
    )
    _remove_other(tmp_path)

    outcome = _run(cfg, FakeBoards(), now=NOW + timedelta(days=1))

    assert outcome.dropped == ["Other"] and outcome.retired == 1
    rows = _stored(tmp_path)
    assert rows["lever-3"].dead_at is not None
    assert rows["lever-3"].dead_reason == "company removed"
    assert rows["greenhouse-1"].dead_at is None and rows["greenhouse-2"].dead_at is None
    assert "1 stored posting(s) retired" in R.format_report(outcome)
    assert [c.name for c in bm.load_map(cfg.board_map_file).companies] == ["Acme"]


def test_a_dry_run_retires_nothing(tmp_path):
    cfg = _config(tmp_path, "Acme\nOther\n")
    _run(cfg, FakeBoards({"greenhouse acme": 5, "lever other": 1}))
    _store(tmp_path, _row("lever-3", "other"))
    _remove_other(tmp_path)

    outcome = _run(cfg, FakeBoards(), dry_run=True, now=NOW + timedelta(days=1))

    assert outcome.retired == 0
    assert _stored(tmp_path)["lever-3"].dead_at is None
    assert [c.name for c in bm.load_map(cfg.board_map_file).companies] == ["Acme", "Other"]


def test_a_removed_companys_pinned_board_is_not_retired(tmp_path):
    cfg = _config(tmp_path, "Acme\nOther\n", pinned="lever other\n")
    # `lever other` is pinned, so resolve never maps it; the map holds another board.
    _run(cfg, FakeBoards({"greenhouse other": 3}))
    _store(tmp_path, _row("lever-3", "other"), _row("greenhouse-4", "other"))
    _remove_other(tmp_path)

    _run(cfg, FakeBoards(), now=NOW + timedelta(days=1))

    rows = _stored(tmp_path)
    assert rows["lever-3"].dead_at is None  # still scraped from sources.txt
    assert rows["greenhouse-4"].dead_at is not None


def test_a_board_a_remaining_company_still_uses_is_not_retired(tmp_path):
    cfg = _config(tmp_path, "Acme\nAcme Pty Ltd\n")
    _run(cfg, FakeBoards({"greenhouse acme": 5}))  # both names reduce to the token "acme"
    board_map = bm.load_map(cfg.board_map_file)
    # Give both companies the same board, as two spellings of one employer can.
    board_map = bm.merge_check(board_map, "Acme Pty Ltd", [CheckResult(GH, True, 5, 0)], NOW)
    bm.save_map(board_map, cfg.board_map_file)
    _store(tmp_path, _row("greenhouse-1", "acme"))
    _remove_other(tmp_path)

    outcome = _run(cfg, FakeBoards(), now=NOW + timedelta(days=1))

    assert outcome.dropped == ["Acme Pty Ltd"] and outcome.retired == 0
    assert _stored(tmp_path)["greenhouse-1"].dead_at is None


def test_retire_is_not_called_when_no_company_was_removed(tmp_path):
    cfg = _config(tmp_path, "Acme\n")
    calls = []
    R.run_resolve(
        cfg, check=FakeBoards(), now=NOW, sleep=lambda s: None,
        retire=lambda config, sources: calls.append(sources) or 0,
    )
    assert calls == []


def test_removed_boards_lists_each_board_once_and_skips_pinned_ones():
    board_map = bm.merge_check(BoardMap(), "A", [CheckResult(GH, True, 3, 0)], NOW)
    board_map = bm.merge_check(board_map, "B", [CheckResult(GH, True, 3, 0)], NOW)
    assert R.removed_boards(board_map, BoardMap(), []) == [GH]
    assert R.removed_boards(board_map, BoardMap(), [GH]) == []
