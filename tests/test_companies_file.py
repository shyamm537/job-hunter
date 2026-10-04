"""Tests for companies.txt parsing (load_companies_file in src/config.py)."""

import pytest

from src.config import (
    AshbySource,
    ConfigError,
    GreenhouseSource,
    WorkableSource,
    WorkdaySource,
    load_companies_file,
)


def _load(tmp_path, body: str):
    p = tmp_path / "companies.txt"
    p.write_text(body, encoding="utf-8")
    return load_companies_file(str(p))


def test_names_blank_lines_and_comments(tmp_path):
    result = _load(
        tmp_path,
        "# my employers\n\nCanva\n   \nAtlassian\n# Atlassian again\n",
    )
    assert [c.name for c in result.companies] == ["Canva", "Atlassian"]
    assert all(c.aliases == [] and c.known == [] for c in result.companies)
    assert result.blocked == []


def test_plain_aliases_are_kept_exactly_as_written(tmp_path):
    result = _load(tmp_path, "Commonwealth Bank | cba, CommBank\n")
    company = result.companies[0]
    assert company.name == "Commonwealth Bank"
    assert company.aliases == ["cba", "CommBank"]  # case kept: Workable is case-sensitive


def test_alias_commas_tolerate_spaces_and_empties(tmp_path):
    result = _load(tmp_path, "Acme |  a1 ,a2 ,, a3  ,\n")
    assert result.companies[0].aliases == ["a1", "a2", "a3"]


def test_a_url_alias_is_a_known_board(tmp_path):
    result = _load(
        tmp_path,
        "Commonwealth Bank | cba, https://cba.wd3.myworkdayjobs.com/CommBank_Careers\n",
    )
    company = result.companies[0]
    assert company.aliases == ["cba"]
    assert company.known == [
        WorkdaySource(type="workday", tenant="cba", datacenter="wd3", site="CommBank_Careers")
    ]


def test_workday_url_with_and_without_a_locale(tmp_path):
    result = _load(
        tmp_path,
        "A | https://cba.wd3.myworkdayjobs.com/en-US/CommBank_Careers\n"
        "B | cba.wd3.myworkdayjobs.com/CommBank_Careers\n",
    )
    a, b = result.companies
    assert a.known == b.known
    assert a.known[0].site == "CommBank_Careers"


def test_other_url_aliases(tmp_path):
    result = _load(
        tmp_path,
        "Squiz | https://apply.workable.com/squiz\n"
        "Stripe | boards.greenhouse.io/stripe\n",
    )
    assert result.companies[0].known == [WorkableSource(type="workable", account="squiz")]
    assert result.companies[1].known == [GreenhouseSource(type="greenhouse", board="stripe")]


def test_block_lines(tmp_path):
    result = _load(tmp_path, "AMP\n- ashby amp\n- greenhouse apex\n")
    assert result.blocked == [
        AshbySource(type="ashby", org="amp"),
        GreenhouseSource(type="greenhouse", board="apex"),
    ]
    assert [c.name for c in result.companies] == ["AMP"]


def test_block_line_with_a_pasted_url(tmp_path):
    result = _load(tmp_path, "- https://jobs.ashbyhq.com/amp\n")
    assert result.blocked == [AshbySource(type="ashby", org="amp")]


def test_apostrophes_in_names_are_not_quotes(tmp_path):
    result = _load(tmp_path, "Moody's\nDan Murphy's | danmurphys\n")
    moodys, dan = result.companies
    assert moodys.name == "Moody's"
    assert dan.name == "Dan Murphy's"
    assert dan.aliases == ["danmurphys"]


def test_a_hash_inside_a_name_or_url_is_kept(tmp_path):
    result = _load(
        tmp_path,
        "C#\nFoo | https://jobs.lever.co/foo#team\n",
    )
    assert result.companies[0].name == "C#"
    assert result.companies[1].known[0].company == "foo"  # fragment ignored by the URL parse


def test_trailing_comments_are_cut_on_company_and_block_lines(tmp_path):
    result = _load(
        tmp_path,
        "Canva   # design tools\n"
        "Flinders University | flinders  # it's the uni\n"
        "- ashby amp  # not the bank\n",
    )
    canva, flinders = result.companies
    assert canva.name == "Canva"
    assert flinders.aliases == ["flinders"]
    assert result.blocked == [AshbySource(type="ashby", org="amp")]


def test_a_line_that_is_only_a_trailing_comment_is_skipped(tmp_path):
    assert _load(tmp_path, "   # just a note\n").companies == []


def test_duplicate_names_are_an_error_in_any_case(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, "Canva\nAtlassian\ncanva\n")
    message = str(exc.value)
    assert "line 3" in message and "duplicate" in message and "line 1" in message


@pytest.mark.parametrize(
    "body, lineno",
    [
        ("Canva\n | alias\n", 2),  # no name
        ("Canva\nAcme | https://example.com/x\n", 2),  # unrecognised host
        ("Canva\n-\n", 2),  # empty block line
        ("Canva\n- nosuchboard x\n", 2),  # unknown source type
        ("Canva\n- adzuna au\n", 2),  # a search is not a board
        ("Canva\n- greenhouse a b\n", 2),  # wrong token count
    ],
)
def test_bad_lines_name_their_line_number(tmp_path, body, lineno):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, body)
    assert f"companies.txt line {lineno}" in str(exc.value)


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="companies_file not found"):
        load_companies_file(str(tmp_path / "nope.txt"))
