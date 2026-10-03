import textwrap

import pytest

from src.config import (
    AdzunaSource,
    AshbySource,
    Config,
    ConfigError,
    GreenhouseSource,
    LeverSource,
    WorkableSource,
    load_sources_file,
    source_from_url,
)


def _write(tmp_path, body: str):
    p = tmp_path / "sources.txt"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_parses_sources(tmp_path):
    path = _write(
        tmp_path,
        """
        # boards + a search
        greenhouse stripe
        lever figma
        ashby ashby
        adzuna in
        """,
    )
    srcs = load_sources_file(path)
    assert len(srcs) == 4
    assert isinstance(srcs[0], GreenhouseSource) and srcs[0].board == "stripe"
    assert isinstance(srcs[1], LeverSource) and srcs[1].company == "figma"
    assert isinstance(srcs[2], AshbySource) and srcs[2].org == "ashby"
    assert isinstance(srcs[3], AdzunaSource) and srcs[3].country == "in"


def test_leftover_seek_line_is_an_unknown_source(tmp_path):
    # SEEK was removed (KAN-32); an old sources.txt still saying `seek` must
    # fail with the line number rather than be silently skipped.
    path = _write(tmp_path, "greenhouse stripe\nseek\n")
    with pytest.raises(ConfigError) as exc:
        load_sources_file(path)
    assert "line 2" in str(exc.value)
    assert "unknown source type 'seek'" in str(exc.value)


def test_greenhouse_missing_token_raises(tmp_path):
    path = _write(tmp_path, "greenhouse\n")
    with pytest.raises(ConfigError) as exc:
        load_sources_file(path)
    assert "exactly one board token" in str(exc.value)


def test_greenhouse_extra_token_raises(tmp_path):
    path = _write(tmp_path, "greenhouse stripe data\n")
    with pytest.raises(ConfigError):
        load_sources_file(path)


def test_ashby_missing_token_raises(tmp_path):
    path = _write(tmp_path, "ashby\n")
    with pytest.raises(ConfigError) as exc:
        load_sources_file(path)
    assert "exactly one org token" in str(exc.value)


def test_unknown_type_raises(tmp_path):
    path = _write(tmp_path, "linkedin acme\n")
    with pytest.raises(ConfigError) as exc:
        load_sources_file(path)
    assert "unknown source type" in str(exc.value)


def test_missing_file_raises():
    with pytest.raises(ConfigError):
        load_sources_file("/no/such/sources.txt")


@pytest.mark.parametrize(
    "url, board",
    [
        ("https://boards.greenhouse.io/stripe", "stripe"),
        ("https://job-boards.greenhouse.io/stripe", "stripe"),
        ("boards.greenhouse.io/stripe", "stripe"),  # scheme optional
        ("https://boards.greenhouse.io/stripe/", "stripe"),  # trailing slash
        ("https://boards.greenhouse.io/stripe/jobs/12345", "stripe"),  # deep link
        ("HTTPS://BOARDS.GREENHOUSE.IO/Stripe", "Stripe"),  # host case-insensitive
    ],
)
def test_source_from_url_greenhouse(url, board):
    src = source_from_url(url)
    assert isinstance(src, GreenhouseSource)
    assert src.board == board


@pytest.mark.parametrize(
    "url, company",
    [
        ("https://jobs.lever.co/metabase", "metabase"),
        ("jobs.lever.co/metabase", "metabase"),
        ("https://jobs.lever.co/metabase/abc-123-def", "metabase"),  # deep link
    ],
)
def test_source_from_url_lever(url, company):
    src = source_from_url(url)
    assert isinstance(src, LeverSource)
    assert src.company == company


@pytest.mark.parametrize(
    "url, org",
    [
        ("https://jobs.ashbyhq.com/ashby", "ashby"),
        ("jobs.ashbyhq.com/ashby", "ashby"),  # scheme optional
        ("https://jobs.ashbyhq.com/ashby/7458d4e9-uuid", "ashby"),  # deep link
    ],
)
def test_source_from_url_ashby(url, org):
    src = source_from_url(url)
    assert isinstance(src, AshbySource)
    assert src.org == org


def test_workable_line_parses(tmp_path):
    path = _write(tmp_path, "workable squiz\n")
    src = load_sources_file(path)[0]
    assert isinstance(src, WorkableSource) and src.account == "squiz"


def test_workable_keeps_the_account_case_as_typed(tmp_path):
    # Workable account slugs are case-sensitive (SQUIZ is a 404).
    src = load_sources_file(_write(tmp_path, "workable Squiz-Co\n"))[0]
    assert src.account == "Squiz-Co"


def test_workable_missing_token_raises(tmp_path):
    path = _write(tmp_path, "workable\n")
    with pytest.raises(ConfigError) as exc:
        load_sources_file(path)
    assert "exactly one account token" in str(exc.value)


def test_workable_extra_token_raises(tmp_path):
    path = _write(tmp_path, "workable squiz extra\n")
    with pytest.raises(ConfigError):
        load_sources_file(path)


@pytest.mark.parametrize(
    "url, account",
    [
        ("https://apply.workable.com/squiz", "squiz"),
        ("apply.workable.com/squiz", "squiz"),  # scheme optional
        ("https://apply.workable.com/squiz/", "squiz"),  # trailing slash
        ("https://apply.workable.com/squiz/j/EB6B3555A0", "squiz"),  # deep link with account
        ("HTTPS://APPLY.WORKABLE.COM/Squiz", "Squiz"),  # host case-insensitive, slug kept
    ],
)
def test_source_from_url_workable(url, account):
    src = source_from_url(url)
    assert isinstance(src, WorkableSource)
    assert src.account == account


@pytest.mark.parametrize(
    "url",
    [
        "https://apply.workable.com/j/EB6B3555A0",  # a single posting: "j" is not an account
        "apply.workable.com/j/EB6B3555A0/apply",
        "https://apply.workable.com/J/EB6B3555A0",
    ],
)
def test_source_from_url_workable_posting_link_asks_for_the_board(url):
    with pytest.raises(ConfigError) as exc:
        source_from_url(url)
    assert "single Workable posting" in str(exc.value)
    assert "https://apply.workable.com/<account>" in str(exc.value)


def test_source_from_url_workable_missing_token_raises():
    with pytest.raises(ConfigError) as exc:
        source_from_url("https://apply.workable.com/")
    assert "could not find a board token" in str(exc.value)


def test_sources_file_accepts_a_workable_url(tmp_path):
    srcs = load_sources_file(_write(tmp_path, "https://apply.workable.com/legalvision\n"))
    assert isinstance(srcs[0], WorkableSource) and srcs[0].account == "legalvision"


def test_unknown_type_error_lists_workable(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_sources_file(_write(tmp_path, "linkedin acme\n"))
    assert "workable" in str(exc.value)


def test_source_from_url_unknown_host_raises():
    with pytest.raises(ConfigError) as exc:
        source_from_url("https://jobs.workday.com/acme")
    assert "unrecognised careers URL host" in str(exc.value)


def test_source_from_url_missing_token_raises():
    with pytest.raises(ConfigError) as exc:
        source_from_url("https://boards.greenhouse.io/")
    assert "could not find a board token" in str(exc.value)


def test_sources_file_accepts_urls(tmp_path):
    path = _write(
        tmp_path,
        """
        https://boards.greenhouse.io/stripe
        https://jobs.lever.co/metabase
        https://jobs.ashbyhq.com/ramp
        greenhouse airbnb
        adzuna
        """,
    )
    srcs = load_sources_file(path)
    assert len(srcs) == 5
    assert isinstance(srcs[0], GreenhouseSource) and srcs[0].board == "stripe"
    assert isinstance(srcs[1], LeverSource) and srcs[1].company == "metabase"
    assert isinstance(srcs[2], AshbySource) and srcs[2].org == "ramp"
    assert isinstance(srcs[3], GreenhouseSource) and srcs[3].board == "airbnb"
    assert isinstance(srcs[4], AdzunaSource) and srcs[4].country == "au"


def test_sources_file_bad_url_names_line(tmp_path):
    path = _write(tmp_path, "\nhttps://jobs.workday.com/acme\n")
    with pytest.raises(ConfigError) as exc:
        load_sources_file(path)
    assert "line 2" in str(exc.value)


def test_config_merges_inline_and_file(tmp_path):
    path = _write(tmp_path, "lever figma\n")
    cfg = Config.model_validate(
        {"sources": [{"type": "greenhouse", "board": "stripe"}], "sources_file": path}
    )
    assert {s.type for s in cfg.resolved_sources} == {"greenhouse", "lever"}
