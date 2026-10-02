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
    load_config,
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
    [{"batch_size": -1}, {"max_retries": -1}, {"retry_backoff": -0.5}],
)
def test_llm_rejects_negative_values(bad):
    with pytest.raises(ValidationError):
        Config.model_validate({**MIN, "llm": bad})


def test_empty_filters_default():
    f = Filters()
    assert f.titles == [] and f.locations == []
