"""Tests for the LLM batching queue: pending_llm_jobs(limit=...) and the
companion count_pending_llm_jobs(), which together let `make process` run in
bounded batches without mistaking a partial pass for an empty queue.
"""

import pytest

import src.storage.database as database
from src.storage.database import (
    count_pending_llm_jobs,
    get_session,
    init_db,
    pending_llm_jobs,
)
from src.storage.models import JobPost


@pytest.fixture
def db(tmp_path):
    database._database_url = None
    database._engine = None
    database.set_database_url(f"sqlite:///{tmp_path}/jobs.db")
    init_db()
    yield
    database._database_url = None
    database._engine = None


def _seed(n: int) -> None:
    with get_session() as session:
        for i in range(n):
            session.add(
                JobPost(
                    job_board_id=f"seek-{i}",
                    title="Data Analyst",
                    company=f"Acme {i}",
                    location="Remote",
                    description="A role.",
                    url=f"https://example.com/{i}",
                )
            )
        session.commit()


def test_no_limit_returns_whole_queue(db):
    _seed(5)
    with get_session() as session:
        assert len(pending_llm_jobs(session)) == 5
        assert len(pending_llm_jobs(session, limit=None)) == 5


def test_limit_caps_returned_rows(db):
    _seed(5)
    with get_session() as session:
        assert len(pending_llm_jobs(session, limit=2)) == 2


def test_limit_larger_than_queue_returns_all(db):
    _seed(3)
    with get_session() as session:
        assert len(pending_llm_jobs(session, limit=10)) == 3


def test_count_ignores_limit_and_excludes_generated(db):
    _seed(4)
    with get_session() as session:
        # Mark one job done; it should drop out of both the queue and the count.
        job = pending_llm_jobs(session, limit=1)[0]
        job.generated_cover_letter = "done"
        session.add(job)
        session.commit()
        assert count_pending_llm_jobs(session) == 3
        # A small batch limit doesn't change the reported total.
        assert len(pending_llm_jobs(session, limit=1)) == 1
        assert count_pending_llm_jobs(session) == 3
