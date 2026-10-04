"""Entry point for `make scrape`.

Reads config.yaml, plans concrete scrapes from (sources x filters), runs each,
and writes new postings into the database. The sources are the pinned ones
(config.yaml and sources.txt) plus, when `companies_file` is set, the live
boards in the board map that `make resolve` writes. Run this, then `make process`
to generate materials, then `make app` to view everything.

Adzuna sources expand into one search per (title, location); ATS boards
(Greenhouse, Lever, Ashby, Workable, Workday) are scraped whole and then filtered by the same
titles and locations. Each planned scrape runs independently — one failing
(network error, bad token) is logged and skipped, not fatal.

A board scrape returns the board's whole list, so after it the board's stored
rows are compared with it (reconcile_board): rows the board no longer lists
are marked dead ("gone from board"), the rest are marked seen. A failed or
empty scrape skips that step. At the end, dead postings are moved to the
archive (src/storage/archive.py), and an archived posting a scrape returns
again is restored from it.

Set JOBHUNTER_DUMP_DIR to capture each scrape's *unfiltered* output to JSON
(see src/ingestion/capture.py) — handy when a filter is eating everything, and
as a source of fixtures for tests.
"""

import logging
import sys
from typing import List, Tuple

from sqlmodel import Session

from src.config import Config, ConfigError, load_config, source_key
from src.ingestion.board_map import (
    BoardKey,
    Outcome,
    apply_scrape_outcomes,
    load_map,
    save_map,
)
from src.ingestion.capture import dump_jobs, resolve_dump_dir
from src.ingestion.filtering import job_matches
from src.ingestion.planner import plan_scrapes
from src.logging_config import setup_logging
from src.storage.archive import archive_dead_jobs, configure_archive, get_archive_session
from src.storage.database import (
    get_session,
    init_db,
    reconcile_board,
    record_scrape_run,
    retire_board,
    set_database_url,
    upsert_job,
    utcnow,
)

log = logging.getLogger("jobhunter.scrape")


def update_board_map(
    config: Config, session: Session, outcomes: List[Tuple[BoardKey, Outcome]]
) -> None:
    """Tell the board map how each board scrape ended (plans/05, stage 2).

    Only boards that came from the map are affected (apply_scrape_outcomes
    skips the pinned ones). A mapped board that gave a definite "not here" on
    `resolve.stale_after` separate days becomes `gone`: its stored rows are
    retired here, so the archive step that follows moves them out, and its
    company is flagged for the next `make resolve`. Does nothing when
    `companies_file` is unset.
    """
    if not config.companies_file or not outcomes:
        return
    try:
        board_map = load_map(config.board_map_file)
        pinned = {source_key(source) for source in config.pinned_sources}
        updated, retire = apply_scrape_outcomes(
            board_map, outcomes, config.resolve.stale_after, utcnow(), pinned
        )
    except ConfigError as exc:
        log.error("board map not updated: %s", exc)
        return

    for source in retire:
        kind, token = source_key(source)
        retired = retire_board(session, kind, token)
        log.warning(
            "%s[%s] was not there on %d separate days: marked gone in the board "
            "map, %d stored posting(s) retired. Run `make resolve` to look for the "
            "company on other boards.",
            kind, token, config.resolve.stale_after, retired,
        )
    session.commit()  # the rows first: if saving the map fails, they are not stranded live
    if updated != board_map:
        save_map(updated, config.board_map_file)


def main() -> None:
    setup_logging()

    try:
        config = load_config()
        sources = config.resolved_sources
        if not sources:
            hint = ""
            if config.companies_file:
                hint = (
                    f" Or list companies in {config.companies_file} and run "
                    "`make resolve` first: it finds their boards and records "
                    f"them in {config.board_map_file}."
                )
            raise ConfigError(
                "No sources configured: add at least one active line to "
                f"{config.sources_file or 'sources'} (e.g. 'adzuna au' or "
                f"'greenhouse <board>'). See sources.txt.example.{hint}"
            )
        filters = config.resolved_filters
        plans = plan_scrapes(sources, filters, adzuna_auth=config.adzuna_auth)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    set_database_url(config.database.url)
    init_db()
    try:
        configure_archive(config.database)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    dump_dir = resolve_dump_dir()
    if dump_dir:
        log.info("capturing unfiltered output to %s", dump_dir)

    total_seen = 0
    total_new = 0
    total_gone = 0
    # How each board scrape ended, for the board map (see update_board_map).
    outcomes: List[Tuple[BoardKey, Outcome]] = []

    with get_session() as session, get_archive_session() as archive_session:
        # Jobs first stored from here on are "new" in the dashboard.
        record_scrape_run(session)
        for plan in plans:
            try:
                jobs = plan.scraper.scrape()  # unfiltered
            except Exception as exc:  # noqa: BLE001 - one bad source shouldn't kill the run
                log.error("scrape failed for %s: %s", plan.label, exc)
                if plan.board:
                    outcomes.append((plan.board, exc))
                continue

            if dump_dir:
                dump_jobs(plan.label, jobs, dump_dir)

            unfiltered = jobs
            fetched = len(jobs)
            if plan.board:
                outcomes.append((plan.board, fetched))
            if plan.post_filter:
                jobs = [
                    job for job in jobs
                    if job_matches(job, filters, check_location=plan.check_location)
                ]
            kept = len(jobs)

            new_here = 0
            for job in jobs:
                _, created = upsert_job(session, job, archive_session)
                if created:
                    new_here += 1

            gone_here = 0
            if plan.board and fetched:
                source, token = plan.board
                _, _, gone_here = reconcile_board(
                    session, source, token, {job.job_board_id for job in unfiltered}
                )
                session.commit()
            elif plan.board:
                log.warning("%s returned 0 jobs; not reconciling the board", plan.label)

            total_seen += kept
            total_new += new_here
            total_gone += gone_here

            gone_note = f", {gone_here} gone from board" if gone_here else ""
            if plan.post_filter and fetched != kept:
                log.info(
                    "%s: %d fetched, %d kept after filters, %d new%s",
                    plan.label, fetched, kept, new_here, gone_note,
                )
            else:
                log.info("%s: %d posting(s), %d new%s", plan.label, kept, new_here, gone_note)

        update_board_map(config, session, outcomes)
        archived = archive_dead_jobs(session, archive_session)

    log.info(
        "Done. %d posting(s) kept across all planned scrapes, %d new, "
        "%d gone from their board, %d dead posting(s) moved to the archive.",
        total_seen,
        total_new,
        total_gone,
        archived,
    )


if __name__ == "__main__":
    main()
