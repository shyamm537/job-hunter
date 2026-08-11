"""Tests for the one-time Adzuna dedup-key migration in src/storage/database.py.

Legacy `adzuna-` rows were keyed by a hash of the redirect URL's path; the
scraper now keys by Adzuna's own `id`. _migrate_adzuna_dedup_keys() re-keys the
old rows in place (preserving status/generated materials) so the next scrape
doesn't re-insert duplicates. It runs automatically from init_db().
"""

import pytest

import src.storage.database as database
from src.ingestion.adzuna import id_dedup_key, url_dedup_key
from src.storage.models import JobPost


@pytest.fixture(autouse=True)
def _fresh_sqlite(tmp_path):
    # Each test gets its own on-disk SQLite db and a clean engine cache.
    database._database_url = None
    database._engine = None
    database.set_database_url(f"sqlite:///{tmp_path / 'jobs.db'}")
    yield
    database._database_url = None
    database._engine = None


def _insert_raw(job: JobPost) -> int:
    """Insert a row exactly as given (no dedup) and return its PK."""
    with database.get_session() as session:
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.id


def test_legacy_row_is_rekeyed_in_place():
    database.init_db()
    url = "https://www.adzuna.in/land/ad/123456?se=AAA&utm_source=api"
    legacy_id = url_dedup_key(url)
    pk = _insert_raw(
        JobPost(
            job_board_id=legacy_id,
            title="Data Analyst",
            company="Acme",
            location="Mumbai",
            description="snippet",
            url=url,
            status="Applied",
            generated_cover_letter="dear hiring manager",
        )
    )

    database._migrate_adzuna_dedup_keys(database.get_engine())

    with database.get_session() as session:
        row = session.get(JobPost, pk)
    # Same row (PK preserved), new key, status + materials intact.
    assert row.id == pk
    assert row.job_board_id == id_dedup_key("123456")
    assert row.status == "Applied"
    assert row.generated_cover_letter == "dear hiring manager"


def test_migration_is_idempotent_and_leaves_id_keyed_rows_alone():
    database.init_db()
    url = "https://www.adzuna.in/land/ad/123456?se=AAA"
    _insert_raw(
        JobPost(
            job_board_id=url_dedup_key(url),
            title="X", company="Acme", location="Mumbai",
            description="s", url=url,
        )
    )
    engine = database.get_engine()
    database._migrate_adzuna_dedup_keys(engine)
    database._migrate_adzuna_dedup_keys(engine)  # second pass must be a no-op

    with database.get_session() as session:
        rows = session.exec(database.select(JobPost)).all()
    assert len(rows) == 1
    assert rows[0].job_board_id == id_dedup_key("123456")


def test_migration_collapses_legacy_and_new_duplicate():
    # A legacy URL-keyed row AND an already-id-keyed row for the SAME ad:
    # migrating the legacy one would collide on the unique key, so it's dropped
    # and the id-keyed row survives.
    database.init_db()
    url = "https://www.adzuna.in/land/ad/123456?se=AAA"
    _insert_raw(
        JobPost(
            job_board_id=id_dedup_key("123456"),
            title="new", company="Acme", location="Mumbai",
            description="s", url=url, status="Interviewing",
        )
    )
    _insert_raw(
        JobPost(
            job_board_id=url_dedup_key(url),
            title="legacy", company="Acme", location="Mumbai",
            description="s", url=url,
        )
    )

    database._migrate_adzuna_dedup_keys(database.get_engine())

    with database.get_session() as session:
        rows = session.exec(database.select(JobPost)).all()
    assert len(rows) == 1
    assert rows[0].job_board_id == id_dedup_key("123456")
    assert rows[0].status == "Interviewing"  # the id-keyed row is the survivor


def test_init_db_runs_the_migration():
    # End-to-end: init_db() should re-key a legacy row with no explicit call.
    database.init_db()
    url = "https://www.adzuna.au/jobs/land/ad/777?se=Z"
    pk = _insert_raw(
        JobPost(
            job_board_id=url_dedup_key(url),
            title="Y", company="Acme", location="Sydney",
            description="s", url=url,
        )
    )
    database.init_db()  # safe to call repeatedly; triggers the migration

    with database.get_session() as session:
        row = session.get(JobPost, pk)
    assert row.job_board_id == id_dedup_key("777")
