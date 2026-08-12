"""Database schema for Job Hunter AI.

A single table, JobPost, tracks the full lifecycle of a posting from
discovery through generated application materials.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class JobPost(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_board_id: str = Field(unique=True)  # e.g. "seek-4029412"
    title: str
    company: str
    location: str  # Adelaide, Bangalore, Remote, etc.
    description: str
    url: str
    date_scraped: datetime = Field(default_factory=datetime.utcnow)
    status: str = Field(default="To Apply")  # To Apply, Applied, Interviewing, Rejected
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
