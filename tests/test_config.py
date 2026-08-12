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
    SeekSource,
    load_config,
)


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_legacy_search_becomes_seek_source_and_filters(tmp_path):
    path = _write(
        tmp_path,
        """
        search:
          title: "data analyst"
          location: "Adelaide"
        """,
    )
    cfg = load_config(path)
    assert len(cfg.resolved_sources) == 1
    assert isinstance(cfg.resolved_sources[0], SeekSource)
    assert cfg.resolved_filters.titles == ["data analyst"]
    assert cfg.resolved_filters.locations == ["Adelaide"]


def test_filters_plus_sources(tmp_path):
    path = _write(
        tmp_path,
        """
        filters:
          titles: ["data analyst", "data scientist"]
          locations: ["Adelaide", "Sydney"]
        sources:
          - type: seek
          - type: greenhouse
            board: stripe
          - type: lever
            company: figma
        """,
    )
    cfg = load_config(path)
    types = [s.type for s in cfg.resolved_sources]
    assert types == ["seek", "greenhouse", "lever"]
    assert isinstance(cfg.resolved_sources[1], GreenhouseSource)
    assert isinstance(cfg.resolved_sources[2], LeverSource)
    assert cfg.resolved_filters.titles == ["data analyst", "data scientist"]


def test_filters_take_precedence_over_legacy_search(tmp_path):
    path = _write(
        tmp_path,
        """
        filters:
          titles: ["engineer"]
        search:
          title: "ignored"
          location: "Nowhere"
        """,
    )
    cfg = load_config(path)
    assert cfg.resolved_filters.titles == ["engineer"]


def test_defaults(tmp_path):
    path = _write(tmp_path, "search:\n  title: x\n")
    cfg = load_config(path)
    assert cfg.database.url == DEFAULT_DATABASE_URL
    assert cfg.llm.backend == "ollama"
    assert cfg.resolved_filters.locations == ["All Australia"]


def test_missing_any_source_raises(tmp_path):
    path = _write(tmp_path, "filters:\n  titles: [x]\n")  # filters but no source
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_source_type_raises(tmp_path):
    path = _write(tmp_path, "sources:\n  - type: linkedin\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_llm_allows_unknown_keys():
    cfg = Config.model_validate(
        {"search": {"title": "x"}, "llm": {"backend": "future", "temperature": 0.7}}
    )
    assert cfg.llm.backend == "future"


def test_llm_batch_and_retry_defaults():
    cfg = Config.model_validate({"search": {"title": "x"}})
    assert cfg.llm.batch_size == 0  # 0 = unbounded
    assert cfg.llm.max_retries == 2
    assert cfg.llm.retry_backoff == 1.0


def test_llm_accepts_positive_batch_and_retry():
    cfg = Config.model_validate(
        {"search": {"title": "x"}, "llm": {"batch_size": 20, "max_retries": 5}}
    )
    assert cfg.llm.batch_size == 20
    assert cfg.llm.max_retries == 5


@pytest.mark.parametrize(
    "bad",
    [{"batch_size": -1}, {"max_retries": -1}, {"retry_backoff": -0.5}],
)
def test_llm_rejects_negative_values(bad):
    with pytest.raises(ValidationError):
        Config.model_validate({"search": {"title": "x"}, "llm": bad})


def test_empty_filters_default():
    f = Filters()
    assert f.titles == [] and f.locations == []
