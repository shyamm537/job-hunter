"""The dead-postings archive: a second SQLite file that dead jobs move to.

A posting that has closed is still data (when it was posted, first scraped,
last seen, found dead, and why), so it is kept for later analysis rather than
deleted, but it no longer belongs in the main database the dashboard and the
workers read. archive_dead_jobs() moves dead rows whose status is in
ARCHIVED_STATUSES; jobs you have applied to or are interviewing for stay in
the main database, marked dead, so they don't vanish from what you track.

The archive uses the same JobPost table and columns. Rows are matched on
job_board_id; ids are NOT preserved (each database assigns its own), so a
restored job gets a new id in the main database.

The two files can't be written in one transaction, so a move copies first and
deletes second. A crash in between leaves the row in both files, and the next
move simply copies it again (overwriting) and deletes it, so re-running is
always safe.

Callers: the scrape (src/ingestion/cli.py) and the link checker
(src/ingestion/check_links.py) both run archive_dead_jobs() once at the end of
a run, and upsert_job() restores an archived posting that a scrape returns
again. `python -m src.storage.archive` runs just the move.
"""

import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, create_engine, select

from src.storage.database import _ID_CHUNK_SIZE, _migrate_sqlite_columns, utcnow
from src.storage.models import JobPost

log = logging.getLogger("jobhunter.archive")

ARCHIVE_FILENAME = "dead_jobs.db"

# Dead rows with these statuses move to the archive. Applied / Interviewing
# rows stay in the main database, marked dead.
ARCHIVED_STATUSES = ("To Apply", "Rejected")

_archive_url: Optional[str] = None
_archive_engine = None


def default_archive_url(database_url: str) -> str:
    """`dead_jobs.db` in the same folder as a file-backed SQLite main database.

    An in-memory main database gets an in-memory archive. Anything else (the
    unverified Postgres path) has no sensible default and must set
    `database.archive_url` explicitly.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError(
            "database.archive_url must be set when database.url is not SQLite"
        )
    if not url.database or url.database == ":memory:":
        return "sqlite://"
    return f"sqlite:///{Path(url.database).parent.as_posix()}/{ARCHIVE_FILENAME}"


def set_archive_url(url: str) -> None:
    """Point the archive at `url` and reset the cached engine."""
    global _archive_url, _archive_engine
    _archive_url = url
    _archive_engine = None


def configure_archive(database_config) -> None:
    """set_archive_url() from a DatabaseConfig: its archive_url, or the
    default next to the main database. Then create the table if needed."""
    set_archive_url(
        database_config.archive_url or default_archive_url(database_config.url)
    )
    init_archive_db()


def get_archive_engine():
    global _archive_engine
    if _archive_engine is None:
        if _archive_url is None:
            raise RuntimeError("archive not configured: call set_archive_url() first")
        _archive_engine = create_engine(_archive_url, echo=False)
    return _archive_engine


def init_archive_db() -> None:
    """Create the archive's table if missing. Safe to call repeatedly."""
    engine = get_archive_engine()
    db_path = engine.url.database
    if engine.url.get_backend_name() == "sqlite" and db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine, tables=[JobPost.__table__])
    _migrate_sqlite_columns(engine)


@contextmanager
def get_archive_session() -> Iterator[Session]:
    session = Session(get_archive_engine())
    try:
        yield session
    finally:
        session.close()


def _row_values(job: JobPost) -> dict:
    """Every column except the database-local id."""
    values = job.model_dump()
    values.pop("id", None)
    return values


def _put(session: Session, job: JobPost) -> None:
    """Insert or overwrite `job` in `session`'s database, matched on
    job_board_id. Does not commit."""
    values = _row_values(job)
    existing = session.exec(
        select(JobPost).where(JobPost.job_board_id == job.job_board_id)
    ).first()
    if existing is None:
        session.add(JobPost(**values))
        return
    for name, value in values.items():
        setattr(existing, name, value)
    session.add(existing)


def archive_dead_jobs(session: Session, archive_session: Session) -> int:
    """Move dead rows with an ARCHIVED_STATUSES status into the archive.

    Copies (and commits) to the archive first, then deletes from the main
    database and commits. Returns how many rows moved.
    """
    rows = session.exec(
        select(JobPost).where(
            JobPost.dead_at.is_not(None), JobPost.status.in_(ARCHIVED_STATUSES)
        )
    ).all()
    if not rows:
        return 0

    for row in rows:
        _put(archive_session, row)
    archive_session.commit()

    ids = [row.id for row in rows]
    for start in range(0, len(ids), _ID_CHUNK_SIZE):
        chunk = ids[start : start + _ID_CHUNK_SIZE]
        session.exec(delete(JobPost).where(JobPost.id.in_(chunk)))
    session.commit()
    return len(ids)


def restore_from_archive(
    session: Session, archive_session: Session, job_board_id: str
) -> Optional[JobPost]:
    """Move an archived posting back into the main database, alive again.

    Returns the restored main-database row, or None if it isn't archived.
    Status, generated materials, contact fields and dates come back as they
    were; dead_at/dead_reason are cleared and last_seen_at set to now, as
    upsert_job does when it revives a row.
    """
    archived = archive_session.exec(
        select(JobPost).where(JobPost.job_board_id == job_board_id)
    ).first()
    if archived is None:
        return None

    restored = JobPost(**_row_values(archived))
    restored.dead_at = None
    restored.dead_reason = None
    restored.last_seen_at = utcnow()
    session.add(restored)
    session.commit()
    session.refresh(restored)

    archive_session.delete(archived)
    archive_session.commit()
    log.info(
        "restored %s (%s — %s) from the archive",
        job_board_id, restored.company, restored.title,
    )
    return restored


def main() -> None:
    """`python -m src.storage.archive`: run just the move and report it."""
    from src.config import ConfigError, load_config
    from src.logging_config import setup_logging
    from src.storage.database import get_session, init_db, set_database_url

    setup_logging()
    try:
        config = load_config()
        set_database_url(config.database.url)
        init_db()
        configure_archive(config.database)
    except (ConfigError, ValueError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    with get_session() as session, get_archive_session() as archive_session:
        moved = archive_dead_jobs(session, archive_session)
    log.info("Moved %d dead posting(s) to %s.", moved, get_archive_engine().url)


if __name__ == "__main__":
    main()
