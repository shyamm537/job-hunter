from datetime import datetime
from unittest.mock import patch

import pytest
import requests

from src.ingestion.http_util import REQUEST_DELAY
from src.ingestion.workday import (
    LIST_CEILING,
    PAGE_SIZE,
    WorkdayError,
    WorkdayScraper,
    _requisition_id,
)

TENANT, DC, SITE = "acme", "wd3", "Careers"
BASE = "https://acme.wd3.myworkdayjobs.com"

# Rows trimmed from live captures (2026-10-03): CBA, Bunnings, Flinders.
CBA_DS = {
    "title": "Data Scientist",
    "externalPath": "/job/Bangalore---Manyata-Tech-Park-Road/Data-Scientist_REQ265273",
    "locationsText": "Bangalore - Manyata Tech Park Road",
    "postedOn": "Posted 11 Days Ago",
    "bulletFields": ["REQ265273"],
}
CBA_TWO_LOCATIONS = {
    "title": "Senior Legal Counsel, Employment Legal",
    "externalPath": "/job/Sydney-CBD-Area/Senior-Legal-Counsel--Employment-Legal_REQ266540",
    "locationsText": "2 Locations",
    "postedOn": "Posted Today",
    "bulletFields": ["REQ266540"],
}
BUNNINGS_REPOST = {  # "-1" repost marker; the base id is the last bullet
    "title": "Department Manager - Paget Mackay",
    "externalPath": "/job/Paget-Mackay-Warehouse/Department-Manager---Paget-Mackay_R066730-1",
    "locationsText": "2 Locations",
    "postedOn": "Posted 7 Days Ago",
    "bulletFields": ["Permanent", "Queensland", "R066730"],
}
FLINDERS = {  # location packs level and closing date; no postedOn; base id is 2nd bullet
    "title": "Research Associate in Marine Biology",
    "externalPath": "/job/Bedford-Park--Kaurna-Country/Research-Associate-in-Marine-Biology_JR0000017701-1",
    "timeType": "Full time",
    "locationsText": "Bedford Park / Kaurna Country   |   Research Associate Level A   |   Closes 25 Oct 2026",
    "bulletFields": ["Fixed Term (Fixed Term)", "JR0000017701", "Academic", "Research Only"],
}


def _row(n, title=None, **over):
    """A synthetic posting number n (distinct path and requisition id)."""
    row = {
        "title": title or f"Role {n}",
        "externalPath": f"/job/Adelaide/Role-{n}_R{n:06d}",
        "locationsText": "Adelaide",
        "postedOn": "Posted Today",
        "bulletFields": [f"R{n:06d}"],
    }
    row.update(over)
    return row


def _detail(location="Sydney CBD Area", additional=None, start="2026-10-02",
            html="<p>Build <b>models</b>.</p><ul><li>SQL</li><li>Python</li></ul>"):
    return {"jobPostingInfo": {
        "jobDescription": html, "location": location, "additionalLocations": additional,
        "startDate": start, "jobReqId": "x", "externalUrl": "x",
    }}


class FakeWorkday:
    """Stands in for post_json and get_json the way Workday behaves: 20 rows a
    page, `total` on the first page only (0 afterwards), detail looked up by path."""

    def __init__(self, rows, details=None, failing_details=(), fail_page_at=None):
        self.rows = rows
        self.details = details or {}
        self.failing_details = set(failing_details)
        self.fail_page_at = fail_page_at
        self.post_calls = []
        self.get_calls = []

    def post(self, url, body, headers=None, **kw):
        self.post_calls.append((url, body, headers))
        if self.fail_page_at is not None and body["offset"] == self.fail_page_at:
            raise requests.HTTPError("500 Server Error")
        offset, limit = body["offset"], body["limit"]
        return {
            "total": len(self.rows) if offset == 0 else 0,
            "jobPostings": self.rows[offset: offset + limit],
            "facets": [], "userAuthenticated": False,
        }

    def get(self, url, headers=None, **kw):
        self.get_calls.append((url, headers))
        path = url.split(f"/wday/cxs/{TENANT}/{SITE}", 1)[1]
        if path in self.failing_details:
            raise requests.HTTPError("404 Not Found")
        return self.details.get(path, _detail())


def _run(fake, detail_titles=None, **kw):
    with patch("src.ingestion.workday.post_json", side_effect=fake.post), \
         patch("src.ingestion.workday.get_json", side_effect=fake.get), \
         patch("src.ingestion.workday.time.sleep") as sleep:
        jobs = WorkdayScraper(TENANT, DC, SITE, detail_titles=detail_titles, **kw).scrape()
    return jobs, sleep


def _by_title(jobs, title):
    return next(j for j in jobs if j.title == title)


# --- the list ---------------------------------------------------------------------


def test_request_url_body_and_headers():
    fake = FakeWorkday([CBA_DS])
    _run(fake)
    url, body, headers = fake.post_calls[0]
    assert url == f"{BASE}/wday/cxs/acme/Careers/jobs"
    assert body == {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    assert headers == {"Accept": "application/json"}
    assert PAGE_SIZE == 20


def test_several_pages_are_combined_with_a_pause_between_them():
    fake = FakeWorkday([_row(n) for n in range(45)])
    jobs, sleep = _run(fake)
    assert len(jobs) == 45
    # total comes from page one only; later pages report 0, yet all 3 pages are read.
    assert [call[1]["offset"] for call in fake.post_calls] == [0, 20, 40]
    assert [c.args for c in sleep.call_args_list] == [(REQUEST_DELAY,)] * 2


def test_empty_board_returns_an_empty_list_after_one_request():
    fake = FakeWorkday([])
    jobs, _ = _run(fake)
    assert jobs == []
    assert len(fake.post_calls) == 1


def test_a_board_at_the_ceiling_raises_before_reading_more_pages():
    class Capped(FakeWorkday):
        def post(self, url, body, headers=None, **kw):
            self.post_calls.append((url, body, headers))
            return {"total": LIST_CEILING, "jobPostings": [_row(n) for n in range(20)]}

    fake = Capped([])
    with pytest.raises(WorkdayError, match="ceiling"):
        _run(fake)
    assert len(fake.post_calls) == 1


def test_a_board_just_under_the_ceiling_is_read():
    assert LIST_CEILING == 2000
    fake = FakeWorkday([_row(n) for n in range(60)])
    jobs, _ = _run(fake)
    assert len(jobs) == 60


def test_short_list_raises():
    class Short(FakeWorkday):
        def post(self, url, body, headers=None, **kw):
            page = super().post(url, body, headers)
            if body["offset"] == 0:
                page["total"] = 45  # reports 45 but only 40 rows exist
            return page

    with pytest.raises(WorkdayError, match="40 unique postings .* 45"):
        _run(Short([_row(n) for n in range(40)]))


def test_a_posting_repeated_across_pages_counts_once():
    class Wrapped(FakeWorkday):
        # Page 2 repeats the first posting (as offsets past the ceiling do).
        def post(self, url, body, headers=None, **kw):
            page = super().post(url, body, headers)
            if body["offset"] == 20:
                page["jobPostings"] = self.rows[:20]
            return page

    rows = [_row(n) for n in range(30)]
    with pytest.raises(WorkdayError, match="listed 20 unique postings"):
        _run(Wrapped(rows))  # 30 rows were reported, only 20 distinct were seen

    # A duplicate that does not hide a missing posting is simply counted once.
    class Overlap(FakeWorkday):
        def post(self, url, body, headers=None, **kw):
            page = super().post(url, body, headers)
            if body["offset"] == 20:
                page["jobPostings"] = self.rows[19:30]  # repeats row 19
            return page

    jobs, _ = _run(Overlap([_row(n) for n in range(30)]))
    assert len(jobs) == 30


def test_failing_page_raises_and_nothing_is_returned():
    with pytest.raises(requests.HTTPError):
        _run(FakeWorkday([_row(n) for n in range(45)], fail_page_at=20))


def test_dead_board_error_propagates():
    # A wrong site is a 404 and an unknown tenant a 422; post_json raises both.
    with patch("src.ingestion.workday.post_json", side_effect=requests.HTTPError("422")):
        with pytest.raises(requests.HTTPError):
            WorkdayScraper(TENANT, DC, SITE).scrape()


def test_first_page_with_postings_but_no_total_raises():
    class NoTotal(FakeWorkday):
        def post(self, url, body, headers=None, **kw):
            page = super().post(url, body, headers)
            page.pop("total")
            return page

    with pytest.raises(WorkdayError, match="no total"):
        _run(NoTotal([_row(1)]))


def test_posting_with_no_external_path_raises():
    bad = {"title": "Ghost", "locationsText": "Adelaide", "bulletFields": []}
    with pytest.raises(WorkdayError, match="externalPath"):
        _run(FakeWorkday([_row(1), bad]))


def test_postings_missing_optional_keys_do_not_raise():
    bare = {"title": "Bare", "externalPath": "/job/X/Bare_R000001"}  # no other keys
    no_title = {"externalPath": "/job/X/Other_R000002", "locationsText": None, "bulletFields": None}
    jobs, _ = _run(FakeWorkday([bare, no_title]))
    first, second = jobs
    assert (first.title, first.location, first.description, first.posted_at) == ("Bare", "", "", None)
    assert second.title == "Untitled" and second.location == ""


# --- mapping ------------------------------------------------------------------------


def test_url_company_and_list_fields():
    jobs, _ = _run(FakeWorkday([CBA_DS]))
    job = jobs[0]
    assert job.url == f"{BASE}/Careers/job/Bangalore---Manyata-Tech-Park-Road/Data-Scientist_REQ265273"
    assert job.company == TENANT
    assert job.location == "Bangalore - Manyata Tech Park Road"
    assert job.job_board_id.startswith("workday-")
    assert len(job.job_board_id) == len("workday-") + 10
    assert job.description == "" and job.posted_at is None  # postedOn is not a date


def test_location_text_is_cut_at_the_first_bar():
    jobs, _ = _run(FakeWorkday([FLINDERS]))
    assert jobs[0].location == "Bedford Park / Kaurna Country"
    jobs, _ = _run(FakeWorkday([_row(1, locationsText="5 Locations   |      |")]))
    assert jobs[0].location == "5 Locations"


def test_url_and_id_come_from_the_list_and_ignore_the_detail_call():
    rows = [CBA_DS, CBA_TWO_LOCATIONS]
    without, _ = _run(FakeWorkday(rows))
    with_detail, _ = _run(FakeWorkday(rows), detail_titles=["data", "legal"])
    assert [(j.url, j.job_board_id) for j in without] == [(j.url, j.job_board_id) for j in with_detail]


def test_ids_are_deterministic_and_distinct():
    rows = [_row(n) for n in range(25)]
    first, _ = _run(FakeWorkday(rows))
    second, _ = _run(FakeWorkday(rows))
    assert [j.job_board_id for j in first] == [j.job_board_id for j in second]
    assert len({j.job_board_id for j in first}) == 25


# --- the id rule -----------------------------------------------------------------------


def test_requisition_id_uses_the_base_id_found_in_bullet_fields_at_any_position():
    assert _requisition_id(BUNNINGS_REPOST["externalPath"], BUNNINGS_REPOST["bulletFields"]) == "R066730"
    assert _requisition_id(FLINDERS["externalPath"], FLINDERS["bulletFields"]) == "JR0000017701"
    assert _requisition_id(CBA_DS["externalPath"], CBA_DS["bulletFields"]) == "REQ265273"


def test_the_longest_matching_bullet_wins():
    path = "/job/X/Title_REQ12-3"
    assert _requisition_id(path, ["REQ", "REQ12", "Permanent"]) == "REQ12"


def test_without_a_matching_bullet_the_whole_suffix_is_used_unchanged():
    # A hyphenated id must NOT lose its tail: stripping "-12345" would leave "R".
    assert _requisition_id("/job/X/Title_R-12345", ["Permanent"]) == "R-12345"
    assert _requisition_id("/job/X/Title_R-12345", []) == "R-12345"
    assert _requisition_id("/job/X/Title_REQ99-1", ["Other"]) == "REQ99-1"


def test_a_path_with_no_underscore_has_no_requisition_id():
    assert _requisition_id("/job/X/Title-without-id", ["R1"]) == ""
    assert _requisition_id("/job/X_Y/Title", ["R1"]) == ""  # an underscore in an earlier segment


def test_hyphenated_ids_get_different_ids_and_are_never_built_from_the_prefix():
    a = _row(1, externalPath="/job/X/Title_R-12345", bulletFields=["Permanent"])
    b = _row(2, externalPath="/job/X/Title_R-67890", bulletFields=["Permanent"])
    jobs, _ = _run(FakeWorkday([a, b]))
    assert jobs[0].job_board_id != jobs[1].job_board_id


def test_id_survives_a_repost_marker_change():
    v1 = _row(1, externalPath="/job/Adelaide/Role_R066730", bulletFields=["R066730"])
    v2 = _row(1, externalPath="/job/Adelaide/Role_R066730-1", bulletFields=["R066730"])
    v3 = _row(1, externalPath="/job/Adelaide/Role_R066730-2", bulletFields=["Permanent", "R066730"])
    ids = {_run(FakeWorkday([v]))[0][0].job_board_id for v in (v1, v2, v3)}
    assert len(ids) == 1


def test_id_survives_a_title_or_location_slug_change():
    before = _row(1, externalPath="/job/Adelaide/Data-Analyst_R000001")
    after = _row(1, externalPath="/job/Sydney---CBD/Senior-Data-Analyst_R000001")
    a = _run(FakeWorkday([before]))[0][0]
    b = _run(FakeWorkday([after]))[0][0]
    assert a.job_board_id == b.job_board_id
    assert a.url != b.url  # the stored URL still follows the path


def test_a_path_with_no_id_falls_back_to_the_url_and_stays_distinct():
    a = _row(1, externalPath="/job/Adelaide/First-role", bulletFields=[])
    b = _row(2, externalPath="/job/Adelaide/Second-role", bulletFields=[])
    jobs, _ = _run(FakeWorkday([a, b]))
    assert jobs[0].job_board_id != jobs[1].job_board_id


def test_two_postings_mapping_to_one_id_raise():
    a = _row(1, externalPath="/job/Adelaide/Data-Analyst_R000001")
    b = _row(2, externalPath="/job/Sydney/Other-Title_R000001", bulletFields=["R000001"])
    with pytest.raises(WorkdayError, match="two postings map to the id"):
        _run(FakeWorkday([a, b]))


# --- details --------------------------------------------------------------------------------


def test_detail_is_fetched_only_for_title_matches_with_a_pause_between():
    rows = [_row(1, "Senior Data Analyst"), _row(2, "Chef"), _row(3, "Machine Learning Engineer"),
            _row(4, "Welder")]
    fake = FakeWorkday(rows)
    jobs, sleep = _run(fake, detail_titles=["data analyst", "machine learning"])
    assert len(jobs) == 4  # every posting is returned, matched or not
    fetched = [url.split("Careers", 1)[1] for url, _ in fake.get_calls]
    assert fetched == [rows[0]["externalPath"], rows[2]["externalPath"]]
    assert all(h == {"Accept": "application/json"} for _, h in fake.get_calls)
    assert [c.args for c in sleep.call_args_list] == [(REQUEST_DELAY,)]  # between the two only
    assert _by_title(jobs, "Chef").description == ""


def test_detail_url_is_the_cxs_path_plus_the_external_path():
    fake = FakeWorkday([CBA_DS])
    _run(fake, detail_titles=["data scientist"])
    assert fake.get_calls[0][0] == f"{BASE}/wday/cxs/acme/Careers{CBA_DS['externalPath']}"


def test_detail_replaces_location_and_sets_description_and_posted_at():
    fake = FakeWorkday(
        [CBA_TWO_LOCATIONS],
        details={CBA_TWO_LOCATIONS["externalPath"]: _detail(
            "Sydney CBD Area", ["Melbourne, VIC - 435 Bourke Street"], "2026-10-02")},
    )
    job = _run(fake, detail_titles=["legal counsel"])[0][0]
    assert job.location == "Sydney CBD Area; Melbourne, VIC - 435 Bourke Street"
    assert job.description == "Build models.\n\n- SQL\n- Python"
    assert "<" not in job.description
    assert job.posted_at == datetime(2026, 10, 2) and job.posted_at.tzinfo is None


def test_detail_without_a_location_keeps_the_list_location():
    fake = FakeWorkday([CBA_DS], details={CBA_DS["externalPath"]: _detail(location="")})
    job = _run(fake, detail_titles=["data"])[0][0]
    assert job.location == "Bangalore - Manyata Tech Park Road"
    assert job.description != ""


def test_failed_detail_keeps_the_posting_and_the_list_values():
    rows = [CBA_DS, FLINDERS]
    fake = FakeWorkday(rows, failing_details={FLINDERS["externalPath"]})
    jobs, _ = _run(fake, detail_titles=["data scientist", "research associate"])
    assert len(jobs) == 2
    flinders = _by_title(jobs, "Research Associate in Marine Biology")
    assert flinders.description == "" and flinders.posted_at is None
    assert flinders.location == "Bedford Park / Kaurna Country"  # cut at the first "|"
    assert _by_title(jobs, "Data Scientist").description != ""  # the other detail still worked


def test_a_malformed_detail_does_not_fail_the_scrape():
    fake = FakeWorkday([CBA_DS], details={CBA_DS["externalPath"]: {"unexpected": True}})
    job = _run(fake, detail_titles=["data"])[0][0]
    assert job.description == "" and job.posted_at is None
    assert job.location == "Bangalore - Manyata Tech Park Road"


@pytest.mark.parametrize("empty", [None, []])
def test_no_title_filters_means_no_detail_calls(empty):
    # title_matches(title, []) is True, so without an explicit check every
    # posting on the board would be fetched.
    fake = FakeWorkday([_row(n) for n in range(5)])
    jobs, _ = _run(fake, detail_titles=empty)
    assert len(jobs) == 5
    assert fake.get_calls == []
    assert all(j.description == "" for j in jobs)
