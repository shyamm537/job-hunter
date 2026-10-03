"""One-off: re-key legacy Adzuna rows to the id key and merge duplicates (KAN-7).

The Adzuna scraper keys postings by Adzuna's own ad `id`
(`id_dedup_key`, src/ingestion/adzuna.py). Rows scraped before that switch
were keyed by a hash of the full redirect URL, query string included, so the
same ad fetched under two searches got two keys, and once the id key arrived
every re-seen ad got a third. The stored URL always carries the ad id
(`/details/<id>` or `/land/ad/<id>`), so each legacy row can be re-keyed.

Per database (main and archive, separately), the rows for one ad are merged
into a single survivor:

- Survivor: the id-keyed row if there is one (it's what future scrapes
  match), else the oldest row.
- Empty fields on the survivor are filled from the other copies, oldest first:
  generated materials, the contact lookup (name, email and confidence taken
  together from one copy), posted_at.
- date_scraped is the earliest, last_seen_at and last_checked_at the latest.
- A "To Apply" survivor takes another copy's status if one has moved on.
- Alive if any copy is alive; otherwise the earliest death date and reason.
- The other copies are deleted and the survivor gets the id key.

Rows whose URL carries no ad id are left alone. Re-running is a no-op.

    python -m src.storage.adzuna_rekey            # dry run: report only
    python -m src.storage.adzuna_rekey --apply    # write (back up data/ first)
"""

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy import delete
from sqlmodel import Session, select

from src.ingestion.adzuna import adzuna_id_from_url, id_dedup_key
from src.storage.database import _ID_CHUNK_SIZE
from src.storage.models import JobPost

log = logging.getLogger("jobhunter.rekey")

# Columns filled on the survivor from another copy when the survivor's is empty.
_MATERIAL_FIELDS = ("generated_cover_letter", "generated_cold_email", "posted_at")
_CONTACT_FIELDS = ("contact_name", "contact_email", "contact_confidence")


@dataclass
class RekeyReport:
    ads: int = 0              # distinct ads that needed work
    rekeyed: int = 0          # survivors whose key changed
    merged_away: int = 0      # duplicate rows deleted
    materials_kept: int = 0   # survivors that gained a cover letter from a copy
    contacts_kept: int = 0    # survivors that gained a contact lookup from a copy
    no_id: int = 0            # adzuna rows with no ad id in the URL (left alone)

    def summary(self) -> str:
        return (
            f"{self.ads} ad(s) to fix: {self.rekeyed} re-keyed, "
            f"{self.merged_away} duplicate row(s) merged away, "
            f"{self.materials_kept} cover letter(s) and {self.contacts_kept} "
            f"contact lookup(s) carried over; {self.no_id} row(s) without an ad id left alone"
        )


def _oldest_first(rows: List[JobPost]) -> List[JobPost]:
    return sorted(rows, key=lambda r: (r.date_scraped, r.id))


def _merge_into(survivor: JobPost, others: List[JobPost], report: RekeyReport) -> None:
    donors = _oldest_first(others)

    had_letter = survivor.generated_cover_letter is not None
    for field in _MATERIAL_FIELDS:
        if getattr(survivor, field) is None:
            for donor in donors:
                if getattr(donor, field) is not None:
                    setattr(survivor, field, getattr(donor, field))
                    break
    if not had_letter and survivor.generated_cover_letter is not None:
        report.materials_kept += 1

    if survivor.contact_confidence is None:
        donor = next((d for d in donors if d.contact_confidence is not None), None)
        if donor is not None:
            for field in _CONTACT_FIELDS:
                setattr(survivor, field, getattr(donor, field))
            report.contacts_kept += 1

    everyone = [survivor] + donors
    survivor.date_scraped = min(r.date_scraped for r in everyone)
    for field in ("last_seen_at", "last_checked_at"):
        stamps = [getattr(r, field) for r in everyone if getattr(r, field) is not None]
        setattr(survivor, field, max(stamps) if stamps else None)

    if survivor.status == "To Apply":
        moved_on = [d for d in reversed(donors) if d.status != "To Apply"]
        if moved_on:
            survivor.status = moved_on[0].status

    if any(r.dead_at is None for r in everyone):
        survivor.dead_at = None
        survivor.dead_reason = None
    else:
        first_death = min(everyone, key=lambda r: r.dead_at)
        survivor.dead_at = first_death.dead_at
        survivor.dead_reason = first_death.dead_reason


def rekey_adzuna(session: Session, *, apply: bool = False) -> RekeyReport:
    """Re-key and merge this database's Adzuna rows. Commits only with apply=True."""
    report = RekeyReport()
    by_ad: Dict[str, List[JobPost]] = defaultdict(list)
    for row in session.exec(select(JobPost).where(JobPost.job_board_id.like("adzuna-%"))).all():
        ad_id = adzuna_id_from_url(row.url)
        if ad_id is None:
            report.no_id += 1
            continue
        by_ad[ad_id].append(row)

    doomed: List[int] = []
    survivors: List[JobPost] = []
    for ad_id, rows in by_ad.items():
        key = id_dedup_key(ad_id)
        if len(rows) == 1 and rows[0].job_board_id == key:
            continue  # already right
        report.ads += 1
        survivor: Optional[JobPost] = next((r for r in rows if r.job_board_id == key), None)
        if survivor is None:
            survivor = _oldest_first(rows)[0]
            report.rekeyed += 1
        others = [r for r in rows if r is not survivor]
        _merge_into(survivor, others, report)
        survivor.job_board_id = key
        doomed.extend(r.id for r in others)
        survivors.append(survivor)
    report.merged_away = len(doomed)

    if not apply:
        session.rollback()  # drop the in-memory edits
        return report

    # Delete the copies first so no survivor's new key collides with a row
    # that is about to go.
    for start in range(0, len(doomed), _ID_CHUNK_SIZE):
        session.exec(delete(JobPost).where(JobPost.id.in_(doomed[start : start + _ID_CHUNK_SIZE])))
    for survivor in survivors:
        session.add(survivor)
    session.commit()
    return report


def main(argv: Optional[List[str]] = None) -> None:
    from src.config import ConfigError, load_config
    from src.logging_config import setup_logging
    from src.storage.archive import configure_archive, get_archive_session
    from src.storage.database import get_session, init_db, set_database_url

    setup_logging()
    parser = argparse.ArgumentParser(
        prog="python -m src.storage.adzuna_rekey",
        description="Re-key legacy Adzuna rows to the ad-id key and merge duplicates.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="write the changes; without this flag nothing is written",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
        set_database_url(config.database.url)
        init_db()
        configure_archive(config.database)
    except (ConfigError, ValueError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    mode = "" if args.apply else " (dry run, nothing written; use --apply)"
    with get_session() as session:
        log.info("main database: %s%s", rekey_adzuna(session, apply=args.apply).summary(), mode)
    with get_archive_session() as archive_session:
        log.info("archive: %s%s", rekey_adzuna(archive_session, apply=args.apply).summary(), mode)


if __name__ == "__main__":
    main()
