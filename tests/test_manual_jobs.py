"""Tests for adding jobs by hand (KAN-3): the storage helper, how hand-added
jobs sit in the queues, and the dashboard's "Add a job" form.

The form test drives the real Streamlit script headlessly (AppTest) against a
temp database, so it never touches data/.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import select

import src.config as config
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

APP = str(Path(__file__).resolve().parents[1] / "src" / "app" / "main.py")


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


# --- the dashboard form ----------------------------------------------------------


@pytest.fixture
def app(db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    cfg = config.Config.model_validate(
        {"sources": [{"type": "greenhouse", "board": "acme"}], "database": {"url": db}}
    )
    monkeypatch.setattr(config, "load_config", lambda *a, **k: cfg)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    return at


def _fill(at, **values):
    labels = {"title": "Title *", "company": "Company *", "location": "Location",
              "url": "Job URL"}
    for field, label in labels.items():
        if field in values:
            next(w for w in at.text_input if w.label == label).input(values[field])
    if "description" in values:
        next(w for w in at.text_area if w.label == "Job description *").input(values["description"])
    next(b for b in at.button if b.label == "Add job").click()
    at.run()
    assert not at.exception


def test_form_adds_a_job(app):
    _fill(app, title="ML Engineer", company="Canva", location="Sydney",
          url="https://www.linkedin.com/jobs/view/7", description="Build models.")

    (row,) = _rows()
    assert (row.title, row.company, row.location) == ("ML Engineer", "Canva", "Sydney")
    assert row.job_board_id.startswith("manual-")
    assert any("Added" in s.value for s in app.success)
    # The new job is in the list straight away.
    assert "ML Engineer" in app.dataframe[0].value["Title"].tolist()


def test_form_reports_missing_fields_and_adds_nothing(app):
    _fill(app, title="ML Engineer", company="Canva")  # no description

    assert _rows() == []
    assert any("description" in e.value for e in app.error)


def test_form_says_when_a_job_is_already_there(app):
    _add(title="ML Engineer", company="Canva", description="Build models.",
         url="https://www.linkedin.com/jobs/view/7")

    _fill(app, title="ML Engineer", company="Canva",
          url="https://www.linkedin.com/jobs/view/7", description="Build models.")

    assert len(_rows()) == 1
    assert any("already in your list" in i.value for i in app.info)
