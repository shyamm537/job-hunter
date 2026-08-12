"""Discover candidate ATS boards from companies you're already seeing.

A months-long hunt shouldn't be capped at a static board list, but auto-adding
*random* boards is the firehose `TODO.md` rejects — it grows noise, not signal.
This grows the list from companies that ALREADY appear in your search-source
results (SEEK/Adzuna) for your roles, so every candidate is relevant by construction. It then validates
each against the live ATS APIs and writes the confirmed-live ones as **commented
proposals** for you to approve (uncomment) — never auto-adding. See
docs/board-discovery.md.

This runs AFTER `make scrape`, not before: it mines the SEEK postings scrape
already collected. It mines SEEK specifically because that's the source that
surfaces companies you don't already have a board for — mining your Greenhouse/
Lever rows would just re-derive tokens already in sources.txt.

What it can and can't do, honestly: it guesses a board token from the company
name (slugified) and checks Greenhouse/Lever/Ashby. When the token equals the
name slug it works; when it doesn't (or the company is on Workday, whose
data-center subdomain isn't derivable from a name — see docs/workday.md) it
misses. Every proposal is validated, so there are no false positives; there are
plenty of false negatives. It finds the easy wins, not everything.

Pure helpers (`slug_variants`, `candidates_for_company`, `new_candidates`) are
unit-tested; the network step reuses `validate_source` (already covered).
"""

import argparse
import logging
import re
import sys
from typing import List, Optional, Set

from sqlmodel import select

from src.config import (
    AshbySource,
    ConfigError,
    Filters,
    GreenhouseSource,
    LeverSource,
    Source,
    load_config,
    source_to_line,
)
from src.ingestion.validate import validate_source
from src.logging_config import setup_logging
from src.storage.database import get_session, init_db, set_database_url
from src.storage.models import JobPost

log = logging.getLogger("jobhunter.discover")

# Legal-entity / filler words to drop before slugifying a company name.
_STOPWORDS = {
    "the", "pty", "ltd", "limited", "inc", "incorporated", "llc", "llp",
    "corp", "corporation", "co", "company", "group", "holdings", "plc",
    "gmbh", "ag", "technologies", "technology", "labs", "global",
}

ATS_TOKEN_FIELD = {"greenhouse": "board", "lever": "company", "ashby": "org"}


def slug_variants(name: str) -> List[str]:
    """Company name → a small set of plausible board-token slugs.

    "Acme Pty Ltd"  -> ["acme"]
    "Foo Bar Labs"  -> ["foobar", "foo-bar"]
    Conservative on purpose: two variants at most keeps the validation calls
    bounded. Returns [] for an empty/uninformative name.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if w]
    words = [w for w in words if w not in _STOPWORDS]
    if not words:
        return []
    joined = "".join(words)
    variants = [joined]
    if len(words) > 1:
        variants.append("-".join(words))
    # De-dup, preserve order.
    seen: List[str] = []
    for v in variants:
        if v and v not in seen:
            seen.append(v)
    return seen


def candidates_for_company(name: str) -> List[Source]:
    """Every (slug × ATS) candidate Source for a company name.

    Workday is intentionally excluded: its data-center subdomain isn't derivable
    from a name, so it can only be added from a pasted URL (see docs/workday.md).
    """
    out: List[Source] = []
    for slug in slug_variants(name):
        out.append(GreenhouseSource(type="greenhouse", board=slug))
        out.append(LeverSource(type="lever", company=slug))
        out.append(AshbySource(type="ashby", org=slug))
    return out


def _token_of(source: Source) -> str:
    return getattr(source, ATS_TOKEN_FIELD[source.type])


def existing_tokens(sources: List[Source]) -> Set[str]:
    """(type, token) pairs already configured, so we don't re-propose them."""
    return {
        f"{s.type}:{_token_of(s)}" for s in sources if s.type in ATS_TOKEN_FIELD
    }


def new_candidates(names: List[str], already: Set[str]) -> List[Source]:
    """Candidate Sources for these company names, minus ones already configured
    and de-duplicated across companies."""
    out: List[Source] = []
    seen = set(already)
    for name in names:
        for cand in candidates_for_company(name):
            key = f"{cand.type}:{_token_of(cand)}"
            if key not in seen:
                seen.add(key)
                out.append(cand)
    return out


def search_companies(session) -> List[str]:
    """Distinct company names from search-source postings already in the DB.

    Search sources (SEEK, Adzuna) surface companies you don't already have a
    board for; ATS rows are companies you do, so they're excluded. Rows are
    identified by their `seek-` / `adzuna-` job_board_id prefix."""
    rows = session.exec(
        select(JobPost.company).where(
            JobPost.job_board_id.like("seek-%")
            | JobPost.job_board_id.like("adzuna-%")
        )
    ).all()
    seen: List[str] = []
    for c in rows:
        c = (c or "").strip()
        if c and c not in seen:
            seen.append(c)
    return seen


def discover(names: List[str], existing: List[Source], filters: Filters) -> List:
    """Validate candidate boards for these companies; return the live results.

    Returns the ValidationResult list (live only), so the caller can show match
    counts and write proposals."""
    candidates = new_candidates(names, existing_tokens(existing))
    log.info(
        "%d compan(ies) → %d candidate board(s) to check.",
        len(names), len(candidates),
    )
    live = []
    for cand in candidates:
        r = validate_source(cand, filters)
        if r.live:
            live.append(r)
            log.info("  FOUND %-26s %s (match=%d)", r.label, r.status, r.matched)
    return live


def _proposal_lines(live_results: List) -> List[str]:
    lines = [
        "# Discovered boards (proposals) — review and uncomment to approve.",
        "# Each was confirmed live; 'match' = has a role matching your filters now.",
        "",
    ]
    # Matches first (most interesting), then live-but-no-match.
    for r in sorted(live_results, key=lambda r: (r.matched == 0, r.label)):
        note = f"match: {r.matched} role(s)" if r.matched else "live, no current match"
        lines.append(f"# {source_to_line(r.source)}    # {note}")
    return lines


def main(argv: Optional[List[str]] = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m src.ingestion.discover",
        description="Propose ATS boards from companies in your SEEK results.",
    )
    parser.add_argument(
        "--out", default="sources.discovered.txt",
        help="file to write commented proposals to (default: sources.discovered.txt)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="cap how many companies to check (0 = all; useful to bound calls)",
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
        names = search_companies(session)
        total = len(session.exec(select(JobPost.id)).all())
    if args.limit:
        names = names[: args.limit]
    if not names:
        if total == 0:
            msg = "No postings in the database yet — run `make scrape` first."
        else:
            # Common case: the DB has ATS rows but no SEEK rows. Discovery mines
            # SEEK results (the source that surfaces companies you don't already
            # have a board for), so ATS-only data gives it nothing to work with.
            msg = (
                f"Found {total} posting(s), but none from a search source "
                "(SEEK/Adzuna). Discovery mines those for companies you don't "
                "already have a board for. SEEK's public RSS is dead; set up "
                "Adzuna (an 'adzuna:' creds block + an 'adzuna' source) so there's "
                "a working feeder, then `make scrape`. See docs/board-discovery.md."
            )
        print(msg, file=sys.stderr)
        raise SystemExit(1)

    live = discover(names, config.resolved_sources, config.resolved_filters)
    log.info("%d live board(s) discovered.", len(live))

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_proposal_lines(live)) + "\n")
    log.info("Wrote proposals to %s — review and uncomment to approve.", args.out)


if __name__ == "__main__":
    main()
