from src.config import Filters
from src.ingestion.filtering import job_matches, location_matches, title_matches
from src.storage.models import JobPost


def test_title_matches_empty_is_true():
    assert title_matches("Anything", []) is True


def test_title_matches_substring_case_insensitive():
    assert title_matches("Senior Data Analyst", ["data analyst"]) is True
    assert title_matches("Chef", ["data analyst"]) is False


def test_location_empty_is_true():
    assert location_matches("Mars", []) is True


def test_location_remote_always_passes():
    assert location_matches("Remote - US", ["Adelaide"]) is True


def test_location_substring():
    assert location_matches("Adelaide, SA", ["adelaide"]) is True
    assert location_matches("Sydney", ["adelaide"]) is False


def _job(title, location):
    return JobPost(
        job_board_id="x", title=title, company="c", location=location,
        description="d", url="u",
    )


def test_job_matches_both_dimensions():
    f = Filters(titles=["analyst"], locations=["adelaide"])
    assert job_matches(_job("Data Analyst", "Adelaide, SA"), f) is True
    assert job_matches(_job("Data Analyst", "Sydney"), f) is False  # wrong location
    assert job_matches(_job("Chef", "Adelaide"), f) is False         # wrong title
    assert job_matches(_job("Analyst", "Remote"), f) is True         # remote passes
