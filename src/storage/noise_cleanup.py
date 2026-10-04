"""One-off: mark stored jobs that fail the current filters "Not interested" (KAN-37).

The title, exclude and remote-region rules (`filters`, src/ingestion/filtering.py)
only apply to jobs scraped from now on. Jobs already stored from before still sit
in the To Apply queue. This command runs the same rules over them.

A job is checked only if you have not touched it:

- status is "To Apply" (never Applied, Interviewing, Rejected or Not interested);
- it is unread (`opened_at` is empty);
- it has no generated cover letter (never throw away work the LLM already did);
- it was not added by hand (`manual-` rows are never touched).

A checked job that fails the rules becomes "Not interested": it leaves the To
Apply queue and `make process` / `make contacts` skip it, but nothing is deleted
and you can set it back from the dashboard. Adzuna rows are checked by title only
(the same as the scrape: their search already narrowed the location); board rows
by title and location.

    python -m src.storage.noise_cleanup            # dry run: report only
    python -m src.storage.noise_cleanup --apply    # write (back up data/ first)

Re-running is a no-op: a job already marked "Not interested" is not checked again.
"""

import argparse
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlmodel import Session, select

from src.config import Filters
from src.ingestion.filtering import (
    job_matches,
    title_excluded,
    title_matches,
    wanted_titles,
)
from src.storage.database import MANUAL_SOURCE
from src.storage.models import NOT_INTERESTED, TO_APPLY, JobPost

log = logging.getLogger("jobhunter.noise_cleanup")

NO_WANTED_TITLE = "no wanted title"
EXCLUDED_TITLE = "excluded title"
REMOTE_OUTSIDE_REGIONS = "remote outside wanted regions"
LOCATION = "location"


@dataclass
class CleanupReport:
    checked: int = 0  # candidate rows examined (unread To Apply, no letter, not by hand)
    flagged: int = 0  # of those, how many fail the rules
    applied: bool = False
    by_reason: Counter = field(default_factory=Counter)
    by_source: Counter = field(default_factory=Counter)
    examples: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))

    def summary(self) -> str:
        verb = "marked Not interested" if self.applied else "would be marked Not interested"
        return f"{self.checked} job(s) checked, {self.flagged} {verb}"


def _source_of(job: JobPost) -> str:
    return job.job_board_id.split("-", 1)[0]


def noise_reason(job: JobPost, filters: Filters) -> Optional[str]:
    """Why the stored job fails the current rules, or None if it passes."""
    if not title_matches(job.title, wanted_titles(filters)):
        return NO_WANTED_TITLE
    if title_excluded(job.title, filters.exclude_titles):
        return EXCLUDED_TITLE
    if _source_of(job) == "adzuna":
        return None  # title only: the Adzuna query already narrowed the location
    if job_matches(job, filters):
        return None
    # Say whether it was the remote-region rule that dropped it.
    if filters.remote_regions and job_matches(
        job, filters.model_copy(update={"remote_regions": []})
    ):
        return REMOTE_OUTSIDE_REGIONS
    return LOCATION


def cleanup_noise(
    session: Session, filters: Filters, *, apply: bool = False, examples: int = 3
) -> CleanupReport:
    """Report (and with apply=True, mark) stored jobs that fail the rules."""
    candidates = session.exec(
        select(JobPost).where(
            JobPost.status == TO_APPLY,
            JobPost.opened_at.is_(None),
            JobPost.generated_cover_letter.is_(None),
            JobPost.job_board_id.not_like(f"{MANUAL_SOURCE}-%"),
        )
    ).all()

    report = CleanupReport(checked=len(candidates), applied=apply)
    for job in candidates:
        reason = noise_reason(job, filters)
        if reason is None:
            continue
        report.flagged += 1
        report.by_reason[reason] += 1
        report.by_source[_source_of(job)] += 1
        if len(report.examples[reason]) < examples:
            report.examples[reason].append(f"{job.company}: {job.title} ({job.location or 'no location'})")
        if apply:
            job.status = NOT_INTERESTED
            session.add(job)
    if apply:
        session.commit()  # one commit: it either all lands or none of it does
    return report


def main(argv: Optional[List[str]] = None) -> None:
    from src.config import ConfigError, load_config
    from src.logging_config import setup_logging
    from src.storage.database import get_session, init_db, set_database_url

    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m src.storage.noise_cleanup",
        description="Mark stored, untouched jobs that fail the current filters as Not interested.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="write the changes; without this flag nothing is written",
    )
    parser.add_argument(
        "--examples", type=int, default=3, metavar="N",
        help="show up to N example jobs per reason (default %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
        set_database_url(config.database.url)
        init_db()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    with get_session() as session:
        report = cleanup_noise(
            session, config.resolved_filters, apply=args.apply, examples=args.examples
        )

    mode = "" if args.apply else " (dry run, nothing written; use --apply)"
    log.info("%s%s", report.summary(), mode)
    for reason, count in report.by_reason.most_common():
        log.info("  %-30s %d", reason, count)
        for line in report.examples[reason]:
            log.info("      e.g. %s", line)
    if report.by_source:
        log.info("  by source: %s", ", ".join(f"{s} {n}" for s, n in report.by_source.most_common()))


if __name__ == "__main__":
    main()
