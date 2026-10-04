# Data Model

There is one table today: `JobPost`, defined in `src/storage/models.py`. The
dead-postings archive (`data/dead_jobs.db`) is a second SQLite file with the
same table; see "Dead postings and the archive" below.

```python
class JobPost(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_board_id: str = Field(unique=True)
    title: str
    company: str
    location: str
    description: str
    url: str
    date_scraped: datetime = Field(default_factory=datetime.utcnow)
    status: str = Field(default="To Apply")
    generated_cover_letter: Optional[str] = None
    generated_cold_email: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_confidence: Optional[str] = None
    dead_at: Optional[datetime] = None
    dead_reason: Optional[str] = None
    posted_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
```

## Field reference

| Field | Type | Notes |
|---|---|---|
| `id` | `int` | Auto-incrementing primary key. |
| `job_board_id` | `str` | **Unique.** Dedup key — see below. Format is `<source>-<hash>`, e.g. `greenhouse-a1b2c3d4e5`. |
| `title` | `str` | As scraped, no normalization. |
| `company` | `str` | As scraped. Not validated or normalised, so the same employer can appear under slightly different names across sources. |
| `location` | `str` | Currently just echoes back whatever location string was passed into the scraper at construction time, not parsed from the listing itself. |
| `description` | `str` | The posting text. Adzuna: a raw feed summary, HTML entities may be present. Workable and Workday descriptions are converted from HTML to plain text (`html_to_text()`). A re-scrape never replaces a description that has text, but fills one that is empty (a Workday detail call that failed). |
| `url` | `str` | Link to the original posting. |
| `date_scraped` | `datetime` | UTC, set automatically on insert. Not updated on re-scrape. |
| `status` | `str` | One of `To Apply`, `Applied`, `Interviewing`, `Rejected`. Plain string, not an enum — see "Known looseness" below. |
| `generated_cover_letter` | `str \| None` | `None` is the queue signal: `pending_llm_jobs()` selects rows where this is null. |
| `generated_cold_email` | `str \| None` | Generated alongside the cover letter, same LLM call cycle. |
| `contact_name` | `str \| None` | A contact found in the posting's own text, or `None`. See [`docs/hiring-manager-lookup.md`](./hiring-manager-lookup.md). |
| `contact_email` | `str \| None` | A published address or a flagged guess — never asserted as verified. `None` if none found. |
| `contact_confidence` | `str \| None` | `"published"` / `"pattern-guess"` / `"none"`. **Also the queue signal:** `pending_contact_jobs()` selects rows where this is null, so a looked-up row (even a miss, set to `"none"`) leaves the queue. Free-text, like `status`. |
| `dead_at` | `datetime \| None` | Naive UTC. When the posting was found closed; `None` = alive. Set by the scrape (board no longer lists it) or `make check-links` (404/410). The first date is kept if a row is marked dead again; cleared if a scrape returns the posting. |
| `dead_reason` | `str \| None` | Set with `dead_at`: `"gone from board"`, `"http 404"` or `"http 410"`. Cleared with it. |
| `posted_at` | `datetime \| None` | Naive UTC. The source's own publish date (Greenhouse `first_published`, falling back to `updated_at`; Lever `createdAt`; Ashby `publishedAt`; Workable `published_on`, falling back to `created_at`; Workday `startDate`, from the detail call; Adzuna `created`), or `None` if it doesn't say. Filled in on a later scrape if missing. |
| `last_seen_at` | `datetime \| None` | Naive UTC. Last time a scrape returned the posting. Board scrapes set it for every listed posting, even ones the filters drop. A recent value keeps the row out of the link-check queue. |
| `last_checked_at` | `datetime \| None` | Naive UTC. Last time `make check-links` requested the URL, whatever the outcome. |
| `opened_at` | `datetime \| None` | Naive UTC. When you first opened the job in the dashboard or changed its status; `None` = unread (shown in bold). "Mark these as read" sets it for the whole filtered list. |

## Dedup strategy

`job_board_id` is `f"{source}-{sha1(<key>)[:10]}"`. Re-running `make scrape` produces the same IDs for postings still live, so `upsert_job()` (`src/storage/database.py`) skips them instead of inserting duplicates. What goes into the hash depends on the source:

- **ATS boards (Greenhouse, Lever, Ashby, Workable)** hash the posting URL directly. Workable lists a posting with several locations once per location (same `shortcode` and URL); the scraper merges those into one row, so the id is the same either way.
- **Workday** hashes `<tenant>/<site>/<requisition id>` instead, because its URL holds location and title slugs that can change while the posting stays open. The requisition id is the part after the last `_` in the posting's path, using the matching `bulletFields` item (the id without a `-N` repost marker) when there is one; see [`workday.md`](./workday.md).
- **Jobs added by hand** (the dashboard's "Add a job", `add_manual_job()`) are `manual-<hash>`: the hash of the URL if one was given, otherwise of the lower-cased title + company + description, so adding the same job twice finds the first one. `manual-` rows are never board-reconciled or link-checked, and `pending_llm_jobs()` returns them first.
- **Adzuna** hashes Adzuna's own ad `id` (`id_dedup_key()` in `src/ingestion/adzuna.py`), so the same ad surfaced by two different searches is one row whatever tracking params (`se`, `utm_*`) its `redirect_url` carries. If a result ever lacks an `id`, the key falls back to a hash of the URL's scheme + host + path. Rows scraped before the switch to `id` were keyed by a hash of the full URL, which stored the same ad up to eight times; `python -m src.storage.adzuna_rekey` re-keyed them from the ad id in the stored URL (`/details/<id>` or `/land/ad/<id>`) and merged the copies, keeping generated materials, contact lookups and status (KAN-7). It's a dry run without `--apply`, and re-running is a no-op.

This means: if a posting's *identifying* URL changes (e.g. a board re-publishes it under a new listing ID), it's treated as a new job, not an update to an old one. That's a known limitation, not a bug — there's no canonical job identity across re-postings. Dedup is also per-source: the same role posted to two different boards is two rows (different URLs, different `<source>-` prefix).

## Scrape runs

A second small table, `ScrapeRun` (`id`, `started_at`), gets one row each time `make scrape` starts. The dashboard marks a job **New** when its `date_scraped` is at or after the latest run's start, so the badge covers exactly what the last scrape added (jobs you add by hand don't get it). Before the first recorded run there are no New badges.

## Status lifecycle

`To Apply → Applied → Interviewing → Rejected`, plus `Not interested` for jobs you dismiss, is enforced only by the dashboard (`STATUSES` in `src/storage/models.py`; a POST with any other status is refused). `Not interested` jobs are skipped by `make process` and `make contacts`, and archived like `To Apply` ones when they close — the database column is a free-text string with no `CHECK` constraint. Editing a row directly (e.g. via a SQLite browser) could set any string and the app wouldn't reject it.

## Dead postings and the archive

A posting is marked dead (`dead_at`, plus a short `dead_reason`) in two ways:

- **`"gone from board"`**: after a successful scrape of a Greenhouse, Lever
  Ashby, Workable or Workday board, any stored row for that board that the board no longer lists
  (`reconcile_board()` in `src/storage/database.py`). A board's rows are the
  `<source>-` rows whose `company` is the board token. A failed or empty scrape
  changes nothing. Rows still listed get `last_seen_at`, even when the filters
  drop them, and a dead one is revived.
- **`"http 404"` / `"http 410"`**: the link checker (`src/ingestion/check_links.py`),
  for rows no scrape has seen recently. In practice that means Adzuna rows and
  rows from boards no longer in `sources.txt`.

Dead rows with status `To Apply` or `Rejected` are then **moved** to a separate
SQLite file, `dead_jobs.db`, next to the main database by default
(`database.archive_url` overrides it). See `src/storage/archive.py`. The
archive has the same `jobpost` table and columns, so dates, reason, generated
materials and contact fields are kept for later analysis. `Applied` and
`Interviewing` rows stay in the main database, marked dead, so jobs you're
tracking don't disappear. Both `make scrape` and `make check-links` run
the move at the end; `python -m src.storage.archive` runs it alone.

- Rows are matched on `job_board_id`; `id` is **not** preserved across the two
  files.
- If a scrape returns an archived posting again, `upsert_job()` restores it
  (status and materials intact, `dead_*` cleared) instead of inserting a new
  row. It gets a new `id`.
- The move copies first and deletes second, so an interrupted move can be
  re-run safely.

## Known looseness (intentional, for now)

- No `CHECK` constraint or enum on `status`.
- No foreign keys — there's only one table.
- No column for which LLM model/version generated the materials — if you change `llm.model` in config and re-run `make process`, old rows are simply overwritten with no history.

## (TODO) Future tables

Not designed yet. Candidates if the project grows past one table:

- A `GeneratedMaterial` table (one-to-many with `JobPost`) to keep a history of generated cover letters/emails instead of overwriting in place.
- A `Resume` table if resume parsing gets built, instead of the current flat `resume_summary` string in `config.yaml`.
- An `Application` table if status tracking needs timestamps per transition (e.g. "applied on X, interview on Y") rather than a single current-status string.
