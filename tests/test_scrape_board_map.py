"""Tests for what `make scrape` does to the board map (KAN-41, plans/05 stage 2):
a mapped board that keeps answering "not here" is marked gone and its rows are
retired and archived; a pinned board that fails is only logged. The planner and
config are mocked, the clock is faked, and nothing touches the network."""

from datetime import datetime

import pytest
import requests
from sqlmodel import select

import src.config as config
import src.storage.archive as archive
import src.storage.database as database
from src.config import GreenhouseSource
from src.ingestion import board_map as bm
from src.ingestion import cli
from src.ingestion.board_map import BoardMap, CheckResult
from src.ingestion.planner import PlannedScrape
from src.storage.models import JobPost

GH = GreenhouseSource(type="greenhouse", board="acme")
BOARD = ("greenhouse", "acme")
DAY1 = datetime(2026, 10, 5, 9, 0, 0)
DAY2 = datetime(2026, 10, 6, 9, 0, 0)
DAY3 = datetime(2026, 10, 7, 9, 0, 0)


@pytest.fixture(autouse=True)
def _reset_db_state(monkeypatch):
    monkeypatch.delenv(database.ENV_VAR, raising=False)
    database._database_url = None
    database._engine = None
    yield
    database._database_url = None
    database._engine = None
    archive._archive_url = None
    archive._archive_engine = None


class FakeScraper:
    def __init__(self, specs=None, error=None):
        self._specs = specs or []
        self._error = error

    def scrape(self):
        if self._error is not None:
            raise self._error
        return [JobPost(**spec) for spec in self._specs]


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Error", response=response)


def _spec(n):
    return dict(
        job_board_id=f"greenhouse-acme-{n}", title="Data Analyst", company="acme",
        location="Remote", description="d", url=f"http://x/acme/{n}",
    )


class Setup:
    """A temp database, a config, a board map and a clock the test can move."""

    def __init__(self, tmp_path, monkeypatch, *, pinned=False, companies=True):
        self.db = tmp_path / "jobs.db"
        self.map_path = str(tmp_path / "board_map.yaml")
        self.monkeypatch = monkeypatch
        self.now = DAY1
        raw = {
            "sources": [{"type": "greenhouse", "board": "acme"}] if pinned
            else [{"type": "adzuna", "country": "au"}],
            "database": {"url": f"sqlite:///{self.db}"},
            "board_map_file": self.map_path,
        }
        if companies:
            raw["companies_file"] = "companies.txt"
        self.config = config.Config.model_validate(raw)
        monkeypatch.setattr(cli, "load_config", lambda *a, **k: self.config)
        monkeypatch.setattr(cli, "utcnow", lambda: self.now)
        database.set_database_url(f"sqlite:///{self.db}")
        database.init_db()
        with database.get_session() as session:
            session.add_all([JobPost(**_spec(1)), JobPost(**_spec(2))])
            session.commit()
        bm.save_map(
            bm.merge_check(BoardMap(), "Acme", [CheckResult(GH, True, 2, 0)], DAY1),
            self.map_path,
        )

    def scrape(self, scraper, day):
        self.now = day
        plan = PlannedScrape(scraper, "greenhouse[acme]", post_filter=True, board=BOARD)
        self.monkeypatch.setattr(cli, "plan_scrapes", lambda s, f, **k: [plan])
        cli.main()

    def board(self):
        return bm.load_map(self.map_path).find_company("Acme").find_board(GH)

    def company(self):
        return bm.load_map(self.map_path).find_company("Acme")

    def rows(self):
        database.set_database_url(f"sqlite:///{self.db}")
        with database.get_session() as session:
            return session.exec(select(JobPost)).all()

    def archived(self):
        path = self.db.parent / archive.ARCHIVE_FILENAME
        if not path.exists():
            return []
        archive.set_archive_url(f"sqlite:///{path}")
        with archive.get_archive_session() as session:
            return session.exec(select(JobPost)).all()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def test_a_mapped_board_missing_on_three_separate_days_is_retired_and_archived(setup):
    setup.scrape(FakeScraper(error=_http_error(404)), DAY1)
    setup.scrape(FakeScraper(specs=[]), DAY2)  # zero postings counts too
    assert setup.board().status == "live" and setup.board().misses == 2
    assert len(setup.rows()) == 2

    setup.scrape(FakeScraper(error=_http_error(422)), DAY3)

    assert setup.board().status == "gone"
    assert setup.company().needs_check is True
    assert setup.rows() == []  # retired, then moved out by the archive step
    archived = setup.archived()
    assert {r.job_board_id for r in archived} == {"greenhouse-acme-1", "greenhouse-acme-2"}
    assert {r.dead_reason for r in archived} == {"board retired"}


def test_three_scrapes_on_one_day_leave_the_board_live(setup):
    for hour in (9, 12, 15):
        setup.scrape(FakeScraper(error=_http_error(404)), datetime(2026, 10, 5, hour))
    assert setup.board().status == "live" and setup.board().misses == 1
    assert len(setup.rows()) == 2 and setup.archived() == []
    assert setup.company().needs_check is False


@pytest.mark.parametrize(
    "error",
    [requests.Timeout("slow"), requests.ConnectionError("down"), _http_error(503),
     RuntimeError("scraper bug")],
)
def test_a_board_that_times_out_or_errors_stays_live(setup, error):
    for day in (DAY1, DAY2, DAY3):
        setup.scrape(FakeScraper(error=error), day)
    assert setup.board().status == "live" and setup.board().misses == 0
    assert len(setup.rows()) == 2 and setup.archived() == []


def test_a_successful_scrape_clears_earlier_misses(setup):
    setup.scrape(FakeScraper(error=_http_error(404)), DAY1)
    setup.scrape(FakeScraper(specs=[_spec(1), _spec(2)]), DAY2)
    assert setup.board().misses == 0 and setup.board().last_miss_on is None
    setup.scrape(FakeScraper(error=_http_error(404)), DAY3)
    assert setup.board().misses == 1 and setup.board().status == "live"


def test_a_pinned_board_failing_is_only_logged(tmp_path, monkeypatch, caplog):
    setup = Setup(tmp_path, monkeypatch, pinned=True)
    before = bm.load_map(setup.map_path)
    for day in (DAY1, DAY2, DAY3, datetime(2026, 10, 8)):
        setup.scrape(FakeScraper(error=_http_error(404)), day)
    assert bm.load_map(setup.map_path) == before  # the map is not even rewritten
    assert len(setup.rows()) == 2 and setup.archived() == []
    assert "scrape failed for greenhouse[acme]" in caplog.text


def test_stale_after_comes_from_config(tmp_path, monkeypatch):
    setup = Setup(tmp_path, monkeypatch)
    setup.config = config.Config.model_validate(
        {**setup.config.model_dump(mode="json", exclude_none=True), "resolve": {"stale_after": 2}}
    )
    setup.scrape(FakeScraper(specs=[]), DAY1)
    setup.scrape(FakeScraper(specs=[]), DAY2)
    assert setup.board().status == "gone"


def test_without_companies_file_the_map_is_never_touched(tmp_path, monkeypatch):
    setup = Setup(tmp_path, monkeypatch, pinned=True, companies=False)
    before = bm.load_map(setup.map_path)
    for day in (DAY1, DAY2, DAY3):
        setup.scrape(FakeScraper(error=_http_error(404)), day)
    assert bm.load_map(setup.map_path) == before
    assert len(setup.rows()) == 2


def test_postings_found_update_the_map_without_retiring_anything(setup):
    setup.scrape(FakeScraper(specs=[_spec(1), _spec(2)]), DAY1)
    board = setup.board()
    assert (board.status, board.postings, board.last_live) == ("live", 2, "2026-10-05T09:00:00")
    assert len(setup.rows()) == 2


def test_a_corrupt_map_is_logged_and_the_scrape_still_finishes(setup, caplog):
    # Break the file after config load (resolved_sources already read it).
    plan = PlannedScrape(
        FakeScraper(specs=[_spec(1), _spec(2)]), "greenhouse[acme]", post_filter=True, board=BOARD
    )
    setup.monkeypatch.setattr(cli, "plan_scrapes", lambda s, f, **k: [plan])
    open(setup.map_path, "w", encoding="utf-8").write("- not a mapping\n")
    setup.monkeypatch.setattr(
        type(setup.config), "resolved_sources", property(lambda self: self.pinned_sources)
    )
    cli.main()
    assert "board map not updated" in caplog.text
    assert len(setup.rows()) == 2


def test_a_retired_board_that_resolve_finds_again_gets_its_postings_back(setup):
    """Gone, archived, then found live again by resolve: the next scrape restores
    the archived postings the filters keep (upsert_job restores from the archive)."""
    setup.scrape(FakeScraper(specs=[]), DAY1)
    setup.scrape(FakeScraper(specs=[]), DAY2)
    setup.scrape(FakeScraper(specs=[]), DAY3)
    assert setup.board().status == "gone" and setup.rows() == []

    back = bm.merge_check(
        bm.load_map(setup.map_path), "Acme", [CheckResult(GH, True, 2, 0)], datetime(2026, 10, 20)
    )
    bm.save_map(back, setup.map_path)
    setup.scrape(FakeScraper(specs=[_spec(1), _spec(2)]), datetime(2026, 10, 21))

    assert {r.job_board_id for r in setup.rows()} == {"greenhouse-acme-1", "greenhouse-acme-2"}
    assert all(r.dead_at is None for r in setup.rows())
    assert setup.board().status == "live"
