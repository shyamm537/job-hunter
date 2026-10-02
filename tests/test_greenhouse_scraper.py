from datetime import datetime
from unittest.mock import patch

from src.ingestion.greenhouse import GreenhouseScraper

FAKE_RESPONSE = {
    "jobs": [
        {
            "title": "Data Analyst",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
            "location": {"name": "Adelaide, AU"},
            "content": "A great data analyst role.",
        },
        {
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/456",
            "location": {"name": "Remote"},
            "content": "Go build APIs.",
        },
    ]
}


@patch("src.ingestion.greenhouse.get_json", return_value=FAKE_RESPONSE)
def test_greenhouse_scraper_parses_entries(mock_get):
    scraper = GreenhouseScraper(board="acme")
    jobs = scraper.scrape()

    assert len(jobs) == 2
    job = jobs[0]
    assert job.title == "Data Analyst"
    assert job.company == "acme"
    assert job.location == "Adelaide, AU"
    assert job.url == "https://boards.greenhouse.io/acme/jobs/123"
    assert job.job_board_id.startswith("greenhouse-")
    mock_get.assert_called_once()


@patch("src.ingestion.greenhouse.get_json", return_value=FAKE_RESPONSE)
def test_greenhouse_scraper_title_filter(mock_get):
    scraper = GreenhouseScraper(board="acme", title_contains="analyst")
    jobs = scraper.scrape()

    assert len(jobs) == 1
    assert jobs[0].title == "Data Analyst"


@patch("src.ingestion.greenhouse.get_json", return_value=FAKE_RESPONSE)
def test_greenhouse_job_board_id_is_deterministic(mock_get):
    scraper = GreenhouseScraper(board="acme")
    first = scraper.scrape()
    second = scraper.scrape()

    assert first[0].job_board_id == second[0].job_board_id
    # Distinct postings get distinct ids.
    assert first[0].job_board_id != first[1].job_board_id


DATED_RESPONSE = {
    "jobs": [
        {
            "title": "Both dates",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "first_published": "2026-06-14T10:55:28-04:00",
            "updated_at": "2026-06-20T09:00:00-04:00",
        },
        {
            "title": "Only updated_at",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
            "updated_at": "2026-06-20T09:00:00-04:00",
        },
        {
            "title": "No dates",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/3",
        },
    ]
}


@patch("src.ingestion.greenhouse.get_json", return_value=DATED_RESPONSE)
def test_greenhouse_sets_posted_at(mock_get):
    both, only_updated, neither = GreenhouseScraper(board="acme").scrape()

    # first_published wins; the -04:00 offset is converted to naive UTC.
    assert both.posted_at == datetime(2026, 6, 14, 14, 55, 28)
    assert both.posted_at.tzinfo is None
    # No first_published: fall back to updated_at.
    assert only_updated.posted_at == datetime(2026, 6, 20, 13, 0, 0)
    assert neither.posted_at is None
