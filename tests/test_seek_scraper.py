from unittest.mock import patch

from src.ingestion.seek import SeekScraper


class FakeEntry:
    title = "Data Analyst"
    link = "https://www.seek.com.au/job/123456"
    summary = "A great data analyst role in Adelaide."
    author = "Acme Corp"


class FakeFeed:
    entries = [FakeEntry()]


@patch("src.ingestion.seek.feedparser.parse", return_value=FakeFeed())
def test_seek_scraper_parses_entries(mock_parse):
    scraper = SeekScraper(search_terms="data analyst", location="Adelaide")
    jobs = scraper.scrape()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Data Analyst"
    assert job.company == "Acme Corp"
    assert job.job_board_id.startswith("seek-")
    assert job.url == FakeEntry.link
    mock_parse.assert_called_once()


@patch("src.ingestion.seek.feedparser.parse", return_value=FakeFeed())
def test_seek_scraper_job_board_id_is_deterministic(mock_parse):
    scraper = SeekScraper(search_terms="data analyst", location="Adelaide")
    first_run = scraper.scrape()
    second_run = scraper.scrape()

    assert first_run[0].job_board_id == second_run[0].job_board_id
