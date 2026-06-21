# Job Hunter AI

A local-first job application pipeline: discover and scrape postings across SEEK, Adzuna, and ATS boards (Greenhouse, Lever, Ashby), store them in a database, look up a public contact for each posting, generate tailored cover letters and cold emails with a local LLM, and track application status — all from a Streamlit dashboard.

No cloud dependency required. Runs on SQLite + Ollama by default. SQLite is the product; Postgres is a documented escape hatch that would need a real test pass before you trusted it (the URL is configurable and the abstraction can speak Postgres, but that path is unverified and no driver is pinned — see `docs/configuration.md` and `TODO.md`). A hosted LLM is likewise an open, not-yet-built option.

> [!CAUTION]
> **Always run the dashboard bound to localhost.** Launch Streamlit as
> `streamlit run src/app/main.py --server.address localhost` (the `make app`
> target already does this for you). By default Streamlit binds to **all**
> network interfaces (`0.0.0.0`), which exposes your dashboard — and the job
> data in it — to anyone on your local network. Binding to `localhost` keeps it
> reachable only from your own machine. This is the current security baseline;
> further hardening is tracked in `TODO.md`.

## Why this exists

Manually tracking job applications across spreadsheets and tabs doesn't scale past a handful of roles. This project treats the job hunt as a small data pipeline: ingest postings from several sources, persist them with a defined schema, optionally look up a contact, generate application materials, and track status through a lifecycle (`To Apply` → `Applied` → `Interviewing` → `Rejected`).

## Architecture

```
job-hunter-ai/
├── .github/workflows/      # CI: lint + test on push
├── src/
│   ├── config.py            # Pydantic config: filters/sources/sources_file, source_from_url()
│   ├── logging_config.py    # One-call structured logging setup
│   ├── ingestion/           # Scraper module (Strategy Pattern)
│   │   ├── base_scraper.py  # Abstract base class — defines .scrape()
│   │   ├── seek.py          # SEEK public RSS search — DEAD, see "Scraping: scope and ethics" below
│   │   ├── greenhouse.py    # Greenhouse public board API (ATS)
│   │   ├── lever.py         # Lever public board API (ATS)
│   │   ├── ashby.py         # Ashby public board API (ATS)
│   │   ├── adzuna.py        # Adzuna search API — per-country, replaces dead SEEK
│   │   ├── planner.py       # Expands sources × filters → planned scrapes
│   │   ├── filtering.py     # Title/location filters applied to ATS results
│   │   ├── capture.py       # Dump unfiltered scrape output (debug/fixtures)
│   │   ├── validate.py      # `make validate` — check board tokens are still live
│   │   ├── discover.py      # `make discover` — propose new boards from existing postings
│   │   ├── http_util.py     # Shared GET-with-retries helper
│   │   └── cli.py           # `make scrape` — runs every planned scrape
│   ├── contacts/            # Hiring-contact lookup (public, in-posting text only)
│   │   ├── extract.py       # Pure function: JobPost -> ContactResult
│   │   └── cli.py           # `make contacts` — queue consumer
│   ├── storage/             # ORM + migrations
│   │   ├── models.py        # SQLModel schema (JobPost, incl. contact_* columns)
│   │   └── database.py      # Connection handling, CRUD, DB-URL resolution, additive SQLite column migration
│   ├── llm/                 # LLM abstraction layer
│   │   ├── client.py        # Wraps Ollama / Llama.cpp / OpenAI behind one interface
│   │   └── prompts.py       # Prompt templates for cover letters, cold emails
│   └── app/
│       └── main.py          # Streamlit entry point
├── data/                    # Local SQLite DB (gitignored)
├── tests/                   # PyTest suite
├── sources.txt              # WHERE to look — one board/search per line (see sources.txt.example)
├── sources.candidates.txt   # Unconfirmed boards to (re)check with `make validate`
├── run-pipeline.ps1         # Windows: runs discover → validate → scrape → contacts → process → app
├── requirements.txt
├── Makefile                 # make scrape / validate / discover / contacts / process / app
├── config.yaml              # filters (titles/locations), sources, LLM + DB settings
└── README.md
```

### Design decisions worth knowing about

**Strategy Pattern for scrapers.** `BaseScraper` is an `abc.ABC` with one required method, `.scrape()`. Each job board gets its own subclass and is a pure fetcher — no DB access, no awareness of filters. Five scrapers exist today (SEEK, Greenhouse, Lever, Ashby, Adzuna); adding another is a new file, a config model, and a branch in the planner.

**Filters are separate from sources.** `config.yaml` splits *what* you want (`filters.titles`, `filters.locations`) from *where* you look (`sources` / `sources_file`). SEEK and Adzuna are search engines, so each `(title, location)` pair becomes a query. ATS boards (Greenhouse, Lever, Ashby) return a company's whole list, so the same filters are applied to the results afterwards. `src/ingestion/planner.py` is what combines the two. See `docs/configuration.md`.

**Queue via the database, not asyncio.** Streamlit's threading model doesn't play well with background async workers updating UI state — you end up fighting race conditions for no real benefit at this scale. Instead, each stage writes rows with the next stage's column left `NULL`, and a separate CLI script selects exactly those rows, does its work, and writes back: `make scrape` leaves `generated_cover_letter`/`contact_confidence` null, `make contacts` fills `contact_*` (queue: `contact_confidence IS NULL`), `make process` fills the generated materials (queue: `generated_cover_letter IS NULL`). Streamlit only ever reads from the database, plus simple status-field edits. No in-flight state to lose, and any step can be re-run safely.

**LLM abstraction layer.** `llm/client.py` wraps whatever inference backend you're running (Ollama locally by default) behind one interface, so swapping to Llama.cpp or an OpenAI-compatible API is a config change, not a rewrite. Only `OllamaClient` exists today.

**Config-driven, not code-driven.** `config.yaml` (validated with Pydantic) holds what you're searching for, where you're searching, and model settings. You should never need to edit Python to change a search.

## Scraping: scope and ethics

LinkedIn and SEEK both prohibit automated scraping in their terms of service and have a track record of legal action against scrapers. This project does **not** include stealth/anti-detection scraping of authenticated pages.

In scope:
- Official APIs where a job board provides one (Greenhouse, Lever, Ashby)
- A sanctioned search API with a free tier (Adzuna)
- Public, non-authenticated feeds where available

Out of scope:
- Logging into LinkedIn/SEEK to scrape behind auth
- Bypassing bot detection or CAPTCHAs
- Data-broker / email-finder APIs for the contact lookup (see below)

**SEEK's public RSS feed is dead** (confirmed: it returns zero results for every query as of 2026-06-20). It's left in the codebase — `src/ingestion/seek.py` still works against the feed format, and a `seek` source is still a one-line re-enable in `sources.txt` if the feed ever comes back — but it is **not** a working source right now. Treat any documentation that describes SEEK as a live source as describing intent, not current behavior. **Adzuna** (`src/ingestion/adzuna.py`) replaces it as the search/aggregator source: it's a sanctioned API with a free tier, reaches India as well as Australia, and just needs free credentials from developer.adzuna.com in `config.yaml`'s `adzuna:` block. See `docs/configuration.md` and `docs/scrapers.md`.

The **contact lookup** (`make contacts`, below) follows the same ethic: it reads only text a company already published in its own posting, makes no network calls, and never queries a data broker (Hunter, Apollo, RocketReach, ZoomInfo, etc.) or a logged-in source. A guessed address is always flagged as a guess, never asserted as verified. See `docs/hiring-manager-lookup.md`.

If you fork this and add a scraper or contact source that requires login or a broker API, that's your decision and your risk — document it clearly in your own README rather than relying on this one.

## Data model

```python
class JobPost(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_board_id: str = Field(unique=True)   # e.g. "greenhouse-4029412abc"
    title: str
    company: str
    location: str
    description: str
    url: str
    date_scraped: datetime = Field(default_factory=datetime.utcnow)
    status: str = Field(default="To Apply")  # To Apply, Applied, Interviewing, Rejected
    generated_cover_letter: Optional[str] = None
    generated_cold_email: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_confidence: Optional[str] = None  # "published" | "pattern-guess" | "none"
```

`job_board_id` is unique so re-running a scrape doesn't duplicate postings. The three `contact_*` columns are additive — `src/storage/database.py` adds them to an existing SQLite file on first run after upgrading, no manual migration needed. Full field-by-field notes: `docs/data-model.md`.

## Getting started

### Prerequisites
- Python 3.11+
- [Ollama](https://ollama.com) installed locally, with a model pulled (e.g. `ollama pull llama3.2:3b-instruct-q4_K_M`)
- (Optional, recommended) free Adzuna API credentials from [developer.adzuna.com](https://developer.adzuna.com) — SEEK is dead, so Adzuna is the working search source

### Setup

```bash
git clone <your-repo-url>
cd job-hunter-ai
make setup                          # one-time: creates venv + installs dependencies
source venv/bin/activate            # Windows: venv\Scripts\activate
cp config.yaml.example config.yaml  # then edit filters (titles/locations), llm.model, resume_summary
cp sources.txt.example sources.txt  # then edit which boards to scrape
```

`make setup` is the one-time installer — it builds the virtualenv and installs everything into it. It can't activate the venv for your shell (no child process can), so activate it yourself afterwards before running the steps below.

### Usage

```bash
make scrape    # populate data/jobs.db with new postings (sources × filters)
make validate  # check your configured boards are still live (tokens go stale)
make discover  # propose new boards from companies already in your results
make contacts  # (optional) find a contact for each posting from its own text
make process   # generate cover letters / cold emails for pending rows
make app       # launch the Streamlit dashboard (bound to localhost)
```

`validate` and `discover` are maintenance/growth steps, not required every run — see `docs/board-discovery.md` for how they fit together (curate candidates → validate → scrape; mine existing results → discover → review → validate). `contacts` is optional but should run before `process` if you want the cold email addressed to someone. On Windows, `run-pipeline.ps1` runs all six steps in order in one go (with `-SkipApp` to stop before the dashboard).

> [!CAUTION]
> Run the dashboard bound to localhost: `streamlit run src/app/main.py --server.address localhost` (`make app` already does this). See the security caution near the top of this README for why.

### Windows (no `make`)

`make` isn't installed on Windows by default, so the `make ...` shortcuts above won't work in PowerShell or cmd. That's expected — those targets are just thin wrappers around `python -m ...`. Either use `run-pipeline.ps1` (runs the whole pipeline for you), or create a virtual environment and call its interpreter directly, with no activation step needed.

One-time setup (this example names the venv `job-hunter`):

```powershell
python -m venv job-hunter
job-hunter\Scripts\python.exe -m pip install --upgrade pip
job-hunter\Scripts\python.exe -m pip install -r requirements.txt
copy config.yaml.example config.yaml   # then edit filters, llm.model, resume_summary
copy sources.txt.example sources.txt   # then edit which boards to scrape
```

Then run each step by pointing at the venv's interpreter (or just run `.\run-pipeline.ps1`):

```powershell
job-hunter\Scripts\python.exe -m src.ingestion.cli       # = make scrape
job-hunter\Scripts\python.exe -m src.ingestion.validate  # = make validate
job-hunter\Scripts\python.exe -m src.ingestion.discover  # = make discover
job-hunter\Scripts\python.exe -m src.contacts.cli        # = make contacts
job-hunter\Scripts\python.exe -m src.llm.cli             # = make process
job-hunter\Scripts\streamlit.exe run src\app\main.py --server.address localhost   # = make app
job-hunter\Scripts\python.exe -m pytest tests\           # = make test
```

Calling `job-hunter\Scripts\python.exe` directly is equivalent to activating the venv first — activation only adds that folder to your PATH. (If you'd rather activate: `job-hunter\Scripts\Activate.ps1`.)

### Running tests

```bash
pytest tests/
```

## Roadmap

This is being built incrementally. Rough sequence:

1. `JobPost` schema + SQLite storage ✅
2. One working scraper (SEEK public feed) implementing `BaseScraper` — ✅ built, but the feed itself has since gone dead (see "Scraping: scope and ethics")
3. LLM client wrapper + cover letter / cold email prompt templates ✅
4. `make process` queue consumer ✅
5. Streamlit dashboard (read-only view + status updates) ✅
6. Second scraper (Greenhouse public board API) to prove the Strategy Pattern decouples cleanly ✅
7. CI workflow (lint + pytest on push) ✅
8. Pydantic-validated config + multi-source/multi-search support ✅
9. `database.url` wired through (SQLite default, Postgres via config or `JOBHUNTER_DATABASE_URL`) ✅
10. `filters` / `sources` split + `sources_file` (plain-text board list, careers-URL auto-detection) ✅
11. Lever and Ashby scrapers — third and fourth ATS sources ✅
12. Adzuna scraper — sanctioned search API replacing the dead SEEK feed, reaches AU + India ✅
13. Board validation (`make validate`) and discovery (`make discover`) — keep the board list live and growing without a noise-adding firehose ✅
14. Hiring-contact lookup v1 (`make contacts`) — public, in-posting-text only; shown with a confidence flag in the dashboard ✅
15. `capture`/`JOBHUNTER_DUMP_DIR` debug dumping of unfiltered scrape output ✅

A hosted-LLM config and resume parsing are still deferred — the LLM layer is intentionally left at one backend (Ollama) for now. A Workday scraper is designed but not built (`docs/workday.md`) — it's a multi-call, POST-based ATS unlike the others, and needs a live-board spike before it's trusted. See `docs/roadmap.md` for the detailed version and `TODO.md` for the working tracker.

## Documentation

Deeper, module-by-module docs live in [`docs/`](./docs/README.md): architecture, the full data model, a worked-example walkthrough of every scraper, the LLM-provider contract, the complete config reference, board discovery/validation, the hiring-contact-lookup design, a Workday scraper design doc, and manual per-ATS submission notes. Kept updated alongside the code, with `(TODO)` markers on anything not yet decided — though if something here ever looks out of sync with `src/`, trust the code and open an issue (or just fix the doc).

## License

MIT (or your choice — update before publishing).
