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

A scraper takes whatever it needs in `__init__` and returns a list of
`JobPost` objects. It does **not** touch the database, and it does **not** know
about filters — it's a pure fetcher. Selecting and filtering happen above it
(see "Sources, filters, and the planner").

## Worked example 1: `GreenhouseScraper`

`src/ingestion/greenhouse.py`. The first ATS source. Hits the public boards API
`https://boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true` (the
`stripe` in `boards.greenhouse.io/stripe`) and maps the JSON onto `JobPost`. It
returns the company's **whole** board — filtering by title/location happens
afterwards (see the planner). Its `job_board_id` is
`f"greenhouse-{sha1(url)[:10]}"`, the convention the other board scrapers
follow.

## Worked example 2: `LeverScraper`

`src/ingestion/lever.py`. The second ATS source. Hits
`https://api.lever.co/v0/postings/<company>?mode=json` (the `figma` in
`jobs.lever.co/figma`). The response is a top-level JSON **array** (Greenhouse is
a `{"jobs": [...]}` object) — a reminder that "ATS" isn't one shape, which is why
each gets its own subclass. Like Greenhouse, it returns the whole board.

## Worked example 3: `AshbyScraper`

`src/ingestion/ashby.py`. The third ATS source. Hits
`https://api.ashbyhq.com/posting-api/job-board/<org>` (the `ashby` in
`jobs.ashbyhq.com/ashby`); response is `{"jobs": [...]}` like Greenhouse. Two
wrinkles worth knowing: it carries `isListed` (we skip unlisted postings), and
it exposes remote as a separate `isRemote` boolean rather than baking it into
the location string. We fold that flag into the location (`"Portugal"` →
`"Portugal (Remote)"`) so the shared "remote always passes" filter behaves the
same as it does for the other ATSs — otherwise a genuinely-remote Ashby role
would be dropped by a city location filter.

## Worked example 4: `AdzunaScraper`

`src/ingestion/adzuna.py`. The search/aggregator source. It's a sanctioned API
with a free tier, and **per-country** — `https://api.adzuna.com/v1/api/jobs/<country>/
search/<page>?app_id=…&app_key=…&what=…&where=…` — so it reaches India (`in`) as
well as Australia (`au`). It's a *search* source (`post_filter=False`):
the planner expands `titles × locations` into queries, pairing each location with
its country via `country_of()` (Adelaide→au, Mumbai→in) so it doesn't query the
wrong index. Credentials come from the top-level `adzuna:` config block, threaded
through `plan_scrapes(..., adzuna_auth=...)`. Its `job_board_id` is built from a
*normalized* redirect URL — `_dedup_key()` hashes scheme+host+path only, dropping
the per-search query string (`se`/`utm_*`/`where`) so the same ad fetched under
two different searches collapses to one row instead of N (see
[`docs/data-model.md`](./data-model.md)). Two honest limits: it needs an API
key (config.yaml becomes sensitive — already gitignored), and the API returns
only a description *snippet*, so generated materials/contacts have less to chew on.

## Worked example 5: `WorkableScraper`

`src/ingestion/workable.py`. The fourth ATS source. Hits the public widget API
`https://apply.workable.com/api/v1/widget/accounts/<account>?details=true` (the
`squiz` in `apply.workable.com/squiz`) — one GET per board, no key. Checked
against live boards on 2026-10-03 (`robots.txt` allows it). Things worth
knowing:

- **A multi-location posting is listed once per location.** Same `shortcode`
  and URL, a different city each time (`legalvision` returned 32 entries for 27
  postings, matching the site's own count). The scraper merges entries by
  shortcode into one `JobPost` whose location joins every place with `"; "`, so
  the location filter sees all of them and the board reconciles correctly.
- **The account slug is case-sensitive** (`SQUIZ` is a 404). An unknown account
  is a 404 too, so a dead token raises and `make validate` reports it; an
  account with no openings is a 200 with no jobs.
- **Descriptions are HTML.** `?details=true` puts one on every job, so there is
  no per-posting request. The scraper converts it with `html_to_text()`
  (`src/ingestion/text_util.py`, standard library only), so the dashboard and the
  cover-letter prompt get text, not markup.
- **Remote is a `telecommuting` boolean**, folded into the location (`"Remote"`
  or `"<place> (Remote)"`) like Ashby's `isRemote`, so the shared "remote always
  passes" filter behaves the same.
- A pasted single-posting link (`apply.workable.com/j/<code>`) has no account
  in it, so `source_from_url()` rejects it and asks for the board URL.

Its `job_board_id` is `workable-<sha1(url)[:10]>`, where `url` is
`https://apply.workable.com/j/<shortcode>`. Not covered: `make discover` does not
guess Workable accounts (many guessed slugs exist with no openings, which would
be proposed as "live, no current match"), so add boards by hand.

## Worked example 6: `WorkdayScraper`

`src/ingestion/workday.py`. The fifth ATS source and the chattiest: a POST to
`https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs`, 20
postings a page, plus an optional detail GET per posting. A board needs three
values (tenant, data centre, site) that are read off its careers URL, so it is
always added by hand. Full detail in [`docs/workday.md`](./workday.md); what makes
it different from the other scrapers:

- **Its list is capped.** A tenant's `total` tops out at 2000 and offsets past
  that return the first page again, so the scraper raises for a board that large
  instead of returning a list that only looks complete. It also raises on a short
  list (fewer unique postings than `total`) and on a posting with no path.
- **Descriptions cost a request each**, so details are fetched only for postings
  whose title matches `filters.titles` (the planner passes them in as
  `detail_titles`), and not at all when no titles are set. Every other posting is
  still returned, with an empty description. A failed detail call is logged and
  the posting is kept.
- **The id is the requisition id, not the URL.** The URL carries location and title
  slugs that can change; the id key is `<tenant>/<site>/<requisition id>`.
- **One site per tenant**, because the tenant is the board token.
- Politeness: it sleeps `REQUEST_DELAY` between requests, and uses `post_json()`.

Known cost: the scraper repeats the detail call for every title match on every
scrape, because it cannot see the database. It is bounded by the number of title
matches (8 on a 167-posting board).

Why ATS scrapers and not "a scraper per company": most employers rent an ATS
(Greenhouse, Lever, Ashby, Workday…) rather than build their own job site. A raw
careers page is bespoke HTML/JS with no general way to scrape it; an ATS exposes
a predictable public API. One ATS scraper unlocks every company on it. (The
giants — Google, Amazon — run bespoke sites and would each need dedicated,
ToS-sensitive scrapers; they're the hard cases, not the easy ones.)

## Sources, filters, and the planner

What you want (`filters.titles`, `filters.locations`) is kept separate from
where you look (`sources`). `src/ingestion/planner.py`'s `plan_scrapes(sources,
filters)` combines them, because the two source kinds use the intent
differently:

```python
def plan_scrapes(sources, filters):
    # Adzuna: each (title, location) is a query -> one AdzunaScraper per pair
    #         in that country, no post-filter (the query already filtered).
    # ATS:  scrape the whole board once, post_filter=True -> filter results
    #       with src/ingestion/filtering.py's job_matches().
    ...
```

It returns `PlannedScrape(scraper, label, post_filter)` items. `cli.py` runs each,
applies the filter to ATS results, and upserts. A planned scrape that throws is
logged and skipped — it doesn't abort the run. Location filtering is lenient: a
posting whose location mentions "remote" always passes (see
`docs/configuration.md`). Board postings are matched against the optional
`filters.board_locations` (whole words) when it is set, otherwise against
`filters.locations`; Adzuna queries only ever use `filters.locations`.

## Sources from a text file

`config.yaml`'s `sources_file:` points at a plain-text file of *where* to look,
parsed by `load_sources_file()` (`src/config.py`) into the same `Source` models
and appended to inline `sources`. One per line, `#` comments ignored:

```
adzuna au
greenhouse stripe
lever figma
```

Titles and locations are not in this file — they live in `filters`. You can
also paste a board's careers URL (e.g. `https://boards.greenhouse.io/stripe`)
in place of the `<type> <token>` form — `source_from_url()` auto-detects the
ATS and token. See `sources.txt.example` and `docs/configuration.md`.

## Scope: what a new scraper is allowed to do

In scope: public, non-authenticated pages/feeds and official public APIs
(Greenhouse, Lever, Ashby, Workable, Workday boards; the Adzuna search API). A site
whose `robots.txt` disallows the paths we need, or that answers with a bot
challenge, is out of scope: that source is dropped rather than worked around. Out of scope: logging into a site to scrape
behind auth (rules out a sign-in `LinkedInScraper`), and bypassing CAPTCHAs or
anti-bot measures. If you fork and go further, that's your call and your risk.

## Adding another scraper

1. New file `src/ingestion/<source>.py`, subclassing `BaseScraper`, returning
   `JobPost`s with a distinct `source_name` and `<source>-<sha1(url)[:10]>` id.
2. Add a config model in `src/config.py` (a new member of the `Source` union).
3. Add a branch in `src/ingestion/planner.py` (`post_filter=True` for boards
   that return everything, `False` for search-style sources).
4. Add a test mirroring `tests/test_lever_scraper.py`.

Nothing in `base_scraper.py` or `cli.py` should need to change.

## Validating board tokens (`make validate`)

Tokens go stale: companies switch ATS, rename a board, or wind down. "Valid"
means the token still resolves to a live public board. `src/ingestion/validate.py`
checks a list of sources against the live APIs and reports each as **match**
(live, ≥1 posting matching your `filters`), **live** (resolves, nothing matching
right now), or **dead** (token didn't resolve — usually a 404).

It reuses everything: `plan_scrapes` builds the scraper, the scraper hits the
same public API `make scrape` uses, and `job_matches()` counts matches. A dead
token is caught and reported, not propagated — validating a list never aborts on
one bad token. Adzuna is reported live without a network call (it's a search
engine, not a board).

```bash
make validate                                   # check the boards in your config
python -m src.ingestion.validate cands.txt --out sources.txt          # keep live boards
python -m src.ingestion.validate cands.txt --out sources.txt --require-match  # only boards with a current match
```

Where boards come from is a curation problem, not a scraping one: the companies
that expose Greenhouse/Lever public APIs skew remote/global tech, so a curated
list of those that hire your role beats a giant scraped token dump (slow, noisy,
mostly irrelevant — the position `TODO.md` takes). `sources.txt` ships a
validated starter set; `sources.candidates.txt` holds a wider pool to re-check.
Paste a careers URL and `source_from_url()` detects the ATS + token for you.

## Rate limiting / politeness

`src/ingestion/http_util.py`'s `get_json()` and `post_json()` add a polite
User-Agent (naming this repository) and retry-with-backoff on transient
(connection/timeout/5xx) failures; 4xx is raised immediately. The ATS scrapers use
them. `REQUEST_DELAY` (0.5 s, the same pause the link checker uses) is the shared
throttle for scrapers that make several requests in a row: Workday sleeps it
between list pages and between detail calls. The one-request-per-board scrapers
(Greenhouse, Lever, Ashby, Workable) don't need it.

## Debugging a scrape (capture)

When a scrape returns fewer jobs than expected, the cause is usually filtering,
not the source. Two aids:

- The per-plan log distinguishes fetched vs. kept for ATS boards, e.g.
  `greenhouse[stripe]: 168 fetched, 0 kept after filters, 0 new` — if `kept` is
  0 but `fetched` isn't, your `filters` are too strict (a common one: a
  `locations` filter for a city the company has no office in; remote roles still
  pass).
- Set `JOBHUNTER_DUMP_DIR` to write each scrape's **unfiltered** output to JSON
  (`src/ingestion/capture.py`). 