"""Check whether individual job posting URLs are still live.

Complements validate.py: that module checks whether *board tokens* resolve;
this one checks whether specific *postings* already in the database are still
reachable (i.e. the role probably hasn't been closed/removed).
Only checks if not already marked dead in the db
Usage:
    python -m src.ingestion.check_links
    python -m src.ingestion.check_links --limit 50
    python -m src.ingestion.check_links --dead-out dead_links.txt
    python -m src.ingestion.check_links --mark-dead
    python -m src.ingestion.check_links --mark-dead --limit 20
"""
from datetime import datetime, timezone
import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import List, Optional
from curl_cffi import requests

from sqlmodel import select

from src.config import ConfigError, load_config
from src.logging_config import setup_logging
from src.storage.database import get_session, init_db, set_database_url
from src.storage.models import JobPost

log = logging.getLogger("jobhunter.check_links")

TIMEOUT = 10  # seconds per request
DELAY = 0.5  # polite pause between requests
HEADERS = {
    # Some ATS/career sites reject requests with no User-Agent.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        )
}

# Phrases that indicate a "soft 404": HTTP 200 but the role is gone.
# Lever and many career pages do this instead of returning 404/410.
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
    status_code: Optional[int]  # None if the request itself failed
    live: bool
    error: Optional[str]  # short reason if not live / unknown

    @property
    def status(self) -> str:
        """live = reachable and open; dead = 404/410/closed page;
        unknown = temporary-looking failure (5xx, 429, timeout)."""
        if self.live:
            return "live"
        if self.status_code in (404, 410) or (
            self.error and self.error.startswith("closed:")
        ):
            return "dead"
        return "unknown"


def check_link(url: str, http: requests.Session) -> "tuple[Optional[int], bool, Optional[str]]":
    """Return (status_code, live, error) for one URL.

    HEAD first (cheap); fall back to GET when the server rejects HEAD.
    A 200 GET body is scanned for closed-role phrases (soft 404s).
    """
    try:
        resp = http.head(url, timeout=TIMEOUT, allow_redirects=True)
        # HEAD gives no body, and some servers treat HEAD oddly. Retry as GET on
        # rejection (403/405), on 404/410 (need the body for the aggregator
        # rescue check), and on 200 (need the body for the soft-404 scan).
        if resp.status_code in (403, 404, 405, 410, 200):
            resp = http.get(url, timeout=TIMEOUT, allow_redirects=True)
    except Exception as exc:  # curl_cffi raises RequestsError, not RequestException
        return None, False, type(exc).__name__

    code = resp.status_code
    body = (resp.text or "").lower()

    if code in (404, 410):
        # Aggregators (Adzuna) serve full job pages with a 404 status. 
        # An apply control means the page is still usable.
        if "apply for this job" in body:
            return code, True, "live-content-despite-404"
        return code, False, None
    if code != 200:
        return code, False, f"HTTP {code}"

    for phrase in CLOSED_PHRASES:
        if phrase in body:
            return code, False, f"closed: '{phrase}'"
    return code, True, None


def check_links(posts: List[JobPost]) -> List[LinkResult]:
    results: List[LinkResult] = []
    http = requests.Session(impersonate="chrome")
    seen_urls = {}  # url -> (status_code, live, error); dedupe across posts
    for post in posts:
        url = (post.url or "").strip()  # adjust field name if yours differs
        label = f"{post.company} — {getattr(post, 'title', post.job_board_id)}"
        if not url:
            results.append(
                LinkResult(post.id, label, url, None, False, "no url on record")
            )
            continue
        if url in seen_urls:
            code, live, error = seen_urls[url]
        else:
            code, live, error = check_link(url, http)
            seen_urls[url] = (code, live, error)
            time.sleep(DELAY)
        r = LinkResult(post.id, label, url, code, live, error)
        results.append(r)
        log.info(
            "%-40s %-7s %s%s",
            r.label[:40], r.status,
            f"HTTP {r.status_code}" if r.status_code else "-",
            f"  ({r.error})" if r.error else "",
        )
    return results


def main(argv: Optional[List[str]] = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m src.ingestion.check_links",
        description="Check whether stored job posting URLs are still live.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="cap how many postings to check (0 = all)",
    )
    parser.add_argument(
        "--dead-out",
        help="write the job IDs and URLs of dead links to this file",
    )
    parser.add_argument(
        "--mark-dead", action="store_true",
        help="set dead_at on postings whose links are dead",
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
        query = select(JobPost).where(JobPost.dead_at.is_(None)) #wont check rows which are already marked dead
        query = query.order_by(JobPost.id.desc())
        posts = session.exec(query).all() 
        
    if args.limit:
        posts = posts[: args.limit]
    if not posts:
        print("No postings in the database — run `make scrape` first.", file=sys.stderr)
        raise SystemExit(1)

    log.info("Checking %d posting link(s)...", len(posts))
    results = check_links(posts)

    live = [r for r in results if r.status == "live"]
    dead = [r for r in results if r.status == "dead"]
    unknown = [r for r in results if r.status == "unknown"]
    log.info(
        "Done: %d live, %d dead, %d unknown.", len(live), len(dead), len(unknown)
    )

    if args.dead_out and dead:
        with open(args.dead_out, "w", encoding="utf-8") as fh:
            fh.write("# Dead posting links — candidates for pruning.\n")
            for r in dead:
                fh.write(f"{r.job_id}\t{r.url}\t{r.error or r.status_code}\n")
        log.info("Wrote %d dead link(s) to %s", len(dead), args.dead_out)

    if args.mark_dead and dead:
        dead_ids = [r.job_id for r in dead]
        with get_session() as session:
            rows = session.exec(
                select(JobPost).where(JobPost.id.in_(dead_ids))
            ).all()
            now = datetime.now(timezone.utc)
            for row in rows:
                row.dead_at = now
                session.add(row)
            session.commit()
        log.info("Marked %d posting(s) dead.", len(rows))

if __name__ == "__main__":
    main()