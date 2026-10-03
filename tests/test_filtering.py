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
