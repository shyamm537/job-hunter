"""Propose companies to follow from the Adzuna ads you already have.

`make discover` reads the distinct company names from the Adzuna postings stored
in the database, drops the ones already in companies.txt, merges spelling
variants ("Thermo Fisher Scientific" and "ThermoFisher Scientific"), and writes
the rest to `companies.discovered.txt` as commented lines, with the number of
ads beside each. Names with ads in your wanted places come first. You copy the
ones you want into companies.txt and run `make resolve`, which finds their
boards. See docs/board-discovery.md.

Proposals only: nothing is added for you, and it makes no network calls. Adzuna
is mined because it is the source that surfaces companies you have no board for
(mining Greenhouse/Lever rows would only re-derive boards you already have). The
list includes recruiters ("Hire Resolve.com") and agencies; skipping them is your
call. It cannot say which board a company is on: that is what `make resolve`
does with the names you pick.

`slug_variants` is also what `make resolve` uses to turn a company name into
board tokens. The helpers here are pure and unit-tested.
"""

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlmodel import select

from src.config import Company, ConfigError, Filters, load_companies_file, load_config
from src.ingestion.filtering import whole_word_in
from src.logging_config import setup_logging
from src.storage.database import get_session, init_db, set_database_url
from src.storage.models import JobPost

log = logging.getLogger("jobhunter.discover")

DEFAULT_OUT = "companies.discovered.txt"

# Legal-entity / filler words to drop before slugifying a company name.
_STOPWORDS = {
    "the", "pty", "ltd", "limited", "inc", "incorporated", "llc", "llp",
    "corp", "corporation", "co", "company", "group", "holdings", "plc",
    "gmbh", "ag", "technologies", "technology", "labs", "global",
}


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


def company_key(name: str) -> str:
    """What makes two spellings of a company name the same company: the joined
    slug ("Thermo Fisher Scientific" and "ThermoFisher Scientific" share one). A
    name with no usable words falls back to its lower-cased text."""
    slugs = slug_variants(name)
    return slugs[0] if slugs else " ".join((name or "").lower().split())


def existing_keys(companies: Iterable[Company]) -> Set[str]:
    """Keys of the companies already in companies.txt, including their plain
    aliases (`cba`, `flinders`), so a name already followed is not proposed."""
    keys: Set[str] = set()
    for company in companies:
        keys.add(company_key(company.name))
        for alias in company.aliases:
            squashed = re.sub(r"[^a-z0-9]", "", alias.lower())
            if squashed:
                keys.add(squashed)
    return keys


@dataclass
class Proposal:
    name: str  # the commonest spelling
    ads: int = 0
    local: int = 0  # ads in your wanted places
    spellings: Dict[str, int] = field(default_factory=dict)  # spelling -> ads

    @property
    def other_spellings(self) -> List[str]:
        ordered = sorted(self.spellings, key=lambda s: (-self.spellings[s], s))
        return [s for s in ordered if s != self.name]


def wanted_places(filters: Filters) -> List[str]:
    """The places that count as local: `board_locations` when set, else
    `locations`. A literal "remote" entry is dropped: it says nothing about
    where the company is."""
    places = filters.board_locations or filters.locations
    return [p.strip() for p in places if p and p.strip() and p.strip().lower() != "remote"]


def is_local(location: str, places: List[str]) -> bool:
    """Is this ad in one of the wanted places? Whole-word, like board_locations,
    so "SA" matches "Adelaide, SA" but not "San Francisco"."""
    return any(whole_word_in(place, location or "") for place in places)


def propose(
    ads: Iterable[Tuple[str, str]],
    places: List[str],
    already: Set[str],
    min_ads: int = 1,
) -> List[Proposal]:
    """Group (company, location) pairs into proposals.

    Spelling variants merge (`company_key`); companies in `already` are dropped;
    companies with fewer than `min_ads` ads are dropped. Ordered with the ones
    that have ads in your wanted places first, then by ad count, then by name.
    """
    grouped: Dict[str, Proposal] = {}
    for company, location in ads:
        name = " ".join((company or "").split())
        if not name:
            continue
        key = company_key(name)
        if key in already:
            continue
        proposal = grouped.setdefault(key, Proposal(name=name))
        proposal.ads += 1
        proposal.spellings[name] = proposal.spellings.get(name, 0) + 1
        if is_local(location, places):
            proposal.local += 1

    for proposal in grouped.values():
        proposal.name = max(proposal.spellings, key=lambda s: (proposal.spellings[s], s))

    kept = [p for p in grouped.values() if p.ads >= min_ads]
    kept.sort(key=lambda p: (p.local == 0, -p.local, -p.ads, p.name.lower()))
    return kept


def _safe_name(name: str) -> str:
    """A name as one companies.txt line can hold it: no `|` (it starts the
    aliases) and no ` #` (it starts a comment)."""
    return " ".join(re.sub(r"\s#", " ", name.replace("|", "/")).split())


def _line(proposal: Proposal) -> str:
    note = f"{proposal.ads} ad{'s' if proposal.ads != 1 else ''}"
    if proposal.local:
        note += f", {proposal.local} local"
    if proposal.other_spellings:
        note += "; also written " + ", ".join(
            _safe_name(s) for s in proposal.other_spellings[:2]
        )
    return f"# {_safe_name(proposal.name)}    # {note}"


def proposal_lines(proposals: List[Proposal], places: List[str]) -> List[str]:
    """The text of companies.discovered.txt."""
    local = [p for p in proposals if p.local]
    elsewhere = [p for p in proposals if not p.local]
    where = f" ({', '.join(places)})." if places else " (none set: every ad is elsewhere)."
    lines = [
        "# Company names found in your Adzuna results (proposals).",
        "# Uncomment the employers you want to follow and copy them into",
        "# companies.txt, then run `make resolve` to find their boards.",
        "# 'ads' = stored Adzuna ads for the name; 'local' = those in your wanted",
        "# places" + where,
        "# Recruiters and agencies appear here too: skip them.",
        "",
        f"# --- With ads in your wanted places ({len(local)}) ---",
    ]
    lines += [_line(p) for p in local]
    lines += ["", f"# --- Elsewhere ({len(elsewhere)}) ---"]
    lines += [_line(p) for p in elsewhere]
    return lines


def adzuna_ads(session) -> List[Tuple[str, str]]:
    """(company, location) of every stored Adzuna ad, identified by its
    `adzuna-` job_board_id prefix. ATS rows are excluded: they are companies you
    already have a board for."""
    rows = session.exec(
        select(JobPost.company, JobPost.location).where(JobPost.job_board_id.like("adzuna-%"))
    ).all()
    return [(company or "", location or "") for company, location in rows]


def main(argv: Optional[List[str]] = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m src.ingestion.discover",
        description="Propose company names from your Adzuna results.",
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUT,
        help=f"file to write commented proposals to (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--min-ads", type=int, default=1,
        help="leave out companies with fewer than this many ads (default: 1)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
        companies: List[Company] = []
        if config.companies_file and Path(config.companies_file).exists():
            companies = load_companies_file(config.companies_file).companies
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    set_database_url(config.database.url)
    init_db()

    with get_session() as session:
        ads = adzuna_ads(session)
        total = len(session.exec(select(JobPost.id)).all())
    if not ads:
        if total == 0:
            msg = "No postings in the database yet — run `make scrape` first."
        else:
            msg = (
                f"Found {total} posting(s), but none from Adzuna. Discovery mines "
                "those for company names. Set up Adzuna (an 'adzuna:' creds block + "
                "an 'adzuna' source), then `make scrape`. See docs/board-discovery.md."
            )
        print(msg, file=sys.stderr)
        raise SystemExit(1)

    places = wanted_places(config.resolved_filters)
    already = existing_keys(companies)
    proposals = propose(ads, places, already, min_ads=args.min_ads)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(proposal_lines(proposals, places)) + "\n")
    followed = {company_key(name) for name, _ in ads} & already
    log.info(
        "%d company name(s) proposed (%d with ads in your wanted places) from %d "
        "Adzuna ad(s); %d already in your companies list. Wrote %s — review it and "
        "copy the employers you want into companies.txt.",
        len(proposals), sum(1 for p in proposals if p.local), len(ads),
        len(followed), args.out,
    )


if __name__ == "__main__":
    main()
