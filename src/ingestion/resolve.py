"""Resolve companies to boards: `python -m src.ingestion.resolve` (`make resolve`).

companies.txt lists the employers you want to follow, with no board named. This
checks each company against every board type in `RESOLVABLE` (Greenhouse, Lever,
Ashby, Workable), records where it found a live board in the board map
(data/board_map.yaml), and prints a report. `make scrape` then reads the map.
See docs/board-discovery.md and plans/05-company-board-map.md.

A company is checked when it is *due* (`board_map.is_due`): new to the map,
flagged, or last checked more than `resolve.recheck_days` ago. Every due company
is checked against every board and all live boards are kept; stopping at the
first hit would be wrong, because a token can belong to another company on one
board while the real one sits on another.

What it proves, honestly: that a board with that token exists, not whose it is.
Every new board is printed with sample postings so a wrong company stands out,
and a block line (`- ashby amp`) in companies.txt removes a wrong match for good.

Network: the same public list endpoints `make validate` and `make discover` use,
one request per candidate with a pause between (most answer a quick 404).
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from src.config import (
    RESOLVABLE,
    Company,
    Config,
    ConfigError,
    Source,
    load_companies_file,
    load_config,
    source_key,
    source_to_line,
)
from src.ingestion.board_map import (
    BoardMap,
    CheckResult,
    drop_missing_companies,
    is_due,
    load_map,
    merge_check,
    save_map,
)
from src.ingestion.discover import slug_variants
from src.ingestion.http_util import REQUEST_DELAY
from src.ingestion.validate import ValidationResult, validate_source
from src.logging_config import setup_logging

log = logging.getLogger("jobhunter.resolve")


@dataclass
class Candidates:
    to_check: List[Source] = field(default_factory=list)
    pinned: List[Source] = field(default_factory=list)  # already in sources.txt


@dataclass
class ReportRow:
    company: str
    line: str
    status: str  # live / empty / gone / pinned / dead
    postings: int = 0
    matched: int = 0
    flag: str = ""  # NEW / MOVED / BACK / EMPTY / GONE / NOT FOUND
    samples: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class ResolveOutcome:
    board_map: BoardMap
    rows: List[ReportRow]
    checked: List[str]  # names of the companies checked
    dropped: List[str]  # names removed from the map (no longer in companies.txt)


def candidates_for(
    company: Company,
    boards: List[str],
    blocked: List[Source],
    pinned: List[Source],
) -> Candidates:
    """Every board to try for a company.

    Known boards (careers URLs) come first, then every enabled board type x
    every token: the aliases as written, then the slugs of the name. De-duplicated
    by `source_key()`, so an alias keeps its case when a slug differs only by it.
    Blocked boards are dropped; boards already pinned in sources.txt are listed
    separately and not checked.
    """
    tokens: List[str] = list(company.aliases) + slug_variants(company.name)
    ordered: List[Source] = list(company.known)
    for board in boards:
        for token in tokens:
            ordered.append(RESOLVABLE[board](token))

    blocked_keys = {source_key(s) for s in blocked}
    pinned_keys = {source_key(s) for s in pinned}
    result = Candidates()
    seen = set()
    for source in ordered:
        key = source_key(source)
        if key in seen or key in blocked_keys:
            continue
        seen.add(key)
        (result.pinned if key in pinned_keys else result.to_check).append(source)
    return result


def select_due(
    companies: List[Company],
    board_map: BoardMap,
    now: datetime,
    recheck_days: int,
    check_all: bool = False,
    only: Optional[str] = None,
    limit: int = 0,
) -> List[Company]:
    """The companies to check this run, in file order. `only` (a name) and
    `check_all` skip the due test; `limit` caps how many are returned."""
    if only is not None:
        wanted = only.strip().lower()
        picked = [c for c in companies if c.name.lower() == wanted]
    elif check_all:
        picked = list(companies)
    else:
        picked = [
            c for c in companies
            if is_due(board_map.find_company(c.name), now, recheck_days)
        ]
    return picked[:limit] if limit else picked


def _flag(before: Optional[str], after: str, company_has_gone: bool) -> str:
    """The report flag for one board, from its status before and after a check."""
    if after == "gone":
        return "GONE" if before in ("live", "empty") else ""
    if before is None:
        if after == "empty":
            return "EMPTY"
        return "MOVED" if company_has_gone else "NEW"
    if before == "gone" and after == "live":
        return "BACK"
    if before == "live" and after == "empty":
        return "EMPTY"
    return ""


def resolve_company(
    board_map: BoardMap,
    company: Company,
    candidates: Candidates,
    check: Callable[[Source], ValidationResult],
    now: datetime,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[BoardMap, List[ReportRow]]:
    """Check one company's candidates and fold the answers into the map.

    Returns the new map and the report rows for this company: boards found or
    changed, pinned boards that were skipped, and a known (URL) board that did
    not answer. Dead guesses are not listed; there are dozens per run."""
    validations: List[ValidationResult] = []
    for index, source in enumerate(candidates.to_check):
        if index:
            sleep(REQUEST_DELAY)
        validations.append(check(source))

    results = [
        CheckResult(v.source, v.live, v.total, v.matched) for v in validations
    ]
    before = board_map.find_company(company.name)
    updated = merge_check(board_map, company.name, results, now)
    after = updated.find_company(company.name)
    has_gone = any(b.status == "gone" for b in after.boards)
    known_keys = {source_key(s) for s in company.known}

    rows: List[ReportRow] = []
    for v in validations:
        board_before = before.find_board(v.source) if before else None
        board_after = after.find_board(v.source)
        line = source_to_line(v.source)
        if board_after is None:
            if source_key(v.source) in known_keys:
                rows.append(ReportRow(company.name, line, "dead", flag="NOT FOUND"))
            continue
        flag = _flag(
            board_before.status if board_before else None, board_after.status, has_gone
        )
        rows.append(
            ReportRow(
                company.name, line, board_after.status, board_after.postings,
                board_after.matched, flag,
                samples=list(v.samples) if flag in ("NEW", "MOVED", "BACK") else [],
            )
        )
    for source in candidates.pinned:
        rows.append(ReportRow(company.name, source_to_line(source), "pinned"))
    return updated, rows


def run_resolve(
    config: Config,
    *,
    only: Optional[str] = None,
    check_all: bool = False,
    limit: int = 0,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    check: Optional[Callable[[Source], ValidationResult]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ResolveOutcome:
    """The resolve step: load companies.txt and the map, check the due companies,
    and save the map after each one unless `dry_run`."""
    if not config.companies_file:
        raise ConfigError(
            "companies_file is not set in config.yaml: add `companies_file: "
            '"companies.txt"` (see companies.txt.example)'
        )
    companies_file = load_companies_file(config.companies_file)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    filters = config.resolved_filters

    def default_check(source: Source) -> ValidationResult:
        # No detail calls: the report needs titles and locations only.
        return validate_source(source, filters, details=False)

    check = check or default_check

    names = [c.name for c in companies_file.companies]
    if only is not None and only.strip().lower() not in {n.lower() for n in names}:
        raise ConfigError(f"--company {only!r} is not in {config.companies_file}")

    board_map = load_map(config.board_map_file)
    kept = drop_missing_companies(board_map, names)
    dropped = [
        c.name for c in board_map.companies if kept.find_company(c.name) is None
    ]
    board_map = kept

    due = select_due(
        companies_file.companies, board_map, now, config.resolve.recheck_days,
        check_all=check_all, only=only, limit=limit,
    )
    pinned = config.pinned_sources
    rows: List[ReportRow] = []
    checked: List[str] = []
    for company in due:
        candidates = candidates_for(
            company, config.resolve.boards, companies_file.blocked, pinned
        )
        log.info("%s: checking %d candidate board(s)", company.name, len(candidates.to_check))
        if not candidates.to_check and not candidates.pinned:
            log.warning("%s: no board names to try (add an alias or a careers URL)", company.name)
        board_map, company_rows = resolve_company(
            board_map, company, candidates, check, now, sleep
        )
        rows.extend(company_rows)
        checked.append(company.name)
        if not dry_run:
            save_map(board_map, config.board_map_file)
    if not dry_run and dropped:
        save_map(board_map, config.board_map_file)

    return ResolveOutcome(board_map, rows, checked, dropped)


def format_report(outcome: ResolveOutcome, dry_run: bool = False) -> str:
    """The report: one row per board, then three sample postings under each new
    one, then a summary line."""
    lines: List[str] = []
    if outcome.rows:
        name_w = max(len(r.company) for r in outcome.rows)
        line_w = max(len(r.line) for r in outcome.rows)
        header = (
            f"{'COMPANY':<{name_w}}  {'BOARD':<{line_w}}  {'STATUS':<6}  "
            f"{'POSTINGS':>8}  {'MATCHED':>7}  FLAG"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for r in outcome.rows:
            lines.append(
                f"{r.company:<{name_w}}  {r.line:<{line_w}}  {r.status:<6}  "
                f"{r.postings:>8}  {r.matched:>7}  {r.flag}".rstrip()
            )
            for title, location in r.samples:
                lines.append(f"    - {title}  ({location or 'no location'})")
    else:
        lines.append("No boards found or changed.")

    counts: Dict[str, int] = {}
    for r in outcome.rows:
        if r.flag:
            counts[r.flag] = counts.get(r.flag, 0) + 1
    flags = ", ".join(f"{n} {flag}" for flag, n in sorted(counts.items()))
    summary = f"{len(outcome.checked)} company(ies) checked"
    if flags:
        summary += f"; {flags}"
    if outcome.dropped:
        summary += f"; dropped from the map: {', '.join(outcome.dropped)}"
    lines += ["", summary + "."]
    if dry_run:
        lines.append("Dry run: nothing was saved.")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m src.ingestion.resolve",
        description="Check the companies in companies.txt against every board "
        "and record where they are in the board map.",
    )
    parser.add_argument("--all", action="store_true", dest="check_all",
                        help="check every company, due or not")
    parser.add_argument("--company", metavar="NAME",
                        help="check only this company (as named in companies.txt)")
    parser.add_argument("--limit", type=int, default=0,
                        help="check at most this many companies (0 = no cap)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the report and save nothing")
    args = parser.parse_args(argv)

    try:
        config = load_config()
        outcome = run_resolve(
            config, only=args.company, check_all=args.check_all,
            limit=args.limit, dry_run=args.dry_run,
        )
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    # Posting titles can hold characters a Windows console cannot encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    print(format_report(outcome, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
