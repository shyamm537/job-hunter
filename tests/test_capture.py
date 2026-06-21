import json
import os

import pytest

from src.ingestion.capture import (
    ENV_VAR,
    dump_jobs,
    load_jobs,
    new_temp_dump_dir,
    resolve_dump_dir,
)
from src.storage.models import JobPost


def _job(i):
    return JobPost(
        job_board_id=f"gh-{i}", title=f"Role {i}", company="acme",
        location="Remote", description="d", url=f"http://x/{i}",
    )


def test_dump_jobs_writes_valid_json(tmp_path):
    jobs = [_job(1), _job(2)]
    path = dump_jobs("greenhouse[acme]", jobs, str(tmp_path))
    assert os.path.exists(path)
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["count"] == 2
    assert payload["label"] == "greenhouse[acme]"
    assert len(payload["jobs"]) == 2
    assert payload["jobs"][0]["job_board_id"] == "gh-1"


def test_dump_filename_is_slugged(tmp_path):
    path = dump_jobs("seek[data analyst @ Adelaide]", [_job(1)], str(tmp_path))
    name = os.path.basename(path)
    assert name.startswith("seek-data-analyst-adelaide-")
    assert name.endswith(".json")


def test_dump_round_trips_through_load(tmp_path):
    jobs = [_job(1), _job(2)]
    path = dump_jobs("lever[figma]", jobs, str(tmp_path))
    loaded = load_jobs(path)
    assert [j.job_board_id for j in loaded] == ["gh-1", "gh-2"]
    assert isinstance(loaded[0], JobPost)


def test_resolve_dump_dir_unset(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_dump_dir() is None


def test_resolve_dump_dir_explicit_path(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, str(tmp_path))
    assert resolve_dump_dir() == str(tmp_path)


def test_resolve_dump_dir_auto_creates_temp(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "temp")
    d = resolve_dump_dir()
    assert d is not None and os.path.isdir(d)


def test_new_temp_dump_dir_exists():
    d = new_temp_dump_dir()
    assert os.path.isdir(d)


def test_dump_creates_missing_dir(tmp_path):
    nested = tmp_path / "a" / "b"
    path = dump_jobs("seek", [_job(1)], str(nested))
    assert os.path.exists(path)


@pytest.mark.parametrize("label,expected_prefix", [
    ("", "scrape-"),
    ("   ", "scrape-"),
    ("Greenhouse[Acme]!!", "greenhouse-acme-"),
])
def test_slug_edge_cases(tmp_path, label, expected_prefix):
    path = dump_jobs(label, [_job(1)], str(tmp_path))
    assert os.path.basename(path).startswith(expected_prefix)
