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
    WorkdaySource,
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


# --- comments (KAN-39) ----------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("adzuna au   # an Adzuna search", ("adzuna", "au")),
        ("adzuna in # ... and India", ("adzuna", "in")),
        ("greenhouse stripe  # a note", ("greenhouse", "stripe")),
        ("lever figma\t# a tab before the comment", ("lever", "figma")),
        ("ashby ashby #no space after the hash", ("ashby", "ashby")),
        ("workable squiz # case-sensitive", ("workable", "squiz")),
        ("greenhouse stripe #", ("greenhouse", "stripe")),  # an empty comment
        ("greenhouse stripe # it's a note, with an apostrophe", ("greenhouse", "stripe")),
        ('greenhouse stripe # and "an unbalanced quote', ("greenhouse", "stripe")),
    ],
)
def test_trailing_comment_after_an_entry_is_ignored(tmp_path, line, expected):
    (src,) = load_sources_file(_write(tmp_path, line + "\n"))
    assert src.type == expected[0]
    assert getattr(src, {"adzuna": "country", "greenhouse": "board", "lever": "company",
                         "ashby": "org", "workable": "account"}[expected[0]]) == expected[1]


def test_trailing_comment_on_a_workday_line_and_a_pasted_url(tmp_path):
    srcs = load_sources_file(_write(
        tmp_path,
        """
        workday cba wd3 CommBank_Careers   # one site per tenant
        https://jobs.lever.co/metabase   # a pasted careers URL
        https://boards.greenhouse.io/stripe#jobs   # a URL fragment is not a comment
        """,
    ))
    assert (srcs[0].tenant, srcs[0].datacenter, srcs[0].site) == ("cba", "wd3", "CommBank_Careers")
    assert isinstance(srcs[1], LeverSource) and srcs[1].company == "metabase"
    assert isinstance(srcs[2], GreenhouseSource) and srcs[2].board == "stripe"


def test_a_hash_inside_a_token_is_not_a_comment(tmp_path):
    # "au#x" has no space before the "#", so it stays one token (and is the country).
    (src,) = load_sources_file(_write(tmp_path, "adzuna au#x\n"))
    assert src.country == "au#x"


def test_whole_line_comments_and_blank_lines_are_still_ignored(tmp_path):
    srcs = load_sources_file(_write(
        tmp_path,
        """
        # a whole-line comment
           # an indented one, with a trailing # hash
        greenhouse stripe

        # greenhouse commented-out  # and a trailing comment on it
        """,
    ))
    assert [s.type for s in srcs] == ["greenhouse"]


def test_a_comment_does_not_hide_a_real_error_before_it(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_sources_file(_write(tmp_path, "greenhouse # forgot the token\n"))
    assert "exactly one board token" in str(exc.value)
    assert "line 1" in str(exc.value)


def test_an_unclosed_quote_in_the_entry_still_reports_the_line(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_sources_file(_write(tmp_path, "greenhouse stripe\nworkday a wd3 \"Site # nope\n"))
    assert "line 2" in str(exc.value)


def test_a_discover_proposal_works_after_uncommenting_it(tmp_path):
    # `make discover` writes "# ashby acme    # match: 2 role(s)" and says to
    # uncomment the ones to keep; that must load, trailing note and all.
    from src.ingestion.discover import _proposal_lines
    from src.ingestion.validate import ValidationResult

    results = [
        ValidationResult(AshbySource(type="ashby", org="airwallex"), "ashby[airwallex]", True, 40, 7, None),
        ValidationResult(GreenhouseSource(type="greenhouse", board="acme"), "greenhouse[acme]", True, 9, 0, None),
    ]
    lines = _proposal_lines(results)
    proposals = [line for line in lines if line.startswith("# ashby") or line.startswith("# greenhouse")]
    assert len(proposals) == 2 and "# match: 7 role(s)" in proposals[0]
    approved = [line[2:] for line in proposals]  # uncomment: drop the leading "# "

    srcs = load_sources_file(_write(tmp_path, "\n".join(approved) + "\n"))
    assert [(s.type, getattr(s, "org", None) or getattr(s, "board", None)) for s in srcs] == [
        ("ashby", "airwallex"), ("greenhouse", "acme"),
    ]


def test_the_shipped_example_sources_file_loads():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "sources.txt.example"
    assert load_sources_file(str(example))  # at least one active source, no errors


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


def test_workday_line_parses_and_keeps_the_site_case(tmp_path):
    src = load_sources_file(_write(tmp_path, "workday cba wd3 CommBank_Careers\n"))[0]
    assert isinstance(src, WorkdaySource)
    assert (src.tenant, src.datacenter, src.site) == ("cba", "wd3", "CommBank_Careers")


@pytest.mark.parametrize(
    "line", ["workday", "workday cba", "workday cba wd3", "workday cba wd3 Site extra"]
)
def test_workday_needs_exactly_three_tokens(tmp_path, line):
    with pytest.raises(ConfigError) as exc:
        load_sources_file(_write(tmp_path, line + "\n"))
    assert "exactly three tokens" in str(exc.value)
    assert "line 1" in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://cba.wd3.myworkdayjobs.com/en-US/CommBank_Careers",
        "https://cba.wd3.myworkdayjobs.com/CommBank_Careers",  # no locale
        "cba.wd3.myworkdayjobs.com/CommBank_Careers",  # scheme optional
        "https://cba.wd3.myworkdayjobs.com/CommBank_Careers/",  # trailing slash
        "https://CBA.WD3.MyWorkdayJobs.com/en-AU/CommBank_Careers",  # host case-insensitive
        # a deep posting link still resolves to the site
        "https://cba.wd3.myworkdayjobs.com/en-US/CommBank_Careers/job/Sydney-CBD-Area/Data-Scientist_REQ1",
        "https://cba.wd3.myworkdayjobs.com/CommBank_Careers/job/Sydney-CBD-Area/Data-Scientist_REQ1",
        "https://cba.wd3.myworkdayjobs.com/en-US/CommBank_Careers?q=data",  # query string
    ],
)
def test_source_from_url_workday(url):
    src = source_from_url(url)
    assert isinstance(src, WorkdaySource)
    assert (src.tenant, src.datacenter, src.site) == ("cba", "wd3", "CommBank_Careers")


def test_source_from_url_workday_other_data_centres_and_tenants():
    src = source_from_url("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
    assert (src.tenant, src.datacenter, src.site) == ("nvidia", "wd5", "NVIDIAExternalCareerSite")
    src = source_from_url("https://my-co.wd103.myworkdayjobs.com/en-GB/Ext")
    assert (src.tenant, src.datacenter, src.site) == ("my-co", "wd103", "Ext")


@pytest.mark.parametrize(
    "url",
    [
        "https://cba.wd3.myworkdayjobs.com",
        "https://cba.wd3.myworkdayjobs.com/",
        "https://cba.wd3.myworkdayjobs.com/en-US",  # a locale and nothing else
        "https://cba.wd3.myworkdayjobs.com/en-US/",
    ],
)
def test_source_from_url_workday_without_a_site_names_the_expected_form(url):
    with pytest.raises(ConfigError) as exc:
        source_from_url(url)
    assert "could not find the site name" in str(exc.value)
    assert "<tenant>.<datacenter>.myworkdayjobs.com/<site>" in str(exc.value)


@pytest.mark.parametrize(
    "host",
    [
        "cba.myworkdayjobs.com",  # no data centre
        "cba.wd3.myworkdayjobs.com.evil.test",  # a look-alike
        "wd3.myworkdayjobs.com",  # no tenant
        "cba.wd3.myworkdaysite.com",  # a different Workday host form, not covered
    ],
)
def test_workday_look_alike_hosts_are_not_recognised(host):
    with pytest.raises(ConfigError) as exc:
        source_from_url(f"https://{host}/Careers")
    assert "unrecognised careers URL host" in str(exc.value)


def test_unrecognised_host_error_lists_the_workday_form():
    with pytest.raises(ConfigError) as exc:
        source_from_url("https://example.com/acme")
    assert "<tenant>.<wdN>.myworkdayjobs.com" in str(exc.value)


def test_sources_file_accepts_a_workday_url_and_the_explicit_line(tmp_path):
    srcs = load_sources_file(_write(
        tmp_path,
        """
        https://cba.wd3.myworkdayjobs.com/en-US/CommBank_Careers
        workday flinders wd3 flinders_employment
        """,
    ))
    assert [(s.tenant, s.datacenter, s.site) for s in srcs] == [
        ("cba", "wd3", "CommBank_Careers"),
        ("flinders", "wd3", "flinders_employment"),
    ]


def test_unknown_type_error_lists_workday(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_sources_file(_write(tmp_path, "linkedin acme\n"))
    assert "workday" in str(exc.value)


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
