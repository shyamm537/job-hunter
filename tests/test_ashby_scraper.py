from datetime import datetime
from unittest.mock import patch

from src.ingestion.ashby import AshbyScraper

# Ashby returns {"jobs": [...]}; remote is a separate boolean, not in `location`.
FAKE_RESPONSE = {
    "jobs": [
        {
            "title": "Analytics Engineer",
            "location": "Portugal",
            "isRemote": True,
            "isListed": True,
            "jobUrl": "https://jobs.ashbyhq.com/acme/1",
            "descriptionPlain": "Build pipelines.",
            "publishedAt": "2026-06-21T03:12:45.393+00:00",
        },
        {
            "title": "Office Manager",
            "location": "Lisbon",
            "isRemote": False,
            "isListed": True,
            "jobUrl": "https://jobs.ashbyhq.com/acme/2",
            "descriptionPlain": "Run the office.",
        },
        {
            "title": "Hidden Role",
            "location": "Nowhere",
            "isRemote": False,
            "isListed": False,  # unlisted -> skipped
            "jobUrl": "https://jobs.ashbyhq.com/acme/3",
            "descriptionPlain": "secret",
        },
    ]
}


@patch("src.ingestion.ashby.get_json", return_value=FAKE_RESPONSE)
def test_ashby_parses_and_skips_unlisted(mock_get):
    jobs = AshbyScraper(org="acme").scrape()
    # unlisted posting dropped
    assert len(jobs) == 2
    titles = {j.title for j in jobs}
    assert titles == {"Analytics Engineer", "Office Manager"}
    mock_get.assert_called_once()


@patch("src.ingestion.ashby.get_json", return_value=FAKE_RESPONSE)
def test_ashby_folds_remote_into_location(mock_get):
    jobs = AshbyScraper(org="acme").scrape()
    analytics = next(j for j in jobs if j.title == "Analytics Engineer")
    # isRemote True but location lacks 'remote' -> folded in so the filter sees it
    assert "remote" in analytics.location.lower()
    office = next(j for j in jobs if j.title == "Office Manager")
    assert office.location == "Lisbon"  # not remote, untouched


@patch("src.ingestion.ashby.get_json", return_value=FAKE_RESPONSE)
def test_ashby_fields_and_id(mock_get):
    job = AshbyScraper(org="acme").scrape()[0]
    assert job.company == "acme"
    assert job.url == "https://jobs.ashbyhq.com/acme/1"
    assert job.job_board_id.startswith("ashby-")
    assert job.description == "Build pipelines."


@patch("src.ingestion.ashby.get_json", return_value=FAKE_RESPONSE)
def test_ashby_job_board_id_deterministic(mock_get):
    a = AshbyScraper(org="acme").scrape()
    b = AshbyScraper(org="acme").scrape()
    assert a[0].job_board_id == b[0].job_board_id
    assert a[0].job_board_id != a[1].job_board_id


@patch("src.ingestion.ashby.get_json", return_value=FAKE_RESPONSE)
def test_ashby_sets_posted_at_from_published_at(mock_get):
    jobs = AshbyScraper(org="acme").scrape()
    analytics = next(j for j in jobs if j.title == "Analytics Engineer")
    office = next(j for j in jobs if j.title == "Office Manager")
    assert analytics.posted_at == datetime(2026, 6, 21, 3, 12, 45, 393000)  # naive UTC
    assert analytics.posted_at.tzinfo is None
    assert office.posted_at is None  # no publishedAt on that posting
