"""Tests for generating one job's cover letter or cold email from its page
(KAN-43): the buttons, the background run, errors, the reload check, and
`make process` keeping a cold email the dashboard already wrote.

Flask's test client on a temp database; the LLM client is a fake.
"""

import pytest

import src.app.generate as generate
import src.config as config
import src.storage.database as database
from src.app import create_app
from src.storage.database import get_session
from src.storage.models import JobPost


class FakeLLM:
    def __init__(self, reply="Generated text.", error=None):
        self.reply, self.error, self.prompts = reply, error, []

    def generate(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.reply


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(generate, "get_llm_client", lambda settings: fake)
    return fake


@pytest.fixture
def app(tmp_path, monkeypatch, llm):
    monkeypatch.delenv(database.ENV_VAR, raising=False)
    cfg = config.Config.model_validate(
        {"sources": [{"type": "greenhouse", "board": "acme"}],
         "database": {"url": f"sqlite:///{tmp_path}/jobs.db"},
         "resume_summary": "Five years of SQL."}
    )
    app = create_app(cfg)
    app.testing = True
    yield app
    database._database_url = None
    database._engine = None


@pytest.fixture
def client(app):
    return app.test_client()


def _seed(**overrides):
    values = dict(job_board_id="greenhouse-1", title="Data Analyst", company="acme",
                  location="Adelaide", description="Build dashboards.",
                  url="https://boards.greenhouse.io/acme/jobs/1")
    values.update(overrides)
    with get_session() as session:
        job = JobPost(**values)
        session.add(job)
        session.commit()
        return job.id


def _row(job_id):
    with get_session() as session:
        return session.get(JobPost, job_id)


def _generate(client, job_id, kind):
    return client.post(f"/jobs/{job_id}/generate", data={"kind": kind},
                       headers={"Origin": "http://localhost"})


def _wait(app):
    app.extensions["generator"].wait()


def test_job_page_offers_both_buttons(client):
    job_id = _seed()
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Generate cover letter</button>" in page
    assert "Generate email</button>" in page


def test_existing_text_offers_regenerate(client):
    job_id = _seed(generated_cover_letter="Old letter")
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Regenerate cover letter</button>" in page and "Generate email</button>" in page


@pytest.mark.parametrize("kind, column, other", [
    ("cover_letter", "generated_cover_letter", "generated_cold_email"),
    ("cold_email", "generated_cold_email", "generated_cover_letter"),
])
def test_generates_only_that_material_for_that_job(app, client, llm, kind, column, other):
    job_id = _seed()
    untouched = _seed(job_board_id="greenhouse-2")
    resp = _generate(client, job_id, kind)
    assert resp.headers["Location"] == f"/jobs/{job_id}"
    _wait(app)
    row = _row(job_id)
    assert getattr(row, column) == "Generated text." and getattr(row, other) is None
    assert _row(untouched).generated_cover_letter is None
    assert len(llm.prompts) == 1
    assert "Build dashboards." in llm.prompts[0] and "Five years of SQL." in llm.prompts[0]


def test_regenerate_replaces_the_text(app, client):
    job_id = _seed(generated_cold_email="Old email")
    _generate(client, job_id, "cold_email")
    _wait(app)
    assert _row(job_id).generated_cold_email == "Generated text."


def test_page_shows_generating_while_it_runs(app, client):
    job_id = _seed()
    generator = app.extensions["generator"]
    generator._running.add((job_id, "cover_letter"))  # as if a thread were mid-call
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Generating&hellip;</button>" in page and "location.reload()" in page
    assert "Generate email</button>" in page
    assert client.get(f"/jobs/{job_id}/generating").get_json() == {"running": ["cover_letter"]}
    assert not generator.start(job_id, "cover_letter")  # no second run of the same thing


def test_reload_check_is_empty_when_done(app, client):
    job_id = _seed()
    _generate(client, job_id, "cover_letter")
    _wait(app)
    assert client.get(f"/jobs/{job_id}/generating").get_json() == {"running": []}
    assert "location.reload()" not in client.get(f"/jobs/{job_id}").get_data(as_text=True)


def test_a_failure_is_shown_once_on_the_job_page(app, client, llm):
    llm.error = ConnectionError("Ollama is not running")
    job_id = _seed()
    _generate(client, job_id, "cold_email")
    _wait(app)
    assert _row(job_id).generated_cold_email is None
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Couldn&#39;t generate the cold email: Ollama is not running" in page
    assert "Ollama is not running" not in client.get(f"/jobs/{job_id}").get_data(as_text=True)


def test_an_unreachable_server_gets_a_plain_message(app, client, llm):
    import requests

    llm.error = requests.ConnectionError("HTTPConnectionPool(...) refused")
    job_id = _seed()
    _generate(client, job_id, "cover_letter")
    _wait(app)
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Is Ollama running?" in page and "HTTPConnectionPool" not in page


def test_an_empty_reply_is_an_error_not_a_blank_letter(app, client, llm):
    llm.reply = ""
    job_id = _seed()
    _generate(client, job_id, "cover_letter")
    _wait(app)
    assert _row(job_id).generated_cover_letter is None
    assert "empty response" in client.get(f"/jobs/{job_id}").get_data(as_text=True)


def test_unknown_material_or_job(client):
    job_id = _seed()
    assert _generate(client, job_id, "resume").status_code == 400
    assert _generate(client, 999, "cover_letter").status_code == 404


def test_cross_origin_generate_is_refused(client):
    job_id = _seed()
    resp = client.post(f"/jobs/{job_id}/generate", data={"kind": "cover_letter"},
                       headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_make_process_keeps_a_cold_email_from_the_dashboard(tmp_path, monkeypatch):
    from src.llm import cli

    cfg = config.Config.model_validate(
        {"sources": [{"type": "greenhouse", "board": "acme"}],
         "database": {"url": f"sqlite:///{tmp_path}/jobs.db"}}
    )
    fake = FakeLLM(reply="Batch text.")
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cli, "get_llm_client", lambda settings: fake)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    database.set_database_url(cfg.database.url)
    database.init_db()
    try:
        job_id = _seed(generated_cold_email="Written on the job page")
        cli.main()
        row = _row(job_id)
        assert row.generated_cover_letter == "Batch text."
        assert row.generated_cold_email == "Written on the job page"
        assert len(fake.prompts) == 1
    finally:
        database._database_url = None
        database._engine = None


def test_a_timeout_says_how_to_raise_the_limit(app, client, llm):
    import requests

    llm.error = requests.ReadTimeout("read timed out")
    job_id = _seed()
    _generate(client, job_id, "cover_letter")
    _wait(app)
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "longer than 600 seconds" in page and "llm.timeout" in page
