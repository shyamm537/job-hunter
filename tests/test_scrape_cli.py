"""Tests for the scrape CLI loop (src/ingestion/cli.py).

Covers the behaviour with real regression risk: running every planned scrape,
applying the post-filter to ATS results, skipping a failing scrape without
aborting, deduping on a second run, exiting cleanly on a config error, and
reconciling a board's rows against its full list (KAN-25): gone -> dead and
archived, still listed -> seen, archived and back -> restored.
The planner and config are mocked — no network, no real config.yaml. The
archive is the dead_jobs.db the CLI puts next to the temp database.
"""

import pytest
from sqlmodel import select

import src.config as config
import src.storage.archive as archive
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


def test_exits_when_no_sources_are_active(tmp_path, monkeypatch, capsys):
    # A sources file with every line commented out used to run zero scrapes
    # and report "Done". It should stop and say why instead.
    sources = tmp_path / "sources.txt"
    sources.write_text("# seek\n# greenhouse acme\n", encoding="utf-8")
    cfg = config.Config.model_validate({"sources_file": str(sources)})
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "No sources configured" in capsys.readouterr().err


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


# --- board reconcile and the archive (KAN-25) ---------------------------------


def _gh(n, title="Data Analyst", company="acme"):
    """Spec for a Greenhouse posting on board `company`."""
    return dict(
        job_board_id=f"greenhouse-{company}-{n}", title=title, company=company,
        location="Remote", description="d", url=f"http://x/{company}/{n}",
    )


def _seed(db_path, *jobs):
    """Store JobPosts in the main database before a run."""
    database.set_database_url(f"sqlite:///{db_path}")
    database.init_db()
    with database.get_session() as session:
        session.add_all(jobs)
        session.commit()


def _archived(db_path):
    path = db_path.parent / archive.ARCHIVE_FILENAME
    if not path.exists():
        return []
    archive.set_archive_url(f"sqlite:///{path}")
    with archive.get_archive_session() as session:
        return session.exec(select(JobPost)).all()


def _run_board(monkeypatch, db_path, scraper, titles=None, board=("greenhouse", "acme")):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _config(db_path, titles=titles))
    plan = PlannedScrape(scraper, f"greenhouse[{board[1]}]", post_filter=True, board=board)
    monkeypatch.setattr(cli, "plan_scrapes", lambda s, f, **k: [plan])
    cli.main()


def test_job_gone_from_board_is_marked_dead_and_archived(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    _seed(db, JobPost(**_gh(1)), JobPost(**_gh(2)))

    _run_board(monkeypatch, db, FakeScraper(specs=[_gh(1)]))

    assert [r.job_board_id for r in _rows(db)] == ["greenhouse-acme-1"]
    (gone,) = _archived(db)
    assert gone.job_board_id == "greenhouse-acme-2"
    assert gone.dead_reason == "gone from board"
    assert gone.dead_at is not None


def test_applied_job_gone_from_board_stays_in_main_marked_dead(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    _seed(db, JobPost(**_gh(1)), JobPost(**_gh(2), status="Applied"))

    _run_board(monkeypatch, db, FakeScraper(specs=[_gh(1)]))

    rows = {r.job_board_id: r for r in _rows(db)}
    assert rows["greenhouse-acme-2"].status == "Applied"
    assert rows["greenhouse-acme-2"].dead_reason == "gone from board"
    assert _archived(db) == []


def test_job_still_on_board_but_filtered_out_is_marked_seen(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    _seed(db, JobPost(**_gh(1, title="Recruiter")))

    _run_board(monkeypatch, db, FakeScraper(specs=[_gh(1, title="Recruiter")]), titles=["analyst"])

    (row,) = _rows(db)
    assert row.last_seen_at is not None
    assert row.dead_at is None


def test_dead_job_still_on_board_is_revived(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    from datetime import datetime

    _seed(db, JobPost(**_gh(1), status="Applied", dead_at=datetime(2026, 9, 1), dead_reason="http 404"))

    _run_board(monkeypatch, db, FakeScraper(specs=[_gh(1)]), titles=["nothing matches"])

    (row,) = _rows(db)
    assert row.dead_at is None and row.dead_reason is None


def test_archived_job_that_reappears_is_restored(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    _seed(db, JobPost(**_gh(1)), JobPost(**_gh(2), generated_cover_letter="Dear Acme"))
    _run_board(monkeypatch, db, FakeScraper(specs=[_gh(1)]))  # gh 2 leaves -> archived
    assert [r.job_board_id for r in _archived(db)] == ["greenhouse-acme-2"]

    _run_board(monkeypatch, db, FakeScraper(specs=[_gh(1), _gh(2)]))  # and comes back

    rows = {r.job_board_id: r for r in _rows(db)}
    back = rows["greenhouse-acme-2"]
    assert back.generated_cover_letter == "Dear Acme"
    assert back.dead_at is None and back.dead_reason is None
    assert _archived(db) == []


def test_restored_job_is_not_counted_as_new(tmp_path, monkeypatch, caplog):
    import logging

    db = tmp_path / "jobs.db"
    _seed(db, JobPost(**_gh(1)), JobPost(**_gh(2)))
    _run_board(monkeypatch, db, FakeScraper(specs=[_gh(1)]))
    caplog.set_level(logging.INFO, logger="jobhunter.scrape")

    _run_board(monkeypatch, db, FakeScraper(specs=[_gh(1), _gh(2)]))

    assert "2 posting(s), 0 new" in caplog.text


def test_failed_board_scrape_changes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    _seed(db, JobPost(**_gh(1)))

    _run_board(monkeypatch, db, FakeScraper(error=RuntimeError("503")))

    (row,) = _rows(db)
    assert row.dead_at is None and row.last_seen_at is None
    assert _archived(db) == []


def test_empty_board_scrape_changes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    _seed(db, JobPost(**_gh(1)))

    _run_board(monkeypatch, db, FakeScraper(specs=[]))

    (row,) = _rows(db)
    assert row.dead_at is None and row.last_seen_at is None
    assert _archived(db) == []


def test_other_boards_and_adzuna_rows_are_untouched(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    other = JobPost(**_gh(9, company="other"))
    lever_same_token = JobPost(**{**_gh(8), "job_board_id": "lever-acme-8"})
    adzuna = JobPost(job_board_id="adzuna-1", title="Data Analyst", company="acme",
                     location="Adelaide", description="d", url="http://a/1")
    _seed(db, JobPost(**_gh(1)), other, lever_same_token, adzuna)

    _run_board(monkeypatch, db, FakeScraper(specs=[_gh(1)]))

    rows = {r.job_board_id: r for r in _rows(db)}
    for job_board_id in ("greenhouse-other-9", "lever-acme-8", "adzuna-1"):
        assert rows[job_board_id].dead_at is None, job_board_id
        assert rows[job_board_id].last_seen_at is None, job_board_id
