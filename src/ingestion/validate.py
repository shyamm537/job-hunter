"""Validate ATS board tokens against their live public APIs.

The problem: a `sources.txt` accumulates board tokens (`greenhouse stripe`,
`lever metabase`, pasted careers URLs), but tokens go stale — companies switch
ATS, rename their board, or wind down. "Valid" means the token still resolves to
a live public board. This module is how you check, and it's the durable answer
to "give me a list of valid boards": curate candidates, then validate.

It reuses the existing machinery rather than re-implementing any of it:
`plan_scrapes` turns a source into the right scraper, the scraper hits the same
public API `make scrape` uses, and `job_matches` reports how many current
postings match your filters. A dead token surfaces as the scraper raising
(usually a 404 from `http_util.get_json`).

Adzuna is a search engine, not a board, so it's reported as "live (search
source)" without a network call — there's nothing to validate.

Run it (`make validate`, or directly):

    python -m src.ingestion.validate                  # check config's sources
    python -m src.ingestion.validate cands.txt --out sources.txt
    python -m src.ingestion.validate cands.txt --out sources.txt --require-match

Network note: this hits live ATS APIs, exactly like `make scrape`. It is
in-scope (public, unauthenticated endpoints) per docs/scrapers.md.
"""

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from src.config import (
    ConfigError,
    Filters,
    Source,
    load_config,
    load_sources_file,
    source_key,
    source_to_line,
)
from src.ingestion.filtering import job_matches
from src.ingestion.planner import plan_scrapes
from src.logging_config import setup_logging

log = logging.getLogger("jobhunter.validate")

SAMPLE_COUNT = 3


@dataclass
class ValidationResult:
    source: Source
    label: str
    live: bool
    total: int  # postings fetched (0 if dead or genuinely empty)
    matched: int  # of those, how many match the filters
    error: Optional[str]  # short reason if not live
    # (title, location) of the first few postings, so a wrong company is easy to
    # spot in the resolve report.
    samples: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def status(self) -> str:
        """match = live with >=1 matching role; live = live but none matching
        right now; dead = token didn't resolve."""
        if not self.live:
            return "dead"
        return "match" if self.matched > 0 else "live"


def validate_source(
    source: Source, filters: Filters, details: bool = True
) -> ValidationResult:
    """Hit one board's live API and report liveness + filter matches.

    A scraper that raises (e.g. a 404 for a stale token) is reported dead, not
    propagated — validating a list shouldn't abort on the first bad token.

    With `details=False` the scraper is planned with no title filters, so a
    source that fetches one description per title match (Workday) fetches none.
    Liveness, the posting count and the matches (counted with the real filters)
    are the same; only the descriptions are missing. `make resolve` uses this.
    """
    if source.type == "adzuna":
        # A search engine, not a board — nothing to validate here. (Bad Adzuna
        # creds fail at scrape time, not as a stale-token check.)
        return ValidationResult(
            source, source.type, live=True, total=0, matched=0, error=None
        )

    plan_filters = filters
    if not details:
        plan_filters = filters.model_copy(update={"titles": [], "also_match_titles": []})
    planned = plan_scrapes([source], plan_filters)[0]
    try:
        jobs = planned.scraper.scrape()
    except Exception as exc:  # noqa: BLE001 — any failure means "not usable now"
        return ValidationResult(
            source, planned.label, live=False, total=0, matched=0,
            error=type(exc).__name__ + ": " + str(exc)[:160],
        )
    matched = sum(1 for j in jobs if job_matches(j, filters))
    return ValidationResult(
        source, planned.label, live=True, total=len(jobs), matched=matched, error=None,
        samples=[(j.title, j.location) for j in jobs[:SAMPLE_COUNT]],
    )


def validate_sources(sources: List[Source], filters: Filters) -> List[ValidationResult]:
    results: List[ValidationResult] = []
    for src in sources:
        r = validate_source(src, filters)
        results.append(r)
        log.info(
            "%-26s %-5s total=%d match=%d%s",
            r.label, r.status, r.total, r.matched,
            f"  ({r.error})" if r.error else "",
        )
    return results


def _kept(results: List[ValidationResult], require_match: bool) -> List[ValidationResult]:
    """Boards to write out: live ones, or (with --require-match) only those with
    at least one currently-matching role. Search sources always kept."""
    out = []
    for r in results:
        if r.source.type == "adzuna" or (
            r.live and (r.matched > 0 or not require_match)
        ):
            out.append(r)
    return out


def main(argv: Optional[List[str]] = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m src.ingestion.validate",
        description="Validate ATS board tokens against their live public APIs.",
    )
    parser.add_argument(
        "input", nargs="?",
        help="sources-file of candidate boards to validate "
        "(default: the sources resolved from config.yaml)",
    )
    parser.add_argument(
        "--out", help="write confirmed-live boards to this sources file"
    )
    parser.add_argument(
        "--require-match", action="store_true",
        help="only keep boards with >=1 posting matching your filters "
        "(default: keep every live board)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
        filters = config.resolved_filters
        sources = (
            load_sources_file(args.input) if args.input else config.resolved_sources
        )
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    if not sources:
        print("No sources to validate.", file=sys.stderr)
        raise SystemExit(1)

    log.info("Validating %d source(s)...", len(sources))
    results = validate_sources(sources, filters)

    live = [r for r in results if r.live and r.source.type != "adzuna"]
    match = [r for r in live if r.matched > 0]
    dead = [r for r in results if not r.live]
    log.info(
        "Done: %d board(s) live, %d with a current match, %d dead.",
        len(live), len(match), len(dead),
    )
    if dead:
        log.info("Dead/unreachable: %s", ", ".join(r.label for r in dead))

    if args.out:
        kept = _kept(results, args.require_match)
        if not args.input:
            # Checking the configured sources: write the pinned ones only. The
            # mapped boards are generated state (data/board_map.yaml); writing
            # them out would copy them into sources.txt and pin them.
            pinned = {source_key(s) for s in config.pinned_sources}
            kept = [r for r in kept if source_key(r.source) in pinned]
        lines = [
            "# Generated by `python -m src.ingestion.validate`.",
            "# Confirmed-live boards" + (" with a current match." if args.require_match else "."),
            "",
        ]
        lines += [source_to_line(r.source) for r in kept]
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        log.info("Wrote %d source(s) to %s", len(kept), args.out)


if __name__ == "__main__":
    main()
