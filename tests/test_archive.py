"""Tests for the dead-postings archive (src/storage/archive.py): which rows
move, that every column survives the move, that an interrupted move can be
re-run, and that a restored posting comes back intact.

Both databases are temp on-disk SQLite files.
"""

from datetime import datetime

import pytest
from sqlmodel import select

import src.storage.archive as archive
import src.storage.database as database
from src.storage.archive import (
    archive_dead_jobs,
    default_archive_url,
    get_archive_session,
    restore_from_archive,
)
from src.storage.database import get_session
from src.storage.models import JobPost

DEAD = datetime(2026, 9, 20, 10, 0, 0)


@pytest.fixture
def dbs(tmp_path):
    database.set_database_url(f"sqlite:///{tmp_path}/jobs.db")
    database.init_db()
    archive.set_archive_url(f"sqlite:///{tmp_path}/dead_jobs.db")
    archive.init_archive_db()
    yield tmp_path
    database._database_url = None
    database._engine = None
    archive._archive_url = None
    archive._archive_engine = None


def _job(n, **overrides):
    values = dict(
        job_board_id=f"greenhouse-{n}", title=f"Data Analyst {n}", company="acme",
        location="Remote", description="A role.", url=f"https://x/{n}",
    )
    values.update(overrides)
    return JobPost(**values)


def _seed(*jobs):
    with get_session() as session:
        session.add_all(jobs)
        session.commit()


def _main_ids():
    with get_session() as session:
        return sorted(r.job_board_id for r in session.exec(select(JobPost)).all())


def _archive_rows():
    with get_archive_session() as session:
        return session.exec(select(JobPost)).all()


def _move():
    with get_session() as session, get_archive_session() as archive_session:
        return archive_dead_jobs(session, archive_session)


def test_only_dead_to_apply_and_rejected_rows_move(dbs):
    _seed(
        _job(1, dead_at=DEAD, dead_reason="http 404"),
        _job(2, dead_at=DEAD, dead_reason="http 404", status="Rejected"),
        _job(3, dead_at=DEAD, dead_reason="http 404", status="Applied"),
        _job(4, dead_at=DEAD, dead_reason="http 404", status="Interviewing"),
        _job(5),  # alive
    )

    assert _move() == 2

    assert _main_ids() == ["greenhouse-3", "greenhouse-4", "greenhouse-5"]
    assert sorted(r.job_board_id for r in _archive_rows()) == ["greenhouse-1", "greenhouse-2"]


def test_every_column_survives_the_move(dbs):
    original = _job(
        1,
        dead_at=DEAD,
        dead_reason="gone from board",
        date_scraped=datetime(2026, 8, 1, 9, 0),
        posted_at=datetime(2026, 7, 30),
        last_seen_at=datetime(2026, 9, 10),
        last_checked_at=datetime(2026, 9, 18),
        generated_cover_letter="Dear Acme",
        generated_cold_email="Hi",
        contact_name="Sam",
        contact_email="sam@acme.test",
        contact_confidence="published",
    )
    _seed(original)
    with get_session() as session:
        expected = session.exec(select(JobPost)).one().model_dump()

    _move()

    (archived,) = _archive_rows()
    got = archived.model_dump()
    expected.pop("id")
    got.pop("id")
    assert got == expected


def test_nothing_to_move_returns_zero(dbs):
    _seed(_job(1))
    assert _move() == 0
    assert _archive_rows() == []


def test_rerun_after_an_interrupted_move_leaves_one_copy(dbs):
    # Simulate a crash after the archive commit but before the main delete:
    # the row is in both files.
    _seed(_job(1, dead_at=DEAD, dead_reason="http 404"))
    with get_archive_session() as archive_session:
        archive_session.add(_job(1, dead_at=DEAD, dead_reason="http 404"))
        archive_session.commit()

    assert _move() == 1

    assert _main_ids() == []
    assert [r.job_board_id for r in _archive_rows()] == ["greenhouse-1"]


def test_existing_archive_row_is_overwritten(dbs):
    with get_archive_session() as archive_session:
        archive_session.add(_job(1, dead_at=DEAD, dead_reason="http 404"))
        archive_session.commit()
    _seed(_job(1, dead_at=DEAD, dead_reason="gone from board", status="Rejected"))

    _move()

    (row,) = _archive_rows()
    assert row.dead_reason == "gone from board"
    assert row.status == "Rejected"


def test_restore_round_trips_the_row(dbs):
    _seed(_job(1, dead_at=DEAD, dead_reason="http 404", generated_cover_letter="Dear Acme",
               status="Rejected", posted_at=datetime(2026, 7, 30)))
    _move()

    with get_session() as session, get_archive_session() as archive_session:
        restored = restore_from_archive(session, archive_session, "greenhouse-1")

    assert restored is not None
    assert restored.generated_cover_letter == "Dear Acme"
    assert restored.status == "Rejected"
    assert restored.posted_at == datetime(2026, 7, 30)
    assert restored.dead_at is None and restored.dead_reason is None
    assert restored.last_seen_at is not None
    assert _main_ids() == ["greenhouse-1"]
    assert _archive_rows() == []


def test_restore_of_an_unknown_posting_returns_none(dbs):
    with get_session() as session, get_archive_session() as archive_session:
        assert restore_from_archive(session, archive_session, "greenhouse-404") is None


def test_upsert_without_an_archive_session_inserts_fresh(dbs):
    _seed(_job(1, dead_at=DEAD, dead_reason="http 404", generated_cover_letter="Dear Acme"))
    _move()

    with get_session() as session:
        row, created = database.upsert_job(session, _job(1))

    assert created is True
    assert row.generated_cover_letter is None


def test_upsert_with_an_archive_session_restores(dbs):
    _seed(_job(1, dead_at=DEAD, dead_reason="http 404", generated_cover_letter="Dear Acme"))
    _move()

    with get_session() as session, get_archive_session() as archive_session:
        row, created = database.upsert_job(session, _job(1), archive_session)

    assert created is False
    assert row.generated_cover_letter == "Dear Acme"


@pytest.mark.parametrize(
    "main_url, expected",
    [
        ("sqlite:///data/jobs.db", "sqlite:///data/dead_jobs.db"),
        ("sqlite:///C:/tmp/x/jobs.db", "sqlite:///C:/tmp/x/dead_jobs.db"),
        ("sqlite://", "sqlite://"),
        ("sqlite:///:memory:", "sqlite://"),
    ],
)
def test_default_archive_url_sits_next_to_the_main_database(main_url, expected):
    assert default_archive_url(main_url) == expected


def test_default_archive_url_needs_sqlite():
    with pytest.raises(ValueError):
        default_archive_url("postgresql://u@h/db")
