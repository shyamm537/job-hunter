"""Tests for the database-URL resolution chain in src/storage/database.py.

Resolution priority is: explicit set_database_url() > JOBHUNTER_DATABASE_URL
env var > database.url from config.yaml > the SQLite default. These were the
trickiest bit of the storage wiring and previously had no committed coverage.
"""

import pytest

import src.config as config
import src.storage.database as database


@pytest.fixture(autouse=True)
def _reset_db_state(monkeypatch):
    # Each test starts with no explicit override, no env var, and a fresh
    # (uncached) engine, and leaves the module globals clean afterwards.
    monkeypatch.delenv(database.ENV_VAR, raising=False)
    database._database_url = None
    database._engine = None
    yield
    database._database_url = None
    database._engine = None


def _config_returning(url: str):
    cfg = config.Config.model_validate(
        {"search": {"title": "x"}, "database": {"url": url}}
    )
    return lambda *args, **kwargs: cfg


def test_explicit_override_wins_over_env_and_config(monkeypatch):
    monkeypatch.setenv(database.ENV_VAR, "sqlite:///from-env.db")
    monkeypatch.setattr(config, "load_config", _config_returning("sqlite:///from-config.db"))
    database.set_database_url("sqlite:///explicit.db")
    assert database._resolve_url() == "sqlite:///explicit.db"


def test_env_var_used_when_no_override(monkeypatch):
    monkeypatch.setenv(database.ENV_VAR, "postgresql://localhost/jobs")
    # Even if config would say otherwise, env beats config.
    monkeypatch.setattr(config, "load_config", _config_returning("sqlite:///from-config.db"))
    assert database._resolve_url() == "postgresql://localhost/jobs"


def test_config_used_when_no_override_or_env(monkeypatch):
    monkeypatch.setattr(config, "load_config", _config_returning("sqlite:///from-config.db"))
    assert database._resolve_url() == "sqlite:///from-config.db"


def test_default_when_config_unavailable(monkeypatch):
    def boom(*args, **kwargs):
        raise config.ConfigError("no config.yaml")

    monkeypatch.setattr(config, "load_config", boom)
    assert database._resolve_url() == database.DEFAULT_DATABASE_URL


def test_set_database_url_rebuilds_engine():
    database.set_database_url("sqlite:///a.db")
    first = database.get_engine()
    assert database.get_engine() is first  # cached within a URL

    database.set_database_url("sqlite:///b.db")
    second = database.get_engine()
    assert second is not first
    assert str(second.url) == "sqlite:///b.db"


def test_init_db_creates_sqlite_parent_dir(tmp_path):
    db = tmp_path / "nested" / "deeper" / "jobs.db"
    assert not db.parent.exists()
    database.set_database_url(f"sqlite:///{db}")
    database.init_db()
    assert db.parent.exists()
