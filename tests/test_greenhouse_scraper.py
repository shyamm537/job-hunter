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
