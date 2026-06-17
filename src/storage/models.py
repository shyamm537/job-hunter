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
