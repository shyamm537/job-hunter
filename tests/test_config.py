import textwrap

import pytest

from src.config import (
    DEFAULT_DATABASE_URL,
    Config,
    ConfigError,
    GreenhouseSource,
    SeekSource,
    load_config,
)


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_legacy_search_block_derives_single_seek_source(tmp_path):
    path = _write(
        tmp_path,
        """
        search:
          title: "data analyst"
          location: "Adelaide"
        """,
    )
    config = load_config(path)
    sources = config.resolved_sources

    assert len(sources) == 1
    assert isinstance(sources[0], SeekSource)
    assert sources[0].title == "data analyst"
    assert sources[0].location == "Adelaide"


def test_defaults_when_only_search_given(tmp_path):
    path = _write(
        tmp_path,
        """
        search:
          title: "data analyst"
        """,
    )
    config = load_config(path)

    # Sensible defaults are filled in rather than required.
    assert config.database.url == DEFAULT_DATABASE_URL
    assert config.llm.backend == "ollama"
    assert config.resolved_sources[0].location == "All Australia"


def test_sources_list_parses_discriminated_union(tmp_path):
    path = _write(
        tmp_path,
        """
        sources:
          - type: seek
            title: "data analyst"
            location: "Sydney"
          - type: greenhouse
            board: "stripe"
            title_contains: "engineer"
        database:
          url: "postgresql://localhost/jobs"
        """,
    )
    config = load_config(path)
    sources = config.resolved_sources

    assert isinstance(sources[0], SeekSource)
    assert isinstance(sources[1], GreenhouseSource)
    assert sources[1].board == "stripe"
    assert sources[1].title_contains == "engineer"
    assert config.database.url == "postgresql://localhost/jobs"


def test_sources_takes_precedence_over_search(tmp_path):
    path = _write(
        tmp_path,
        """
        search:
          title: "ignored"
        sources:
          - type: greenhouse
            board: "acme"
        """,
    )
    config = load_config(path)
    sources = config.resolved_sources

    assert len(sources) == 1
    assert isinstance(sources[0], GreenhouseSource)


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(str(tmp_path / "does-not-exist.yaml"))
    assert "not found" in str(exc.value)


def test_no_source_or_search_raises_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
        llm:
          backend: ollama
        """,
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_source_type_raises_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
        sources:
          - type: linkedin
            title: "data analyst"
        """,
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_llm_section_allows_unknown_keys():
    # The LLM backend is undecided, so extra keys must not be rejected.
    config = Config.model_validate(
        {
            "search": {"title": "x"},
            "llm": {"backend": "some-future-thing", "temperature": 0.7},
        }
    )
    assert config.llm.backend == "some-future-thing"
