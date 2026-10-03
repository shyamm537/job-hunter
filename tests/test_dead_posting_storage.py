"""Tests for the storage half of dead-posting handling in
src/storage/database.py: the additive column migration for an older database,
the mark_seen / mark_checked / mark_dead bulk helpers, the link-check queue,
and what upsert_job now does when a scrape sees a posting again.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import select

import src.storage.database as database
from src.storage.database import (
    GONE_FROM_BOARD,
    LinkCheckItem,
    count_link_check_queue,
    get_session,
    init_db,
    link_check_queue,
    mark_checked,
    mark_dead,
    mark_seen,
    reconcile_board,
    upsert_job,
    utcnow,
)
from src.storage.models import JobPost

NEW_COLUMNS = {
    "contact_name",
    "contact_email",
    "contact_confidence",
    "dead_at",
    "dead_reason",
    "posted_at",
    "last_seen_at",
    "last_checked_at",
}

# A fixed "now" so the queue tests don't depend on the wall clock.
NOW = datetime(2026, 10, 2, 12, 0, 0)
T1 = datetime(2026, 9, 1, 8, 30, 0)
T2 = datetime(2026, 9, 15, 9, 45, 0)


@pytest.fixture
def db_url(tmp_path):
    """Point the storage layer at a fresh on-disk SQLite file, tables NOT created."""
    database._database_url = None
    database._engine = None
    database.set_database_url(f"sqlite:///{tmp_path}/jobs.db")
    yield
    database._database_url = None
    database._engine = None


@pytest.fixture
def db(db_url):
    init_db()


def _job(i: int, **overrides) -> JobPost:
    fields = dict(
        job_board_id=f"lever-{i}",
        title=f"Data Analyst {i}",
        company=f"Acme {i}",
        location="Remote",
        description="A role.",
        url=f"https://example.com/{i}",
    )
    fields.update(overrides)
    return JobPost(**fields)


def _add(*jobs: JobPost) -> list:
    """Insert rows exactly as given (no upsert) and return their ids in order."""
    with get_session() as session:
        for job in jobs:
            session.add(job)
        session.commit()
        return [job.id for job in jobs]


def _get(job_id: int) -> JobPost:
    """Read one row in a brand-new session, so only committed data is seen."""
    with get_session() as session:
        return session.get(JobPost, job_id)


# --- migration ---------------------------------------------------------------


def test_utcnow_is_naive():
    assert utcnow().tzinfo is None


def test_old_database_gains_the_new_columns(db_url):
    # A database from before any of the later columns existed: only the
    # original eleven. create_all() won't touch an existing table, so without
    # the _ADDED_COLUMNS migration every ORM read fails with
    # "no such column: dead_at".
    engine = database.get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE jobpost (
                    id INTEGER NOT NULL PRIMARY KEY,
                    job_board_id VARCHAR NOT NULL UNIQUE,
                    title VARCHAR NOT NULL,
                    company VARCHAR NOT NULL,
                    location VARCHAR NOT NULL,
                    description VARCHAR NOT NULL,
                    url VARCHAR NOT NULL,
                    date_scraped DATETIME NOT NULL,
                    status VARCHAR NOT NULL,
                    generated_cover_letter VARCHAR,
                    generated_cold_email VARCHAR
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO jobpost (id, job_board_id, title, company, location,
                    description, url, date_scraped, status,
                    generated_cover_letter, generated_cold_email)
                VALUES (1, 'lever-old', 'Data Analyst', 'Acme', 'Remote',
                    'An old role.', 'https://example.com/old',
                    '2026-01-05 09:00:00.000000', 'Applied', 'dear team', NULL)
                """
            )
        )
        before = {row[1] for row in conn.execute(text("PRAGMA table_info(jobpost)"))}
    assert not (NEW_COLUMNS & before)

    init_db()
    init_db()  # a second run must not try to add the columns again

    with engine.connect() as conn:
        after = {row[1] for row in conn.execute(text("PRAGMA table_info(jobpost)"))}
    assert NEW_COLUMNS <= after

    # The old row reads through the ORM, with its data intact and NULLs in
    # the new columns...
    with get_session() as session:
        row = session.exec(select(JobPost)).one()
        assert row.job_board_id == "lever-old"
        assert row.status == "Applied"
        assert row.generated_cover_letter == "dear team"
        assert row.date_scraped == datetime(2026, 1, 5, 9, 0, 0)
        assert row.dead_at is None
        assert row.dead_reason is None
        assert row.posted_at is None
        assert row.last_seen_at is None
        assert row.last_checked_at is None

        # ...and can be updated in them.
        row.last_checked_at = T1
        row.dead_at = T2
        row.dead_reason = "http 404"
        row.posted_at = T1
        row.last_seen_at = T1
        session.add(row)
        session.commit()

    row = _get(1)
    assert row.last_checked_at == T1
    assert row.dead_at == T2
    assert row.dead_reason == "http 404"
    assert row.posted_at == T1
    assert row.last_seen_at == T1


# --- mark_* helpers ----------------------------------------------------------


def test_mark_seen_sets_last_seen_at_only_on_the_given_rows(db):
    a, b, c = _add(_job(1), _job(2), _job(3))
    with get_session() as session:
        assert mark_seen(session, [a, b], when=T1) == 2
        session.commit()

    assert _get(a).last_seen_at == T1
    assert _get(b).last_seen_at == T1
    assert _get(c).last_seen_at is None
    assert _get(a).last_checked_at is None
    assert _get(a).dead_at is None


def test_mark_checked_sets_last_checked_at_only_on_the_given_rows(db):
    a, b, c = _add(_job(1), _job(2), _job(3))
    with get_session() as session:
        assert mark_checked(session, [a, c], when=T1) == 2
        session.commit()

    assert _get(a).last_checked_at == T1
    assert _get(c).last_checked_at == T1
    assert _get(b).last_checked_at is None
    assert _get(a).last_seen_at is None
    assert _get(a).dead_at is None


def test_mark_dead_sets_dead_at_and_reason_but_not_last_checked_at(db):
    a, b = _add(_job(1), _job(2))
    with get_session() as session:
        assert mark_dead(session, [a], "http 404", when=T1) == 1
        session.commit()

    assert _get(a).dead_at == T1
    assert _get(a).dead_reason == "http 404"
    assert _get(a).last_checked_at is None
    assert _get(b).dead_at is None
    assert _get(b).dead_reason is None


def test_mark_dead_keeps_the_first_death_date_and_reason(db):
    a, b = _add(_job(1, dead_at=T1, dead_reason="http 404"), _job(2))
    with get_session() as session:
        # Only the live row counts as changed.
        assert mark_dead(session, [a, b], "closed page", when=T2) == 1
        session.commit()

    assert _get(a).dead_at == T1
    assert _get(a).dead_reason == "http 404"
    assert _get(b).dead_at == T2
    assert _get(b).dead_reason == "closed page"


def test_mark_helpers_default_when_to_now(db):
    (a,) = _add(_job(1))
    before = utcnow()
    with get_session() as session:
        mark_seen(session, [a])
        mark_checked(session, [a])
        mark_dead(session, [a], "http 410")
        session.commit()
    after = utcnow()

    row = _get(a)
    for value in (row.last_seen_at, row.last_checked_at, row.dead_at):
        assert value.tzinfo is None
        assert before <= value <= after


def test_mark_helpers_ignore_unknown_ids(db):
    (a,) = _add(_job(1))
    with get_session() as session:
        assert mark_seen(session, [a, 9999], when=T1) == 1
        assert mark_checked(session, [9999], when=T1) == 0
        assert mark_dead(session, [9999], "http 404", when=T1) == 0


@pytest.mark.parametrize(
    "mark",
    [
        lambda session, ids: mark_seen(session, ids, when=T1),
        lambda session, ids: mark_checked(session, ids, when=T1),
        lambda session, ids: mark_dead(session, ids, "http 404", when=T1),
    ],
    ids=["mark_seen", "mark_checked", "mark_dead"],
)
def test_mark_helpers_do_not_commit(db, mark):
    (a,) = _add(_job(1))

    def committed_state():
        row = _get(a)  # a second session: sees committed data only
        return (row.last_seen_at, row.last_checked_at, row.dead_at, row.dead_reason)

    with get_session() as session:
        assert mark(session, [a]) == 1
        assert committed_state() == (None, None, None, None)
        session.commit()
    assert committed_state() != (None, None, None, None)

    # And without a commit, the change is gone once the session closes.
    (b,) = _add(_job(2))
    with get_session() as session:
        assert mark(session, [b]) == 1
    row = _get(b)
    assert (row.last_seen_at, row.last_checked_at, row.dead_at) == (None, None, None)


@pytest.mark.parametrize(
    "mark",
    [
        lambda session, ids: mark_seen(session, ids),
        lambda session, ids: mark_checked(session, ids),
        lambda session, ids: mark_dead(session, ids, "http 404"),
    ],
    ids=["mark_seen", "mark_checked", "mark_dead"],
)
def test_mark_helpers_with_no_ids_run_no_query(db, mark):
    class NoQuerySession:
        """Stands in for a Session; any attribute access means a query was tried."""

        def __getattr__(self, name):
            raise AssertionError(f"session.{name} used for an empty id list")

    assert mark(NoQuerySession(), []) == 0
    assert mark(NoQuerySession(), ()) == 0


def test_mark_helpers_chunk_a_long_id_list(db):
    # More ids than fit in one statement, with a remainder: 2 full chunks + 7.
    n = database._ID_CHUNK_SIZE * 2 + 7
    ids = _add(*[_job(i) for i in range(n)])
    assert len(ids) == n

    with get_session() as session:
        assert mark_seen(session, ids, when=T1) == n
        assert mark_checked(session, ids, when=T2) == n
        assert mark_dead(session, ids, "http 404", when=T2) == n
        session.commit()

    with get_session() as session:
        rows = session.exec(
            select(JobPost.last_seen_at, JobPost.last_checked_at, JobPost.dead_reason)
        ).all()
    assert len(rows) == n
    assert set(rows) == {(T1, T2, "http 404")}


def test_chunking_issues_one_statement_per_chunk(db, monkeypatch):
    # With the chunk size forced down, every id must still be reached.
    monkeypatch.setattr(database, "_ID_CHUNK_SIZE", 3)
    ids = _add(*[_job(i) for i in range(8)])

    with get_session() as session:
        calls = []
        real_exec = session.exec
        monkeypatch.setattr(
            session, "exec", lambda stmt: calls.append(stmt) or real_exec(stmt)
        )
        assert mark_checked(session, ids, when=T1) == 8
        assert len(calls) == 3  # 3 + 3 + 2
        session.commit()

    assert all(_get(i).last_checked_at == T1 for i in ids)


# --- link_check_queue --------------------------------------------------------


def test_link_check_queue_returns_light_rows(db):
    (a,) = _add(_job(1))
    with get_session() as session:
        queue = link_check_queue(session, now=NOW)

    assert queue == [
        LinkCheckItem(
            id=a, title="Data Analyst 1", company="Acme 1", url="https://example.com/1"
        )
    ]
    assert isinstance(queue[0], LinkCheckItem)
    assert queue[0]._fields == ("id", "title", "company", "url")


def test_link_check_queue_excludes_rows_that_do_not_need_a_check(db):
    recent = NOW - timedelta(days=6, hours=23)
    stale = NOW - timedelta(days=7, seconds=1)

    (
        never,
        dead,
        no_url,
        blank_url,
        checked_recently,
        seen_recently,
        checked_long_ago,
        seen_long_ago,
        both_long_ago,
        stale_check_fresh_sighting,
        fresh_check_stale_sighting,
    ) = _add(
        _job(1),
        _job(2, dead_at=T1, dead_reason="http 404"),
        _job(3, url=""),
        _job(4, url="   "),
        _job(5, last_checked_at=recent),
        _job(6, last_seen_at=recent),
        _job(7, last_checked_at=stale),
        _job(8, last_seen_at=stale),
        _job(9, last_checked_at=stale, last_seen_at=stale),
        _job(10, last_checked_at=stale, last_seen_at=recent),
        _job(11, last_checked_at=recent, last_seen_at=stale),
    )

    with get_session() as session:
        queued = {item.id for item in link_check_queue(session, now=NOW)}
        assert count_link_check_queue(session, now=NOW) == len(queued)

    assert queued == {never, checked_long_ago, seen_long_ago, both_long_ago}
    for excluded in (
        dead,
        no_url,
        blank_url,
        checked_recently,
        seen_recently,
        stale_check_fresh_sighting,
        fresh_check_stale_sighting,
    ):
        assert excluded not in queued


def test_link_check_queue_honours_recheck_days(db):
    (a,) = _add(_job(1, last_checked_at=NOW - timedelta(days=3)))
    with get_session() as session:
        assert link_check_queue(session, now=NOW) == []  # default 7 days
        assert count_link_check_queue(session, now=NOW) == 0
        assert [i.id for i in link_check_queue(session, recheck_days=2, now=NOW)] == [a]
        assert count_link_check_queue(session, recheck_days=2, now=NOW) == 1
        assert link_check_queue(session, recheck_days=4, now=NOW) == []


def test_link_check_queue_defaults_now_to_the_current_time(db):
    old, fresh = _add(
        _job(1, last_checked_at=utcnow() - timedelta(days=30)),
        _job(2, last_checked_at=utcnow() - timedelta(hours=1)),
    )
    with get_session() as session:
        assert [i.id for i in link_check_queue(session)] == [old]
        assert count_link_check_queue(session) == 1


def test_link_check_queue_order_and_limit(db):
    # Inserted out of order on purpose, so id order alone can't pass the test.
    checked_20d, never_1, checked_30d, never_2, checked_30d_tie = _add(
        _job(1, last_checked_at=NOW - timedelta(days=20)),
        _job(2),
        _job(3, last_checked_at=NOW - timedelta(days=30)),
        _job(4),
        _job(5, last_checked_at=NOW - timedelta(days=30)),
    )
    expected = [never_1, never_2, checked_30d, checked_30d_tie, checked_20d]

    with get_session() as session:
        assert [i.id for i in link_check_queue(session, now=NOW)] == expected
        assert [i.id for i in link_check_queue(session, limit=None, now=NOW)] == expected
        assert [i.id for i in link_check_queue(session, limit=3, now=NOW)] == expected[:3]
        assert [i.id for i in link_check_queue(session, limit=1, now=NOW)] == expected[:1]
        assert [i.id for i in link_check_queue(session, limit=50, now=NOW)] == expected
        assert link_check_queue(session, limit=0, now=NOW) == []
        # The count is of the whole queue, whatever limit a caller might use.
        assert count_link_check_queue(session, now=NOW) == len(expected)


def test_marking_moves_rows_out_of_the_queue(db):
    a, b, c = _add(_job(1), _job(2), _job(3))
    with get_session() as session:
        mark_checked(session, [a], when=NOW)
        mark_dead(session, [b], "http 404", when=NOW)
        session.commit()
        assert [i.id for i in link_check_queue(session, now=NOW)] == [c]
        mark_seen(session, [c], when=NOW)
        session.commit()
        assert link_check_queue(session, now=NOW) == []
        assert count_link_check_queue(session, now=NOW) == 0

        # A week later the checked and the seen rows are due again; the dead
        # one never comes back by itself.
        later = NOW + timedelta(days=7, minutes=1)
        assert [i.id for i in link_check_queue(session, now=later)] == [c, a]


# --- upsert_job --------------------------------------------------------------


def test_upsert_new_row_gets_last_seen_at(db):
    before = utcnow()
    with get_session() as session:
        job, created = upsert_job(session, _job(1))
        job_id = job.id
    after = utcnow()

    assert created is True
    row = _get(job_id)
    assert before <= row.last_seen_at <= after
    assert row.last_seen_at.tzinfo is None
    assert row.dead_at is None


def test_upsert_new_row_keeps_a_last_seen_at_it_already_has(db):
    with get_session() as session:
        job, created = upsert_job(session, _job(1, last_seen_at=T1, posted_at=T2))
        job_id = job.id

    assert created is True
    assert _get(job_id).last_seen_at == T1
    assert _get(job_id).posted_at == T2


def test_upsert_existing_row_updates_last_seen_at(db):
    (job_id,) = _add(_job(1, last_seen_at=T1))
    before = utcnow()
    with get_session() as session:
        existing, created = upsert_job(session, _job(1))
    after = utcnow()

    assert created is False
    assert existing.id == job_id  # still usable after the session closed
    assert before <= _get(job_id).last_seen_at <= after
    with get_session() as session:
        assert len(session.exec(select(JobPost)).all()) == 1


def test_upsert_revives_a_dead_row(db):
    (job_id,) = _add(
        _job(1, dead_at=T1, dead_reason="http 404", last_checked_at=T1, last_seen_at=T1)
    )
    with get_session() as session:
        _, created = upsert_job(session, _job(1))

    assert created is False
    row = _get(job_id)
    assert row.dead_at is None
    assert row.dead_reason is None
    assert row.last_seen_at > T1
    assert row.last_checked_at == T1  # revival doesn't rewrite check history


def test_upsert_backfills_posted_at_only_when_missing(db):
    without, with_date = _add(_job(1), _job(2, posted_at=T1))

    with get_session() as session:
        upsert_job(session, _job(1, posted_at=T2))
        upsert_job(session, _job(2, posted_at=T2))
    assert _get(without).posted_at == T2  # was None: filled in
    assert _get(with_date).posted_at == T1  # had one: kept

    # An incoming job with no posted_at never blanks a stored one.
    with get_session() as session:
        upsert_job(session, _job(1))
    assert _get(without).posted_at == T2


def test_upsert_fills_an_empty_description_only(db):
    empty, blank, has_text = _add(
        _job(1, description=""), _job(2, description="  \n"), _job(3, description="Mine.")
    )

    with get_session() as session:
        for i in (1, 2, 3):
            upsert_job(session, _job(i, description="From the scraper."))
    assert _get(empty).description == "From the scraper."  # was empty: filled in
    assert _get(blank).description == "From the scraper."  # whitespace counts as empty
    assert _get(has_text).description == "Mine."  # had text: kept

    # An incoming job with no description never blanks a stored one.
    with get_session() as session:
        upsert_job(session, _job(1, description=""))
        upsert_job(session, _job(2, description="   "))
    assert _get(empty).description == "From the scraper."
    assert _get(blank).description == "From the scraper."


def test_upsert_existing_row_leaves_everything_else_untouched(db):
    (job_id,) = _add(
        _job(
            1,
            title="Original title",
            description="Original description.",
            url="https://example.com/original",
            status="Applied",
            generated_cover_letter="dear hiring manager",
            generated_cold_email="hello there",
            contact_name="Sam Lee",
            contact_email="sam@example.com",
            contact_confidence="published",
            date_scraped=T1,
            last_checked_at=T2,
        )
    )
    incoming = _job(
        1,
        title="Changed title",
        company="Changed company",
        location="Changed location",
        description="Changed description.",
        url="https://example.com/changed",
        status="To Apply",
        last_checked_at=NOW,
    )
    with get_session() as session:
        _, created = upsert_job(session, incoming)

    assert created is False
    row = _get(job_id)
    assert row.title == "Original title"
    assert row.company == "Acme 1"
    assert row.location == "Remote"
    assert row.description == "Original description."
    assert row.url == "https://example.com/original"
    assert row.status == "Applied"
    assert row.generated_cover_letter == "dear hiring manager"
    assert row.generated_cold_email == "hello there"
    assert row.contact_name == "Sam Lee"
    assert row.contact_email == "sam@example.com"
    assert row.contact_confidence == "published"
    assert row.date_scraped == T1
    assert row.last_checked_at == T2


# --- reconcile_board (KAN-25) -------------------------------------------------


def _board_job(job_board_id, company="acme", **overrides):
    values = dict(
        job_board_id=job_board_id, title="Data Analyst", company=company,
        location="Remote", description="d", url=f"https://x/{job_board_id}",
    )
    values.update(overrides)
    return JobPost(**values)


def _by_id():
    with get_session() as session:
        return {r.job_board_id: r for r in session.exec(select(JobPost)).all()}


def test_reconcile_marks_present_seen_and_missing_dead(db):
    with get_session() as session:
        session.add_all([_board_job("greenhouse-1"), _board_job("greenhouse-2")])
        session.commit()
        counts = reconcile_board(session, "greenhouse", "acme", {"greenhouse-1"}, when=NOW)
        session.commit()

    assert counts == (1, 0, 1)
    rows = _by_id()
    assert rows["greenhouse-1"].last_seen_at == NOW
    assert rows["greenhouse-1"].dead_at is None
    assert rows["greenhouse-2"].dead_at == NOW
    assert rows["greenhouse-2"].dead_reason == GONE_FROM_BOARD


def test_reconcile_revives_a_present_dead_row(db):
    with get_session() as session:
        session.add(_board_job("greenhouse-1", dead_at=T1, dead_reason="http 404"))
        session.commit()
        counts = reconcile_board(session, "greenhouse", "acme", {"greenhouse-1"}, when=NOW)
        session.commit()

    assert counts == (1, 1, 0)
    row = _by_id()["greenhouse-1"]
    assert row.dead_at is None and row.dead_reason is None


def test_reconcile_keeps_the_first_death_of_a_missing_row(db):
    with get_session() as session:
        session.add_all([
            _board_job("greenhouse-1"),
            _board_job("greenhouse-2", dead_at=T1, dead_reason="http 404"),
        ])
        session.commit()
        counts = reconcile_board(session, "greenhouse", "acme", {"greenhouse-1"}, when=NOW)
        session.commit()

    assert counts == (1, 0, 0)
    row = _by_id()["greenhouse-2"]
    assert row.dead_at == T1 and row.dead_reason == "http 404"


def test_reconcile_matches_source_prefix_and_token_ignoring_case(db):
    with get_session() as session:
        session.add_all([
            _board_job("greenhouse-1", company="Acme"),   # same board, other case
            _board_job("greenhouse-2", company="other"),  # other board
            _board_job("lever-3"),                        # same token, other ATS
            _board_job("adzuna-4"),                       # search row
        ])
        session.commit()
        reconcile_board(session, "greenhouse", "ACME ", {"greenhouse-x"}, when=NOW)
        session.commit()

    rows = _by_id()
    assert rows["greenhouse-1"].dead_reason == GONE_FROM_BOARD
    for job_board_id in ("greenhouse-2", "lever-3", "adzuna-4"):
        assert rows[job_board_id].dead_at is None, job_board_id
        assert rows[job_board_id].last_seen_at is None, job_board_id


def test_reconcile_with_no_present_ids_changes_nothing(db):
    with get_session() as session:
        session.add(_board_job("greenhouse-1"))
        session.commit()
        assert reconcile_board(session, "greenhouse", "acme", set(), when=NOW) == (0, 0, 0)
        session.commit()

    row = _by_id()["greenhouse-1"]
    assert row.dead_at is None and row.last_seen_at is None


def test_reconcile_does_not_commit(db_url, tmp_path):
    init_db()
    with get_session() as session:
        session.add(_board_job("greenhouse-1"))
        session.commit()
        reconcile_board(session, "greenhouse", "acme", {"greenhouse-x"}, when=NOW)
        session.rollback()

    assert _by_id()["greenhouse-1"].dead_at is None
