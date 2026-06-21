# Configuration

All user-facing configuration lives in `config.yaml`, created by copying
`config.yaml.example`. `config.yaml` is gitignored. It's loaded and validated
by `src/config.py` (Pydantic): a missing file or bad field fails with a
readable `ConfigError`, not a bare `KeyError`.

## The model: filters vs. sources

The config separates **what** you're looking for from **where** you look:

- `filters` — the `titles` and `locations` you want, shared across all sources.
- `sources` — where to look: a SEEK search, or a Greenhouse / Lever board.

```yaml
filters:
  titles:    ["data analyst", "data scientist"]
  locations: ["Adelaide", "Sydney"]

sources:
  - type: seek                 # uses the filters as searches
  - type: greenhouse
    board: "stripe"            # token from boards.greenhouse.io/stripe
  - type: lever
    company: "figma"           # token from jobs.lever.co/figma
```

This split exists because the source types use the intent differently:

- **SEEK** is a search engine, so `titles × locations` become search queries —
  two titles and two locations is four searches.
- **An ATS board** (Greenhouse, Lever) returns a company's *whole* list, so the
  same titles/locations filter the postings *after* fetching.

Keeping titles/locations out of the per-source lines is what makes the sources
clean and uniform — a source is purely "where".

### Filter semantics

- Empty `titles` or `locations` list = no filter on that dimension (everything
  passes).
- Title: case-insensitive substring against any wanted title.
- Location: case-insensitive substring against any wanted location, **but a
  posting whose location mentions "remote" always passes** — you rarely want to
  drop remote roles. (See `src/ingestion/filtering.py`.)
- SEEK is never post-filtered; its search query already did the filtering.

### Regions: SEEK is AU/NZ, ATS is global

SEEK (`au.seek.com`) only covers Australia/New Zealand, so it can only *search*
AU/NZ locations. ATS boards (Greenhouse/Lever/Ashby) are different: they return
a company's whole global list, and `filters.locations` keeps the ones you want.

That's how you cover other regions — e.g. **India**: put your Indian cities
(`"Bangalore"`, `"Mumbai"`, plus `"Remote"`) in `filters.locations`, add
India-hiring company boards as `greenhouse`/`lever`/`ashby` sources, and those
postings come through the location filter. To stop SEEK from running pointless
searches for non-AU cities, give the seek source its own AU/NZ `locations`:

```yaml
filters:
  titles: ["data analyst"]
  locations: ["Adelaide", "Bangalore", "Remote"]   # ATS filter (all regions)
sources:
  - type: seek
    locations: ["Adelaide"]        # SEEK searches AU/NZ only
  - type: greenhouse
    board: "some-india-hiring-company"
```

There is no clean, public-feed equivalent of SEEK for India (Naukri/Indeed have
no public RSS/API — only ToS-violating scrapers), so India coverage is ATS +
the location filter by design. See `TODO.md`.

## Sources file

For a long list of boards, point at a plain-text file instead of inlining:

```yaml
sources_file: "sources.txt"
```

### How `sources` and `sources_file` relate

They are **merged, not synchronized** — additive, never a mirror of each other.
This trips people up, so to be explicit:

- The final source list is `sources` (inline YAML) **plus** every line in
  `sources_file`, concatenated. A board listed in *either* place is scraped.
- You do **not** have to keep the two in sync. Adding `lever ramp` to
  `sources.txt` does *not* require any edit to `config.yaml`'s `sources:` block
  (and vice versa). Listing the same board in both just scrapes it twice.
- The **only** coupling is a pointer: `config.yaml` must contain the
  `sources_file: "sources.txt"` line for the file to be read at all. With no
  `sources_file:` key, the file is ignored entirely — however many lines it has.
- Filters (`titles` / `locations`) live **only** in `config.yaml` and apply to
  every source regardless of which file it came from. `sources.txt` holds the
  *where* only; it never carries filters.

Its sources are appended to any inline `sources`. One source per line, blank
lines and `#` comments ignored. Lines say *where* only (no titles/locations):

```
seek                 # a SEEK search, driven by filters
greenhouse stripe    # an ATS board, by company token
lever figma
```

### Pasting a careers URL

You don't need to know the ATS *and* token — paste the board's careers URL on a
line and the type + token are auto-detected:

```
https://boards.greenhouse.io/stripe        # → greenhouse stripe
https://job-boards.greenhouse.io/stripe    # → greenhouse stripe (newer host)
https://jobs.lever.co/metabase             # → lever metabase
https://jobs.ashbyhq.com/ashby             # → ashby ashby
```

The scheme is optional (`boards.greenhouse.io/stripe` works), and a deep link
to a specific posting still resolves to the org token (the first path segment).
An unrecognised host fails with a `ConfigError` listing the supported hosts.
Detection lives in `source_from_url()` (`src/config.py`).

A bad line fails with a `ConfigError` naming the line number. See
`sources.txt.example`.

## Field reference

| Key | Used by | Status |
|---|---|---|
| `filters.titles` / `filters.locations` | `src/ingestion/planner.py` (SEEK queries) + `src/ingestion/filtering.py` (ATS post-filter) | Implemented. Empty = no filter. |
| `sources[].type: seek` (`locations?`) | expands to `titles × locations` SEEK searches | Implemented. SEEK is AU/NZ-only; optional `locations` scopes *its* searches (falls back to `filters.locations`). |
| `sources[].type: greenhouse` (`board`) | `GreenhouseScraper`, then post-filtered | Implemented. |
| `sources[].type: lever` (`company`) | `LeverScraper`, then post-filtered | Implemented. |
| `sources[].type: ashby` (`org`) | `AshbyScraper`, then post-filtered | Implemented. `org` is the `jobs.ashbyhq.com/<org>` token. |
| `sources_file` | `src/config.py` → `load_sources_file()` | Implemented. Appended to inline `sources`. |
| `search` (legacy) | shim → one seek source + a one-title/one-location filters block | Implemented. Prefer `filters` + `sources`. |
| `llm.backend` / `llm.model` / `llm.host` | `src/llm/client.py` | Implemented. Only `ollama` valid in the LLM layer; config layer permits extra `llm.*` keys (backend undecided). |
| `resume_summary` | `src/llm/cli.py` → prompt templates | Implemented; a hand-written string, not parsed from a file. |
| `database.url` | `src/storage/database.py` | Wired. See below. |

At least one of `sources`, `sources_file`, or legacy `search` must be present,
or validation fails.

## Database URL resolution

`src/storage/database.py` builds its engine lazily from the first of these
that's set:

1. An explicit `set_database_url(...)` call — the CLIs do this after loading config.
2. The `JOBHUNTER_DATABASE_URL` environment variable.
3. `database.url` from `config.yaml`.
4. The SQLite default, `sqlite:///data/jobs.db`.

**SQLite is the product; Postgres is a documented escape hatch that would need a
real test pass before you trusted it.** The abstraction (SQLModel/SQLAlchemy)
*can* speak Postgres, but that path has never been run here and no non-SQLite
driver (e.g. `psycopg`) is pinned. Treat any non-SQLite URL as
open-but-unimplemented. Tracked in `TODO.md`.

## Validation

`src/config.py` defines a `Config` Pydantic model loaded once via
`load_config()` and passed around as a typed object. It rejects unknown
top-level keys (`extra="forbid"`) but is permissive inside `llm`
(`extra="allow"`), so the undecided LLM backend can carry whatever fields it
eventually needs.
