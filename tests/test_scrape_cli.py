"""Tests for the multi-source scrape loop in src/ingestion/cli.py.

Covers the behaviour that actually carries regression risk: running every
configured source, skipping one that throws without aborting the run,
deduping on a second run, and exiting cleanly on a config error. Scrapers
and config are mocked — no network, no real config.yaml.
"""

import pytest
from sqlmodel import select

import src.config as config
import src.storage.database as database
from src.ingestion import cli
from src.storage.models import JobPost


@pytest.fixture(autouse=True)
def _reset_db_state(monkeypatch):
    monkeypatch.delenv(database.ENV_VAR, raising=False)
    database._database_url = None
    database._engine = None
    yield
    database._database_url = None
    database._engine = None


class FakeScraper:
    """Returns fresh JobPost objects each call (so re-runs are safe), or
    raises a preset error to simulate a dead source."""

    def __init__(self, source_name, specs=None, error=None):
        self.source_name = source_name
        self._specs = specs or []
        self._error = error

    def scrape(self):
        if self._error is not None:
            raise self._error
        return [JobPost(**spec) for spec in self._specs]


def _two_source_config(db_path):
    return config.Config.model_validate(
        {
            "sources": [
                {"type": "seek", "title": "data analyst", "location": "Adelaide"},
                {"type": "greenhouse", "board": "acme"},
            ],
            "database": {"url": f"sqlite:///{db_path}"},
        }
    )


def _rows(db_path):
    database.set_database_url(f"sqlite:///{db_path}")
    with database.get_session() as session:
        return session.exec(select(JobPost)).all()


def test_runs_all_sources_and_skips_a_failing_one(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _two_source_config(db))

    good = FakeScraper(
        "greenhouse",
        specs=[
            dict(job_board_id="greenhouse-1", title="DA", company="acme",
                 location="Remote", description="d", url="http://x/1"),
        ],
    )
    bad = FakeScraper("seek", error=RuntimeError("feed down"))
    monkeypatch.setattr(
        cli, "build_scraper", lambda source: bad if source.type == "seek" else good
    )

    cli.main()  # must not raise even though the seek source throws

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0].job_board_id == "greenhouse-1"


def test_dedups_on_second_run(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _two_source_config(db))

    gh = FakeScraper(
        "greenhouse",
        specs=[
            dict(job_board_id="greenhouse-1", title="DA", company="acme",
                 location="Remote", description="d", url="http://x/1"),
        ],
    )
    empty = FakeScraper("seek", specs=[])
    monkeypatch.setattr(
        cli, "build_scraper", lambda source: empty if source.type == "seek" else gh
    )

    cli.main()
    cli.main()  # same posting again

    assert len(_rows(db)) == 1  # not duplicated


def test_exits_cleanly_on_config_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise config.ConfigError("bad config.yaml")

    monkeypatch.setattr(cli, "load_config", boom)

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "bad config.yaml" in capsys.readouterr().err
