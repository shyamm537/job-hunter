"""Tests for adding jobs by hand (KAN-3): the storage helper and how
hand-added jobs sit in the queues. The dashboard's "Add a job" form is tested
in tests/test_web.py.
"""

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import src.storage.database as database
from src.storage.database import (
    MANUAL_SOURCE,
    add_manual_job,
    get_session,
    link_check_queue,
    pending_llm_jobs,
    present_sources,
)
from src.storage.models import JobPost

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv(database.ENV_VAR, raising=False)
    url = f"sqlite:///{tmp_path}/jobs.db"
    database.set_database_url(url)
    database.init_db()
    yield url
    database._database_url = None
    database._engine = None


def _add(**overrides):
    values = dict(title="Data Analyst", company="Acme", description="Analyse things.")
    values.update(overrides)
    with get_session() as session:
        return add_manual_job(session, **values)


def _rows():
    with get_session() as session:
        return session.exec(select(JobPost)).all()


# --- add_manual_job ----------------------------------------------------------


def test_adds_a_trimmed_manual_job(db):
    job, created = _add(title="  Data Analyst ", location=" Adelaide ",
                        url=" https://www.linkedin.com/jobs/view/42 ")

    assert created is True
    assert job.job_board_id.startswith(f"{MANUAL_SOURCE}-")
    assert (job.title, job.location, job.url) == (
        "Data Analyst", "Adelaide", "https://www.linkedin.com/jobs/view/42")
    assert job.status == "To Apply"
    assert job.generated_cover_letter is None  # waiting for `make process`


def test_same_url_is_not_added_twice(db):
    first, _ = _add(url="https://www.linkedin.com/jobs/view/42")
    again, created = _add(title="Renamed", url="https://www.linkedin.com/jobs/view/42")
    assert created is False and again.id == first.id
    assert len(_rows()) == 1


def test_same_job_without_a_url_is_not_added_twice(db):
    _add()
    _, created = _add(title="data analyst ", company=" ACME")
    assert created is False
    assert len(_rows()) == 1


def test_different_jobs_without_urls_are_both_kept(db):
    _add()
    _, created = _add(title="Data Scientist")
    assert created is True
    assert len(_rows()) == 2


@pytest.mark.parametrize("field", ["title", "company", "description"])
def test_required_fields(db, field):
    with pytest.raises(ValueError, match=field):
        _add(**{field: "   "})
    assert _rows() == []


def test_manual_shows_up_as_a_source(db):
    _add()
    with get_session() as session:
        assert present_sources(session) == [MANUAL_SOURCE]


# --- queues --------------------------------------------------------------------


def _scraped(n):
    return JobPost(job_board_id=f"adzuna-{n}", title=f"Scraped {n}", company="Co",
                   location="Sydney", description="d", url=f"https://x/{n}")


def test_process_queue_does_hand_added_jobs_first(db):
    with get_session() as session:
        session.add_all([_scraped(1), _scraped(2)])
        session.commit()
    _add(title="Mine, first")
    _add(title="Mine, second")

    with get_session() as session:
        titles = [j.title for j in pending_llm_jobs(session)]
        (next_up,) = pending_llm_jobs(session, limit=1)

    assert titles == ["Mine, first", "Mine, second", "Scraped 1", "Scraped 2"]
    assert next_up.title == "Mine, first"


def test_link_checker_never_queues_hand_added_jobs(db):
    _add(url="https://www.linkedin.com/jobs/view/42")
    long_ago = datetime(2026, 1, 1)
    with get_session() as session:
        session.add(_scraped(1))
        session.commit()
        for job in session.exec(select(JobPost)).all():
            job.last_seen_at = long_ago  # make both overdue
            session.add(job)
        session.commit()
        queue = link_check_queue(session, now=long_ago + timedelta(days=60))

    assert [item.title for item in queue] == ["Scraped 1"]
