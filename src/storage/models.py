"""Database schema for Job Hunter AI.

JobPost tracks the full lifecycle of a posting from discovery through
generated application materials. ScrapeRun records when each `make scrape`
started, which is how the dashboard knows which jobs are new.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

# Application statuses, in the order the dashboard offers them. Free text in
# the database (see docs/data-model.md); the dashboard refuses anything else.
TO_APPLY = "To Apply"
NOT_INTERESTED = "Not interested"
STATUSES = (TO_APPLY, "Applied", "Interviewing", "Rejected", NOT_INTERESTED)


class JobPost(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_board_id: str = Field(unique=True)  # e.g. "greenhouse-4029412abc"
    title: str
    company: str
    location: str  # Adelaide, Bangalore, Remote, etc.
    description: str
    url: str
    date_scraped: datetime = Field(default_factory=datetime.utcnow)
    status: str = Field(default=TO_APPLY)  # one of STATUSES
    generated_cover_letter: Optional[str] = None
    generated_cold_email: Optional[str] = None
    # Contact lookup (see docs/hiring-manager-lookup.md). All nullable; populated
    # by `make contacts`, never asserted as verified. contact_confidence doubles
    # as the queue signal — NULL means "not looked up yet".
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    # "published" | "pattern-guess" | "none" (set even on a miss, so the row
    # leaves the queue). Free-text for now, like `status` — see docs/data-model.md.
    contact_confidence: Optional[str] = None
    dead_at: Optional[datetime] = None #When the job listing was found to be dead
    # Dead-posting bookkeeping (see TODO.md "dead posting handling"). All
    # nullable, all naive UTC like date_scraped. Registered in _ADDED_COLUMNS
    # in database.py so an older database gains them on init_db().
    # Short reason set together with dead_at, e.g. "http 404". Cleared with
    # dead_at when a later scrape sees the posting again.
    dead_reason: Optional[str] = None
    # When the source says the ad was published (None if it doesn't say).
    posted_at: Optional[datetime] = None
    # Last time a scrape returned this posting (set by upsert_job).
    last_seen_at: Optional[datetime] = None
    # Last time check_links requested this posting's URL.
    last_checked_at: Optional[datetime] = None
    # When you first opened the job in the dashboard (or changed its status);
    # None means unread.
    opened_at: Optional[datetime] = None


class ScrapeRun(SQLModel, table=True):
    """One `make scrape` run. Jobs first stored since the latest run started
    are "new" in the dashboard."""

    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: datetime
