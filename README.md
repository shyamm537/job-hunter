# Job Hunter AI

A local-first job application pipeline: scrape postings, store them in a database, generate tailored cover letters and cold emails with a local LLM, and track application status — all from a Streamlit dashboard.

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

Manually tracking job applications across spreadsheets and tabs doesn't scale past a handful of roles. This project treats the job hunt as a small data pipeline: ingest postings, persist them with a defined schema, generate application materials, and track status through a lifecycle (`To Apply` → `Applied` → `Interviewing` → `Rejected`).

## Architecture

```
job-hunter-ai/
├── .github/workflows/      # CI: lint + test on push
├── src/
│   ├── config.py            # Pydantic-validated config.yaml loader
│   ├── logging_config.py    # One-call structured logging setup
│   ├── ingestion/           # Scraper module (Strategy Pattern)
│   │   ├── base_scraper.py  # Abstract base class — defines .scrape()
│   │   ├── seek.py          # SEEK public RSS feed
│   │   ├── greenhouse.py    # Greenhouse public board API
│   │   ├── factory.py       # Maps a config source → scraper subclass
│   │   ├── http_util.py     # Shared GET-with-retries helper
│   │   └── cli.py           # `make scrape` — runs every configured source
│   ├── storage/             # ORM + migrations
│   │   ├── models.py        # SQLModel schema (JobPost, etc.)
│   │   └── database.py      # Connection handling, CRUD, DB-URL resolution
│   ├── llm/                 # LLM abstraction layer
│   │   ├── client.py        # Wraps Ollama / Llama.cpp / OpenAI behind one interface
│   │   └── prompts.py       # Prompt templates for cover letters, cold emails
│   └── app/
│       └── main.py          # Streamlit entry point
├── data/                    # Local SQLite DB, raw resume storage (gitignored)
├── tests/                   # PyTest suite
├── requirements.txt
├── Makefile                 # make scrape / make process / make app
├── config.yaml              # Search terms, locations, LLM settings
└── README.md
```

### Design decisions worth knowing about

**Strategy Pattern for scrapers.** `BaseScraper` is an `abc.ABC` with one required method, `.scrape()`. Each job board gets its own subclass. When a site changes its HTML, you fix one file, not the whole pipeline.

**Queue via the database, not asyncio.** Streamlit's threading model doesn't play well with background async workers updating UI state — you end up fighting race conditions for no real benefit at this scale. Instead, the scraper writes rows with no generated materials yet (`generated_cover_letter` is `None`); a separate CLI script (`make process`) selects exactly those rows via `pending_llm_jobs()`, generates cover letters/emails, and writes them back; Streamlit only ever reads from the database. The null column *is* the queue — `status` ("To Apply", "Applied", …) is independent and tracks where you are in the application, not whether materials have been generated. Simpler, no race conditions, and closer to how real lightweight pipelines are built.

**LLM abstraction layer.** `llm/client.py` wraps whatever inference backend you're running (Ollama locally by default) behind one interface, so swapping to Llama.cpp or an OpenAI-compatible API is a config change, not a rewrite.

**Config-driven, not code-driven.** `config.yaml` (validated with Pydantic) holds job titles, target locations, and model settings. You should never need to edit Python to change what you're searching for.

## Scraping: scope and ethics

LinkedIn and SEEK both prohibit automated scraping in their terms of service and have a track record of legal action against scrapers. This project does **not** include stealth/anti-detection scraping of authenticated pages.

In scope:
- Public, non-authenticated pages and official feeds where available (e.g., SEEK's public RSS feeds)
- Official APIs where a job board provides one

Out of scope:
- Logging into LinkedIn/SEEK to scrape behind auth
- Bypassing bot detection or CAPTCHAs

If you fork this and add a scraper for a site that requires login, that's your decision and your risk — document it clearly in your own README rather than relying on this one.

## Data model

```python
class JobPost(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_board_id: str = Field(unique=True)   # e.g. "seek-4029412"
    title: str
    company: str
    location: str
    description: str
    url: str
    date_scraped: datetime = Field(default_factory=datetime.utcnow)
    status: str = Field(default="To Apply")  # To Apply, Applied, Interviewing, Rejected
    generated_cover_letter: Optional[str] = None
    generated_cold_email: Optional[str] = None
```

`job_board_id` is unique so re-running a scrape doesn't duplicate postings.

## Getting started

### Prerequisites
- Python 3.11+
- [Ollama](https://ollama.com) installed locally, with a model pulled (e.g. `ollama pull llama3.2:3b-instruct-q4_K_M`)

### Setup

```bash
git clone <your-repo-url>
cd job-hunter-ai
make setup                          # one-time: creates venv + installs dependencies
source venv/bin/activate            # Windows: venv\Scripts\activate
cp config.yaml.example config.yaml  # then edit search terms, llm.model, resume_summary
```

`make setup` is the one-time installer — it builds the virtualenv and installs everything into it. It can't activate the venv for your shell (no child process can), so activate it yourself afterwards before running the `make scrape` / `make process` / `make app` steps.

### Usage

```bash
make scrape    # populate data/jobs.db with new postings
make process   # generate cover letters / cold emails for pending rows
make app       # launch the Streamlit dashboard (bound to localhost)
```

> [!CAUTION]
> Run the dashboard bound to localhost: `streamlit run src/app/main.py --server.address localhost` (`make app` already does this). See the security caution near the top of this README for why.

### Windows (no `make`)

`make` isn't installed on Windows by default, so the `make ...` shortcuts above won't work in PowerShell or cmd. That's expected — those targets are just thin wrappers around `python -m ...`. The reliable approach on Windows is to create a virtual environment and call its interpreter directly, with no activation step needed.

One-time setup (this example names the venv `job-hunter`):

```powershell
python -m venv job-hunter
job-hunter\Scripts\python.exe -m pip install --upgrade pip
job-hunter\Scripts\python.exe -m pip install -r requirements.txt
copy config.yaml.example config.yaml   # then edit search terms, llm.model, resume_summary
```

Then run each step by pointing at the venv's interpreter:

```powershell
job-hunter\Scripts\python.exe -m src.ingestion.cli      # = make scrape
job-hunter\Scripts\python.exe -m src.llm.cli            # = make process
job-hunter\Scripts\streamlit.exe run src\app\main.py --server.address localhost   # = make app
job-hunter\Scripts\python.exe -m pytest tests\          # = make test
```

Calling `job-hunter\Scripts\python.exe` directly is equivalent to activating the venv first — activation only adds that folder to your PATH. (If you'd rather activate: `job-hunter\Scripts\Activate.ps1`.)

### Running tests

```bash
pytest tests/
```

## Roadmap

This is being built incrementally. Rough sequence:

1. `JobPost` schema + SQLite storage ✅
2. One working scraper (SEEK public feed) implementing `BaseScraper` ✅
3. LLM client wrapper + cover letter / cold email prompt templates ✅
4. `make process` queue consumer ✅
5. Streamlit dashboard (read-only view + status updates) ✅
6. Second scraper (Greenhouse public board API) to prove the Strategy Pattern decouples cleanly ✅
7. CI workflow (lint + pytest on push) ✅
8. Pydantic-validated config + multi-source/multi-search support ✅
9. `database.url` wired through (SQLite default, Postgres via config or `JOBHUNTER_DATABASE_URL`) ✅

A hosted-LLM config and resume parsing are still deferred — the LLM layer is intentionally left at one backend (Ollama) for now. See `docs/roadmap.md` for the detailed version.

## Documentation

Deeper, module-by-module docs live in [`docs/`](./docs/README.md) — architecture, data model, scraper/LLM-provider extension guides, full config reference, and a more detailed roadmap. Kept updated alongside the code, with `(TODO)` markers on anything not yet decided.

## License

MIT (or your choice — update before publishing).
