"""Connection handling and CRUD helpers.

The database URL is resolved lazily, in priority order:

1. An explicit `set_database_url(...)` call (used by the CLIs after they
   load config).
2. The `JOBHUNTER_DATABASE_URL` environment variable.
3. `database.url` from config.yaml (read automatically if present).
4. A local SQLite default (`sqlite:///data/jobs.db`).

Because steps 2-4 are automatic, code paths that don't set the URL
explicitly (the Streamlit app, the LLM worker) still pick up a non-default
URL from config or the environment without any changes of their own.

A note on what's actually supported: SQLite is the product. Postgres (or
any other backend) is a documented escape hatch that would need a real test
pass before you trusted it. The engine is built from whatever URL resolves,
and SQLModel/SQLAlchemy *can* speak Postgres — but that path has never been
run here and no non-SQLite driver (e.g. psycopg) is pinned. Treat non-SQLite
URLs as open-but-unimplemented: the wiring won't stop you, the testing
hasn't happened.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional, Sequence

from sqlalchemy import or_, text
from sqlmodel import Session, SQLModel, create_engine, select

from src.config import DEFAULT_DATABASE_URL
from src.storage.models import JobPost

ENV_VAR = "JOBHUNTER_DATABASE_URL"

_database_url: Optional[str] = None  # explicit override via set_database_url()
_engine = None


def _resolve_url() -> str:
    if _database_url is not None:
        return _database_url

    env_url = os.environ.get(ENV_VAR)
    if env_url:
        return env_url

    # Fall back to config.yaml if it's present and valid; otherwise default.
    try:
        from src.config import load_config

        return load_config().database.url
    except Exception:
        return DEFAULT_DATABASE_URL


def set_database_url(url: str) -> None:
    """Override the database URL and reset the cached engine.

    CLIs call this right after loading config so the resolved URL is
    deterministic rather than re-read from the environment on every use.
    """
    global _database_url, _engine
    _database_url = url
    _engine = None


def get_engine():
    """Return the process-wide engine, building it on first use."""
    global _engine
    if _engine is None:
        # NOTE: a non-SQLite URL will build an engine here and may even work,
        # but that path is unverified (see module docstring). SQLite is the
        # only backend this project actually tests against today.
        _engine = create_engine(_resolve_url(), echo=False)
    return _engine


def init_db() -> None:
    """Create tables if they don't exist yet. Safe to call repeatedly."""
    engine = get_engine()
    # For a file-backed SQLite URL, make sure the parent directory exists so
    # the very first run doesn't fail on a missing data/ folder.
    db_path = engine.url.database
    if engine.url.get_backend_name() == "sqlite" and db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite_columns(engine)


# Columns added to JobPost after the table may already exist on disk.
# create_all() only CREATEs missing tables — it never ALTERs an existing one —
# so a database written before these columns existed would be missing them and
# every read would raise. This is a deliberately tiny, additive, SQLite-only
# migration: it adds any missing nullable columns and nothing else. It is NOT a
# general migration framework; a real schema change (renames, type changes,
# non-SQLite backends) still needs proper tooling (see TODO.md).
_ADDED_COLUMNS = {
    "contact_name": "TEXT",
    "contact_email": "TEXT",
    "contact_confidence": "TEXT",
}


def _migrate_sqlite_columns(engine) -> None:
    if engine.url.get_backend_name() != "sqlite":
        return  # Postgres path is unverified anyway (see module docstring).
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(jobpost)"))}
        if not existing:
            return  # table not created yet / unexpected name — nothing to do
        for name, sqltype in _ADDED_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE jobpost ADD COLUMN {name} {sqltype}"))


@contextmanager
def get_session() -> Iterator[Session]:
    session = Session(get_engine())
    try:
        yield session
    finally:
        session.close()


def upsert_job(session: Session, job: JobPost) -> tuple[JobPost, bool]:
    """Insert a job if new; otherwise return the existing row untouched.

    job_board_id is the dedup key, so re-running a scrape never duplicates
    postings. Returns (job, created) so callers can report how many were
    actually new.
    """
    existing = session.exec(
        select(JobPost).where(JobPost.job_board_id == job.job_board_id)
    ).first()
    if existing:
        return existing, False

    session.add(job)
    session.commit()
    session.refresh(job)
    return job, True


def pending_llm_jobs(session: Session, limit: int | None = None) -> List[JobPost]:
    """Jobs that have been scraped but don't yet have generated materials.

    This is the "queue": the scraper writes rows, this function is how the
    LLM worker (src/llm/cli.py) finds work, and the UI never touches it.

    `limit` bounds how many rows come back (used by `llm.batch_size` so an
    unattended run can stop after a fixed number of jobs). `None` — the
    default — returns the whole queue, preserving the original behaviour for
    every other caller.
    """
    statement = select(JobPost).where(JobPost.generated_cover_letter.is_(None))
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.exec(statement).all())


def count_pending_llm_jobs(session: Session) -> int:
    """Total jobs still awaiting generation, ignoring any batch limit.

    Lets the worker report how many remain after a bounded run so a partial
    pass isn't mistaken for an empty queue.
    """
    return len(
        list(
            session.exec(
                select(JobPost).where(JobPost.generated_cover_letter.is_(None))
            ).all()
        )
    )


def pending_contact_jobs(session: Session) -> List[JobPost]:
    """Jobs that haven't been through contact lookup yet.

    The queue for `make contacts`, mirroring pending_llm_jobs(): NULL
    contact_confidence means "not looked up". The lookup always sets a
    confidence (including "none" on a miss), so a processed row leaves the
    queue and isn't retried on every run.
    """
    return list(
        session.exec(
            select(JobPost).where(JobPost.contact_confidence.is_(None))
        ).all()
    )


# --- Dashboard read helpers -------------------------------------------------
#
# The Streamlit list view must NOT hydrate full JobPost rows: description and
# the two generated blobs (cover letter, cold email) are large and the list
# never shows them. These helpers select only the columns the list renders, so
# the heavy text stays in the database until a single job's detail page asks
# for it. See docs/data-model.md and src/app/main.py.

# Every job_board_id is "<source>-<hash>" (see src/ingestion/*). The source is
# the prefix before the first dash; no source name contains a dash, so a
# LIKE '<source>-%' filter is exact.
KNOWN_SOURCES = ("seek", "adzuna", "greenhouse", "lever", "ashby")


class JobSummary(NamedTuple):
    """The minimal row the list view needs — no description/cover/email."""

    id: int
    title: str
    company: str
    status: str
    location: str
    source: str  # derived from the job_board_id prefix


def _source_of(job_board_id: str) -> str:
    return job_board_id.split("-", 1)[0]


def list_job_summaries(
    session: Session,
    *,
    sources: Optional[Sequence[str]] = None,
    companies: Optional[Sequence[str]] = None,
    statuses: Optional[Sequence[str]] = None,
    locations: Optional[Sequence[str]] = None,
    title_query: Optional[str] = None,
) -> List[JobSummary]:
    """Lightweight, filtered rows for the dashboard list.

    Selects only id/title/company/status/location/job_board_id — never the
    heavy text columns — and pushes every filter into SQL so the database
    returns just the matching rows. An empty/None filter means "no filter".
    """
    stmt = select(
        JobPost.id,
        JobPost.title,
        JobPost.company,
        JobPost.status,
        JobPost.location,
        JobPost.job_board_id,
    )
    if sources:
        stmt = stmt.where(or_(*[JobPost.job_board_id.like(f"{s}-%") for s in sources]))
    if companies:
        stmt = stmt.where(JobPost.company.in_(list(companies)))
    if statuses:
        stmt = stmt.where(JobPost.status.in_(list(statuses)))
    if locations:
        stmt = stmt.where(JobPost.location.in_(list(locations)))
    if title_query and title_query.strip():
        stmt = stmt.where(JobPost.title.ilike(f"%{title_query.strip()}%"))
    stmt = stmt.order_by(JobPost.date_scraped.desc())

    return [
        JobSummary(
            id=row[0],
            title=row[1],
            company=row[2],
            status=row[3],
            location=row[4],
            source=_source_of(row[5]),
        )
        for row in session.exec(stmt).all()
    ]


def distinct_locations(session: Session) -> List[str]:
    """Distinct, sorted locations for the location filter (one column only)."""
    rows = session.exec(
        select(JobPost.location).distinct().order_by(JobPost.location)
    ).all()
    return [r for r in rows if r]


def distinct_companies(session: Session) -> List[str]:
    """Distinct, sorted companies for the company filter (one column only)."""
    rows = session.exec(
        select(JobPost.company).distinct().order_by(JobPost.company)
    ).all()
    return [r for r in rows if r]


def present_sources(session: Session) -> List[str]:
    """Which sources actually have rows, for the source filter.

    A handful of cheap EXISTS-style probes rather than scanning job_board_id
    for the whole table.
    """
    found: List[str] = []
    for src in KNOWN_SOURCES:
        exists = session.exec(
            select(JobPost.id).where(JobPost.job_board_id.like(f"{src}-%")).limit(1)
        ).first()
        if exists is not None:
            found.append(src)
    return found


def get_job(session: Session, job_id: int) -> Optional[JobPost]:
    """Full JobPost for the detail page — the one place heavy text loads, and
    only ever for a single row."""
    return session.get(JobPost, job_id)


def set_job_status(session: Session, job_id: int, status: str) -> None:
    """Persist a status change for one job. Used by both the list quick-edit
    and the detail page."""
    job = session.get(JobPost, job_id)
    if job is None:
        return
    job.status = status
    session.add(job)
    session.commit()
