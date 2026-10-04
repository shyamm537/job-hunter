# Configuration

All user-facing configuration lives in `config.yaml`, created by copying
`config.yaml.example`. `config.yaml` is gitignored. It's loaded and validated
by `src/config.py` (Pydantic): a missing file or bad field fails with a
readable `ConfigError`, not a bare `KeyError`.

## The model: filters vs. sources

The config separates **what** you're looking for from **where** you look:

- `filters` — the `titles` and `locations` you want, shared across all sources.
- `sources` — where to look: an Adzuna search, or a Greenhouse / Lever / Ashby / Workable / Workday board.

```yaml
filters:
  titles:    ["data analyst", "data scientist"]
  locations: ["Adelaide", "Sydney"]

sources:
  - type: adzuna               # uses the filters as searches; needs adzuna: creds
    country: "au"
  - type: greenhouse
    board: "stripe"            # token from boards.greenhouse.io/stripe
  - type: lever
    company: "figma"           # token from jobs.lever.co/figma
```

This split exists because the source types use the intent differently:

- **Adzuna** is a search engine, so `titles × locations` become search queries —
  two titles and two locations is four searches (per matching country).
- **An ATS board** (Greenhouse, Lever, Ashby, Workable, Workday) returns a company's *whole* list, so the
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
- Adzuna is never post-filtered; its search query already did the filtering.

### `filters.board_locations` (optional, boards only)

Boards often name places their own way (`SA Western Area`, `Support Office SA`,
`Bedford Park / Kaurna Country`), and none says "Adelaide". Adding those names to
`filters.locations` would also add Adzuna searches (each location × title is a
search, per matching country). `board_locations` is a second list for boards only:

- When set, it **replaces** `locations` when filtering board postings (it does
  not add to it, so repeat your cities in it). Adzuna never reads it.
- Its entries match **as whole words** (case-insensitive, taken literally), so a
  short code like `SA` matches `SA Western Area` and `Support Office SA` but not
  `San Francisco`, `USA` or `Mount Pleasant`. `locations` still matches by
  substring.
- A `remote` location still always passes.
- Empty or missing: boards use `locations` exactly as before.

```yaml
filters:
  locations:       ["Adelaide", "Sydney"]                       # Adzuna searches
  board_locations: ["Adelaide", "SA", "Bedford Park", "Sydney"] # board filter
```

### Regions: Adzuna searches AU + India, ATS is global

The search source is **Adzuna**. It is
per-country, so it *searches* both Australia (`au`) and India (`in`) — the
planner pairs each location with its country index via `country_of()`. ATS
boards (Greenhouse/Lever/Ashby) are different: they return a company's whole
global list, and `filters.locations` keeps the ones you want.

So **India** is covered two ways: Adzuna's `in` index searches Indian cities
directly, *and* India-hiring ATS boards come through the location filter. Use
**canonical city names** in `filters.locations` — `country_of()` recognizes both
spellings, but listing both (`"Bengaluru"` *and* `"Bangalore"`) just makes Adzuna
run the same city twice and burn double the API quota. Pick one:

```yaml
filters:
  titles: ["data analyst"]
  locations: ["Adelaide", "Bengaluru", "Mumbai", "Remote"]
sources:
  - adzuna au      # searches Australian locations
  - adzuna in      # searches Indian locations
  - greenhouse some-india-hiring-company
```

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
lines and `#` comments ignored (a whole line, or a comment after an entry; a `#`
must follow a space to start one). Lines say *where* only (no titles/locations):

```
adzuna au            # an Adzuna search, driven by filters
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
https://apply.workable.com/squiz           # → workable squiz
https://cba.wd3.myworkdayjobs.com/en-US/CommBank_Careers   # → workday cba wd3 CommBank_Careers
```

The scheme is optional (`boards.greenhouse.io/stripe` works), and a deep link
to a specific posting still resolves to the org token (the first path segment).
The one exception is a Workable posting link, `apply.workable.com/j/<code>`,
whose first segment is `j`, not an account: it is rejected with a message asking
for the board URL (`apply.workable.com/<account>`). A Workday URL is the one
host with three values: the tenant and data centre come from the host
(`<tenant>.<wdN>.myworkdayjobs.com`) and the site from the path, skipping a
leading locale such as `en-US`; a URL with no site is rejected.
An unrecognised host fails with a `ConfigError` listing the supported hosts.
Detection lives in `source_from_url()` (`src/config.py`).

A bad line fails with a `ConfigError` naming the line number. See
`sources.txt.example`.

## Field reference

| Key | Used by | Status |
|---|---|---|
| `filters.titles` / `filters.locations` | `src/ingestion/planner.py` (Adzuna queries) + `src/ingestion/filtering.py` (ATS post-filter) | Implemented. Empty = no filter. |
| `filters.board_locations` | `src/ingestion/filtering.py` (ATS post-filter only) | Implemented. Optional; replaces `locations` for boards, whole-word match. Adzuna ignores it. |
| `sources[].type: adzuna` (`country`) | expands to `titles × locations` Adzuna searches in that country | Implemented. Needs the top-level `adzuna:` block (`app_id`, `app_key`). |
| `sources[].type: greenhouse` (`board`) | `GreenhouseScraper`, then post-filtered | Implemented. |
| `sources[].type: lever` (`company`) | `LeverScraper`, then post-filtered | Implemented. |
| `sources[].type: ashby` (`org`) | `AshbyScraper`, then post-filtered | Implemented. `org` is the `jobs.ashbyhq.com/<org>` token. |
| `sources[].type: workable` (`account`) | `WorkableScraper`, then post-filtered | Implemented. `account` is the `apply.workable.com/<account>` token, case-sensitive. |
| `sources[].type: workday` (`tenant`, `datacenter`, `site`) | `WorkdayScraper`, then post-filtered | Implemented. All three come from the careers URL (`cba`, `wd3`, `CommBank_Careers`); one site per tenant. See [`workday.md`](./workday.md). |
| `sources_file` | `src/config.py` → `load_sources_file()` | Implemented. Appended to inline `sources`. |
| `adzuna.app_id` / `adzuna.app_key` | `src/ingestion/planner.py` → `AdzunaScraper` | Implemented. Free from developer.adzuna.com. |
| `llm.backend` / `llm.model` / `llm.host` | `src/llm/client.py` | Implemented. Only `ollama` valid in the LLM layer; config layer permits extra `llm.*` keys (backend undecided). |
| `resume_summary` | `src/llm/cli.py` → prompt templates | Implemented; a hand-written string, not parsed from a file. |
| `database.url` | `src/storage/database.py` | Wired. See below. |

At least one of `sources` or `sources_file` must be present, or validation
fails. `make scrape` also stops with a clear error if they add up to no active
sources (e.g. every line in `sources.txt` commented out). The old single-search
`search:` block was removed along with SEEK; a config that still has one fails
with a message saying to move it into `filters` + `sources`.

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
