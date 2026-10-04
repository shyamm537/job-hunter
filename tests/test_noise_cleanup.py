"""Tests for the one-off cleanup of stored jobs (KAN-37, src/storage/noise_cleanup.py)."""

from datetime import datetime

import pytest
from sqlmodel import select

import src.config as config
import src.storage.database as database
from src.config import Filters
from src.storage import noise_cleanup
from src.storage.database import get_session
from src.storage.models import NOT_INTERESTED, TO_APPLY, JobPost
from src.storage.noise_cleanup import (
    EXCLUDED_TITLE,
    LOCATION,
    NO_WANTED_TITLE,
    REMOTE_OUTSIDE_REGIONS,
    cleanup_noise,
    noise_reason,
)

FILTERS = Filters(
    titles=["data scientist", "data analyst"],
    also_match_titles=["data engineer"],
    exclude_titles=["senior", "intern"],
    locations=["Adelaide", "Sydney"],
    board_locations=["Adelaide", "SA", "Sydney"],
    remote_regions=["Australia", "India"],
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv(database.ENV_VAR, raising=False)
    url = f"sqlite:///{tmp_path}/jobs.db"
    database.set_database_url(url)
    database.init_db()
    yield url
    database._database_url = None
    database._engine = None


def _job(job_board_id, title, location="Adelaide", **over):
    values = dict(job_board_id=job_board_id, title=title, company="Acme", location=location,
                  description="d", url=f"http://x/{job_board_id}")
    values.update(over)
    return JobPost(**values)


def _seed(*jobs):
    with get_session() as session:
        session.add_all(jobs)
        session.commit()


def _statuses():
    with get_session() as session:
        return {r.job_board_id: r.status for r in session.exec(select(JobPost)).all()}


# --- the rule ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_board_id, title, location, expected",
    [
        # Adzuna: title only, whatever the location.
        ("adzuna-1", "Data Analyst", "Perth", None),
        ("adzuna-2", "Data Engineer", "Hobart", None),  # via also_match_titles
        ("adzuna-3", "Business Analyst", "Adelaide", NO_WANTED_TITLE),
        ("adzuna-4", "Senior Data Scientist", "Adelaide", EXCLUDED_TITLE),
        ("adzuna-5", "Data Analyst Intern", "Remote - United States", EXCLUDED_TITLE),
        ("adzuna-6", "Data Analyst", "Remote - United States", None),  # location is not checked
        # Boards: title and location, including the remote-region rule.
        ("greenhouse-1", "Data Analyst", "Adelaide", None),
        ("greenhouse-2", "Data Analyst", "Mount Gambier, SA", None),  # board_locations, whole word
        ("greenhouse-3", "Data Analyst", "Remote - Australia", None),
        ("greenhouse-4", "Data Analyst", "Global Remote", None),
        ("greenhouse-5", "Data Analyst", "Remote - United States", REMOTE_OUTSIDE_REGIONS),
        ("greenhouse-6", "Data Analyst", "San Francisco, CA", LOCATION),
        ("greenhouse-7", "Chef", "Adelaide", NO_WANTED_TITLE),
        ("workday-8", "Senior Data Analyst", "Adelaide", EXCLUDED_TITLE),
        ("lever-9", "Data Analyst", "Boston, MA, USA", LOCATION),  # "SA" inside "USA" is not SA
    ],
)
def test_noise_reason(job_board_id, title, location, expected):
    assert noise_reason(_job(job_board_id, title, location), FILTERS) == expected


def test_no_title_filter_means_nothing_fails_on_title():
    f = Filters(exclude_titles=["senior"])
    assert noise_reason(_job("adzuna-1", "Chef"), f) is None
    assert noise_reason(_job("adzuna-2", "Senior Chef"), f) == EXCLUDED_TITLE


# --- which stored rows are examined -----------------------------------------------------


def test_only_untouched_to_apply_jobs_without_a_letter_are_flagged(db):
    _seed(
        _job("adzuna-1", "Chef"),  # noise: untouched
        _job("adzuna-2", "Chef", opened_at=datetime(2026, 10, 1)),  # you opened it
        _job("adzuna-3", "Chef", generated_cover_letter="Dear Acme"),  # a letter already exists
        _job("adzuna-4", "Chef", status="Applied"),
        _job("adzuna-5", "Chef", status="Interviewing"),
        _job("adzuna-6", "Chef", status="Rejected"),
        _job("adzuna-7", "Chef", status=NOT_INTERESTED),  # already dismissed
        _job("manual-8", "Chef"),  # added by hand: never touched
        _job("adzuna-9", "Data Analyst"),  # passes
    )
    with get_session() as session:
        report = cleanup_noise(session, FILTERS, apply=True)

    # Only adzuna-1 and adzuna-9 are examined: every other row is read, has a
    # letter, is not "To Apply", or was added by hand.
    assert (report.checked, report.flagged) == (2, 1)
    statuses = _statuses()
    assert statuses["adzuna-1"] == NOT_INTERESTED
    for untouched in ("adzuna-2", "adzuna-3", "adzuna-9"):
        assert statuses[untouched] == TO_APPLY, untouched
    assert statuses["adzuna-4"] == "Applied"
    assert statuses["adzuna-5"] == "Interviewing"
    assert statuses["adzuna-6"] == "Rejected"
    assert statuses["adzuna-7"] == NOT_INTERESTED
    assert statuses["manual-8"] == TO_APPLY  # never touched, even though "Chef" fails the title rule


# --- dry run, report, idempotence -------------------------------------------------------


def test_dry_run_writes_nothing_and_reports_the_same_counts(db):
    _seed(_job("adzuna-1", "Chef"), _job("greenhouse-2", "Data Analyst", "Remote - United States"),
          _job("adzuna-3", "Data Analyst"))

    with get_session() as session:
        dry = cleanup_noise(session, FILTERS)
    assert _statuses() == {"adzuna-1": TO_APPLY, "greenhouse-2": TO_APPLY, "adzuna-3": TO_APPLY}
    assert (dry.checked, dry.flagged, dry.applied) == (3, 2, False)
    assert "would be marked" in dry.summary()

    with get_session() as session:
        real = cleanup_noise(session, FILTERS, apply=True)
    assert (real.checked, real.flagged, real.applied) == (3, 2, True)
    assert "marked Not interested" in real.summary() and "would" not in real.summary()
    assert _statuses() == {"adzuna-1": NOT_INTERESTED, "greenhouse-2": NOT_INTERESTED, "adzuna-3": TO_APPLY}


def test_rerunning_is_a_no_op(db):
    _seed(_job("adzuna-1", "Chef"), _job("adzuna-2", "Data Analyst"))
    with get_session() as session:
        cleanup_noise(session, FILTERS, apply=True)
    with get_session() as session:
        again = cleanup_noise(session, FILTERS, apply=True)
    assert (again.checked, again.flagged) == (1, 0)  # only adzuna-2 is still a candidate
    assert _statuses() == {"adzuna-1": NOT_INTERESTED, "adzuna-2": TO_APPLY}


def test_report_breaks_the_counts_down_by_reason_and_source_with_examples(db):
    _seed(
        _job("adzuna-1", "Chef"), _job("adzuna-2", "Baker"), _job("adzuna-3", "Cook"),
        _job("adzuna-4", "Senior Data Scientist"),
        _job("greenhouse-5", "Data Analyst", "Remote - United States"),
        _job("lever-6", "Data Analyst", "Paris, France"),
    )
    with get_session() as session:
        report = cleanup_noise(session, FILTERS, examples=2)

    assert report.flagged == 6
    assert dict(report.by_reason) == {
        NO_WANTED_TITLE: 3, EXCLUDED_TITLE: 1, REMOTE_OUTSIDE_REGIONS: 1, LOCATION: 1,
    }
    assert dict(report.by_source) == {"adzuna": 4, "greenhouse": 1, "lever": 1}
    assert len(report.examples[NO_WANTED_TITLE]) == 2  # capped at `examples`
    assert report.examples[REMOTE_OUTSIDE_REGIONS] == ["Acme: Data Analyst (Remote - United States)"]


def test_nothing_to_do_on_an_empty_database(db):
    with get_session() as session:
        report = cleanup_noise(session, FILTERS, apply=True)
    assert (report.checked, report.flagged) == (0, 0)


def test_job_without_a_location_is_handled(db):
    _seed(_job("greenhouse-1", "Data Analyst", ""))
    with get_session() as session:
        report = cleanup_noise(session, FILTERS)
    assert report.flagged == 1 and report.by_reason[LOCATION] == 1
    assert "no location" in report.examples[LOCATION][0]


# --- the command ------------------------------------------------------------------------


def _config(db_url):
    return config.Config.model_validate({
        "filters": {"titles": ["data analyst"], "exclude_titles": ["senior"], "locations": ["Adelaide"]},
        "sources": [{"type": "greenhouse", "board": "acme"}],
        "database": {"url": db_url},
    })


def test_main_is_a_dry_run_unless_apply_is_given(db, monkeypatch, caplog):
    _seed(_job("adzuna-1", "Chef"), _job("adzuna-2", "Data Analyst"))
    monkeypatch.setattr(config, "load_config", lambda *a, **k: _config(db))

    with caplog.at_level("INFO", logger="jobhunter.noise_cleanup"):
        noise_cleanup.main([])
    assert _statuses()["adzuna-1"] == TO_APPLY
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "2 job(s) checked, 1 would be marked Not interested" in text
    assert "dry run" in text
    assert "no wanted title" in text and "by source: adzuna 1" in text

    caplog.clear()
    with caplog.at_level("INFO", logger="jobhunter.noise_cleanup"):
        noise_cleanup.main(["--apply"])
    assert _statuses() == {"adzuna-1": NOT_INTERESTED, "adzuna-2": TO_APPLY}
    assert "dry run" not in " ".join(r.getMessage() for r in caplog.records)


def test_main_exits_cleanly_on_a_config_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise config.ConfigError("config.yaml not found")

    monkeypatch.setattr(config, "load_config", boom)
    with pytest.raises(SystemExit) as exc:
        noise_cleanup.main([])
    assert exc.value.code == 1
    assert "config.yaml not found" in capsys.readouterr().err
