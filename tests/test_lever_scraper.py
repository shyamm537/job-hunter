from unittest.mock import patch

from src.ingestion.lever import LeverScraper

# Lever returns a top-level JSON array of postings.
FAKE_RESPONSE = [
    {
        "text": "Data Analyst",
        "hostedUrl": "https://jobs.lever.co/acme/1",
        "categories": {"location": "Remote", "team": "Data"},
        "descriptionPlain": "A great data analyst role.",
    },
    {
        "text": "Head Chef",
        "hostedUrl": "https://jobs.lever.co/acme/2",
        "categories": {"location": "New York"},
        "descriptionPlain": "Cook things.",
    },
]


@patch("src.ingestion.lever.get_json", return_value=FAKE_RESPONSE)
def test_lever_scraper_parses_entries(mock_get):
    jobs = LeverScraper(company="acme").scrape()

    assert len(jobs) == 2
    job = jobs[0]
    assert job.title == "Data Analyst"
    assert job.company == "acme"
    assert job.location == "Remote"
    assert job.url == "https://jobs.lever.co/acme/1"
    assert job.job_board_id.startswith("lever-")
    mock_get.assert_called_once()


@patch("src.ingestion.lever.get_json", return_value=FAKE_RESPONSE)
def test_lever_scraper_title_filter(mock_get):
    jobs = LeverScraper(company="acme", title_contains="analyst").scrape()
    assert len(jobs) == 1
    assert jobs[0].title == "Data Analyst"


@patch("src.ingestion.lever.get_json", return_value=FAKE_RESPONSE)
def test_lever_job_board_id_is_deterministic(mock_get):
    first = LeverScraper(company="acme").scrape()
    second = LeverScraper(company="acme").scrape()
    assert first[0].job_board_id == second[0].job_board_id
    assert first[0].job_board_id != first[1].job_board_id
