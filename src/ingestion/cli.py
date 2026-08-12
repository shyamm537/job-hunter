"""Entry point for `make scrape`.

Reads config.yaml, plans concrete scrapes from (sources x filters), runs each,
and writes new postings into the database. Run this, then `make process` to
generate materials, then `make app` to view everything.

SEEK sources expand into one search per (title, location); ATS boards
(Greenhouse, Lever) are scraped whole and then filtered by the same titles and
locations. Each planned scrape runs independently — one failing (network error,
bad token) is logged and skipped, not fatal.

Set JOBHUNTER_DUMP_DIR to capture each scrape's *unfiltered* output to JSON
(see src/ingestion/capture.py) — handy when a filter is eating everything, and
as a source of fixtures for tests.
"""

import logging
import sys

from src.config import ConfigError, load_config
from src.ingestion.capture import dump_jobs, resolve_dump_dir
from src.ingestion.filtering import job_matches
from src.ingestion.planner import plan_scrapes
from src.logging_config import setup_logging
from src.storage.database import get_session, init_db, set_database_url, upsert_job

log = logging.getLogger("jobhunter.scrape")


def main() -> None:
    setup_logging()

    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    set_database_url(config.database.url)
    init_db()

    filters = config.resolved_filters
    plans = plan_scrapes(
        config.resolved_sources, filters, adzuna_auth=config.adzuna_auth
    )

    dump_dir = resolve_dump_dir()
    if dump_dir:
        log.info("capturing unfiltered output to %s", dump_dir)

    total_seen = 0
    total_new = 0

    with get_session() as session:
        for plan in plans:
            try:
                jobs = plan.scraper.scrape()  # unfiltered
            except Exception as exc:  # noqa: BLE001 - one bad source shouldn't kill the run
                log.error("scrape failed for %s: %s", plan.label, exc)
                continue

            if dump_dir:
                dump_jobs(plan.label, jobs, dump_dir)

            fetched = len(jobs)
            if plan.post_filter:
                jobs = [job for job in jobs if job_matches(job, filters)]
            kept = len(jobs)

            new_here = 0
            for job in jobs:
                _, created = upsert_job(session, job)
                if created:
                    new_here += 1

            total_seen += kept
            total_new += new_here

            if plan.post_filter and fetched != kept:
                log.info(
                    "%s: %d fetched, %d kept after filters, %d new",
                    plan.label, fetched, kept, new_here,
                )
            else:
                log.info("%s: %d posting(s), %d new", plan.label, kept, new_here)

    log.info(
        "Done. %d posting(s) kept across all planned scrapes, %d new.",
        total_seen,
        total_new,
    )


if __name__ == "__main__":
    main()
