"""Tests for the Flask dashboard (src/app): the jobs list and its filters,
status changes, a job's page, the "Add a job" form, and the local-only guards.

Flask's test client against a temp database; data/ is never touched.
"""

import pytest
from sqlmodel import select

import src.config as config
import src.storage.database as database
from src.app import create_app
from src.storage.database import get_session
from src.storage.models import JobPost


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv(database.ENV_VAR, raising=False)
    cfg = config.Config.model_validate(
        {
            "sources": [{"type": "greenhouse", "board": "acme"}],
            "database": {"url": f"sqlite:///{tmp_path}/jobs.db"},
        }
    )
    app = create_app(cfg)
    app.testing = True
    yield app.test_client()
    database._database_url = None
    database._engine = None


def _seed(*jobs):
    with get_session() as session:
        session.add_all(jobs)
        session.commit()
        return [job.id for job in jobs]


def _job(n, **overrides):
    values = dict(
        job_board_id=f"greenhouse-{n}", title=f"Data Analyst {n}", company="acme",
        location="Adelaide", description="A role.", url=f"https://boards.greenhouse.io/acme/jobs/{n}",
    )
    values.update(overrides)
    return JobPost(**values)


def _row(job_id):
    with get_session() as session:
        return session.get(JobPost, job_id)


def _post(client, path, data):
    return client.post(path, data=data, headers={"Origin": "http://localhost"})


# --- the jobs list ---------------------------------------------------------------


def test_empty_list_says_how_to_get_jobs(client):
    page = client.get("/").get_data(as_text=True)
    assert "No jobs yet" in page and "make scrape" in page


def test_list_shows_jobs_with_links(client):
    (job_id,) = _seed(_job(1))
    page = client.get("/").get_data(as_text=True)
    assert "Data Analyst 1" in page
    assert f'href="/jobs/{job_id}"' in page
    assert "1 job(s)" in page


def test_list_never_loads_heavy_text(client):
    _seed(_job(1, description="SECRET-DESCRIPTION", generated_cover_letter="SECRET-LETTER"))
    page = client.get("/").get_data(as_text=True)
    assert "SECRET-DESCRIPTION" not in page and "SECRET-LETTER" not in page


@pytest.mark.parametrize(
    "query, expected",
    [
        ("status=Applied", {"Applied one"}),
        ("source=adzuna", {"Adzuna one"}),
        ("company=other", {"Other company"}),
        ("location=Sydney", {"Sydney one"}),
        ("q=adzuna", {"Adzuna one"}),
        ("status=Applied&status=To+Apply&location=Sydney", {"Sydney one"}),
    ],
)
def test_filters_narrow_the_list(client, query, expected):
    _seed(
        _job(1, title="Applied one", status="Applied"),
        _job(2, title="Sydney one", location="Sydney"),
        _job(3, title="Other company", company="other"),
        JobPost(job_board_id="adzuna-9", title="Adzuna one", company="acme",
                location="Adelaide", description="d", url="https://www.adzuna.com.au/details/9"),
    )
    page = client.get(f"/?{query}").get_data(as_text=True)
    titles = {"Applied one", "Sydney one", "Other company", "Adzuna one"}
    assert {t for t in titles if t in page} == expected
    assert "matching filters" in page


def test_no_match_message(client):
    _seed(_job(1))
    page = client.get("/?q=nothing-like-this").get_data(as_text=True)
    assert "No jobs match the current filters" in page


def test_filter_options_reflect_the_data(client):
    _seed(_job(1, company="acme"), _job(2, company="globex", location="Perth"))
    page = client.get("/").get_data(as_text=True)
    for option in ("<option>acme</option>", "<option>globex</option>", "<option>Perth</option>"):
        assert option in page
    assert 'value="greenhouse"' in page  # the source checkbox


# --- status changes ----------------------------------------------------------------


def test_status_change_saves_and_returns_to_the_filtered_list(client):
    (job_id,) = _seed(_job(1))
    resp = _post(client, f"/jobs/{job_id}/status",
                 {"status": "Applied", "next": "/?status=To+Apply&q=data"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/?status=To+Apply&q=data"
    assert _row(job_id).status == "Applied"


def test_status_change_never_redirects_off_site(client):
    (job_id,) = _seed(_job(1))
    resp = _post(client, f"/jobs/{job_id}/status", {"status": "Applied", "next": "//evil.example/x"})
    assert resp.headers["Location"] == "/"


def test_unknown_status_is_refused(client):
    (job_id,) = _seed(_job(1))
    resp = _post(client, f"/jobs/{job_id}/status", {"status": "Hired!!"})
    assert resp.status_code == 400
    assert _row(job_id).status == "To Apply"


def test_status_change_on_a_missing_job_is_404(client):
    assert _post(client, "/jobs/999/status", {"status": "Applied"}).status_code == 404


# --- a job's page ----------------------------------------------------------------


def test_job_page_shows_the_full_row(client):
    (job_id,) = _seed(_job(1, description="Line one\nLine two", generated_cover_letter="Dear Acme",
                           generated_cold_email="Hi there"))
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Data Analyst 1" in page and "Adelaide" in page
    assert "Line one\nLine two" in page
    assert "Dear Acme" in page and "Hi there" in page
    assert 'href="https://boards.greenhouse.io/acme/jobs/1"' in page


def test_job_page_without_materials_points_at_make_process(client):
    (job_id,) = _seed(_job(1))
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert page.count("Not generated yet. Use the button, or run <code>make process</code>.") == 2
    assert "Contact lookup not run yet" in page


def test_guessed_contact_is_flagged(client):
    (job_id,) = _seed(_job(1, contact_email="jo@acme.test", contact_name="Jo",
                           contact_confidence="pattern-guess"))
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "guessed" in page and "jo@acme.test (Jo)" in page


def test_published_contact_shows_its_confidence(client):
    (job_id,) = _seed(_job(1, contact_email="jo@acme.test", contact_confidence="published"))
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "jo@acme.test" in page and "published" in page and "guessed" not in page


def test_scraped_html_never_reaches_the_page(client):
    (job_id,) = _seed(_job(1, description="<script>alert(1)</script><b>Bold</b> <img src=x onerror=y>"))
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "<script" not in page and "alert(1)" not in page and "onerror" not in page
    assert "Bold" in page


def test_double_escaped_greenhouse_html_reads_as_text(client):
    raw = ("&lt;div class=&quot;content-intro&quot;&gt;&lt;p&gt;We&amp;#39;re hiring &amp;amp; growing."
           "&lt;/p&gt;&lt;ul&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;")
    (job_id,) = _seed(_job(1, description=raw))
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "We&#39;re hiring &amp; growing." in page  # escaped once, for HTML
    assert "• SQL" in page and "• Python" in page
    assert "&lt;p&gt;" not in page and "content-intro" not in page


def test_plain_text_filter():
    from src.app.web import plain_text
    assert plain_text("&lt;p&gt;One&lt;/p&gt;&lt;p&gt;Two&lt;/p&gt;") == "One\n\nTwo"
    assert plain_text("Already plain\nwith lines") == "Already plain\nwith lines"
    assert plain_text("<style>p{}</style>Text") == "Text"
    assert plain_text("") == ""


def test_non_http_urls_are_not_links(client):
    (job_id,) = _seed(_job(1, url="javascript:alert(1)"))
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert 'href="javascript:' not in page
    assert "javascript:alert(1)" in page  # still shown, as text


def test_missing_job_is_404(client):
    resp = client.get("/jobs/12345")
    assert resp.status_code == 404
    assert "doesn't exist" in resp.get_data(as_text=True)


# --- adding a job ----------------------------------------------------------------


def _rows():
    with get_session() as session:
        return session.exec(select(JobPost)).all()


def test_add_form_renders(client):
    page = client.get("/jobs/new").get_data(as_text=True)
    assert 'name="description"' in page and "Add job" in page


def test_adding_a_job_goes_to_its_page(client):
    resp = _post(client, "/jobs/new", {
        "title": "ML Engineer", "company": "Canva", "location": "Sydney",
        "url": "https://www.linkedin.com/jobs/view/7", "description": "Build models.",
    })
    (row,) = _rows()
    assert resp.status_code == 302 and resp.headers["Location"] == f"/jobs/{row.id}"
    assert row.job_board_id.startswith("manual-")
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Added ML Engineer at Canva" in page


def test_adding_the_same_job_again_says_so(client):
    data = {"title": "ML Engineer", "company": "Canva", "description": "Build models.",
            "url": "https://www.linkedin.com/jobs/view/7"}
    _post(client, "/jobs/new", data)
    resp = _post(client, "/jobs/new", data)
    assert len(_rows()) == 1
    assert "already in your list" in client.get(resp.headers["Location"]).get_data(as_text=True)


def test_missing_field_keeps_what_you_typed(client):
    resp = _post(client, "/jobs/new", {"title": "ML Engineer", "company": "Canva",
                                       "description": "   "})
    page = resp.get_data(as_text=True)
    assert resp.status_code == 400
    assert "missing description" in page
    assert 'value="ML Engineer"' in page and 'value="Canva"' in page
    assert _rows() == []


# --- local-only guards -------------------------------------------------------------


def test_cross_origin_post_is_refused(client):
    (job_id,) = _seed(_job(1))
    resp = client.post(f"/jobs/{job_id}/status", data={"status": "Rejected"},
                       headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert _row(job_id).status == "To Apply"


def test_cross_origin_referer_is_refused(client):
    resp = client.post("/jobs/new", data={"title": "x", "company": "y", "description": "z"},
                       headers={"Referer": "https://evil.example/page"})
    assert resp.status_code == 403
    assert _rows() == []


def test_same_origin_post_without_headers_is_allowed(client):
    # Non-browser clients (and some privacy settings) send neither header.
    (job_id,) = _seed(_job(1))
    assert client.post(f"/jobs/{job_id}/status", data={"status": "Applied"}).status_code == 302


@pytest.mark.parametrize("host", ["evil.example", "192.168.1.20:8000", "localhost.evil.example"])
def test_foreign_host_header_is_refused(client, host):
    assert client.get("/", headers={"Host": host}).status_code == 400


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:8000"])
def test_local_host_headers_are_served(client, host):
    assert client.get("/", headers={"Host": host}).status_code == 200


def test_stylesheet_is_served_locally(client):
    resp = client.get("/static/style.css")
    assert resp.status_code == 200 and b"--accent" in resp.data
