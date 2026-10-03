"""Connection handling and CRUD helpers.

The database URL is resolved lazily, in priority order:

1. An explicit `set_database_url(...)` call (used by the CLIs after they
   load config).
2. The `JOBHUNTER_DATABASE_URL` environment variable.
3. `database.url` from config.yaml (read automatically if present).
4. A local SQLite default (`sqlite:///data/jobs.db`).

Because steps 2-4 are automatic, code paths that don't set the URL
explicitly (e.g. the LLM worker) still pick up a non-default
URL from config or the environment without any changes of their own.

A note on what's actually supported: SQLite is the product. Postgres (or
any other backend) is a documented escape hatch that would need a real test
pass before you trusted it. The engine is built from whatever URL resolves,
and SQLModel/SQLAlchemy *can* speak Postgres — but that path has never been
run here and no non-SQLite driver (e.g. psycopg) is pinned. Treat non-SQLite
URLs as open-but-unimplemented: the wiring won't stop you, the testing
hasn't happened.
"""

import hashlib
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Collection, Iterator, List, NamedTuple, Optional, Sequence

from sqlalchemy import case, func, or_, text, update
from sqlmodel import Session, SQLModel, create_engine, select

from src.config import DEFAULT_DATABASE_URL
from src.storage.models import NOT_INTERESTED, JobPost, ScrapeRun

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
    # dead_at was added to the model by hand without being registered here, so
    # an older database failed with "no such column: dead_at".
    "dead_at": "DATETIME",
    "dead_reason": "TEXT",
    "posted_at": "DATETIME",
    "last_seen_at": "DATETIME",
    "last_checked_at": "DATETIME",
    "opened_at": "DATETIME",
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


def utcnow() -> datetime:
    """Naive UTC "now" — the convention for every datetime column here
    (date_scraped, dead_at, posted_at, last_seen_at, last_checked_at)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fill_empty_description(row: JobPost, incoming: JobPost) -> bool:
    """Copy the incoming description onto `row` only if the row has none.

    A stored description is never overwritten, however it differs. Returns
    whether the row changed.
    """
    stored_is_empty = not (row.description or "").strip()
    incoming_has_text = bool((incoming.description or "").strip())
    if stored_is_empty and incoming_has_text:
        row.description = incoming.description
        return True
    return False


def upsert_job(
    session: Session, job: JobPost, archive_session: Optional[Session] = None
) -> tuple[JobPost, bool]:
    """Insert a job if new; otherwise record that the existing row was seen.

    job_board_id is the dedup key, so re-running a scrape never duplicates
    postings. Returns (job, created) so callers can report how many were
    actually new.

    With an `archive_session`, a posting that isn't in the main database but
    is in the dead-postings archive is restored from there (status, generated
    materials and dates intact) instead of being inserted fresh, and returned
    with created=False. See src/storage/archive.py.

    Either way the row's last_seen_at is stamped, which is how check_links
    knows a posting is still being returned by its source. For an existing
    row that is ALL a re-scrape changes, apart from two things:

    - a row marked dead is revived (dead_at and dead_reason cleared): if a
      scrape returns it again, the link check was wrong or the ad reopened;
    - posted_at and description are filled in if the row doesn't have one yet
      (a scraper that fetches descriptions one posting at a time returns "" when
      that call failed, and the next scrape should be able to repair the row).

    Nothing else on an existing row is overwritten — status, generated
    materials, contact fields and a description that is already there are the
    user's/worker's data.
    """
    existing = session.exec(
        select(JobPost).where(JobPost.job_board_id == job.job_board_id)
    ).first()
    if existing:
        existing.last_seen_at = utcnow()
        if existing.dead_at is not None:
            existing.dead_at = None
            existing.dead_reason = None
        if existing.posted_at is None and job.posted_at is not None:
            existing.posted_at = job.posted_at
        _fill_empty_description(existing, job)
        session.add(existing)
        session.commit()
        # commit() expires the row; reload it so the caller gets a usable
        # object even after the session closes (same as the new-row path).
        session.refresh(existing)
        return existing, False

    if archive_session is not None:
        from src.storage.archive import restore_from_archive  # avoids an import cycle

        restored = restore_from_archive(session, archive_session, job.job_board_id)
        if restored is not None:
            changed = False
            if restored.posted_at is None and job.posted_at is not None:
                restored.posted_at = job.posted_at
                changed = True
            changed = _fill_empty_description(restored, job) or changed
            if changed:
                session.add(restored)
                session.commit()
                session.refresh(restored)
            return restored, False

    if job.last_seen_at is None:
        job.last_seen_at = utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job, True


# --- Jobs added by hand ------------------------------------------------------

# Source prefix for jobs added from the dashboard (pasted from LinkedIn, SEEK,
# Naukri, ...). No scraper owns them: they're never board-reconciled or
# link-checked, and `make process` does them first.
MANUAL_SOURCE = "manual"


def manual_job_board_id(*, title: str, company: str, description: str, url: str = "") -> str:
    """`manual-<sha1[:10]>` of the URL, or of title + company + description when
    there's no URL, so adding the same job twice finds the first one."""
    key = url.strip() or "\n".join(
        part.strip().lower() for part in (title, company, description)
    )
    return f"{MANUAL_SOURCE}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"


def add_manual_job(
    session: Session,
    *,
    title: str,
    company: str,
    description: str,
    location: str = "",
    url: str = "",
) -> tuple[JobPost, bool]:
    """Store a job you found yourself. Returns (job, created).

    Title, company and description are required (the description is what the
    cover letter is written from); surrounding whitespace is trimmed. Adding a
    job that's already stored (same URL, or same title/company/description
    without one) returns the existing row with created=False.
    """
    title, company, description = title.strip(), company.strip(), description.strip()
    location, url = location.strip(), url.strip()
    missing = [name for name, value in
               (("title", title), ("company", company), ("description", description))
               if not value]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    job = JobPost(
        job_board_id=manual_job_board_id(
            title=title, company=company, description=description, url=url
        ),
        title=title,
        company=company,
        location=location,
        description=description,
        url=url,
    )
    return upsert_job(session, job)


# --- Dead-posting helpers ---------------------------------------------------
#
# Used by src/ingestion/check_links.py and, through reconcile_board, by the
# scrape. The mark_* helpers issue bulk UPDATEs
# and deliberately do NOT commit: the link checker commits in batches, so a
# long run that is interrupted keeps what it has already recorded.

# Ids per UPDATE statement. SQLite caps bound parameters per statement (999 on
# older builds), so a long id list is split rather than sent as one IN (...).
_ID_CHUNK_SIZE = 500

# dead_reason for a board row that its board no longer lists (reconcile_board).
GONE_FROM_BOARD = "gone from board"


def _update_jobs(
    session: Session, job_ids: Sequence[int], values: dict, *extra_where
) -> int:
    """UPDATE jobpost SET <values> WHERE id IN (<job_ids>) [AND extra_where].

    Chunked, no commit. Returns the number of rows the statements changed.
    """
    ids = list(job_ids)
    changed = 0
    for start in range(0, len(ids), _ID_CHUNK_SIZE):
        chunk = ids[start : start + _ID_CHUNK_SIZE]
        statement = (
            update(JobPost)
            .where(JobPost.id.in_(chunk), *extra_where)
            .values(**values)
        )
        changed += session.exec(statement).rowcount
    return changed


def mark_seen(
    session: Session, job_ids: Sequence[int], *, when: Optional[datetime] = None
) -> int:
    """Set last_seen_at on the given jobs. Does not commit; returns rows changed."""
    if not job_ids:
        return 0
    return _update_jobs(session, job_ids, {"last_seen_at": when or utcnow()})


def mark_checked(
    session: Session, job_ids: Sequence[int], *, when: Optional[datetime] = None
) -> int:
    """Set last_checked_at on the given jobs. Does not commit; returns rows changed."""
    if not job_ids:
        return 0
    return _update_jobs(session, job_ids, {"last_checked_at": when or utcnow()})


def mark_dead(
    session: Session,
    job_ids: Sequence[int],
    reason: str,
    *,
    when: Optional[datetime] = None,
) -> int:
    """Set dead_at and dead_reason on the given jobs that aren't dead already.

    Rows with a dead_at are skipped, so the first death date and reason are
    kept (and aren't counted in the return value). last_checked_at is left
    alone — call mark_checked for that. Does not commit.
    """
    if not job_ids:
        return 0
    return _update_jobs(
        session,
        job_ids,
        {"dead_at": when or utcnow(), "dead_reason": reason},
        JobPost.dead_at.is_(None),
    )


def reconcile_board(
    session: Session,
    source: str,
    token: str,
    present_ids: Collection[str],
    *,
    when: Optional[datetime] = None,
) -> tuple[int, int, int]:
    """Compare one board's stored rows with what its scrape just returned.

    `present_ids` is every job_board_id in the board's UNFILTERED scrape. A
    board's rows are the `<source>-` rows whose company is the board token
    (the Greenhouse/Lever/Ashby scrapers store the token as company); the
    token match ignores case.

    - Present rows get last_seen_at, including ones the filters drop, and a
      dead one is revived (dead_at/dead_reason cleared), as upsert_job does.
    - Missing rows are marked dead with reason "gone from board"; rows that
      are already dead keep their first date and reason.

    An empty `present_ids` changes nothing: an empty or failed scrape proves
    nothing about the board. Does not commit. Returns (seen, revived, newly_dead).
    """
    if not present_ids:
        return 0, 0, 0
    present = set(present_ids)
    rows = session.exec(
        select(JobPost.id, JobPost.job_board_id, JobPost.dead_at).where(
            JobPost.job_board_id.like(f"{source}-%"),
            func.lower(JobPost.company) == token.strip().lower(),
        )
    ).all()

    seen_ids = [row[0] for row in rows if row[1] in present]
    revive_ids = [row[0] for row in rows if row[1] in present and row[2] is not None]
    gone_ids = [row[0] for row in rows if row[1] not in present]

    when = when or utcnow()
    seen = mark_seen(session, seen_ids, when=when)
    revived = (
        _update_jobs(session, revive_ids, {"dead_at": None, "dead_reason": None})
        if revive_ids
        else 0
    )
    dead = mark_dead(session, gone_ids, GONE_FROM_BOARD, when=when)
    return seen, revived, dead


class LinkCheckItem(NamedTuple):
    """The minimal row check_links needs — no description/cover/email."""

    id: int
    title: str
    company: str
    url: str


def _link_check_conditions(recheck_days: int, now: Optional[datetime]) -> list:
    """WHERE clauses shared by link_check_queue and count_link_check_queue.

    A posting is due for a link check when it isn't dead, has a URL, and has
    been neither checked nor returned by a scrape within `recheck_days`. A
    recent scrape sighting counts as proof of life, so those rows are skipped.
    """
    cutoff = (now or utcnow()) - timedelta(days=recheck_days)
    return [
        JobPost.dead_at.is_(None),
        # Hand-added jobs are never URL-checked: LinkedIn/Naukri-style pages
        # often block or redirect logged-out requests, which could mark a live
        # job dead. You track those yourself.
        JobPost.job_board_id.not_like(f"{MANUAL_SOURCE}-%"),
        # NULL fails this comparison too, so a NULL url is excluded as well.
        func.trim(JobPost.url) != "",
        or_(JobPost.last_checked_at.is_(None), JobPost.last_checked_at < cutoff),
        or_(JobPost.last_seen_at.is_(None), JobPost.last_seen_at < cutoff),
    ]


def link_check_queue(
    session: Session,
    *,
    limit: Optional[int] = None,
    recheck_days: int = 7,
    now: Optional[datetime] = None,
) -> List[LinkCheckItem]:
    """Postings due for a link check, most overdue first.

    Order: never-checked rows first, then oldest last_checked_at, ties by id.
    `limit` is applied in SQL; None returns the whole queue. Only the four
    LinkCheckItem columns are selected — the heavy text stays in the database.
    """
    statement = (
        select(JobPost.id, JobPost.title, JobPost.company, JobPost.url)
        .where(*_link_check_conditions(recheck_days, now))
        .order_by(
            # False (0) sorts before True (1): never-checked rows lead. Spelled
            # out rather than relying on where a backend puts NULLs by default.
            JobPost.last_checked_at.is_not(None),
            JobPost.last_checked_at.asc(),
            JobPost.id.asc(),
        )
    )
    if limit is not None:
        statement = statement.limit(limit)
    return [
        LinkCheckItem(id=row[0], title=row[1], company=row[2], url=row[3])
        for row in session.exec(statement).all()
    ]


def count_link_check_queue(
    session: Session, *, recheck_days: int = 7, now: Optional[datetime] = None
) -> int:
    """Size of the whole link-check queue (SQL COUNT), ignoring any limit."""
    statement = (
        select(func.count())
        .select_from(JobPost)
        .where(*_link_check_conditions(recheck_days, now))
    )
    return session.exec(statement).one()


def pending_llm_jobs(session: Session, limit: int | None = None) -> List[JobPost]:
    """Jobs that have been scraped but don't yet have generated materials.

    This is the "queue": the scraper writes rows, this function is how the
    LLM worker (src/llm/cli.py) finds work, and the UI never touches it.

    `limit` bounds how many rows come back (used by `llm.batch_size` so an
    unattended run can stop after a fixed number of jobs). `None` — the
    default — returns the whole queue, preserving the original behaviour for
    every other caller.

    Order: jobs you added by hand come first (oldest first), so the next
    `make process` run does yours before the scraped backlog; then the rest
    in insertion order, as before. Jobs marked "Not interested" are skipped.
    """
    statement = (
        select(JobPost)
        .where(JobPost.generated_cover_letter.is_(None), JobPost.status != NOT_INTERESTED)
        .order_by(
            case((JobPost.job_board_id.like(f"{MANUAL_SOURCE}-%"), 0), else_=1),
            JobPost.id.asc(),
        )
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.exec(statement).all())


def count_pending_llm_jobs(session: Session) -> int:
    """Total jobs still awaiting generation, ignoring any batch limit.

    Lets the worker report how many remain after a bounded run so a partial
    pass isn't mistaken for an empty queue.
    """
    return session.exec(
        select(func.count())
        .select_from(JobPost)
        .where(JobPost.generated_cover_letter.is_(None), JobPost.status != NOT_INTERESTED)
    ).one()


def pending_contact_jobs(session: Session) -> List[JobPost]:
    """Jobs that haven't been through contact lookup yet.

    The queue for `make contacts`, mirroring pending_llm_jobs(): NULL
    contact_confidence means "not looked up". The lookup always sets a
    confidence (including "none" on a miss), so a processed row leaves the
    queue and isn't retried on every run. Jobs marked "Not interested" are
    skipped, as they are by pending_llm_jobs().
    """
    return list(
        session.exec(
            select(JobPost).where(
                JobPost.contact_confidence.is_(None), JobPost.status != NOT_INTERESTED
            )
        ).all()
    )


# --- Dashboard read helpers -------------------------------------------------
#
# The dashboard's list view must NOT hydrate full JobPost rows: description and
# the two generated blobs (cover letter, cold email) are large and the list
# never shows them. These helpers select only the columns the list renders, so
# the heavy text stays in the database until a single job's detail page asks
# for it. See docs/data-model.md and src/app/web.py.

# Every job_board_id is "<source>-<hash>" (see src/ingestion/*). The source is
# the prefix before the first dash; no source name contains a dash, so a
# LIKE '<source>-%' filter is exact.
KNOWN_SOURCES = ("adzuna", "greenhouse", "lever", "ashby", "workable", MANUAL_SOURCE)


class JobSummary(NamedTuple):
    """The minimal row the list view needs — no description/cover/email."""

    id: int
    title: str
    company: str
    status: str
    location: str
    source: str  # derived from the job_board_id prefix
    posted: datetime  # posted_at, or date_scraped when the source doesn't say
    date_scraped: datetime
    url: str
    opened_at: Optional[datetime]


# Sort orders the list view offers. "posted" (the default) is newest first by
# the posted date, falling back to the scrape date when a source gives none.
_POSTED = func.coalesce(JobPost.posted_at, JobPost.date_scraped)
SORTS = {
    "posted": (_POSTED.desc(), JobPost.id.desc()),
    "title": (func.lower(JobPost.title).asc(), _POSTED.desc()),
    "company": (func.lower(JobPost.company).asc(), _POSTED.desc()),
}


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
    sort: str = "posted",
) -> List[JobSummary]:
    """Lightweight, filtered rows for the dashboard list.

    Selects only the columns the list shows — never the heavy text columns —
    and pushes every filter into SQL so the database returns just the
    matching rows. An empty/None filter means "no filter". `sort` is a key of
    SORTS; an unknown one falls back to "posted".
    """
    stmt = select(
        JobPost.id,
        JobPost.title,
        JobPost.company,
        JobPost.status,
        JobPost.location,
        JobPost.job_board_id,
        _POSTED,
        JobPost.date_scraped,
        JobPost.url,
        JobPost.opened_at,
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
    stmt = stmt.order_by(*SORTS.get(sort, SORTS["posted"]))

    return [
        JobSummary(
            id=row[0],
            title=row[1],
            company=row[2],
            status=row[3],
            location=row[4],
            source=_source_of(row[5]),
            posted=_as_datetime(row[6]),
            date_scraped=row[7],
            url=row[8],
            opened_at=row[9],
        )
        for row in session.exec(stmt).all()
    ]


def _as_datetime(value) -> datetime:
    """COALESCE loses SQLite's column type, so the posted date can come back
    as a string; turn it back into a datetime."""
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def mark_opened(
    session: Session, job_ids: Sequence[int], *, when: Optional[datetime] = None
) -> int:
    """Mark jobs as read (opened_at), leaving already-read ones alone.
    Does not commit; returns rows changed."""
    if not job_ids:
        return 0
    return _update_jobs(
        session, job_ids, {"opened_at": when or utcnow()}, JobPost.opened_at.is_(None)
    )


def record_scrape_run(session: Session, *, when: Optional[datetime] = None) -> datetime:
    """Note that a `make scrape` run is starting, and commit. Returns its start."""
    started = when or utcnow()
    session.add(ScrapeRun(started_at=started))
    session.commit()
    return started


def latest_scrape_start(session: Session) -> Optional[datetime]:
    """When the most recent `make scrape` run started, or None if none is recorded."""
    return session.exec(select(func.max(ScrapeRun.started_at))).one()


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
