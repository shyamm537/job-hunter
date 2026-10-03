from datetime import datetime
from unittest.mock import patch

import pytest
import requests

from src.ingestion.workable import WorkableScraper


def _loc(city="", region=None, country="Australia", code="AU", hidden=False):
    return {"country": country, "countryCode": code, "city": city, "region": region, "hidden": hidden}


def _entry(**over):
    """A widget entry with the fields seen on live boards (2026-10-03)."""
    base = {
        "title": "Data Analyst",
        "shortcode": "AAAA111111",
        "code": "",
        "employment_type": "Full-time",
        "telecommuting": False,
        "department": "Data",
        "url": "https://apply.workable.com/j/AAAA111111",
        "shortlink": "https://apply.workable.com/j/AAAA111111",
        "application_url": "https://apply.workable.com/j/AAAA111111/apply",
        "published_on": "2026-09-21",
        "created_at": "2026-09-20",
        "country": "Australia",
        "city": "Sydney",
        "state": "New South Wales",
        "locations": [_loc("Sydney", "New South Wales")],
        "description": "<p>Build <b>dashboards</b>.</p><ul><li>SQL</li><li>Python</li></ul>",
    }
    base.update(over)
    return base


# Trimmed from the legalvision and squiz captures: one posting repeated per
# location (Manchester + London), a remote country-only entry repeated with a
# city (New Zealand + Christchurch), a plain posting, and a remote US posting.
FAKE_RESPONSE = {
    "name": "Acme Pty Ltd",  # display name; the scraper must NOT use it as company
    "description": "<p>Company blurb.</p>",
    "jobs": [
        _entry(
            title="Construction Lawyer 1-3 PQE", shortcode="2569874C2B",
            url="https://apply.workable.com/j/2569874C2B",
            city="Manchester", state="England", country="United Kingdom",
            locations=[_loc("Manchester", "England", "United Kingdom", "GB")],
        ),
        _entry(
            title="Construction Lawyer 1-3 PQE", shortcode="2569874C2B",
            url="https://apply.workable.com/j/2569874C2B",
            city="London", state="England", country="United Kingdom",
            locations=[_loc("London", "England", "United Kingdom", "GB")],
        ),
        _entry(
            title="Construction Senior Associate", shortcode="3E3E994C13",
            url="https://apply.workable.com/j/3E3E994C13", telecommuting=True,
            city="", state="", country="New Zealand",
            locations=[_loc("", None, "New Zealand", "NZ")],
        ),
        _entry(
            title="Construction Senior Associate", shortcode="3E3E994C13",
            url="https://apply.workable.com/j/3E3E994C13", telecommuting=True,
            city="Christchurch", state="Canterbury Region", country="New Zealand",
            locations=[_loc("Christchurch", "Canterbury Region", "New Zealand", "NZ")],
        ),
        _entry(),  # Data Analyst, Sydney
        _entry(
            title="Senior Account Executive - DXP", shortcode="EB6B3555A0",
            url="https://apply.workable.com/j/EB6B3555A0", telecommuting=True,
            city="", state="", country="United States",
            locations=[_loc("", None, "United States", "US")],
        ),
    ],
}


def _scrape(response=FAKE_RESPONSE, account="acme"):
    with patch("src.ingestion.workable.get_json", return_value=response) as mock_get:
        jobs = WorkableScraper(account=account).scrape()
    return jobs, mock_get


def _job(jobs, title):
    return next(j for j in jobs if j.title == title)


def test_repeated_entries_become_one_posting():
    jobs, mock_get = _scrape()
    # 6 entries, 4 distinct postings (shortcodes), in first-seen order.
    assert [j.title for j in jobs] == [
        "Construction Lawyer 1-3 PQE",
        "Construction Senior Associate",
        "Data Analyst",
        "Senior Account Executive - DXP",
    ]
    mock_get.assert_called_once()


def test_calls_the_widget_endpoint_with_details_and_the_account_as_given():
    _, mock_get = _scrape(account="  Acme-Co ")
    mock_get.assert_called_once_with(
        "https://apply.workable.com/api/v1/widget/accounts/Acme-Co?details=true"
    )


def test_repeated_posting_lists_every_location():
    lawyer = _job(_scrape()[0], "Construction Lawyer 1-3 PQE")
    assert lawyer.location == (
        "Manchester, England, United Kingdom; London, England, United Kingdom"
    )


def test_single_location_is_city_region_country():
    assert _job(_scrape()[0], "Data Analyst").location == "Sydney, New South Wales, Australia"


def test_remote_is_folded_into_location():
    jobs, _ = _scrape()
    nz = _job(jobs, "Construction Senior Associate")
    assert nz.location == "New Zealand; Christchurch, Canterbury Region, New Zealand (Remote)"
    assert "remote" in _job(jobs, "Senior Account Executive - DXP").location.lower()
    # Not remote: untouched.
    assert "remote" not in _job(jobs, "Data Analyst").location.lower()


def test_remote_posting_with_no_place_is_just_remote():
    entry = _entry(telecommuting=True, city="", state="", country="", locations=[])
    jobs, _ = _scrape({"jobs": [entry]})
    assert jobs[0].location == "Remote"


def test_remote_already_in_the_place_is_not_doubled():
    entry = _entry(telecommuting=True, locations=[_loc("Remote - Sydney", None)])
    jobs, _ = _scrape({"jobs": [entry]})
    assert jobs[0].location == "Remote - Sydney, Australia"


def test_several_locations_in_one_list_are_joined():
    entry = _entry(locations=[_loc("Sydney", "New South Wales"), _loc("Melbourne", "Victoria")])
    jobs, _ = _scrape({"jobs": [entry]})
    assert jobs[0].location == (
        "Sydney, New South Wales, Australia; Melbourne, Victoria, Australia"
    )


def test_hidden_location_is_skipped_and_does_not_fall_back():
    entry = _entry(
        city="Secretville", locations=[_loc("Sydney", "New South Wales"), _loc("Hidden", hidden=True)]
    )
    jobs, _ = _scrape({"jobs": [entry]})
    assert jobs[0].location == "Sydney, New South Wales, Australia"
    only_hidden = _entry(locations=[_loc("Hidden", hidden=True)])
    jobs, _ = _scrape({"jobs": [only_hidden]})
    assert jobs[0].location == ""


def test_falls_back_to_top_level_fields_when_locations_missing_or_empty():
    for locs in ([], None):
        entry = _entry(locations=locs, city="Perth", state="Western Australia", country="Australia")
        jobs, _ = _scrape({"jobs": [entry]})
        assert jobs[0].location == "Perth, Western Australia, Australia"


def test_duplicate_places_across_entries_are_listed_once():
    jobs, _ = _scrape({"jobs": [_entry(), _entry()]})
    assert len(jobs) == 1
    assert jobs[0].location == "Sydney, New South Wales, Australia"


def test_id_company_and_url():
    job = _job(_scrape()[0], "Data Analyst")
    assert job.job_board_id.startswith("workable-")
    assert len(job.job_board_id) == len("workable-") + 10
    assert job.company == "acme"  # the account slug, not "Acme Pty Ltd"
    assert job.url == "https://apply.workable.com/j/AAAA111111"


def test_id_is_deterministic_and_the_same_for_repeated_entries():
    first, _ = _scrape()
    second, _ = _scrape()
    assert [j.job_board_id for j in first] == [j.job_board_id for j in second]
    assert len({j.job_board_id for j in first}) == 4


def test_company_keeps_the_slug_case_as_given():
    jobs, _ = _scrape(account="Acme-Co")
    assert jobs[0].company == "Acme-Co"


def test_url_falls_back_to_shortlink_then_to_the_shortcode():
    jobs, _ = _scrape({"jobs": [_entry(url="", shortlink="https://apply.workable.com/j/AAAA111111")]})
    assert jobs[0].url == "https://apply.workable.com/j/AAAA111111"
    jobs, _ = _scrape({"jobs": [_entry(url="", shortlink="")]})
    assert jobs[0].url == "https://apply.workable.com/j/AAAA111111"


def test_description_is_converted_from_html_to_text():
    job = _job(_scrape()[0], "Data Analyst")
    assert job.description == "Build dashboards.\n\n- SQL\n- Python"
    assert "<" not in job.description


def test_posted_at_from_published_on_as_naive_datetime():
    job = _job(_scrape()[0], "Data Analyst")
    assert job.posted_at == datetime(2026, 9, 21)
    assert job.posted_at.tzinfo is None


def test_posted_at_falls_back_to_created_at_then_none():
    jobs, _ = _scrape({"jobs": [_entry(published_on=None, created_at="2026-09-20")]})
    assert jobs[0].posted_at == datetime(2026, 9, 20)
    jobs, _ = _scrape({"jobs": [_entry(published_on="", created_at="not a date")]})
    assert jobs[0].posted_at is None


def test_job_with_missing_optional_fields_does_not_raise():
    jobs, _ = _scrape({"jobs": [{"shortcode": "ZZZZ999999"}]})
    job = jobs[0]
    assert job.title == "Untitled"
    assert job.description == ""
    assert job.location == ""
    assert job.posted_at is None
    assert job.url == "https://apply.workable.com/j/ZZZZ999999"


def test_entry_with_no_identity_is_skipped():
    jobs, _ = _scrape({"jobs": [{"title": "Ghost"}, _entry()]})
    assert [j.title for j in jobs] == ["Data Analyst"]


def test_empty_board_returns_empty_list():
    assert _scrape({"name": "Acme", "description": None, "jobs": []})[0] == []
    assert _scrape({})[0] == []


def test_unknown_account_error_propagates():
    # An unknown account is a 404; get_json raises, so a dead token is caught
    # by `make validate` and a scrape is skipped rather than returning [].
    err = requests.HTTPError("404 Not Found")
    with patch("src.ingestion.workable.get_json", side_effect=err):
        with pytest.raises(requests.HTTPError):
            WorkableScraper(account="no-such-board").scrape()
