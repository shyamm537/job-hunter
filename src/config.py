"""Typed, validated configuration for Job Hunter AI.

The config separates *what* you're looking for from *where* you look:

- `filters`: the titles and locations you want (shared across all sources).
- `sources`: where to look — a SEEK/Adzuna search, or a Greenhouse/Lever board.

This split exists because the source types use that intent differently. SEEK and
Adzuna are search engines, so each (title, location) becomes a query. An ATS
board (Greenhouse, Lever) returns a company's whole list, so the filters are
applied to the results afterwards. Keeping titles/locations out of the
per-source lines is what makes the source file clean and the model easy to
reason about.

`load_config()` validates everything up front, so a missing or malformed
`config.yaml` fails with a readable message instead of a bare `KeyError`.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated, List, Literal, Optional, Tuple, Union
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_DATABASE_URL = "sqlite:///data/jobs.db"
DEFAULT_LOCATION = "All Australia"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_ADZUNA_COUNTRY = "au"

CONFIG_PATH = "config.yaml"


class ConfigError(Exception):
    """Raised when config.yaml (or a sources file) is missing or invalid.

    Carries a human-readable message suitable for printing straight to a
    CLI user — no Pydantic traceback required.
    """


class Filters(BaseModel):
    """What you're looking for, shared across every source.

    For SEEK/Adzuna, titles x locations become search queries. For ATS boards,
    they filter the fetched postings (see src/ingestion/filtering.py). Empty
    lists mean "no filter" — every posting passes.
    """

    titles: List[str] = Field(default_factory=list)
    locations: List[str] = Field(default_factory=list)


class SeekSource(BaseModel):
    """A SEEK search. Titles come from `filters`.

    SEEK is an Australia/New-Zealand board, so its *search* locations can be
    scoped here, independently of the global `filters.locations`. Set
    `locations` to the AU/NZ places you want SEEK to search; leave it unset to
    fall back to `filters.locations`. (The global `filters.locations` still
    post-filter ATS results across *all* regions — that's how non-AU coverage,
    e.g. India, comes through Greenhouse/Lever/Ashby boards.)
    """

    type: Literal["seek"] = "seek"
    locations: Optional[List[str]] = None


class GreenhouseSource(BaseModel):
    """A Greenhouse board. `board` is the public token, e.g. the `stripe` in
    `boards.greenhouse.io/stripe`."""

    type: Literal["greenhouse"]
    board: str


class LeverSource(BaseModel):
    """A Lever board. `company` is the public token, e.g. the `figma` in
    `jobs.lever.co/figma`."""

    type: Literal["lever"]
    company: str


class AshbySource(BaseModel):
    """An Ashby board. `org` is the public token, e.g. the `ashby` in
    `jobs.ashbyhq.com/ashby`."""

    type: Literal["ashby"]
    org: str


class AdzunaSource(BaseModel):
    """An Adzuna search, scoped to one country index (`au`, `in`, `gb`, ...).

    Adzuna's API is per-country (the country is a path segment), so you list one
    source per country you want, e.g. `adzuna au` and `adzuna in`. Titles come
    from `filters`; the planner pairs each of `filters.locations` with the
    matching country (so Adelaide is searched under `au`, Mumbai under `in`).
    Credentials are NOT here — they're account-level, in the top-level `adzuna:`
    block (app_id/app_key). See docs/scrapers.md.
    """

    type: Literal["adzuna"]
    country: str = DEFAULT_ADZUNA_COUNTRY

    @model_validator(mode="after")
    def _normalise_country(self) -> "AdzunaSource":
        self.country = (self.country or DEFAULT_ADZUNA_COUNTRY).strip().lower()
        return self


class AshbySource(BaseModel):
    """An Ashby job board. `board` is the public board token, e.g. the
    `board` in `api.ashbyhq.com/posting-api/job-board/{board}`. See
    `src/ingestion/ashbu.py`."""
    type: Literal["ashby"]
    board: str
    # Optional client-side filter: only keep postings whose title contains
    # this substring (case-insensitive). Greenhouse boards list every open
    # role at a company, so this is usually wanted.
    title_contains: Optional[str] = None


# Discriminated union: Pydantic picks the model by the `type` field.
Source = Annotated[
    Union[SeekSource, GreenhouseSource, LeverSource, AshbySource, AdzunaSource],
    Field(discriminator="type"),
]


class SearchConfig(BaseModel):
    """Legacy single-search shim, kept so older configs keep working.

    A `search:` block is equivalent to one SEEK source plus a one-title,
    one-location `filters` block. Prefer `filters` + `sources` for anything new.
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

    # Max pending jobs to process per `make process` run. 0 (the default)
    # means no limit — drain the whole queue. A positive value bounds an
    # unattended/cron run so it stops after a fixed number of jobs and frees
    # the GPU. Interactive runs don't need it: each job commits independently,
    # so Ctrl-C never loses finished work.
    batch_size: int = 0

    # Retry policy for transient LLM-call failures (e.g. Ollama briefly
    # unreachable mid-run). `max_retries` is *additional* attempts after the
    # first, so 2 means up to 3 tries. Backoff is exponential:
    # retry_backoff * 2**attempt seconds between tries (0 disables the wait).
    max_retries: int = 2
    retry_backoff: float = 1.0

    @model_validator(mode="after")
    def _non_negative(self) -> "LLMConfig":
        for name in ("batch_size", "max_retries"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.retry_backoff < 0:
            raise ValueError("retry_backoff must be >= 0")
        return self


class AdzunaConfig(BaseModel):
    """Adzuna API credentials (free tier: register at developer.adzuna.com).

    Account-level, so they live here rather than on each AdzunaSource. Having
    these makes config.yaml genuinely sensitive — it's already gitignored.
    """

    app_id: str
    app_key: str


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

    filters: Filters = Field(default_factory=Filters)
    sources: Optional[List[Source]] = None
    # Path to a plain-text file of sources, one per line (see load_sources_file).
    # Its sources are appended to any inline `sources` above.
    sources_file: Optional[str] = None
    # Legacy: a single SEEK search. Used as a fallback for filters and sources.
    search: Optional[SearchConfig] = None
    llm: LLMConfig = Field(default_factory=LLMConfig)
    adzuna: Optional[AdzunaConfig] = None
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    resume_summary: str = ""

    @model_validator(mode="after")
    def _require_at_least_one_source(self) -> "Config":
        if not self.sources and not self.search and not self.sources_file:
            raise ValueError(
                "config must define 'sources', a 'sources_file', or a legacy "
                "'search' block"
            )
        return self

    @property
    def resolved_sources(self) -> List[Source]:
        """Where to look: inline `sources` + `sources_file`, falling back to a
        single SEEK source derived from a legacy `search:` block."""
        collected: List[Source] = list(self.sources or [])
        if self.sources_file:
            collected.extend(load_sources_file(self.sources_file))
        if collected:
            return collected
        if self.search is not None:
            return [SeekSource()]
        return []

    @property
    def resolved_filters(self) -> Filters:
        """What to look for: the `filters` block, falling back to a legacy
        `search:` block (its title/location become one-element lists)."""
        if self.filters.titles or self.filters.locations:
            return self.filters
        if self.search is not None:
            return Filters(titles=[self.search.title], locations=[self.search.location])
        return Filters()

    @property
    def adzuna_auth(self) -> Optional[Tuple[str, str]]:
        """(app_id, app_key) if Adzuna creds are configured, else None. The
        scrape CLI passes this into the planner so AdzunaScrapers can be built."""
        if self.adzuna is None:
            return None
        return (self.adzuna.app_id, self.adzuna.app_key)


# Careers-URL host → ATS type. The painless way to add a board: paste its
# careers URL and let us work out the ATS + token, instead of needing to know
# both up front. Greenhouse serves boards under two hosts (the older
# `boards.` and the newer `job-boards.`); both map to the same scraper.
_ATS_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}


def _looks_like_url(token: str) -> bool:
    """Heuristic: does this single token look like a careers URL rather than a
    bare source word like `seek` or `greenhouse`?

    True if it carries a scheme (`https://…`) or a host/path separator (`/`) or
    a dotted host (`boards.greenhouse.io`). The bare `<type> <token>` forms have
    none of these, so the two never collide.
    """
    return "://" in token or "/" in token or "." in token


def source_from_url(url: str, where: str = "url") -> Source:
    """Detect the ATS + token from a pasted careers URL.

        https://boards.greenhouse.io/stripe      → greenhouse stripe
        https://job-boards.greenhouse.io/stripe  → greenhouse stripe
        https://jobs.lever.co/metabase           → lever metabase
        https://jobs.ashbyhq.com/ashby           → ashby ashby

    The scheme is optional (`boards.greenhouse.io/stripe` works), trailing
    paths/slashes are ignored (a deep posting link still yields the org token),
    and an unrecognised host raises a `ConfigError` naming the supported ones.
    `where` is a context label (e.g. "sources.txt line 4") for error messages.
    """
    raw = url.strip()
    # urlparse only fills `netloc` when a scheme is present; without one the
    # whole thing lands in `path`. Prepend a scheme so the host parses out.
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc.lower().split(":")[0]  # drop any :port
    if host.startswith("www."):
        host = host[4:]

    ats = _ATS_HOSTS.get(host)
    if ats is None:
        supported = ", ".join(sorted(_ATS_HOSTS))
        raise ConfigError(
            f"{where}: unrecognised careers URL host {host or url!r} "
            f"(supported: {supported})"
        )

    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        raise ConfigError(
            f"{where}: could not find a board token in URL {url!r} "
            f"(expected e.g. https://{host}/<token>)"
        )
    token = segments[0]

    if ats == "greenhouse":
        return GreenhouseSource(type="greenhouse", board=token)
    if ats == "lever":
        return LeverSource(type="lever", company=token)
    return AshbySource(type="ashby", org=token)


def source_to_line(source: Source) -> str:
    """Serialise a Source back to a sources-file line — the inverse of
    `_source_from_line`. Used by the board validator to write a clean
    `sources.txt` of confirmed-live boards."""
    if source.type == "seek":
        return "seek"
    if source.type == "greenhouse":
        return f"greenhouse {source.board}"
    if source.type == "lever":
        return f"lever {source.company}"
    if source.type == "ashby":
        return f"ashby {source.org}"
    if source.type == "adzuna":
        return f"adzuna {source.country}"
    raise ValueError(f"Unknown source type: {source.type!r}")


def _source_from_line(path: str, lineno: int, parts: List[str]) -> Source:
    # A single token that looks like a URL → auto-detect the ATS + token, so a
    # pasted careers URL works alongside the explicit `<type> <token>` form.
    if len(parts) == 1 and _looks_like_url(parts[0]):
        return source_from_url(parts[0], where=f"{path} line {lineno}")

    kind = parts[0].lower()
    args = parts[1:]
    if kind == "seek":
        if args:
            raise ConfigError(
                f"{path} line {lineno}: 'seek' takes no arguments "
                "(titles/locations come from the filters block)"
            )
        return SeekSource()
    if kind == "greenhouse":
        if len(args) != 1:
            raise ConfigError(
                f"{path} line {lineno}: 'greenhouse' needs exactly one board token"
            )
        return GreenhouseSource(type="greenhouse", board=args[0])
    if kind == "lever":
        if len(args) != 1:
            raise ConfigError(
                f"{path} line {lineno}: 'lever' needs exactly one company token"
            )
        return LeverSource(type="lever", company=args[0])
    if kind == "ashby":
        if len(args) != 1:
            raise ConfigError(
                f"{path} line {lineno}: 'ashby' needs exactly one org token"
            )
        return AshbySource(type="ashby", org=args[0])
    if kind == "adzuna":
        if len(args) > 1:
            raise ConfigError(
                f"{path} line {lineno}: 'adzuna' takes at most one country code "
                "(e.g. 'adzuna au' or 'adzuna in'); titles/locations are in filters"
            )
        country = args[0] if args else DEFAULT_ADZUNA_COUNTRY
        return AdzunaSource(type="adzuna", country=country)
    raise ConfigError(
        f"{path} line {lineno}: unknown source type {parts[0]!r} "
        "(expected seek, greenhouse, lever, ashby, or adzuna)"
    )


def load_sources_file(path: str) -> List[Source]:
    """Parse a plain-text sources file into Source models.

    One source per line; blank lines and `#` comments ignored. Format:

        seek                 # a SEEK search (uses the filters block)
        adzuna au            # an Adzuna search, country index au (needs creds)
        adzuna in            # ... and India
        greenhouse stripe    # an ATS board, by company token
        lever figma
        ashby ashby
        https://jobs.lever.co/metabase   # a pasted careers URL also works

    Titles and locations are NOT in this file — they live in `filters`.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"sources_file not found: {path}")

    sources: List[Source] = []
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise ConfigError(f"{path} line {lineno}: {exc}") from exc
        if not parts:
            continue
        sources.append(_source_from_line(path, lineno, parts))
    return sources


def load_config(path: str = CONFIG_PATH) -> Config:
    """Load and validate config.yaml, raising `ConfigError` on any problem."""
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
