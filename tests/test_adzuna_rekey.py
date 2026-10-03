"""Tests for the Adzuna re-key and duplicate merge (src/storage/adzuna_rekey.py).

Legacy rows were keyed by a hash of the full redirect URL (query included);
the scraper now keys by Adzuna's ad id. The re-key moves legacy rows to the id
key and merges copies of the same ad without losing generated materials,
contact lookups or status.
"""

from datetime import datetime

import pytest
from sqlmodel import select

import src.storage.database as database
from src.ingestion.adzuna import _hash, id_dedup_key
from src.storage.adzuna_rekey import rekey_adzuna
from src.storage.database import get_session
from src.storage.models import JobPost

D1 = datetime(2026, 6, 1, 9, 0)
D2 = datetime(2026, 7, 1, 9, 0)
D3 = datetime(2026, 8, 1, 9, 0)


@pytest.fixture(autouse=True)
def db(tmp_path):
    database.set_database_url(f"sqlite:///{tmp_path}/jobs.db")
    database.init_db()
    yield
    database._database_url = None
    database._engine = None


def _legacy(url, **overrides):
    """A row keyed the old way: hash of the full URL."""
    return _row(_hash(url), url, **overrides)


def _new(ad_id, url, **overrides):
    return _row(id_dedup_key(ad_id), url, **overrides)


def _row(job_board_id, url, **overrides):
    values = dict(
        job_board_id=job_board_id, title="Data Analyst", company="Acme",
        location="Adelaide", description="snippet", url=url, date_scraped=D2,
    )
    values.update(overrides)
    return JobPost(**values)


def _seed(*rows):
    with get_session() as session:
        session.add_all(rows)
        session.commit()


def _rows():
    with get_session() as session:
        return session.exec(select(JobPost)).all()


def _run(apply=True):
    with get_session() as session:
        return rekey_adzuna(session, apply=apply)


AU = "https://www.adzuna.com.au/details/111?utm_source=api&se=A"
AU_OTHER_SEARCH = "https://www.adzuna.com.au/details/111?utm_source=api&se=B"
IN_LAND = "https://www.adzuna.in/land/ad/222?se=X"


def test_lone_legacy_row_is_rekeyed_in_place():
    _seed(_legacy(IN_LAND, status="Applied", generated_cover_letter="Dear Acme"))
    (before,) = _rows()

    report = _run()

    (row,) = _rows()
    assert row.id == before.id
    assert row.job_board_id == id_dedup_key("222")
    assert row.status == "Applied" and row.generated_cover_letter == "Dear Acme"
    assert report.rekeyed == 1 and report.merged_away == 0


def test_legacy_and_id_copies_merge_into_the_id_row_keeping_materials():
    # The real-data shape: the old copy has the letter and contact lookup,
    # the id copy (what scrapes now match) has neither.
    _seed(
        _legacy(AU, date_scraped=D1, generated_cover_letter="Dear Acme",
                generated_cold_email="Hi", contact_name="Sam",
                contact_email="sam@acme.test", contact_confidence="published",
                last_checked_at=D2),
        _new("111", AU, date_scraped=D3, last_seen_at=D3),
    )

    report = _run()

    (row,) = _rows()
    assert row.job_board_id == id_dedup_key("111")
    assert row.generated_cover_letter == "Dear Acme"
    assert row.generated_cold_email == "Hi"
    assert (row.contact_name, row.contact_email, row.contact_confidence) == (
        "Sam", "sam@acme.test", "published")
    assert row.date_scraped == D1       # earliest
    assert row.last_seen_at == D3       # latest
    assert row.last_checked_at == D2
    assert report.merged_away == 1 and report.materials_kept == 1 and report.contacts_kept == 1


def test_contact_fields_come_from_one_copy_together():
    _seed(
        _new("111", AU, contact_name="Lee", contact_confidence=None),
        _legacy(AU, contact_email="sam@acme.test", contact_confidence="pattern-guess"),
    )
    _run()
    (row,) = _rows()
    # The survivor had no lookup (confidence NULL), so the whole trio is copied.
    assert (row.contact_name, row.contact_email, row.contact_confidence) == (
        None, "sam@acme.test", "pattern-guess")


def test_several_legacy_copies_without_an_id_row_collapse_to_the_oldest():
    _seed(
        _legacy(AU, date_scraped=D2),
        _legacy(AU_OTHER_SEARCH, date_scraped=D1, generated_cover_letter="Dear Acme"),
    )
    _run()
    (row,) = _rows()
    assert row.job_board_id == id_dedup_key("111")
    assert row.date_scraped == D1
    assert row.generated_cover_letter == "Dear Acme"


def test_to_apply_survivor_takes_a_status_that_moved_on():
    _seed(_new("111", AU), _legacy(AU, status="Applied"))
    _run()
    (row,) = _rows()
    assert row.status == "Applied"


def test_alive_if_any_copy_is_alive():
    _seed(_new("111", AU, dead_at=D2, dead_reason="http 404"), _legacy(AU))
    _run()
    (row,) = _rows()
    assert row.dead_at is None and row.dead_reason is None


def test_all_dead_keeps_the_first_death():
    _seed(
        _new("111", AU, dead_at=D3, dead_reason="http 404"),
        _legacy(AU, dead_at=D1, dead_reason="http 410"),
    )
    _run()
    (row,) = _rows()
    assert (row.dead_at, row.dead_reason) == (D1, "http 410")


def test_dry_run_writes_nothing():
    _seed(_legacy(AU), _new("111", AU))
    report = _run(apply=False)
    assert report.ads == 1 and report.merged_away == 1
    assert len(_rows()) == 2
    assert {r.job_board_id for r in _rows()} == {_hash(AU), id_dedup_key("111")}


def test_rerun_is_a_no_op():
    _seed(_legacy(AU), _new("111", AU), _legacy(IN_LAND))
    _run()
    report = _run()
    assert report.ads == 0 and report.merged_away == 0
    assert len(_rows()) == 2


def test_id_keyed_rows_board_rows_and_urls_without_an_id_are_left_alone():
    board = _row("greenhouse-abc", "https://boards.greenhouse.io/acme/jobs/1", company="acme")
    odd = _row("adzuna-oddone", "https://www.adzuna.com.au/jobs/search?q=x")
    fine = _new("333", "https://www.adzuna.com.au/details/333")
    _seed(board, odd, fine)

    report = _run()

    assert {r.job_board_id for r in _rows()} == {"greenhouse-abc", "adzuna-oddone", id_dedup_key("333")}
    assert report.no_id == 1 and report.ads == 0
