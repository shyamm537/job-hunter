"""Entry point for `make scrape`.

Reads config.yaml, runs every configured scraper, and writes new postings
into the database. Run this, then `make process` to generate materials for
whatever it found, then `make app` to view everything.

Config can describe one search the legacy way (a single `search:` block) or
many via a `sources:` list mixing SEEK and Greenhouse — see
docs/configuration.md. Each source runs independently; one source failing
(network error, bad board token) is logged and skipped rather than aborting
the whole run.
"""

import logging
import sys

from src.config import ConfigError, load_config
from src.ingestion.factory import build_scraper
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

    sources = config.resolved_sources
    total_seen = 0
    total_new = 0

    with get_session() as session:
        for source in sources:
            scraper = build_scraper(source)
            try:
                jobs = scraper.scrape()
            except Exception as exc:  # noqa: BLE001 - one bad source shouldn't kill the run
                log.error("scrape failed for source %s: %s", scraper.source_name, exc)
                continue

            new_here = 0
            for job in jobs:
                _, created = upsert_job(session, job)
                if created:
                    new_here += 1

            total_seen += len(jobs)
            total_new += new_here
            log.info(
                "%s: %d posting(s), %d new", scraper.source_name, len(jobs), new_here
            )

    log.info(
        "Done. %d posting(s) seen across all sources, %d new.", total_seen, total_new
    )


if __name__ == "__main__":
    main()
