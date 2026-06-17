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
from typing import Iterator, List, Optional

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


def pending_llm_jobs(session: Session) -> List[JobPost]:
    """Jobs that have been scraped but don't yet have generated materials.

    This is the "queue": the scraper writes rows, this function is how the
    LLM worker (src/llm/cli.py) finds work, and the UI never touches it.
    """
    return list(
        session.exec(
            select(JobPost).where(JobPost.generated_cover_letter.is_(None))
        ).all()
    )
