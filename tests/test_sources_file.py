import textwrap

import pytest

from src.config import (
    AshbySource,
    Config,
    ConfigError,
    GreenhouseSource,
    LeverSource,
    SeekSource,
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
        seek
        """,
    )
    srcs = load_sources_file(path)
    assert len(srcs) == 4
    assert isinstance(srcs[0], GreenhouseSource) and srcs[0].board == "stripe"
    assert isinstance(srcs[1], LeverSource) and srcs[1].company == "figma"
    assert isinstance(srcs[2], AshbySource) and srcs[2].org == "ashby"
    assert isinstance(srcs[3], SeekSource)


def test_seek_with_args_raises(tmp_path):
    path = _write(tmp_path, 'seek "data analyst" Adelaide\n')
    with pytest.raises(ConfigError) as exc:
        load_sources_file(path)
    assert "takes no arguments" in str(exc.value)


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
        seek
        """,
    )
    srcs = load_sources_file(path)
    assert len(srcs) == 5
    assert isinstance(srcs[0], GreenhouseSource) and srcs[0].board == "stripe"
    assert isinstance(srcs[1], LeverSource) and srcs[1].company == "metabase"
    assert isinstance(srcs[2], AshbySource) and srcs[2].org == "ramp"
    assert isinstance(srcs[3], GreenhouseSource) and srcs[3].board == "airbnb"
    assert isinstance(srcs[4], SeekSource)


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
