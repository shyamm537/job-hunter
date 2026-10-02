# Roadmap

Mirrors the sequence in the root README, with more detail on what "done" means per step and open questions that aren't resolved yet.

## Done

1. **`JobPost` schema + SQLite storage** — `src/storage/models.py`, `src/storage/database.py`. Tested (`tests/test_models.py`).
2. **One working scraper (SEEK public feed)** — `src/ingestion/seek.py`, implementing `BaseScraper`. Tested with mocked feed data (`tests/test_seek_scraper.py`).
3. **LLM client wrapper + prompt templates** — `src/llm/client.py`, `src/llm/prompts.py`. Not yet tested against a real Ollama instance in CI (no Ollama available there).
4. **`make process` queue consumer** — `src/llm/cli.py`. Same caveat: logic is sound, untested against a live model.
5. **Streamlit dashboard** — `src/app/main.py`. Read-only view plus status updates. Not tested (no UI test harness set up).
6. **Second scraper (Greenhouse), proving the Strategy Pattern decouples cleanly.** `src/ingestion/greenhouse.py` reads the public Greenhouse boards JSON API (`boards-api.greenhouse.io/v1/boards/<board>/jobs`) — a completely different shape from SEEK's RSS, yet `BaseScraper` and the CLI didn't change to accommodate it. That's the actual proof the abstraction holds. Tested with mocked API data (`tests/test_greenhouse_scraper.py`). LinkedIn stays out of scope (behind-auth — see [`docs/scrapers.md`](./scrapers.md)).
7. **Pydantic config validation + multi-source.** `src/config.py` validates `config.yaml`, supports a `sources:` list mixing SEEK and Greenhouse (or a legacy single `search:` block), and fails with a readable `ConfigError` instead of a bare `KeyError`. `src/ingestion/factory.py` maps a source to its scraper. Tested (`tests/test_config.py`, `tests/test_factory.py`).
8. **`database.url` wired.** `src/storage/database.py` now resolves the URL from an explicit override → `JOBHUNTER_DATABASE_URL` env var → `config.yaml` → SQLite default, lazily. Postgres is now a config change. The untouched LLM worker and Streamlit app pick this up automatically.

## Not started

- **CI against a live model.** `.github/workflows/ci.yml` runs lint + test on push/PR, but doesn't run against a real Ollama instance, so the LLM layer still has zero CI coverage. Whether that's worth fixing (e.g., a CI step that pulls a tiny model) or just accepted as a manual-testing-only layer is undecided — and waits on the LLM backend decision.

## Deferred (explicitly out of scope for now)

- **PostgreSQL support.** The plumbing is in — `database.url` is read and the engine is built from it. What hasn't been done/verified is actually running the suite against a live Postgres (the `psycopg` driver isn't pinned in `requirements.txt`, and SQLite-specific behaviour hasn't been audited).
- **Additional scrapers** beyond SEEK and Greenhouse.
- **A hosted-LLM config** (OpenAI-compatible API, etc.) — see [`docs/llm-providers.md`](./llm-providers.md). The LLM layer is deliberately left at one backend; the backend choice itself is still open.
- **Resume parsing.** `resume_summary` is a hand-typed string in `config.yaml`, not extracted from an uploaded resume file. Left alone for now as it's part of the LLM/prompt path.

## Open questions worth resolving before the project grows much further

- Does generated-material history matter, or is overwrite-in-place (current behavior) fine? Affects whether [`docs/data-model.md`](./data-model.md)'s proposed `GeneratedMaterial` table is worth building.
- Multi-search-profile support (currently one `search` block per `config.yaml`) — needed once someone's targeting more than one role/location simultaneously.
- Structured logging vs. the current `print()`-based CLI output — fine for one user, not fine if this is ever run unattended or by someone else.
