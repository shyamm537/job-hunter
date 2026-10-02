"""Tests for the dead-link checker (src/ingestion/check_links.py).

No network and no real sleeping: the HTTP session is a fake, DELAY is set to
0, config is mocked and the database is a temp on-disk SQLite file.
"""

import logging
import sqlite3

import pytest
from sqlmodel import select

import src.config as config
import src.storage.database as database
from src.ingestion import check_links
from src.storage.models import JobPost

LIVE_PAGE = "<html><body>Data Analyst. Apply now.</body></html>"


class FakeResponse:
    def __init__(self, status_code=200, text=LIVE_PAGE):
        self.status_code = status_code
        self.text = text


class FakeHttp:
    """Stands in for the curl_cffi session.

    `responses` maps a URL to a FakeResponse, or to an exception to raise.
    URLs that aren't listed get `default`. Every GET is recorded in `calls`.
    """

    def __init__(self, responses=None, default=None, before_get=None):
        self.responses = responses or {}
        self.default = default or FakeResponse()
        self.before_get = before_get  # optional hook: called with the fake before each GET
        self.calls = []  # URLs requested, in order
        self.kwargs = []  # keyword arguments of each GET

    def get(self, url, **kwargs):
        if self.before_get is not None:
            self.before_get(self)
        self.calls.append(url)
        self.kwargs.append(kwargs)
        outcome = self.responses.get(url, self.default)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def head(self, *args, **kwargs):
        # pytest.fail raises a BaseException, so check_link's `except
        # Exception` can't swallow it and quietly report "unknown".
        pytest.fail("check_links must only send GET requests")


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh temp database that main() is pointed at through a fake config."""
    monkeypatch.delenv(database.ENV_VAR, raising=False)
    database._database_url = None
    database._engine = None

    path = tmp_path / "jobs.db"
    fake_config = config.Config.model_validate(
        {
            "filters": {"titles": []},
            "sources": [{"type": "greenhouse", "board": "acme"}],
            "database": {"url": f"sqlite:///{path}"},
        }
    )
    monkeypatch.setattr(check_links, "load_config", lambda *a, **k: fake_config)
    monkeypatch.setattr(check_links, "DELAY", 0)

    database.set_database_url(fake_config.database.url)
    database.init_db()
    yield path
    database._database_url = None
    database._engine = None


def _seed(urls):
    """Insert one posting per URL and return the new ids, in the same order."""
    with database.get_session() as session:
        posts = [
            JobPost(
                job_board_id=f"adzuna-{i}",
                title=f"Data Analyst {i}",
                company="Acme",
                location="Remote",
                description="A role.",
                url=url,
            )
            for i, url in enumerate(urls)
        ]
        session.add_all(posts)
        session.commit()
        return [post.id for post in posts]


def _rows():
    """Every posting as stored right now, keyed by URL."""
    with database.get_session() as session:
        return {row.url: row for row in session.exec(select(JobPost)).all()}


def _urls(n):
    return [f"https://example.com/job/{i}" for i in range(n)]


# --- classification ----------------------------------------------------------


def test_200_is_live():
    assert check_links.classify(200, LIVE_PAGE) == ("live", "http 200")


@pytest.mark.parametrize("code", [404, 410])
def test_404_and_410_are_dead(code):
    assert check_links.classify(code, "<html>Not found</html>") == ("dead", f"http {code}")


@pytest.mark.parametrize("code", [404, 410])
def test_404_with_an_apply_control_is_unknown(code):
    # The old code called this live. It is an unverified guess about Adzuna,
    # so until it's calibrated it must be neither live nor dead.
    status, reason = check_links.classify(code, "<button>Apply for this job</button>")
    assert status == "unknown"
    assert f"http {code}" in reason
    assert "apply for this job" in reason
    assert "unverified" in reason


@pytest.mark.parametrize("phrase", check_links.CLOSED_PHRASES)
def test_200_with_a_closed_phrase_is_unknown(phrase):
    status, reason = check_links.classify(200, f"<p>Sorry, {phrase.upper()}.</p>")
    assert status == "unknown"  # an unverified guess too: reported, never dead
    assert phrase in reason


@pytest.mark.parametrize("code", [301, 403, 429, 500, 503])
def test_other_statuses_are_unknown(code):
    assert check_links.classify(code, "") == ("unknown", f"http {code}")


def test_request_exception_is_unknown():
    http = FakeHttp({"https://example.com/a": TimeoutError("too slow")})
    assert check_links.check_link("https://example.com/a", http) == ("unknown", "TimeoutError")


def test_check_link_sends_one_get_with_timeout_and_redirects():
    http = FakeHttp({"https://example.com/a": FakeResponse(404, "gone")})
    assert check_links.check_link("https://example.com/a", http) == ("dead", "http 404")
    assert http.calls == ["https://example.com/a"]
    assert http.kwargs == [{"timeout": check_links.TIMEOUT, "allow_redirects": True}]


# --- the checking loop -------------------------------------------------------


def test_identical_urls_are_requested_once(db, monkeypatch):
    same, other = "https://example.com/same", "https://example.com/other"
    _seed([same, same, other])
    sleeps = []
    monkeypatch.setattr(check_links, "DELAY", 0.25)
    monkeypatch.setattr(check_links.time, "sleep", sleeps.append)
    http = FakeHttp({same: FakeResponse(404, "gone")})

    check_links.main(["--mark-dead"], http=http)

    assert sorted(http.calls) == sorted([same, other])
    assert sleeps == [0.25]  # one pause, between the two real requests
    rows = _rows()
    assert rows[other].dead_at is None
    with database.get_session() as session:
        dead = session.exec(select(JobPost).where(JobPost.dead_at.is_not(None))).all()
    assert len(dead) == 2  # both postings sharing the URL get the result
    assert {row.url for row in dead} == {same}


# --- dry run vs --mark-dead --------------------------------------------------


def test_dry_run_writes_nothing(db):
    urls = _urls(2)
    _seed(urls)
    http = FakeHttp({urls[0]: FakeResponse(404, "gone")})

    assert check_links.main([], http=http) is None

    assert sorted(http.calls) == urls
    for row in _rows().values():
        assert row.dead_at is None
        assert row.dead_reason is None
        assert row.last_checked_at is None


def test_dry_run_then_mark_dead_checks_the_same_rows(db):
    _seed(_urls(5))
    dry, real = FakeHttp(), FakeHttp()

    check_links.main(["--limit", "2"], http=dry)
    check_links.main(["--limit", "2", "--mark-dead"], http=real)

    assert len(dry.calls) == 2
    assert real.calls == dry.calls


def test_mark_dead_records_results(db):
    gone404, gone410, live, blocked, broken, rescued, closed = _urls(7)
    _seed([gone404, gone410, live, blocked, broken, rescued, closed])
    http = FakeHttp(
        {
            gone404: FakeResponse(404, "not found"),
            gone410: FakeResponse(410, "gone"),
            blocked: FakeResponse(403, "forbidden"),
            broken: ConnectionError("no route to host"),
            rescued: FakeResponse(404, "Apply for this job"),
            closed: FakeResponse(200, "This position has been filled."),
        }
    )

    check_links.main(["--mark-dead"], http=http)

    rows = _rows()
    assert rows[gone404].dead_at is not None
    assert rows[gone404].dead_reason == "http 404"
    assert rows[gone410].dead_at is not None
    assert rows[gone410].dead_reason == "http 410"
    for url in (live, blocked, broken, rescued, closed):
        assert rows[url].dead_at is None, url
        assert rows[url].dead_reason is None, url
    # Every posting that was attempted counts as checked, failures included.
    for url, row in rows.items():
        assert row.last_checked_at is not None, url


def test_limit_is_honoured_and_the_next_run_moves_on(db):
    urls = _urls(5)
    _seed(urls)
    first, second, third = FakeHttp(), FakeHttp(), FakeHttp()

    check_links.main(["--mark-dead", "--limit", "2"], http=first)
    check_links.main(["--mark-dead", "--limit", "2"], http=second)
    check_links.main(["--mark-dead", "--limit", "2"], http=third)

    assert len(first.calls) == 2
    assert len(second.calls) == 2
    assert len(third.calls) == 1  # only one posting was still due
    assert sorted(first.calls + second.calls + third.calls) == urls  # no repeats


def test_limit_zero_means_no_limit(db, monkeypatch):
    monkeypatch.setattr(check_links, "DEFAULT_LIMIT", 2)
    _seed(_urls(5))
    http = FakeHttp()

    check_links.main(["--limit", "0"], http=http)

    assert len(http.calls) == 5


def test_default_limit_caps_the_run(db):
    _seed(_urls(check_links.DEFAULT_LIMIT + 3))
    http = FakeHttp()

    check_links.main([], http=http)

    assert len(http.calls) == check_links.DEFAULT_LIMIT


# --- batching and Ctrl-C -----------------------------------------------------


def _committed_checked_count(path):
    """Rows with last_checked_at set, as seen from a separate connection —
    i.e. only what has actually been committed."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM jobpost WHERE last_checked_at IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()


def _interrupt_on_request(n):
    def hook(http):
        if len(http.calls) == n - 1:  # this is the nth GET
            raise KeyboardInterrupt

    return hook


def test_completed_batches_are_committed_as_the_run_goes(db):
    _seed(_urls(5))
    committed_before_each_get = []
    http = FakeHttp(
        before_get=lambda _: committed_before_each_get.append(_committed_checked_count(db))
    )

    check_links.main(["--mark-dead", "--batch-size", "2"], http=http)

    assert committed_before_each_get == [0, 0, 2, 2, 4]
    assert _committed_checked_count(db) == 5  # the last, partial batch too


def test_ctrl_c_keeps_completed_batches(db):
    _seed(_urls(5))
    http = FakeHttp(default=FakeResponse(404, "gone"), before_get=_interrupt_on_request(5))

    assert check_links.main(["--mark-dead", "--batch-size", "2"], http=http) is None

    assert len(http.calls) == 4  # the fifth request never completed
    rows = _rows()
    for url in http.calls:
        assert rows[url].last_checked_at is not None
        assert rows[url].dead_reason == "http 404"
    (unchecked,) = set(rows) - set(http.calls)
    assert rows[unchecked].last_checked_at is None
    assert rows[unchecked].dead_at is None


def test_ctrl_c_saves_the_partial_batch(db, caplog):
    caplog.set_level(logging.INFO, logger="jobhunter.check_links")
    _seed(_urls(5))
    http = FakeHttp(default=FakeResponse(404, "gone"), before_get=_interrupt_on_request(3))

    check_links.main(["--mark-dead", "--batch-size", "10"], http=http)

    assert len(http.calls) == 2
    rows = _rows()
    assert {url for url, row in rows.items() if row.dead_at is not None} == set(http.calls)
    assert _committed_checked_count(db) == 2
    assert "checked 2 of 5 queued" in caplog.text  # the summary is still logged
    assert "Marked 2 dead." in caplog.text
    assert "3 still queued" in caplog.text


def test_ctrl_c_on_a_dry_run_still_writes_nothing(db):
    _seed(_urls(3))
    http = FakeHttp(default=FakeResponse(404, "gone"), before_get=_interrupt_on_request(3))

    assert check_links.main([], http=http) is None

    assert _committed_checked_count(db) == 0
    assert all(row.dead_at is None for row in _rows().values())


# --- empty queue, exit status, summary ---------------------------------------


def test_empty_queue_returns_normally(db, caplog):
    caplog.set_level(logging.INFO, logger="jobhunter.check_links")
    http = FakeHttp()

    assert check_links.main([], http=http) is None  # no SystemExit

    assert http.calls == []
    assert "Nothing due for a link check" in caplog.text


def test_nothing_due_after_everything_was_checked(db):
    _seed(_urls(2))
    check_links.main(["--mark-dead"], http=FakeHttp())
    http = FakeHttp()

    assert check_links.main(["--mark-dead"], http=http) is None

    assert http.calls == []


def test_dead_postings_are_not_checked_again(db):
    urls = _urls(2)
    _seed(urls)
    check_links.main(["--mark-dead"], http=FakeHttp({urls[0]: FakeResponse(410, "gone")}))
    http = FakeHttp()

    # recheck-days 0 would make a checked posting due again; a dead one never is.
    check_links.main(["--mark-dead", "--recheck-days", "0"], http=http)

    assert urls[0] not in http.calls


def test_exits_1_on_config_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise config.ConfigError("bad config.yaml")

    monkeypatch.setattr(check_links, "load_config", boom)
    with pytest.raises(SystemExit) as exc:
        check_links.main([], http=FakeHttp())
    assert exc.value.code == 1
    assert "bad config.yaml" in capsys.readouterr().err


def test_summary_of_a_dry_run(db, caplog):
    caplog.set_level(logging.INFO, logger="jobhunter.check_links")
    urls = _urls(4)
    _seed(urls)
    http = FakeHttp({urls[0]: FakeResponse(404, "gone"), urls[1]: FakeResponse(500, "oops")})

    check_links.main(["--limit", "3"], http=http)

    assert "Checking 3 of 4 queued" in caplog.text
    summary = caplog.messages[-1]
    assert "checked 3 of 4 queued" in summary
    assert "1 live, 1 dead, 1 unknown" in summary
    assert "Marked 0 dead (dry run" in summary
    assert "4 still queued" in summary  # a dry run takes nothing off the queue


def test_summary_of_a_mark_dead_run(db, caplog):
    caplog.set_level(logging.INFO, logger="jobhunter.check_links")
    urls = _urls(4)
    _seed(urls)
    http = FakeHttp({urls[0]: FakeResponse(404, "gone"), urls[1]: FakeResponse(500, "oops")})

    check_links.main(["--limit", "3", "--mark-dead"], http=http)

    summary = caplog.messages[-1]
    assert "checked 3 of 4 queued" in summary
    assert "1 live, 1 dead, 1 unknown" in summary
    assert "Marked 1 dead." in summary
    assert "dry run" not in summary
    assert "1 still queued" in summary


# --- --dead-out ----------------------------------------------------------------


@pytest.mark.parametrize("extra_args", [[], ["--mark-dead"]])
def test_dead_out_lists_the_dead_postings(db, tmp_path, extra_args):
    urls = _urls(4)
    ids = _seed(urls)
    out = tmp_path / "dead_links.txt"
    http = FakeHttp(
        {
            urls[0]: FakeResponse(404, "gone"),
            urls[2]: FakeResponse(410, "gone"),
            urls[3]: FakeResponse(503, "busy"),
        }
    )

    check_links.main(["--dead-out", str(out)] + extra_args, http=http)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("#")
    assert sorted(lines[1:]) == sorted(
        [f"{ids[0]}\t{urls[0]}\thttp 404", f"{ids[2]}\t{urls[2]}\thttp 410"]
    )
