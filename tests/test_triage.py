"""Tests for the triage view (KAN-34): the default "To Apply" queue, sorting,
the "New" badge (first stored by the latest scrape run), email-style unread
state, mark-as-read, the one-click "Not interested", and how dismissed jobs
leave the cover-letter and contact queues and the archive rules.

Flask's test client on a temp database.
"""

import re
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import src.config as config
import src.storage.archive as archive
import src.storage.database as database
from src.app import create_app
from src.storage.database import (
    get_session,
    latest_scrape_start,
    list_job_summaries,
    mark_opened,
    pending_contact_jobs,
    pending_llm_jobs,
    count_pending_llm_jobs,
    record_scrape_run,
)
from src.storage.models import NOT_INTERESTED, JobPost

T0 = datetime(2026, 9, 1, 9, 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv(database.ENV_VAR, raising=False)
    cfg = config.Config.model_validate(
        {"sources": [{"type": "greenhouse", "board": "acme"}],
         "database": {"url": f"sqlite:///{tmp_path}/jobs.db"}}
    )
    app = create_app(cfg)
    app.testing = True
    yield app.test_client()
    database._database_url = None
    database._engine = None
    archive._archive_url = None
    archive._archive_engine = None


def _job(n, **overrides):
    values = dict(
        job_board_id=f"greenhouse-{n}", title=f"Job {n}", company="acme", location="Adelaide",
        description="d", url=f"https://boards.greenhouse.io/acme/jobs/{n}", date_scraped=T0,
    )
    values.update(overrides)
    return JobPost(**values)


def _seed(*jobs):
    with get_session() as session:
        session.add_all(jobs)
        session.commit()
        return [job.id for job in jobs]


def _row(job_id):
    with get_session() as session:
        return session.get(JobPost, job_id)


def _page(client, path="/"):
    return client.get(path).get_data(as_text=True)


def _titles(page):
    """Job titles in table order."""
    return re.findall(r'<a href="/jobs/\d+">([^<]+)</a>', page)


def _post(client, path, data):
    return client.post(path, data=data, headers={"Origin": "http://localhost"})


# --- default view and sorting --------------------------------------------------


def test_default_view_is_the_to_apply_queue(client):
    _seed(_job(1, title="Fresh"), _job(2, title="Applied one", status="Applied"),
          _job(3, title="Dismissed", status=NOT_INTERESTED))
    page = _page(client)
    assert _titles(page) == ["Fresh"]
    assert "<h1>To Apply</h1>" in page


def test_unticking_every_status_shows_all_of_them(client):
    _seed(_job(1, title="Fresh"), _job(2, title="Applied one", status="Applied"),
          _job(3, title="Dismissed", status=NOT_INTERESTED))
    assert set(_titles(_page(client, "/?f=1"))) == {"Fresh", "Applied one", "Dismissed"}


def test_other_filters_keep_the_queue_default(client):
    _seed(_job(1, title="Data fresh"), _job(2, title="Data applied", status="Applied"))
    assert _titles(_page(client, "/?q=data")) == ["Data fresh"]


def test_newest_posted_first_falling_back_to_scrape_date(client):
    _seed(
        _job(1, title="Old post", posted_at=T0 - timedelta(days=30)),
        _job(2, title="No post date, scraped recently", posted_at=None, date_scraped=T0 + timedelta(days=2)),
        _job(3, title="New post", posted_at=T0 + timedelta(days=5)),
    )
    assert _titles(_page(client)) == ["New post", "No post date, scraped recently", "Old post"]


@pytest.mark.parametrize("sort, expected", [
    ("title", ["alpha", "Beta", "gamma"]),
    ("company", ["gamma", "Beta", "alpha"]),
])
def test_sort_by_title_or_company(client, sort, expected):
    _seed(_job(1, title="Beta", company="m-co"), _job(2, title="gamma", company="a-co"),
          _job(3, title="alpha", company="z-co"))
    assert _titles(_page(client, f"/?sort={sort}")) == expected


def test_unknown_sort_falls_back_to_posted(client):
    _seed(_job(1, title="Older", posted_at=T0), _job(2, title="Newer", posted_at=T0 + timedelta(days=1)))
    assert _titles(_page(client, "/?sort=evil();--")) == ["Newer", "Older"]


def test_sort_links_keep_the_filters(client):
    _seed(_job(1))
    page = _page(client, "/?q=job&status=Applied&status=To+Apply")
    link = re.search(r'<a href="(/\?[^"]*sort=title[^"]*)">Title</a>', page).group(1)
    assert "q=job" in link and "status=Applied" in link and "status=To+Apply" in link


def test_posted_column_shows_an_age(client):
    _seed(_job(1, posted_at=database.utcnow() - timedelta(days=3)))
    assert ">3d<" in _page(client)


# --- New badge ---------------------------------------------------------------


def test_new_badge_marks_jobs_from_the_latest_scrape(client):
    with get_session() as session:
        record_scrape_run(session, when=T0)
        record_scrape_run(session, when=T0 + timedelta(days=7))
        assert latest_scrape_start(session) == T0 + timedelta(days=7)
    _seed(
        _job(1, title="From the last run", date_scraped=T0 + timedelta(days=7, minutes=5)),
        _job(2, title="From an earlier run", date_scraped=T0 + timedelta(minutes=5)),
        JobPost(job_board_id="manual-abc", title="Added by hand", company="x", location="",
                description="d", url="", date_scraped=T0 + timedelta(days=8)),
    )
    page = _page(client)
    rows = {title: row for title, row in re.findall(r'>([^<]+)</a>\s*(<span class="badge">New</span>)?', page)}
    assert rows["From the last run"] and not rows["From an earlier run"] and not rows["Added by hand"]
    assert "1 new" in page


def test_no_new_badges_before_the_first_recorded_scrape(client):
    _seed(_job(1))
    assert 'class="badge">New<' not in _page(client)


def test_scrape_cli_records_a_run(tmp_path, monkeypatch):
    from src.ingestion import cli
    from src.ingestion.planner import PlannedScrape

    class Empty:
        def scrape(self):
            return []

    cfg = config.Config.model_validate(
        {"sources": [{"type": "greenhouse", "board": "acme"}],
         "database": {"url": f"sqlite:///{tmp_path}/jobs.db"}}
    )
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cli, "plan_scrapes", lambda s, f, **k: [PlannedScrape(Empty(), "x", post_filter=False)])
    try:
        cli.main()
        with get_session() as session:
            assert latest_scrape_start(session) is not None
    finally:
        database._database_url = None
        database._engine = None
        archive._archive_url = None
        archive._archive_engine = None


# --- unread ------------------------------------------------------------------


def test_jobs_start_unread_and_opening_one_reads_it(client):
    (job_id,) = _seed(_job(1))
    page = _page(client)
    assert '<tr class="unread">' in page and "1 unread" in page

    _page(client, f"/jobs/{job_id}")

    assert _row(job_id).opened_at is not None
    assert '<tr class="unread">' not in _page(client)


def test_opening_again_keeps_the_first_read_time(client):
    (job_id,) = _seed(_job(1, opened_at=T0))
    _page(client, f"/jobs/{job_id}")
    assert _row(job_id).opened_at == T0


def test_changing_status_reads_the_job(client):
    (job_id,) = _seed(_job(1))
    _post(client, f"/jobs/{job_id}/status", {"status": "Applied"})
    row = _row(job_id)
    assert row.status == "Applied" and row.opened_at is not None


def test_mark_these_as_read_only_touches_the_filtered_list(client):
    a, b, c = _seed(_job(1, title="Data one"), _job(2, title="Data two"), _job(3, title="Other"))
    resp = _post(client, "/jobs/mark-read", {"next": "/?q=data"})
    assert resp.headers["Location"] == "/?q=data"
    assert _row(a).opened_at and _row(b).opened_at and _row(c).opened_at is None
    assert "Marked 2 job(s) as read" in _page(client, "/?q=data")


def test_mark_read_respects_the_default_queue(client):
    fresh, applied = _seed(_job(1), _job(2, status="Applied"))
    _post(client, "/jobs/mark-read", {"next": "/"})
    assert _row(fresh).opened_at is not None and _row(applied).opened_at is None


def test_mark_read_never_redirects_off_site(client):
    resp = _post(client, "/jobs/mark-read", {"next": "https://evil.example/"})
    assert resp.headers["Location"] == "/"


def test_mark_opened_helper(client):
    a, b = _seed(_job(1), _job(2, opened_at=T0))
    with get_session() as session:
        assert mark_opened(session, [a, b], when=T0 + timedelta(days=1)) == 1
        session.commit()
    assert _row(a).opened_at == T0 + timedelta(days=1) and _row(b).opened_at == T0


# --- Not interested ----------------------------------------------------------------


def test_one_click_not_interested_drops_it_from_the_queue(client):
    keep, drop = _seed(_job(1, title="Keep"), _job(2, title="Drop"))
    resp = _post(client, f"/jobs/{drop}/status", {"status": NOT_INTERESTED, "next": "/?q=o"})
    assert resp.headers["Location"] == "/?q=o"
    assert _row(drop).status == NOT_INTERESTED
    assert _titles(_page(client)) == ["Keep"]


def test_dismiss_button_and_posting_link_on_each_row(client):
    _seed(_job(1), _job(2, title="Dismissed", status=NOT_INTERESTED, url="javascript:alert(1)"))
    page = _page(client, "/?f=1")
    assert page.count('aria-label="Not interested in ') == 1  # not on the dismissed one
    assert 'href="https://boards.greenhouse.io/acme/jobs/1" target="_blank"' in page
    assert 'href="javascript:' not in page


def test_not_interested_jobs_skip_the_llm_and_contact_queues(client):
    wanted, dismissed = _seed(_job(1), _job(2, status=NOT_INTERESTED))
    with get_session() as session:
        assert [j.id for j in pending_llm_jobs(session)] == [wanted]
        assert count_pending_llm_jobs(session) == 1
        assert [j.id for j in pending_contact_jobs(session)] == [wanted]


def test_dead_not_interested_jobs_are_archived(tmp_path):
    database.set_database_url(f"sqlite:///{tmp_path}/jobs.db")
    database.init_db()
    archive.set_archive_url(f"sqlite:///{tmp_path}/dead_jobs.db")
    archive.init_archive_db()
    try:
        _seed(_job(1, status=NOT_INTERESTED, dead_at=T0, dead_reason="gone from board"))
        with get_session() as session, archive.get_archive_session() as archive_session:
            assert archive.archive_dead_jobs(session, archive_session) == 1
            assert session.exec(select(JobPost)).all() == []
    finally:
        database._database_url = None
        database._engine = None
        archive._archive_url = None
        archive._archive_engine = None


def test_list_summaries_carry_the_triage_columns(client):
    _seed(_job(1, posted_at=T0, opened_at=T0))
    with get_session() as session:
        (summary,) = list_job_summaries(session)
    assert summary.posted == T0 and summary.opened_at == T0
    assert summary.url.startswith("https://")
