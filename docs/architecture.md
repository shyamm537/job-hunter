# Architecture

> Status: reflects what's actually implemented as of the initial skeleton (storage + one scraper + LLM wrapper + dashboard). Sections marked **(TODO)** describe intent, not working code yet.

## Layers

```
ingestion  →  storage  →  llm  →  app
(scrapers)    (SQLite)    (Ollama)  (Flask)
```

Each layer only talks to the one next to it. `src/app/` never imports a scraper; `ingestion/` never imports the web app. The database is the seam between all of them — every layer reads/writes `JobPost` rows and nothing else.

## The three design decisions

### 1. Strategy Pattern for scrapers

`src/ingestion/base_scraper.py` defines `BaseScraper`, an `abc.ABC` with one abstract method: `scrape() -> List[JobPost]`. There are four concrete implementations: `src/ingestion/adzuna.py` (Adzuna search API — the search source) and three ATS board readers, `greenhouse.py`, `lever.py`, and `ashby.py` (public board JSON APIs).

The contract is deliberately thin — a scraper takes whatever constructor args it needs and returns a list of `JobPost` objects. It's a pure fetcher: it doesn't talk to the database, and it doesn't know about filters. `src/ingestion/planner.py` (`plan_scrapes`) is the one place that turns validated `sources` + `filters` into concrete scrapers, so the CLI loops over `PlannedScrape` items without knowing their concrete types. (`src/ingestion/factory.py` is now a thin deprecation shim re-exporting the planner.)

What you want (titles, locations) is kept separate from where you look (sources), because the two source kinds use the intent differently: Adzuna is a search engine, so each `(title, location)` pair becomes one search; an ATS board returns a company's whole list, so the planner marks it for post-filtering by `src/ingestion/filtering.py`.

Why this matters in practice: if Adzuna changes its API, only `adzuna.py` changes. The abstraction has held through very different transports — the original SEEK scraper read an RSS feed (removed in KAN-32 after the feed died), the others read JSON APIs of different shapes — yet `BaseScraper`, the CLI, and storage didn't change shape to absorb them. Adding a source (see [`docs/scrapers.md`](./scrapers.md)) is a new file, a config model, and one branch in the planner.

### 2. Database-backed queue instead of asyncio

The original plan considered an asyncio worker so the LLM wouldn't block the dashboard (then Streamlit). That was dropped: background workers pushing updates into a UI mean race conditions and shared state, and the complexity wasn't worth it for what this pipeline needs.

Instead:

- `make scrape` (`src/ingestion/cli.py`) writes `JobPost` rows. New rows have `generated_cover_letter = None` by construction.
- `make check-links` (`src/ingestion/check_links.py`) picks rows no scrape has seen lately and marks the ones whose link returns 404/410 dead.
- `make process` (`src/llm/cli.py`) calls `pending_llm_jobs()` (`src/storage/database.py`) to find rows where `generated_cover_letter IS NULL`, generates materials, writes them back.
- `make app` (`src/app/`) only ever reads, plus writes status updates (`To Apply` → `Applied` → ...) and jobs you add by hand.

No process talks to another process directly. The database is the queue. This is slower than an in-memory queue but there is no in-flight state to lose, and any step can be re-run safely.

### 3. LLM abstraction layer

`src/llm/client.py` defines `LLMClient` (ABC, one method: `generate(prompt: str) -> str`) and `OllamaClient`, the only implementation. `get_llm_client(config)` is a factory that picks a backend based on `config.yaml`'s `llm.backend` key.

Today `backend: ollama` is the only valid value — anything else raises `ValueError` with a message pointing at what to do about it. Adding a second backend means adding a subclass and one `elif` branch in the factory; nothing in `cli.py` or `prompts.py` needs to change. See [`docs/llm-providers.md`](./llm-providers.md).

## Data flow, end to end

![Architecture Flowchart](image.png)

1. `make scrape` → `plan_scrapes(sources, filters)` expands Adzuna sources into one search per `(title, location)` and marks ATS boards for post-filtering → each planned scrape runs, ATS results are filtered by `job_matches()` → `upsert_job()` dedupes against `job_board_id` and inserts new rows. One planned scrape failing is logged and skipped, not fatal. After each successful board scrape, `reconcile_board()` marks the board's stored rows that it no longer lists as dead (`"gone from board"`) and the rest as seen. At the end, `archive_dead_jobs()` (`src/storage/archive.py`) moves dead `To Apply`/`Rejected` rows to `data/dead_jobs.db`.
2. `make check-links` (optional) → `link_check_queue()` picks live rows not checked or seen within `--recheck-days` → one GET each → 404/410 marks the row dead → dead rows are archived the same way.
3. `make process` → `pending_llm_jobs()` finds rows with no cover letter → `OllamaClient.generate()` is called twice per job (cover letter, cold email) using templates from `src/llm/prompts.py` → results written back to the same row.
4. `make app` → a small Flask app on `127.0.0.1:8000` (`src/app/`): the jobs list (`list_job_summaries()`, filters in the URL) with an inline status dropdown, a page per job (`get_job()`), and "Add a job" (`add_manual_job()`).

## What's deliberately not built yet

- A second LLM backend (the LLM layer is held at one backend on purpose; the backend choice itself is still open)
- Retry/backoff around **LLM** calls — scrapers now have a small retry helper (`src/ingestion/http_util.py`), but `OllamaClient` does not
- Resume parsing — `resume_summary` in `config.yaml` is a hand-written string, not extracted from a file (left alone as part of the LLM/prompt path)
- A live-Postgres test run — `database.url` is wired and the engine is built from it, but the suite has only been exercised against SQLite (see [`docs/roadmap.md`](./roadmap.md))

## Storage backend: SQLite vs. Postgres

SQLite is the product; Postgres is a documented escape hatch that would need a real test pass before you trusted it. The storage layer goes through SQLModel/SQLAlchemy, so the database URL isn't hardcoded and *can* point at Postgres — but that's optionality, not a validated feature. The Postgres path has never been run here and no non-SQLite driver (e.g. `psycopg`) is pinned. For a single-user job tracker, SQLite has ample headroom; Postgres earns its keep only with concurrent writers or a shared/hosted deployment, none of which is on the table today. See `TODO.md` for what a real Postgres pass would require.

## (TODO) Deployment

Nothing here yet. The project currently assumes local SQLite + local Ollama. A Docker Compose setup for Postgres + a containerized app is mentioned as a stretch goal but has no implementation — and would depend on the Postgres verification above.
