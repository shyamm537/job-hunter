"""Tests for the scrape CLI loop (src/ingestion/cli.py).

Covers the behaviour with real regression risk: running every planned scrape,
applying the post-filter to ATS results, skipping a failing scrape without
aborting, deduping on a second run, and exiting cleanly on a config error.
The planner and config are mocked — no network, no real config.yaml.
"""

import pytest
from sqlmodel import select

import src.config as config
import src.storage.database as database
from src.ingestion import cli
from src.ingestion.planner import PlannedScrape
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
    def __init__(self, specs=None, error=None):
        self._specs = specs or []
        self._error = error

    def scrape(self):
        if self._error is not None:
            raise self._error
        return [JobPost(**spec) for spec in self._specs]


def _config(db_path, titles=None):
    return config.Config.model_validate(
        {
            "filters": {"titles": titles or []},
            "sources": [{"type": "greenhouse", "board": "acme"}],
            "database": {"url": f"sqlite:///{db_path}"},
        }
    )


def _rows(db_path):
    database.set_database_url(f"sqlite:///{db_path}")
    with database.get_session() as session:
        return session.exec(select(JobPost)).all()


def test_applies_post_filter_and_skips_failures(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _config(db, titles=["analyst"]))

    good = FakeScraper(
        specs=[
            dict(job_board_id="gh-1", title="Data Analyst", company="acme",
                 location="Remote", description="d", url="http://x/1"),
            dict(job_board_id="gh-2", title="Recruiter", company="acme",
                 location="Remote", description="d", url="http://x/2"),
        ]
    )
    bad = FakeScraper(error=RuntimeError("boom"))
    plans = [
        PlannedScrape(good, "greenhouse[acme]", post_filter=True),
        PlannedScrape(bad, "greenhouse[dead]", post_filter=True),
    ]
    monkeypatch.setattr(cli, "plan_scrapes", lambda sources, filters, **k: plans)

    cli.main()  # must not raise despite the failing scraper

    rows = _rows(db)
    # "Recruiter" filtered out by the title filter; "Data Analyst" kept.
    assert [r.job_board_id for r in rows] == ["gh-1"]


def test_no_filter_keeps_all(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _config(db, titles=[]))
    good = FakeScraper(
        specs=[
            dict(job_board_id="gh-1", title="Data Analyst", company="acme",
                 location="Remote", description="d", url="http://x/1"),
            dict(job_board_id="gh-2", title="Recruiter", company="acme",
                 location="Remote", description="d", url="http://x/2"),
        ]
    )
    monkeypatch.setattr(
        cli, "plan_scrapes",
        lambda s, f, **k: [PlannedScrape(good, "greenhouse[acme]", post_filter=True)],
    )
    cli.main()
    assert len(_rows(db)) == 2


def test_dedups_on_second_run(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _config(db))
    spec = dict(job_board_id="gh-1", title="Data Analyst", company="acme",
                location="Remote", description="d", url="http://x/1")
    monkeypatch.setattr(
        cli, "plan_scrapes",
        lambda s, f, **k: [PlannedScrape(FakeScraper(specs=[spec]), "greenhouse[acme]", post_filter=False)],
    )
    cli.main()
    cli.main()
    assert len(_rows(db)) == 1


def test_exits_on_config_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise config.ConfigError("bad config.yaml")

    monkeypatch.setattr(cli, "load_config", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "bad config.yaml" in capsys.readouterr().err


def test_dump_dir_writes_unfiltered_output(tmp_path, monkeypatch):
    import os

    from src.ingestion import capture
    from src.ingestion.planner import PlannedScrape

    db = tmp_path / "jobs.db"
    dump = tmp_path / "dump"
    monkeypatch.setenv(capture.ENV_VAR, str(dump))
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _config(db, titles=["analyst"]))

    scraper = FakeScraper(
        specs=[
            dict(job_board_id="gh-1", title="Data Analyst", company="acme",
                 location="Remote", description="d", url="http://x/1"),
            dict(job_board_id="gh-2", title="Recruiter", company="acme",
                 location="Remote", description="d", url="http://x/2"),
        ]
    )
    monkeypatch.setattr(
        cli, "plan_scrapes",
        lambda s, f, **k: [PlannedScrape(scraper, "greenhouse[acme]", post_filter=True)],
    )

    cli.main()

    files = os.listdir(dump)
    assert len(files) == 1
    import json
    payload = json.loads((dump / files[0]).read_text())
    # the dump is UNFILTERED: both jobs, including the one the filter drops
    assert payload["count"] == 2
    assert {j["title"] for j in payload["jobs"]} == {"Data Analyst", "Recruiter"}
