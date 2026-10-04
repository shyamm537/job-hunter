import pytest

from src.config import Filters
from src.ingestion.filtering import (
    job_matches,
    location_matches,
    title_excluded,
    title_matches,
    title_ok,
    wanted_titles,
    whole_word_in,
)
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


# --- board_locations (KAN-35) -------------------------------------------------


def test_whole_word_short_code_does_not_match_inside_words():
    sa = ["SA"]
    assert location_matches("SA Western Area", sa, whole_word=True) is True
    assert location_matches("Support Office SA", sa, whole_word=True) is True
    assert location_matches("Mount Gambier, SA", sa, whole_word=True) is True
    assert location_matches("San Francisco, CA", sa, whole_word=True) is False
    assert location_matches("Santa Clara, USA", sa, whole_word=True) is False
    assert location_matches("Mount Pleasant, QLD", sa, whole_word=True) is False
    # The default stays a substring match (the reason whole_word exists).
    assert location_matches("Mount Pleasant, QLD", sa) is True


def test_whole_word_multi_word_entry_and_case():
    assert location_matches(
        "Bedford Park / Kaurna Country", ["bedford park"], whole_word=True
    ) is True
    assert location_matches("ADELAIDE CBD", ["Adelaide"], whole_word=True) is True
    assert location_matches("Adelaide-based", ["Adelaide"], whole_word=True) is True
    assert location_matches("Adelaidean", ["Adelaide"], whole_word=True) is False


def test_whole_word_entry_is_taken_literally():
    # "." must not act as a regex wildcard.
    assert location_matches("St. Leonards", ["St. Leonards"], whole_word=True) is True
    assert location_matches("StX Leonards", ["St. Leonards"], whole_word=True) is False


def test_remote_passes_in_whole_word_mode():
    assert location_matches("Remote - US", ["Adelaide"], whole_word=True) is True


def test_board_locations_used_when_set():
    f = Filters(titles=["analyst"], locations=["Sydney"], board_locations=["SA"])
    assert job_matches(_job("Data Analyst", "SA Western Area"), f) is True
    assert job_matches(_job("Data Analyst", "San Francisco, CA"), f) is False


def test_board_locations_replace_locations_for_boards():
    # It replaces, it does not add: a city only in `locations` no longer passes.
    f = Filters(titles=["analyst"], locations=["Sydney"], board_locations=["Adelaide"])
    assert job_matches(_job("Data Analyst", "Sydney"), f) is False
    assert job_matches(_job("Data Analyst", "Adelaide CBD"), f) is True


def test_empty_board_locations_falls_back_to_locations_substring():
    f = Filters(titles=["analyst"], locations=["adelaide"], board_locations=[])
    assert job_matches(_job("Data Analyst", "Adelaide, SA"), f) is True
    assert job_matches(_job("Data Analyst", "Sydney"), f) is False
    # Substring matching, as before: "SA" inside a word still counts here.
    f = Filters(locations=["sa"])
    assert job_matches(_job("Chef", "Mount Pleasant, QLD"), f) is True


def test_blank_board_locations_count_as_unset():
    f = Filters(locations=["adelaide"], board_locations=["", "  "])
    assert job_matches(_job("Chef", "Adelaide"), f) is True
    assert job_matches(_job("Chef", "Sydney"), f) is False


def test_board_locations_do_not_affect_titles_or_remote():
    f = Filters(titles=["analyst"], board_locations=["Adelaide"])
    assert job_matches(_job("Chef", "Adelaide"), f) is False
    assert job_matches(_job("Data Analyst", "Remote"), f) is True


# --- exclude_titles, also_match_titles (KAN-37) ----------------------------------


def test_whole_word_in_is_a_public_helper():
    assert whole_word_in("sr", "Sr. Data Analyst") is True
    assert whole_word_in("sr", "Srinivas the Analyst") is False
    assert whole_word_in("lead", "Data Lead") is True
    assert whole_word_in("lead", "Leadership Programme") is False


@pytest.mark.parametrize(
    "title, excluded",
    [
        ("Senior Data Analyst", True),
        ("SENIOR data scientist", True),  # case-insensitive
        ("Sr. Data Scientist", True),  # "sr" before a full stop
        ("Sr Data Scientist", True),
        ("Data Scientist (Lead)", True),
        ("Team Lead, Analytics", True),
        ("Chapter Lead - AI", True),
        ("Intern - Data Team", True),
        ("Data Science Internship", True),  # "internship" is its own entry
        ("Staff Data Engineer", True),
        ("Principal Data Scientist", True),
        ("Data Analyst - Freelance", True),
        ("Data Analyst", False),
        ("Leadership Analyst", False),  # "lead" inside a longer word
        ("Srinivasan, Data Analyst", False),  # "sr" inside a name
        ("Internal Analytics Analyst", False),  # "intern" inside "internal"
        ("Seniority Analyst", False),
        ("Staffing Data Analyst", False),
        ("Graduate Data Analyst", False),  # not in this list
    ],
)
def test_exclude_titles_match_whole_words(title, excluded):
    words = ["senior", "sr", "lead", "principal", "staff", "intern", "internship", "freelance"]
    assert title_excluded(title, words) is excluded


def test_exclude_titles_edge_cases():
    assert title_excluded("Senior Analyst", []) is False
    assert title_excluded("Senior Analyst", None) is False
    assert title_excluded("Senior Analyst", ["", "  "]) is False  # blanks never match
    assert title_excluded("Data Entry Clerk", ["data entry"]) is True  # multi-word entry
    assert title_excluded("Senior Analyst", ["  senior  "]) is True  # entries are trimmed
    assert title_excluded("C++ Developer", ["c++"]) is True  # taken literally, not as a regex
    assert title_excluded("", ["senior"]) is False
    assert title_excluded(None, ["senior"]) is False


def test_also_match_titles_extend_the_wanted_list_only_when_titles_is_set():
    f = Filters(titles=["data analyst"], also_match_titles=["data engineer", "business analyst"])
    assert wanted_titles(f) == ["data analyst", "data engineer", "business analyst"]
    assert title_ok("Senior Data Engineer", f) is True
    assert title_ok("Business Analyst - Payments", f) is True
    assert title_ok("Chef", f) is False
    # An empty `titles` means "no title filter"; `also` must not become the only filter.
    only_also = Filters(titles=[], also_match_titles=["data engineer"])
    assert wanted_titles(only_also) == []
    assert title_ok("Chef", only_also) is True


def test_title_ok_needs_a_wanted_title_and_no_excluded_word():
    f = Filters(
        titles=["data scientist", "data analyst"],
        also_match_titles=["data engineer"],
        exclude_titles=["senior", "lead"],
    )
    assert title_ok("Data Scientist", f) is True
    assert title_ok("Graduate Data Engineer", f) is True  # via also_match_titles
    assert title_ok("Senior Data Scientist", f) is False  # wanted but excluded
    assert title_ok("Lead Data Engineer", f) is False  # also-matched but excluded
    assert title_ok("Frontend Developer", f) is False  # not wanted
    # No exclusions set: only the wanted check applies.
    assert title_ok("Senior Data Scientist", Filters(titles=["data scientist"])) is True


def test_job_matches_applies_exclusions_and_also_titles_to_the_title():
    f = Filters(
        titles=["analyst"], also_match_titles=["data engineer"], exclude_titles=["senior"],
        locations=["adelaide"],
    )
    assert job_matches(_job("Data Engineer", "Adelaide"), f) is True
    assert job_matches(_job("Senior Analyst", "Adelaide"), f) is False
    assert job_matches(_job("Data Engineer", "Sydney"), f) is False  # location still applies


# --- remote_regions -----------------------------------------------------------------

REGIONS = ["Australia", "India", "APAC"]


@pytest.mark.parametrize(
    "location, passes",
    [
        # Region-agnostic: nothing named, or only "global"-style words.
        ("Remote", True),
        ("Global Remote", True),
        ("Remote - Anywhere", True),
        ("Worldwide Remote", True),
        ("Remote (Global)", True),
        # Names a wanted region.
        ("Remote - Australia", True),
        ("Remote, India", True),
        ("Remote - APAC", True),
        ("Sydney, New South Wales, Australia (Remote)", True),  # as Workable folds it
        ("Remote - US, Canada, India", True),  # names one of them among others
        ("australia remote", True),  # case-insensitive
        # Names only other places.
        ("Remote - United States", False),
        ("Remote US", False),
        ("Remote Canada", False),
        ("Canada - Remote (ON, AB, BC, or NS Only)", False),
        ("San Francisco, CA, US; Remote, US", False),
        ("Portugal (Remote)", False),
        ("Remote (Americas)", False),
        ("Remote - EMEA", False),
        # Whole-word: "India" inside another word is not India.
        ("Remote - Indiana", False),
    ],
)
def test_remote_regions_decide_which_remote_jobs_pass(location, passes):
    # A location list with no "Remote" entry and no matching city.
    assert location_matches(location, ["Adelaide"], remote_regions=REGIONS) is passes


def test_remote_passes_for_any_country_when_remote_regions_is_unset():
    for location in ("Remote - United States", "San Francisco, CA, US; Remote, US", "Remote Canada"):
        assert location_matches(location, ["Adelaide"]) is True
        assert location_matches(location, ["Adelaide"], remote_regions=[]) is True
        assert location_matches(location, ["Adelaide"], remote_regions=["", " "]) is True  # blanks = unset


def test_a_wanted_city_still_passes_a_remote_job_when_regions_are_set():
    assert location_matches("Remote - Sydney", ["Sydney"], remote_regions=REGIONS) is True
    assert location_matches("Adelaide", ["Adelaide"], remote_regions=REGIONS) is True
    assert location_matches("Boston, MA", ["Adelaide"], remote_regions=REGIONS) is False  # not remote


def test_a_literal_remote_entry_does_not_bypass_the_region_rule():
    # config.yaml's `locations` often lists "Remote" for Adzuna. With regions set it
    # must not let "Remote - United States" through by substring.
    locations = ["Adelaide", "Remote"]
    assert location_matches("Remote - United States", locations, remote_regions=REGIONS) is False
    assert location_matches("Remote - India", locations, remote_regions=REGIONS) is True
    assert location_matches("Remote", locations, remote_regions=REGIONS) is True
    # Unchanged when regions are unset.
    assert location_matches("Remote - United States", locations) is True
    # A list that only says "Remote" means remote jobs only.
    assert location_matches("Remote - Australia", ["Remote"], remote_regions=REGIONS) is True
    assert location_matches("Sydney", ["Remote"], remote_regions=REGIONS) is False


def test_remote_regions_work_with_board_locations_whole_word_mode():
    boards = ["Adelaide", "SA"]
    assert location_matches("Remote - United States", boards, whole_word=True,
                            remote_regions=REGIONS) is False
    assert location_matches("Remote, SA", boards, whole_word=True, remote_regions=REGIONS) is True
    assert location_matches("Remote - Australia", boards, whole_word=True,
                            remote_regions=REGIONS) is True


def test_job_matches_uses_remote_regions_for_both_location_lists():
    base = dict(titles=["analyst"], remote_regions=REGIONS)
    us_remote = _job("Data Analyst", "Remote - United States")
    au_remote = _job("Data Analyst", "Remote - Australia")
    for f in (Filters(locations=["Adelaide"], **base), Filters(board_locations=["Adelaide"], **base)):
        assert job_matches(us_remote, f) is False
        assert job_matches(au_remote, f) is True
        assert job_matches(_job("Data Analyst", "Remote"), f) is True


def test_job_matches_without_remote_regions_is_unchanged():
    f = Filters(titles=["analyst"], locations=["adelaide"])
    assert job_matches(_job("Data Analyst", "Remote - United States"), f) is True


# --- check_location=False (Adzuna) ----------------------------------------------------


def test_check_location_false_checks_only_the_title():
    f = Filters(titles=["data analyst"], locations=["Adelaide"], exclude_titles=["senior"],
                remote_regions=REGIONS)
    assert job_matches(_job("Data Analyst", "Perth"), f, check_location=False) is True
    assert job_matches(_job("Data Analyst", "Remote - United States"), f, check_location=False) is True
    assert job_matches(_job("Chef", "Adelaide"), f, check_location=False) is False
    assert job_matches(_job("Senior Data Analyst", "Adelaide"), f, check_location=False) is False
    # With the default the location is checked as before.
    assert job_matches(_job("Data Analyst", "Perth"), f) is False


def test_empty_filters_still_pass_everything():
    f = Filters()
    assert job_matches(_job("Anything", "Anywhere"), f) is True
    assert job_matches(_job("Anything", "Anywhere"), f, check_location=False) is True
