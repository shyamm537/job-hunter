"""Check whether individual job posting URLs are still live.

Complements validate.py: that module checks whether *board tokens* resolve;
this one checks whether specific *postings* already in the database are still
reachable (i.e. the role probably hasn't been closed/removed).

Work comes from link_check_queue() in src/storage/database.py: postings that
are not marked dead and were neither checked here nor seen by a scrape in the
last --recheck-days days, never-checked first, then oldest-checked first.

Each posting gets one GET request and one of three outcomes:
    live     HTTP 200
    dead     HTTP 404 or 410
    unknown  anything else: blocked, rate-limited, server error, timeout, or
             one of the two unverified page-text signals (see CLOSED_PHRASES)

Only "dead" is ever marked dead.

Without --mark-dead the run is a dry run: it logs what it found and writes
nothing to the database, so the same postings are queued next time. With
--mark-dead every posting that was attempted gets last_checked_at, and the
dead ones get dead_at + dead_reason. Results are committed every --batch-size
postings, so Ctrl-C keeps the progress made so far. At the end of a
--mark-dead run, dead postings are moved to the archive
(src/storage/archive.py).

Usage:
    python -m src.ingestion.check_links                   # dry run, up to 200 postings
    python -m src.ingestion.check_links --mark-dead       # record results in the database
    python -m src.ingestion.check_links --mark-dead --limit 50
    python -m src.ingestion.check_links --limit 0         # no cap: the whole queue
    python -m src.ingestion.check_links --recheck-days 14
    python -m src.ingestion.check_links --mark-dead --batch-size 10
    python -m src.ingestion.check_links --dead-out dead_links.txt
"""
import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from curl_cffi import requests

from src.config import ConfigError, load_config
from src.logging_config import setup_logging
from src.storage.archive import archive_dead_jobs, configure_archive, get_archive_session
from src.storage.database import (
    LinkCheckItem,
    count_link_check_queue,
    get_session,
    init_db,
    link_check_queue,
    mark_checked,
    mark_dead,
    set_database_url,
)

log = logging.getLogger("jobhunter.check_links")

TIMEOUT = 10  # seconds per request
DELAY = 0.5  # polite pause between network requests

DEFAULT_LIMIT = 200  # postings per run (--limit)
DEFAULT_RECHECK_DAYS = 7  # leave a posting alone this long after a check or a scrape
DEFAULT_BATCH_SIZE = 25  # postings between commits (--batch-size)

# Per-host rules will replace the two "unverified" cases below once real
# responses have been sampled (TODO.md step 4). Until then both are guesses, so
# classify() reports them as "unknown": never live, and never dead. Please
# don't "fix" either of them into dead before that calibration has been done.

# Unverified: the belief is that aggregators (Adzuna) serve a usable job page
# with a 404 status, recognisable by its apply control.
APPLY_PHRASE = "apply for this job"

# Unverified: phrases thought to indicate a "soft 404" (HTTP 200, but the role
# is gone). Matched against the whole page, so they can also false-positive.
CLOSED_PHRASES = (
    "no longer accepting applications",
    "this position has been filled",
    "this job is no longer available",
    "posting has closed",
    "job not found",
)


@dataclass
class LinkResult:
    job_id: int
    label: str  # "Company — Title" for readable logs
    url: str
    status: str  # "live" | "dead" | "unknown"
    reason: str  # short explanation, e.g. "http 404"


def classify(status_code: int, body: str) -> Tuple[str, str]:
    """Turn one HTTP response into (status, reason).

    Only a plain 404/410 is "dead". Anything that isn't clearly live or dead
    is "unknown", which is never marked dead.
    """
    body = (body or "").lower()

    if status_code in (404, 410):
        if APPLY_PHRASE in body:
            return "unknown", f"http {status_code} but page has '{APPLY_PHRASE}' (unverified)"
        return "dead", f"http {status_code}"
    if status_code != 200:
        return "unknown", f"http {status_code}"

    for phrase in CLOSED_PHRASES:
        if phrase in body:
            return "unknown", f"closed phrase '{phrase}' (unverified)"
    return "live", "http 200"


def check_link(url: str, http) -> Tuple[str, str]:
    """GET one URL and return (status, reason).

    `http` is the curl_cffi session, or anything else with a matching
    .get(url, timeout=..., allow_redirects=...) method (the tests use a fake).
    A request that fails outright is "unknown", with the exception's class
    name as the reason.
    """
    try:
        resp = http.get(url, timeout=TIMEOUT, allow_redirects=True)
        status_code, body = resp.status_code, resp.text
    except Exception as exc:  # curl_cffi raises RequestsError, not RequestException
        return "unknown", type(exc).__name__
    return classify(status_code, body)


def save_batch(session, batch: Sequence[LinkResult]) -> int:
    """Write one batch of results and commit. Returns how many were marked dead.

    Every posting in the batch counts as checked, including the unknowns and
    the failed requests, so one unreachable host can't sit at the head of the
    queue forever.
    """
    mark_checked(session, [r.job_id for r in batch])

    dead_ids_by_reason: Dict[str, List[int]] = {}
    for r in batch:
        if r.status == "dead":
            dead_ids_by_reason.setdefault(r.reason, []).append(r.job_id)
    marked = 0
    for reason, job_ids in dead_ids_by_reason.items():
        marked += mark_dead(session, job_ids, reason)

    session.commit()
    return marked


def check_links(
    items: Sequence[LinkCheckItem],
    http,
    session=None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Tuple[List[LinkResult], int]:
    """Check each queued posting in turn. Returns (results, number marked dead).

    Pass a database `session` to record the results, committed every
    `batch_size` postings. With session=None (a dry run) nothing is written.
    Ctrl-C stops early: whatever has been checked so far is still saved and
    returned.
    """
    results: List[LinkResult] = []
    batch: List[LinkResult] = []  # checked but not yet saved
    marked = 0
    seen_urls: Dict[str, Tuple[str, str]] = {}  # url -> (status, reason)

    try:
        for item in items:
            url = item.url.strip()
            if url not in seen_urls:
                # A URL shared by several postings is requested only once.
                if seen_urls:
                    time.sleep(DELAY)
                seen_urls[url] = check_link(url, http)
            status, reason = seen_urls[url]

            result = LinkResult(item.id, f"{item.company} — {item.title}", url, status, reason)
            results.append(result)
            batch.append(result)
            log.info("%-40s %-7s %s", result.label[:40], status, reason)

            if session is not None and len(batch) >= batch_size:
                marked += save_batch(session, batch)
                batch = []
    except KeyboardInterrupt:
        log.warning("Interrupted after %d posting(s); keeping the progress so far.", len(results))
        if session is not None:
            session.rollback()  # drop a half-written batch, if any; it is saved again below

    if session is not None and batch:
        marked += save_batch(session, batch)
    return results, marked


def write_dead_file(path: str, dead: Sequence[LinkResult]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Dead posting links — candidates for pruning.\n")
        for r in dead:
            fh.write(f"{r.job_id}\t{r.url}\t{r.reason}\n")
    log.info("Wrote %d dead link(s) to %s", len(dead), path)


def main(argv: Optional[List[str]] = None, http=None) -> None:
    """Command-line entry point.

    `http` exists for the tests: they pass a fake session so nothing touches
    the network. Left as None, a real curl_cffi session is created.
    """
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m src.ingestion.check_links",
        description="Check whether stored job posting URLs are still live.",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, metavar="N",
        help="most postings to check in this run (default %(default)s; 0 = no limit)",
    )
    parser.add_argument(
        "--recheck-days", type=int, default=DEFAULT_RECHECK_DAYS, metavar="N",
        help="skip postings checked or seen by a scrape in the last N days "
             "(default %(default)s)",
    )
    parser.add_argument(
        "--mark-dead", action="store_true",
        help="write the results to the database; without this flag the run is "
             "a dry run and writes nothing",
    )
    parser.add_argument(
        "--dead-out", metavar="FILE",
        help="write the dead postings (id, URL, reason; tab-separated) to FILE",
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, metavar="N",
        help="with --mark-dead, commit after every N checked postings "
             "(default %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    set_database_url(config.database.url)
    init_db()

    with get_session() as session:
        queued = count_link_check_queue(session, recheck_days=args.recheck_days)
        items = link_check_queue(
            session,
            limit=args.limit if args.limit > 0 else None,
            recheck_days=args.recheck_days,
        )
        if not items:
            log.info(
                "Nothing due for a link check: no live posting has gone "
                "%d day(s) without being checked or seen.",
                args.recheck_days,
            )
            return

        log.info(
            "Checking %d of %d queued posting link(s)%s...",
            len(items), queued, "" if args.mark_dead else " (dry run)",
        )
        if http is None:
            http = requests.Session(impersonate="chrome")
        results, marked = check_links(
            items,
            http,
            session=session if args.mark_dead else None,
            batch_size=args.batch_size,
        )
        remaining = count_link_check_queue(session, recheck_days=args.recheck_days)
        archived = 0
        if args.mark_dead:
            try:
                configure_archive(config.database)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                raise SystemExit(1)
            with get_archive_session() as archive_session:
                archived = archive_dead_jobs(session, archive_session)

    live = [r for r in results if r.status == "live"]
    dead = [r for r in results if r.status == "dead"]
    unknown = [r for r in results if r.status == "unknown"]
    log.info(
        "Done: checked %d of %d queued: %d live, %d dead, %d unknown. "
        "Marked %d dead%s. %d still queued.%s",
        len(results), queued, len(live), len(dead), len(unknown), marked,
        "" if args.mark_dead else " (dry run, nothing written; use --mark-dead)",
        remaining,
        f" Moved {archived} dead posting(s) to the archive." if archived else "",
    )

    if args.dead_out and dead:
        write_dead_file(args.dead_out, dead)


if __name__ == "__main__":
    main()
