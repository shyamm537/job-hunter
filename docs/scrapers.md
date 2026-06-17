# Scrapers

## The contract

`src/ingestion/base_scraper.py`:

```python
class BaseScraper(ABC):
    source_name: str = "unknown"

    @abstractmethod
    def scrape(self) -> List[JobPost]:
        ...
```

That's the entire interface. A scraper:

- Takes whatever it needs in `__init__` (search terms, location, a board token — though see the scope note below)
- Returns a list of fully-formed `JobPost` objects
- Does **not** touch the database — `src/ingestion/cli.py` owns calling `upsert_job()`
- Sets `source_name` so logging and `job_board_id` prefixes are traceable to a source

## Worked example 1: `SeekScraper`

`src/ingestion/seek.py`. It:

1. Builds a SEEK public RSS search URL from `search_terms` and `location`
2. Parses it with `feedparser`
3. Builds `job_board_id` as `f"seek-{sha1(url)[:10]}"` so re-scraping the same listing is idempotent
4. Maps RSS fields (`title`, `author`, `summary`, `link`) onto `JobPost` fields

It does not log in, does not use Playwright, and does not attempt to bypass any bot detection — see the scope note below.

## Worked example 2: `GreenhouseScraper`

`src/ingestion/greenhouse.py`. This is the second scraper, and its whole reason for existing is to prove the Strategy Pattern actually decouples — a SEEK-only project never tests the abstraction, it just gives it something to wrap. Greenhouse is a deliberately *different* shape from SEEK:

1. Hits the public Greenhouse boards JSON API: `https://boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true`. `<board>` is the public board token (the `stripe` in `boards.greenhouse.io/stripe`).
2. Fetches via the shared `get_json()` helper (`src/ingestion/http_util.py`), which adds a couple of retries with backoff.
3. Optionally filters by `title_contains` — Greenhouse lists *every* open role at a company, so this is usually wanted.
4. Builds `job_board_id` as `f"greenhouse-{sha1(url)[:10]}"` — same convention as SEEK, distinct prefix so dedup never collides across sources.

The payoff: it's a JSON API, not RSS, yet `BaseScraper`, `cli.py`, and the storage layer didn't change to accommodate it. That's the abstraction holding.

### Scope note for Greenhouse

This reads the same public, unauthenticated JSON the board itself renders — no login, no API key, no anti-bot bypass. That keeps it inside the project's "public, non-authenticated" rule below.

## Running multiple scrapers

`config.yaml` can list several sources (see `docs/configuration.md`). `src/ingestion/factory.py` maps each validated source onto its scraper class:

```python
def build_scraper(source) -> BaseScraper:
    if source.type == "seek":
        return SeekScraper(search_terms=source.title, location=source.location)
    if source.type == "greenhouse":
        return GreenhouseScraper(board=source.board, title_contains=source.title_contains)
    raise ValueError(...)
```

`cli.py` loops over `config.resolved_sources`, runs each scraper, and upserts the results. A source that throws (network error, bad board token) is logged and skipped — it doesn't abort the rest of the run.

## Scope: what a new scraper is allowed to do

Per the README's "Scraping: scope and ethics" section, in-scope sources are:

- Public, non-authenticated pages or feeds (what `SeekScraper` and `GreenhouseScraper` do)
- Official APIs, where a job board provides one (Greenhouse's public boards API qualifies)

Out of scope:

- Logging into a site to scrape behind auth (this specifically rules out a `LinkedInScraper` that signs in)
- Bypassing CAPTCHAs or anti-bot measures

If you fork this and want to go further than that, it's your call and your risk — don't assume the project's existing ethics framing covers it.

## Adding a third scraper

The checklist:

1. New file `src/ingestion/<source>.py`, subclassing `BaseScraper`
2. Set a distinct `source_name`
3. Build `job_board_id` with that source's prefix so dedup doesn't collide across sources
4. Add a config model in `src/config.py` (a new member of the `Source` union) and a branch in `src/ingestion/factory.py`
5. Add a test mirroring `tests/test_greenhouse_scraper.py`'s mocking approach

Nothing in `base_scraper.py` or `cli.py` should need to change — if it does, that's a smell worth looking at.

## Rate limiting / politeness

`src/ingestion/http_util.py` provides `get_json()` with a polite User-Agent and retry-with-backoff on transient (connection/timeout/5xx) failures; 4xx errors are raised immediately since retrying won't help. `GreenhouseScraper` uses it. `SeekScraper` makes a single `feedparser` request per `scrape()` and doesn't go through it. If a future scraper paginates heavily, it should add its own inter-page delay — `get_json()` covers retries, not throttling.
