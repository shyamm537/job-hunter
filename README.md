# Job Hunter AI

A local-first job application pipeline: discover and scrape postings across Adzuna and ATS boards (Greenhouse, Lever, Ashby, Workable, Workday), store them in a database, look up a public contact for each posting, generate tailored cover letters and cold emails with a local LLM, and track application status — all from a small local web dashboard.

No cloud dependency required. Runs on SQLite + Ollama by default. SQLite is the product; Postgres is a documented escape hatch that would need a real test pass before you trusted it (the URL is configurable and the abstraction can speak Postgres, but that path is unverified and no driver is pinned — see [`docs/configuration.md`](docs/configuration.md) and `TODO.md`). A hosted LLM is likewise an open, not-yet-built option.

> [!NOTE]
> **The dashboard is local-only.** `make app` (`python -m src.app`) serves it on
> `http://127.0.0.1:8000`, never on your network. It also refuses requests whose
> `Host` isn't `localhost`/`127.0.0.1` (blocking DNS-rebinding) and form posts
> from other origins, so a web page you visit can't change your data. There's
> no login; further hardening is tracked in `TODO.md`.

## About this project

**The main aim of this project is to learn how to build something using an AI tool.** It is a learning project first and a job-hunting tool second: a real, end-to-end problem to build against with an AI coding assistant (Claude Code), so the habits get practised on something that has to actually work. The job hunt is the excuse; the working method is the point.

What that has meant in practice:

- **Plan first, then build.** Each new source (Workable, Workday, and two that were dropped) started as a written plan, was tested against the live service with a small probe before any code was written, and only then built. The plans were reviewed more than once, and the reviews caught real bugs (an id rule that would have collapsed every posting on a board into one, a location filter that would have dropped every Adelaide role) before they reached `main`.
- **One change, one ticket, one pull request.** Every change has a Jira key (project `KAN`), a branch named after it, small commits prefixed with the key, and a pull request that CI (lint and tests) has to pass before it is merged by a person. The AI proposes and writes; a person decides and merges.
- **Check the work, don't trust it.** New code is tested offline and then run against the real service in a scratch database before the PR is opened. Claims in the plans are marked as seen live, documented, or from memory. Where something could not be verified it says so in the docs (for example `docs/workday.md`).
- **Rules the AI has to follow.** Scope limits (see below), no emojis in PRs or comments, and a source whose `robots.txt` disallows scraping is dropped rather than worked around.

If you are reading this to learn the same thing, `docs/` and the pull-request history are the best record of how the work went.

## Why this exists

Manually tracking job applications across spreadsheets and tabs doesn't scale past a handful of roles. This project treats the job hunt as a small data pipeline: ingest postings from several sources, persist them with a defined schema, optionally look up a contact, generate application materials, and track status through a lifecycle (`To Apply` → `Applied` → `Interviewing` → `Rejected`).

## Status

As of 2026-10-04. Progress is tracked in the Jira project `KAN`.

- **Works:** scraping six sources (Adzuna plus Greenhouse, Lever, Ashby, Workable and Workday boards), keeping postings current as they close, a local dashboard for triaging and tracking applications, and cover letters and cold emails from a local LLM.
- **Not yet:** better cover letters, scheduled runs and a hosted-LLM option. The filters can now cut noise from results (extra and excluded titles, remote regions), but the lists have to be set in your `config.yaml`. No Workable or Workday employers are configured yet.
- **Dropped:** SmartRecruiters and some PageUp employers, because their sites don't allow scraping.

## Architecture

```
job-hunter-ai/
├── .github/workflows/      # CI: lint + test on push
├── src/
│   ├── config.py            # Pydantic config: filters/sources/sources_file, source_from_url()
│   ├── logging_config.py    # One-call structured logging setup
│   ├── ingestion/           # Scraper module (Strategy Pattern)
│   │   ├── base_scraper.py  # Abstract base class — defines .scrape()
│   │   ├── greenhouse.py    # Greenhouse public board API (ATS)
│   │   ├── lever.py         # Lever public board API (ATS)
│   │   ├── ashby.py         # Ashby public board API (ATS)
│   │   ├── workable.py      # Workable public widget API (ATS)
│   │   ├── workday.py       # Workday careers API: POST list + detail per title match (ATS)
│   │   ├── text_util.py     # html_to_text() for HTML job descriptions
│   │   ├── adzuna.py        # Adzuna search API — per-country (AU, India, ...)
│   │   ├── planner.py       # Expands sources × filters → planned scrapes
│   │   ├── filtering.py     # Title/location filters applied to ATS results
│   │   ├── capture.py       # Dump unfiltered scrape output (debug/fixtures)
│   │   ├── validate.py      # `make validate` — check board tokens are still live
│   │   ├── discover.py      # `make discover` — propose new boards from existing postings
│   │   ├── resolve.py       # `make resolve` — check companies.txt against every board, write the board map
│   │   ├── board_map.py     # the board map (data/board_map.yaml): load, save, merge
│   │   ├── check_links.py   # `make check-links` — mark postings whose link is gone (404/410) dead
│   │   ├── http_util.py     # Shared GET/POST-with-retries helpers, REQUEST_DELAY
│   │   └── cli.py           # `make scrape` — runs every planned scrape
│   ├── contacts/            # Hiring-contact lookup (public, in-posting text only)
│   │   ├── extract.py       # Pure function: JobPost -> ContactResult
│   │   └── cli.py           # `make contacts` — queue consumer
│   ├── storage/             # ORM + migrations
│   │   ├── models.py        # SQLModel schema (JobPost, incl. contact_* and dead-posting columns)
│   │   ├── database.py      # Connection handling, CRUD, DB-URL resolution, additive SQLite column migration
│   │   ├── archive.py       # Dead postings move to data/dead_jobs.db (kept for analysis)
│   │   └── adzuna_rekey.py  # One-off re-key of legacy Adzuna rows (`python -m src.storage.adzuna_rekey`)
│   ├── llm/                 # LLM abstraction layer
│   │   ├── client.py        # Wraps Ollama / Llama.cpp / OpenAI behind one interface
│   │   └── prompts.py       # Prompt templates for cover letters, cold emails
│   └── app/                 # The dashboard: a small Flask app (`make app`)
│       ├── __init__.py      # create_app(): config + database
│       ├── __main__.py      # `python -m src.app` — serves on 127.0.0.1:8000
│       ├── web.py           # Routes: jobs list, job page, status changes, "Add a job"
│       ├── templates/       # Jinja templates
│       └── static/          # style.css
├── data/                    # Local SQLite DBs: jobs.db + dead_jobs.db archive (gitignored)
├── tests/                   # PyTest suite
├── sources.txt              # WHERE to look — one board/search per line (see sources.txt.example)
├── companies.txt            # Optional: employers to follow; `make resolve` finds their boards (see companies.txt.example)
├── sources.candidates.txt   # Unconfirmed boards to (re)check with `make validate`
├── run-pipeline.ps1         # Windows: runs discover → validate → scrape → check-links → contacts → process → app
├── requirements.txt
├── Makefile                 # make scrape / check-links / validate / resolve / discover / contacts / process / app
├── config.yaml              # filters (titles/locations), sources, LLM + DB settings
└── README.md
```

### Design decisions worth knowing about

**Strategy Pattern for scrapers.** `BaseScraper` is an `abc.ABC` with one required method, `.scrape()`. Each job board gets its own subclass and is a pure fetcher — no DB access, no awareness of filters. Six scrapers exist today (Adzuna, Greenhouse, Lever, Ashby, Workable, Workday); adding another is a new file, a config model, and a branch in the planner.

**Filters are separate from sources.** `config.yaml` splits *what* you want (`filters.titles`, `filters.locations`) from *where* you look (`sources` / `sources_file`). Adzuna is a search engine, so each `(title, location)` pair becomes a query. ATS boards (Greenhouse, Lever, Ashby, Workable, Workday) return a company's whole list, so the same filters are applied to the results afterwards (an optional `filters.board_locations` list can replace `locations` for boards only, for boards that use their own place names). Every source's results are also checked by title, since Adzuna's search is fuzzy; optional `also_match_titles`, `exclude_titles` and `remote_regions` lists cut the rest of the noise, and `python -m src.storage.noise_cleanup` tidies jobs already stored. `src/ingestion/planner.py` is what combines the two. See `docs/configuration.md`.

**Queue via the database, not asyncio.** Background async workers pushing updates into a UI mean fighting race conditions for no real benefit at this scale. Instead, each stage writes rows with the next stage's column left `NULL`, and a separate CLI script selects exactly those rows, does its work, and writes back: `make scrape` leaves `generated_cover_letter`/`contact_confidence` null, `make contacts` fills `contact_*` (queue: `contact_confidence IS NULL`), `make process` fills the generated materials (queue: `generated_cover_letter IS NULL`). The dashboard only reads from the database, plus status edits and jobs you add by hand; it never runs a scrape or the LLM. No in-flight state to lose, and any step can be re-run safely.

**LLM abstraction layer.** `llm/client.py` wraps whatever inference backend you're running (Ollama locally by default) behind one interface, so swapping to Llama.cpp or an OpenAI-compatible API is a config change, not a rewrite. Only `OllamaClient` exists today.

**Config-driven, not code-driven.** `config.yaml` (validated with Pydantic) holds what you're searching for, where you're searching, and model settings. You should never need to edit Python to change a search.

## Scraping: scope and ethics

LinkedIn and SEEK both prohibit automated scraping in their terms of service and have a track record of legal action against scrapers. This project does **not** include stealth/anti-detection scraping of authenticated pages.

In scope:
- Official APIs where a job board provides one (Greenhouse, Lever, Ashby, Workable), and the public careers API behind a Workday site (not a published contract; checked against each tenant's `robots.txt`)
- A sanctioned search API with a free tier (Adzuna)
- Public, non-authenticated feeds where available

Out of scope:
- Logging into LinkedIn/SEEK to scrape behind auth
- Bypassing bot detection or CAPTCHAs
- Data-broker / email-finder APIs for the contact lookup (see below)

**SEEK is not a source.** It was the original search source, via its public RSS feed, until that feed went dead (zero results for every query from 2026-06-20); the scraper was removed in KAN-32, and a leftover `seek` line in `sources.txt` now fails as an unknown source. **Adzuna** (`src/ingestion/adzuna.py`) is the search/aggregator source: it's a sanctioned API with a free tier, reaches India as well as Australia, and just needs free credentials from developer.adzuna.com in `config.yaml`'s `adzuna:` block. See `docs/configuration.md` and [`docs/scrapers.md`](docs/scrapers.md).

The **contact lookup** (`make contacts`, below) follows the same ethic: it reads only text a company already published in its own posting, makes no network calls, and never queries a data broker (Hunter, Apollo, RocketReach, ZoomInfo, etc.) or a logged-in source. A guessed address is always flagged as a guess, never asserted as verified. See [`docs/hiring-manager-lookup.md`](docs/hiring-manager-lookup.md).

**A source that doesn't allow scraping by default is dropped, not worked around.** If a site's `robots.txt` disallows the paths a scraper needs (for example `Disallow: /` for every crawler), or it answers with a bot challenge, that source is out of scope. SmartRecruiters and the Adelaide PageUp employers were evaluated on that basis (see Status above). For Workday, which has no published contract, each tenant's `robots.txt` is checked before the board is added.

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
    status: str = Field(default="To Apply")  # To Apply, Applied, Interviewing, Rejected, Not interested
    generated_cover_letter: Optional[str] = None
    generated_cold_email: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_confidence: Optional[str] = None  # "published" | "pattern-guess" | "none"
    dead_at: Optional[datetime] = None        # when the posting was found closed
    dead_reason: Optional[str] = None         # "gone from board" | "http 404" | "http 410"
    posted_at: Optional[datetime] = None      # the source's own publish date, if given
    last_seen_at: Optional[datetime] = None   # last time a scrape returned it
    last_checked_at: Optional[datetime] = None  # last time check_links requested its URL
    opened_at: Optional[datetime] = None      # when you first opened it (None = unread, shown bold)
```

A second small table, `ScrapeRun`, records when each `make scrape` started so the dashboard can mark the last run's jobs **New**.

`job_board_id` is unique so re-running a scrape doesn't duplicate postings. Columns added after the first release (`contact_*`, the dead-posting columns and `opened_at`) are additive — `src/storage/database.py` adds them to an existing SQLite file on first run after upgrading, no manual migration needed. Full field-by-field notes: [`docs/data-model.md`](docs/data-model.md).

**Closed postings.** A board scrape marks a job dead when its board stops listing it (`"gone from board"`); `make check-links` does the same for postings whose link returns 404/410 (in practice Adzuna ads, which no board re-lists). Dead postings with status `To Apply` or `Rejected` are then moved to a second SQLite file, `data/dead_jobs.db`, with every column kept for later analysis; `Applied` and `Interviewing` rows stay in `jobs.db`, marked dead. A posting a scrape returns again is restored. See [`docs/data-model.md`](docs/data-model.md#dead-postings-and-the-archive).

## Getting started

### Prerequisites
- Python 3.11+
- [Ollama](https://ollama.com) installed locally, with a model pulled (e.g. `ollama pull llama3.2:3b-instruct-q4_K_M`)
- (Optional, recommended) free Adzuna API credentials from [developer.adzuna.com](https://developer.adzuna.com) — Adzuna is the only search source; without it you only get the ATS boards in `sources.txt`

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
make check-links  # (optional) mark postings whose link is gone dead, and archive them
make validate  # check your configured boards are still live (tokens go stale)
make resolve   # (optional) find the boards of the employers in companies.txt
make discover  # propose new boards from companies already in your results
make contacts  # (optional) find a contact for each posting from its own text
make process   # generate cover letters / cold emails for pending rows
make app       # serve the dashboard on http://127.0.0.1:8000 and open it
```

**Triaging.** The dashboard opens on your **To Apply** queue, newest posted first; click a column header to sort by title or company. Jobs from the last `make scrape` carry a **New** badge, and unread ones are bold until you open them (or use **Mark these as read**). **×** marks a job *Not interested*: it leaves the queue, and `make process`/`make contacts` skip it. **↗** opens the original posting.

**Adding a job by hand.** Found a role on LinkedIn, SEEK or Naukri? Open **Add a job** at the top of the dashboard and paste its title, company and description (location and URL optional). It's stored with the `manual` source, the next `make process` run writes its cover letter before the scraped backlog, and it's never link-checked (those sites often block logged-out requests).

`validate`, `resolve` and `discover` are maintenance/growth steps, not required every run. `resolve` is the hands-off one: list the employers you want in `companies.txt` and it finds which boards they are on and records them in a board map that `scrape` reads (boards pinned in `sources.txt` always win). See [`docs/board-discovery.md`](docs/board-discovery.md) for how they fit together (list companies → resolve → scrape; or curate candidates → validate → scrape; or mine existing results → discover → review → validate). `check-links` is optional: board postings are already checked by every scrape, so it mostly catches closed Adzuna ads; it checks up to 200 links per run. `contacts` is optional but should run before `process` if you want the cold email addressed to someone. `process` generates `llm.batch_size` cover letters per run (0 = every pending row). On Windows, `run-pipeline.ps1` runs all seven steps in order in one go (with `-SkipApp` to stop before the dashboard); the optional ones warn and carry on if they fail.

`make app` takes `--port N` and `--no-browser` if you run it directly: `python -m src.app --port 8001`.

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
job-hunter\Scripts\python.exe -m src.ingestion.check_links --mark-dead   # = make check-links
job-hunter\Scripts\python.exe -m src.ingestion.validate  # = make validate
job-hunter\Scripts\python.exe -m src.ingestion.discover  # = make discover
job-hunter\Scripts\python.exe -m src.contacts.cli        # = make contacts
job-hunter\Scripts\python.exe -m src.llm.cli             # = make process
job-hunter\Scripts\python.exe -m src.app               # = make app
job-hunter\Scripts\python.exe -m pytest tests\           # = make test
```

Calling `job-hunter\Scripts\python.exe` directly is equivalent to activating the venv first — activation only adds that folder to your PATH. (If you'd rather activate: `job-hunter\Scripts\Activate.ps1`.)

### Running tests

```bash
pytest tests/    # 489 tests; none touch the network
ruff check src tests
```

CI runs both on every pull request (Python 3.11).

## Roadmap

This is being built incrementally. Rough sequence:

1. `JobPost` schema + SQLite storage (done)
2. One working scraper (SEEK public feed) implementing `BaseScraper` — the feed later went dead and the scraper was removed (KAN-32)
3. LLM client wrapper + cover letter / cold email prompt templates (done)
4. `make process` queue consumer (done)
5. Dashboard (read-only view + status updates) — first in Streamlit, replaced by a small Flask app in KAN-33 (done)
6. Second scraper (Greenhouse public board API) to prove the Strategy Pattern decouples cleanly (done)
7. CI workflow (lint + pytest on push) (done)
8. Pydantic-validated config + multi-source/multi-search support (done)
9. `database.url` wired through (SQLite default, Postgres via config or `JOBHUNTER_DATABASE_URL`) (done)
10. `filters` / `sources` split + `sources_file` (plain-text board list, careers-URL auto-detection) (done)
11. Lever and Ashby scrapers — second and third ATS sources (done)
12. Adzuna scraper — sanctioned search API, the replacement for the SEEK feed, reaches AU + India (done)
13. Board validation (`make validate`) and discovery (`make discover`) — keep the board list live and growing without a noise-adding firehose (done)
14. Hiring-contact lookup v1 (`make contacts`) — public, in-posting-text only; shown with a confidence flag in the dashboard (done)
15. `capture`/`JOBHUNTER_DUMP_DIR` debug dumping of unfiltered scrape output (done)
16. Dead-posting handling: mark jobs dead when their board drops them, rework the link checker, archive dead rows (done; the "closed" marker for tracked jobs, scheduled checks and a parallel harness are open)
17. Adzuna duplicates merged (one row per ad), and jobs can be added by hand from the dashboard (done)
18. Dashboard moved from Streamlit to a small Flask app, with a triage view (done)
19. `filters.board_locations`: a board-only location list (done)
20. Workable and Workday scrapers — fifth and sixth sources (done). SmartRecruiters and PageUp were evaluated and dropped (see Status)
21. Cut noise at ingest (KAN-37): Adzuna results are checked by title, with `also_match_titles`, `exclude_titles` and `remote_regions`, plus a one-off cleanup of stored jobs (done)
22. Next: better cover letters, and picking the Workable and Workday employers to follow

A hosted-LLM config and resume parsing are still deferred — the LLM layer is intentionally left at one backend (Ollama) for now. The Workday scraper is built ([`docs/workday.md`](docs/workday.md)) — a multi-call, POST-based ATS unlike the others, checked against live boards; it needs a careers URL per employer and reads one site per tenant. See [`docs/roadmap.md`](docs/roadmap.md) for the detailed version and `TODO.md` for the working tracker.

## Documentation

Deeper, module-by-module docs live in [`docs/`](./docs/README.md): architecture, the full data model, a worked-example walkthrough of every scraper, the LLM-provider contract, the complete config reference, board discovery/validation, the hiring-contact-lookup design, the Workday scraper notes, and manual per-ATS submission notes. Kept updated alongside the code, with `(TODO)` markers on anything not yet decided — though if something here ever looks out of sync with `src/`, trust the code and open an issue (or just fix the doc).

## License

MIT (or your choice — update before publishing).
