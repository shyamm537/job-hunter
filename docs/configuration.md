# Configuration

All user-facing configuration lives in `config.yaml`, which you create by copying `config.yaml.example`. `config.yaml` is gitignored — it's meant to hold your search criteria and resume summary, not something you commit.

As of the config-validation work, `config.yaml` is loaded and validated by `src/config.py` (Pydantic). A missing file or a bad field fails with a readable `ConfigError` naming the problem, not a bare `KeyError` deep in a CLI.

## Declaring what to scrape

Two mutually exclusive ways:

**Legacy single search** — one SEEK search, kept for backward compatibility:

```yaml
search:
  title: "data analyst"
  location: "Adelaide"
```

**Preferred: a `sources` list** — mix SEEK searches and Greenhouse boards, run as many as you like in one `make scrape`:

```yaml
sources:
  - type: seek
    title: "data analyst"
    location: "Sydney"
  - type: greenhouse
    board: "stripe"            # board token from boards.greenhouse.io/stripe
    title_contains: "data"     # optional case-insensitive title filter
```

If both are present, `sources` wins. If neither is present, validation fails — you have to declare at least one.

## Field reference

| Key | Used by | Status |
|---|---|---|
| `search.title` / `search.location` | `src/config.py` → derived into a single SEEK source | Implemented. `location` defaults to `"All Australia"`. |
| `sources[]` | `src/ingestion/factory.py` → scrapers | Implemented. Discriminated on `type` (`seek` \| `greenhouse`). Unknown types fail validation. |
| `sources[].type: seek` (`title`, `location`) | `SeekScraper` | Implemented. |
| `sources[].type: greenhouse` (`board`, `title_contains?`) | `GreenhouseScraper` | Implemented. `title_contains` is an optional client-side filter. |
| `llm.backend` | `src/llm/client.py` → `get_llm_client()` | Implemented. Only `ollama` is valid in the LLM layer; anything else raises `ValueError` there. The config layer is permissive about `llm.*` (extra keys allowed) since the backend is undecided. |
| `llm.model` / `llm.host` | `OllamaClient` | Implemented. `host` defaults to `http://localhost:11434`. |
| `resume_summary` | `src/llm/cli.py` → prompt templates | Implemented, but it's a hand-written string, not parsed from an actual resume file. |
| `database.url` | `src/storage/database.py` | **Now wired.** Resolved as: explicit override → `JOBHUNTER_DATABASE_URL` env var → `database.url` in config → `sqlite:///data/jobs.db` default. |

## Database URL resolution

`src/storage/database.py` builds its engine lazily from the first of these that's set:

1. An explicit `set_database_url(...)` call — `make scrape` does this right after loading config.
2. The `JOBHUNTER_DATABASE_URL` environment variable.
3. `database.url` from `config.yaml`.
4. The SQLite default, `sqlite:///data/jobs.db`.

Because 2–4 are automatic, the Streamlit app and the LLM worker — which don't set the URL themselves — still pick up a Postgres URL from config or the environment. Pointing the whole pipeline at Postgres is a config change. (Running the suite against a live Postgres, and pinning a driver like `psycopg`, is still on the roadmap.)

## Validation

`src/config.py` defines a `Config` Pydantic model loaded once via `load_config()` and passed around as a typed object instead of a raw `dict`. The model rejects unknown top-level keys (`extra="forbid"`) but is permissive inside `llm` (`extra="allow"`), deliberately, so the undecided LLM backend can carry whatever fields it eventually needs without a schema change here.

## Multiple search profiles

Supported via the `sources` list above — e.g. "data analyst in Adelaide" and "data analyst in Sydney" as two `seek` entries in one config, scraped in a single `make scrape` run. No need to edit and re-run per search anymore.
