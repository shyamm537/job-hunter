"""Typed, validated configuration for Job Hunter AI.

Replaces the previous `yaml.safe_load()` + raw-dict access with a Pydantic
schema. Load once with `load_config()` and pass the resulting `Config`
object around — a missing or malformed `config.yaml` now fails with a
readable message instead of a bare `KeyError` deep in a CLI.

The `llm` section is intentionally permissive (`extra="allow"`): the LLM
backend hasn't been decided yet, so this layer validates everything around
it without locking the LLM config down. The existing `src/llm/` code still
reads `config.yaml` on its own and is untouched by this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_DATABASE_URL = "sqlite:///data/jobs.db"
DEFAULT_LOCATION = "All Australia"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"

CONFIG_PATH = "config.yaml"


class ConfigError(Exception):
    """Raised when config.yaml is missing or fails validation.

    Carries a human-readable message suitable for printing straight to a
    CLI user — no Pydantic traceback required.
    """


class SeekSource(BaseModel):
    """A SEEK public-RSS search. See `src/ingestion/seek.py`."""

    type: Literal["seek"] = "seek"
    title: str
    location: str = DEFAULT_LOCATION


class GreenhouseSource(BaseModel):
    """A Greenhouse job board. `board` is the public board token, e.g. the
    `stripe` in `boards.greenhouse.io/stripe`. See
    `src/ingestion/greenhouse.py`."""

    type: Literal["greenhouse"]
    board: str
    # Optional client-side filter: only keep postings whose title contains
    # this substring (case-insensitive). Greenhouse boards list every open
    # role at a company, so this is usually wanted.
    title_contains: Optional[str] = None


# Discriminated union: Pydantic picks the model by the `type` field.
Source = Annotated[
    Union[SeekSource, GreenhouseSource],
    Field(discriminator="type"),
]


class SearchConfig(BaseModel):
    """Legacy single-search block, kept for backward compatibility.

    A bare `search:` block is treated as one SEEK source. Prefer the newer
    `sources:` list for anything beyond one search.
    """

    title: str
    location: str = DEFAULT_LOCATION


class LLMConfig(BaseModel):
    """LLM settings. Deliberately permissive — the backend is undecided, so
    unknown keys are allowed rather than rejected."""

    model_config = ConfigDict(extra="allow")

    backend: str = "ollama"
    model: str = "llama3"
    host: str = DEFAULT_OLLAMA_HOST
    api_key: Optional[str] = None


class DatabaseConfig(BaseModel):
    """Database connection. `url` is now actually read (it used to be a
    documented-but-ignored field — see docs/configuration.md).

    SQLite is the product; Postgres is a documented escape hatch that would
    need a real test pass before you trusted it. Any non-SQLite URL is
    *supported by abstraction only* (SQLModel/SQLAlchemy speak it), but
    unverified: no live run, and no non-SQLite driver is pinned in
    requirements.txt. Set such a URL at your own risk until that changes.
    """

    url: str = DEFAULT_DATABASE_URL


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: Optional[SearchConfig] = None
    sources: Optional[List[Source]] = None
    llm: LLMConfig = Field(default_factory=LLMConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    resume_summary: str = ""

    @model_validator(mode="after")
    def _require_at_least_one_source(self) -> "Config":
        if not self.sources and not self.search:
            raise ValueError(
                "config must define either a 'search' block or a 'sources' list"
            )
        return self

    @property
    def resolved_sources(self) -> List[Source]:
        """The list of sources to scrape.

        Prefers the explicit `sources:` list; falls back to deriving a
        single SEEK source from a legacy `search:` block.
        """
        if self.sources:
            return self.sources
        assert self.search is not None  # guaranteed by the validator above
        return [SeekSource(title=self.search.title, location=self.search.location)]


def load_config(path: str = CONFIG_PATH) -> Config:
    """Load and validate config.yaml, raising `ConfigError` on any problem.

    The error message is meant to be shown directly to the user — it names
    the offending fields rather than dumping a stack trace.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"{path} not found. Copy config.yaml.example to {path} and edit it:\n"
            f"    cp config.yaml.example {path}"
        )

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML:\n{exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")

    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        problems = "\n".join(
            f"  - {' -> '.join(str(loc) for loc in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"{path} is invalid:\n{problems}") from exc
