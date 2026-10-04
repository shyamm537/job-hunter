"""Tests for `make discover` (src/ingestion/discover.py): proposing company names
from the stored Adzuna ads. No network."""

import pytest

import src.config as config
import src.storage.database as database
from src.config import Company, Filters, load_companies_file
from src.ingestion import discover as D
from src.storage.models import JobPost

PLACES = ["Adelaide", "SA", "South Australia"]


def test_slug_variants_strips_legal_suffixes():
    assert D.slug_variants("Acme Pty Ltd") == ["acme"]
    assert D.slug_variants("Stripe, Inc.") == ["stripe"]


def test_slug_variants_multiword_gives_joined_and_hyphenated():
    assert D.slug_variants("Foo Bar Labs") == ["foobar", "foo-bar"]


def test_slug_variants_empty_or_all_stopwords():
    assert D.slug_variants("") == []
    assert D.slug_variants("The Group Ltd") == []


# --- keys --------------------------------------------------------------------


def test_spelling_variants_share_a_key():
    assert D.company_key("Thermo Fisher Scientific") == D.company_key("ThermoFisher Scientific")
    assert D.company_key("Acme Pty Ltd") == D.company_key("ACME") == "acme"
    assert D.company_key("Acme") != D.company_key("Acme Bank")


def test_a_name_with_no_usable_words_falls_back_to_its_text():
    assert D.company_key("The  Group Ltd") == "the group ltd"
    assert D.company_key("") == ""


def test_existing_keys_include_names_and_plain_aliases():
    companies = [
        Company(name="Commonwealth Bank", aliases=["cba", "CommBank"]),
        Company(name="Flinders University", aliases=["flinders"]),
    ]
    assert D.existing_keys(companies) == {
        "commonwealthbank", "cba", "commbank", "flindersuniversity", "flinders",
    }


# --- wanted places -----------------------------------------------------------


def test_wanted_places_prefer_board_locations_and_drop_remote():
    both = Filters(locations=["Sydney", "Remote"], board_locations=["Adelaide", "SA", "remote"])
    assert D.wanted_places(both) == ["Adelaide", "SA"]
    only_locations = Filters(locations=["Sydney", " Remote "])
    assert D.wanted_places(only_locations) == ["Sydney"]
    assert D.wanted_places(Filters()) == []


def test_is_local_matches_whole_words_only():
    assert D.is_local("Adelaide, South Australia", PLACES)
    assert D.is_local("Hindmarsh, SA", PLACES)
    assert not D.is_local("San Francisco", PLACES)  # "SA" must not match inside a word
    assert not D.is_local("Sydney, Sydney Region", PLACES)
    assert not D.is_local("", PLACES)
    assert not D.is_local("Adelaide", [])


# --- propose -----------------------------------------------------------------


def _ads(*pairs):
    return list(pairs)


def test_ads_are_counted_per_company_with_local_ones_first():
    ads = _ads(
        ("Big Corp", "Sydney"), ("Big Corp", "Sydney"), ("Big Corp", "Sydney"),
        ("Local Co", "Adelaide, SA"),
        ("Mixed Co", "Sydney"), ("Mixed Co", "Adelaide"), ("Mixed Co", "Adelaide"),
    )
    result = D.propose(ads, PLACES, set())
    assert [(p.name, p.ads, p.local) for p in result] == [
        ("Mixed Co", 3, 2),   # most local ads first
        ("Local Co", 1, 1),
        ("Big Corp", 3, 0),   # then the rest, by ad count
    ]


def test_spelling_variants_merge_and_the_commonest_spelling_wins():
    ads = _ads(
        ("Thermo Fisher Scientific", "Sydney"),
        ("ThermoFisher Scientific", "Sydney"),
        ("Thermo Fisher Scientific", "Adelaide"),
        ("Thermo Fisher Scientific Pty Ltd", "Sydney"),
    )
    (merged,) = D.propose(ads, PLACES, set())
    assert merged.name == "Thermo Fisher Scientific"
    assert (merged.ads, merged.local) == (4, 1)
    assert set(merged.other_spellings) == {"ThermoFisher Scientific", "Thermo Fisher Scientific Pty Ltd"}


def test_companies_already_followed_are_not_proposed():
    ads = _ads(("Canva", "Sydney"), ("Canva Pty Ltd", "Sydney"), ("Xero", "Sydney"),
               ("Commonwealth Bank", "Sydney"))
    already = D.existing_keys(
        [Company(name="Canva"), Company(name="CBA Group", aliases=["commonwealthbank"])]
    )
    assert [p.name for p in D.propose(ads, PLACES, already)] == ["Xero"]


def test_min_ads_drops_rare_names():
    ads = _ads(("Once", "Sydney"), ("Twice", "Sydney"), ("Twice", "Sydney"))
    assert [p.name for p in D.propose(ads, PLACES, set(), min_ads=2)] == ["Twice"]


def test_blank_names_and_stray_whitespace_are_handled():
    ads = _ads(("", "Sydney"), ("   ", "Sydney"), ("  Acme   Co ", "Sydney"))
    (only,) = D.propose(ads, PLACES, set())
    assert only.name == "Acme Co"


def test_ties_are_ordered_by_name():
    ads = _ads(("Beta", "Sydney"), ("alpha", "Sydney"))
    assert [p.name for p in D.propose(ads, PLACES, set())] == ["alpha", "Beta"]


# --- the file ----------------------------------------------------------------


def test_proposal_lines_are_all_comments_with_counts_and_sections():
    ads = _ads(("Local Co", "Adelaide"), ("Local Co", "Sydney"), ("Far Co", "Sydney"))
    lines = D.proposal_lines(D.propose(ads, PLACES, set()), PLACES)
    assert all(line.startswith("#") or not line for line in lines)
    text = "\n".join(lines)
    assert "# Local Co    # 2 ads, 1 local" in text
    assert "# Far Co    # 1 ad" in text
    assert text.index("Local Co") < text.index("Elsewhere") < text.index("Far Co")
    assert "(1) ---" in text  # section counts


def test_proposal_lines_with_no_places_say_so():
    text = "\n".join(D.proposal_lines(D.propose(_ads(("Acme", "Sydney")), [], set()), []))
    assert "none set" in text


def test_an_uncommented_proposal_is_a_valid_companies_line(tmp_path):
    ads = _ads(
        ("Moody's", "Adelaide"), ("Dan Murphy's | Woolworths", "Adelaide"),
        ("Odd # Name", "Sydney"), ("Thermo Fisher Scientific", "Sydney"),
        ("ThermoFisher Scientific", "Sydney"), ("Thermo Fisher Scientific", "Sydney"),
    )
    lines = D.proposal_lines(D.propose(ads, PLACES, set()), PLACES)
    # What the user does: delete the leading "# " of each proposal line.
    uncommented = [
        line[2:] for line in lines
        if line.startswith("# ") and "    # " in line
    ]
    path = tmp_path / "companies.txt"
    path.write_text("\n".join(uncommented) + "\n", encoding="utf-8")

    names = [c.name for c in load_companies_file(str(path)).companies]
    assert names == ["Dan Murphy's / Woolworths", "Moody's", "Thermo Fisher Scientific", "Odd Name"]


# --- main --------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db():
    database._database_url = None
    database._engine = None
    yield
    database._database_url = None
    database._engine = None


def _job(job_board_id, company, location):
    return JobPost(
        job_board_id=job_board_id, title="Data Analyst", company=company,
        location=location, description="d", url=f"http://x/{job_board_id}",
    )


def _run(tmp_path, monkeypatch, jobs, *, companies_text=None, argv=()):
    raw = {
        "sources": [{"type": "greenhouse", "board": "acme"}],
        "filters": {"locations": ["Adelaide"]},
        "database": {"url": f"sqlite:///{tmp_path / 'jobs.db'}"},
    }
    if companies_text is not None:
        companies = tmp_path / "companies.txt"
        companies.write_text(companies_text, encoding="utf-8")
        raw["companies_file"] = str(companies)
    cfg = config.Config.model_validate(raw)
    monkeypatch.setattr(D, "load_config", lambda: cfg)
    database.set_database_url(cfg.database.url)
    database.init_db()
    with database.get_session() as session:
        session.add_all(jobs)
        session.commit()
    out = tmp_path / "proposals.txt"
    D.main(["--out", str(out), *argv])
    return out.read_text(encoding="utf-8")


def test_main_writes_proposals_from_adzuna_rows_only(tmp_path, monkeypatch):
    jobs = [
        _job("adzuna-1", "Local Co", "Adelaide, South Australia"),
        _job("adzuna-2", "Far Co", "Sydney"),
        _job("greenhouse-3", "Board Co", "Adelaide"),  # an ATS row: not mined
    ]
    text = _run(tmp_path, monkeypatch, jobs)
    assert "# Local Co    # 1 ad, 1 local" in text
    assert "# Far Co    # 1 ad" in text
    assert "Board Co" not in text


def test_main_skips_companies_already_in_the_companies_file(tmp_path, monkeypatch):
    jobs = [_job("adzuna-1", "Canva Pty Ltd", "Sydney"), _job("adzuna-2", "Xero", "Sydney")]
    text = _run(tmp_path, monkeypatch, jobs, companies_text="Canva\n")
    assert "Xero" in text and "Canva" not in text


def test_main_works_when_the_companies_file_does_not_exist_yet(tmp_path, monkeypatch):
    jobs = [_job("adzuna-1", "Xero", "Sydney")]
    cfg = config.Config.model_validate(
        {
            "sources": [{"type": "greenhouse", "board": "acme"}],
            "companies_file": str(tmp_path / "missing.txt"),
            "database": {"url": f"sqlite:///{tmp_path / 'jobs.db'}"},
        }
    )
    monkeypatch.setattr(D, "load_config", lambda: cfg)
    database.set_database_url(cfg.database.url)
    database.init_db()
    with database.get_session() as session:
        session.add_all(jobs)
        session.commit()
    out = tmp_path / "proposals.txt"
    D.main(["--out", str(out)])
    assert "Xero" in out.read_text(encoding="utf-8")


def test_main_min_ads(tmp_path, monkeypatch):
    jobs = [
        _job("adzuna-1", "Once", "Sydney"),
        _job("adzuna-2", "Twice", "Sydney"), _job("adzuna-3", "Twice", "Sydney"),
    ]
    text = _run(tmp_path, monkeypatch, jobs, argv=["--min-ads", "2"])
    assert "Twice" in text and "Once" not in text


def test_main_with_no_adzuna_rows_exits_with_a_message(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, monkeypatch, [_job("greenhouse-1", "Acme", "Adelaide")])
    assert exc.value.code == 1
    assert "none from Adzuna" in capsys.readouterr().err


def test_main_with_an_empty_database_says_to_scrape_first(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run(tmp_path, monkeypatch, [])
    assert "run `make scrape` first" in capsys.readouterr().err


def test_main_exits_on_a_config_error(monkeypatch, capsys):
    def boom():
        raise config.ConfigError("bad config.yaml")

    monkeypatch.setattr(D, "load_config", boom)
    with pytest.raises(SystemExit) as exc:
        D.main([])
    assert exc.value.code == 1
    assert "bad config.yaml" in capsys.readouterr().err
