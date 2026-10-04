import textwrap

import pytest
from pydantic import ValidationError

from src.config import (
    DEFAULT_DATABASE_URL,
    Config,
    ConfigError,
    Filters,
    GreenhouseSource,
    LeverSource,
    WorkdaySource,
    load_config,
    RESOLVABLE,
    source_key,
)


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


# Smallest valid config body for tests that only care about other fields.
MIN = {"sources": [{"type": "greenhouse", "board": "acme"}]}


def test_legacy_search_block_is_rejected_with_a_pointer(tmp_path):
    # `search:` only ever drove SEEK (removed in KAN-32). It must fail loudly
    # and say what to do, not silently plan zero scrapes.
    path = _write(
        tmp_path,
        """
        search:
          title: "data analyst"
          location: "Adelaide"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "'search:' block is no longer supported" in str(exc.value)
    assert "filters" in str(exc.value)


def test_filters_plus_sources(tmp_path):
    path = _write(
        tmp_path,
        """
        filters:
          titles: ["data analyst", "data scientist"]
          locations: ["Adelaide", "Sydney"]
        sources:
          - type: adzuna
            country: in
          - type: greenhouse
            board: stripe
          - type: lever
            company: figma
        """,
    )
    cfg = load_config(path)
    types = [s.type for s in cfg.resolved_sources]
    assert types == ["adzuna", "greenhouse", "lever"]
    assert cfg.resolved_sources[0].country == "in"
    assert isinstance(cfg.resolved_sources[1], GreenhouseSource)
    assert isinstance(cfg.resolved_sources[2], LeverSource)
    assert cfg.resolved_filters.titles == ["data analyst", "data scientist"]


def test_defaults(tmp_path):
    path = _write(tmp_path, "sources:\n  - type: greenhouse\n    board: x\n")
    cfg = load_config(path)
    assert cfg.database.url == DEFAULT_DATABASE_URL
    assert cfg.llm.backend == "ollama"
    assert cfg.resolved_filters.titles == []
    assert cfg.resolved_filters.locations == []


def test_missing_any_source_raises(tmp_path):
    path = _write(tmp_path, "filters:\n  titles: [x]\n")  # filters but no source
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_source_type_raises(tmp_path):
    path = _write(tmp_path, "sources:\n  - type: linkedin\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_seek_source_type_is_unknown(tmp_path):
    path = _write(tmp_path, "sources:\n  - type: seek\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_all_commented_sources_file_resolves_to_nothing(tmp_path):
    (tmp_path / "sources.txt").write_text("# seek\n# greenhouse x\n", encoding="utf-8")
    cfg = Config.model_validate({"sources_file": str(tmp_path / "sources.txt")})
    assert cfg.resolved_sources == []


def test_llm_allows_unknown_keys():
    cfg = Config.model_validate(
        {**MIN, "llm": {"backend": "future", "temperature": 0.7}}
    )
    assert cfg.llm.backend == "future"


def test_llm_batch_and_retry_defaults():
    cfg = Config.model_validate(MIN)
    assert cfg.llm.batch_size == 0  # 0 = unbounded
    assert cfg.llm.max_retries == 2
    assert cfg.llm.retry_backoff == 1.0


def test_llm_accepts_positive_batch_and_retry():
    cfg = Config.model_validate(
        {**MIN, "llm": {"batch_size": 20, "max_retries": 5}}
    )
    assert cfg.llm.batch_size == 20
    assert cfg.llm.max_retries == 5


@pytest.mark.parametrize(
    "bad",
    [{"batch_size": -1}, {"max_retries": -1}, {"retry_backoff": -0.5}, {"timeout": -1}],
)
def test_llm_rejects_negative_values(bad):
    with pytest.raises(ValidationError):
        Config.model_validate({**MIN, "llm": bad})


def test_empty_filters_default():
    f = Filters()
    assert f.titles == [] and f.locations == []
    assert f.board_locations == []


def test_noise_filters_default_to_empty_and_parse_from_yaml(tmp_path):
    f = Filters()
    assert f.also_match_titles == [] and f.exclude_titles == [] and f.remote_regions == []

    path = _write(
        tmp_path,
        """
        filters:
          titles: ["data analyst"]
          also_match_titles: ["data engineer", "business analyst"]
          exclude_titles: ["senior", "sr", "intern"]
          remote_regions: ["Australia", "India", "APAC"]
        sources:
          - type: greenhouse
            board: stripe
        """,
    )
    f = load_config(path).resolved_filters
    assert f.also_match_titles == ["data engineer", "business analyst"]
    assert f.exclude_titles == ["senior", "sr", "intern"]
    assert f.remote_regions == ["Australia", "India", "APAC"]


def test_board_locations_parse_from_yaml(tmp_path):
    path = _write(
        tmp_path,
        """
        filters:
          locations: ["Adelaide"]
          board_locations: ["Adelaide", "SA", "Bedford Park"]
        sources:
          - type: greenhouse
            board: stripe
        """,
    )
    f = load_config(path).resolved_filters
    assert f.locations == ["Adelaide"]
    assert f.board_locations == ["Adelaide", "SA", "Bedford Park"]


def test_archive_url_defaults_to_unset():
    # Unset means "dead_jobs.db next to the main database" (src/storage/archive.py).
    assert Config.model_validate(MIN).database.archive_url is None


def test_resolvable_makes_a_source_of_each_board_type_from_a_token():
    made = {kind: build("Tok") for kind, build in RESOLVABLE.items()}
    assert set(made) == {"greenhouse", "lever", "ashby", "workable"}
    assert all(src.type == kind for kind, src in made.items())
    assert "workday" not in RESOLVABLE  # needs a URL, not a name


def test_source_key_lowercases_the_token_for_every_type_but_keeps_the_source():
    for kind, build in RESOLVABLE.items():
        upper, lower = build("Spotify"), build("spotify")
        assert source_key(upper) == source_key(lower) == (kind, "spotify")
    # The source keeps the token exactly as written.
    assert RESOLVABLE["workable"]("Squiz").account == "Squiz"


def test_source_key_differs_by_type_and_workday_is_keyed_by_tenant():
    assert source_key(RESOLVABLE["greenhouse"]("acme")) != source_key(RESOLVABLE["lever"]("acme"))
    one = WorkdaySource(type="workday", tenant="CBA", datacenter="wd3", site="A")
    two = WorkdaySource(type="workday", tenant="cba", datacenter="wd3", site="B")
    assert source_key(one) == source_key(two) == ("workday", "cba")


def test_resolve_and_companies_keys_parse(tmp_path):
    path = _write(
        tmp_path,
        """
        companies_file: "companies.txt"
        board_map_file: "data/map.yaml"
        resolve:
          boards: [greenhouse, workable]
          recheck_days: 7
          stale_after: 2
        """,
    )
    cfg = load_config(path)
    assert cfg.companies_file == "companies.txt"
    assert cfg.board_map_file == "data/map.yaml"
    assert cfg.resolve.boards == ["greenhouse", "workable"]
    assert (cfg.resolve.recheck_days, cfg.resolve.stale_after) == (7, 2)


def test_resolve_defaults():
    cfg = Config.model_validate(MIN)
    assert cfg.companies_file is None
    assert cfg.board_map_file == "data/board_map.yaml"
    assert cfg.resolve.boards == ["greenhouse", "lever", "ashby", "workable"]
    assert (cfg.resolve.recheck_days, cfg.resolve.stale_after) == (14, 3)


def test_companies_file_alone_satisfies_the_source_check():
    cfg = Config.model_validate({"companies_file": "companies.txt"})
    assert cfg.pinned_sources == []


def test_no_sources_at_all_still_fails():
    with pytest.raises(ValidationError):
        Config.model_validate({})


def test_unknown_resolve_board_is_a_config_error_listing_the_valid_ones(tmp_path):
    path = _write(tmp_path, "companies_file: c.txt\nresolve:\n  boards: [greenhouse, taleo]\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    message = str(exc.value)
    assert "taleo" in message and "workable" in message


def test_workday_is_not_a_resolve_board(tmp_path):
    path = _write(tmp_path, "companies_file: c.txt\nresolve:\n  boards: [workday]\n")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("bad", ["stale_after: 0", "recheck_days: -1"])
def test_resolve_numbers_are_range_checked(tmp_path, bad):
    path = _write(tmp_path, f"companies_file: c.txt\nresolve:\n  {bad}\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_resolve_key_is_rejected(tmp_path):
    path = _write(tmp_path, "companies_file: c.txt\nresolve:\n  recheck: 3\n")
    with pytest.raises(ConfigError):
        load_config(path)
