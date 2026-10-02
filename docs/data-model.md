# Data Model

There is one table today: `JobPost`, defined in `src/storage/models.py`.

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
```

## Field reference

| Field | Type | Notes |
|---|---|---|
| `id` | `int` | Auto-incrementing primary key. |
| `job_board_id` | `str` | **Unique.** Dedup key — see below. Format is `<source>-<hash>`, e.g. `seek-a1b2c3d4e5`. |
| `title` | `str` | As scraped, no normalization. |
| `company` | `str` | As scraped. For SEEK's RSS feed this comes from the entry's `author` field, which is occasionally inconsistent — not validated. |
| `location` | `str` | Currently just echoes back whatever location string was passed into the scraper at construction time, not parsed from the listing itself. |
| `description` | `str` | Raw feed summary — HTML entities may be present, not sanitized. |
| `url` | `str` | Link to the original posting. |
| `date_scraped` | `datetime` | UTC, set automatically on insert. Not updated on re-scrape. |
| `status` | `str` | One of `To Apply`, `Applied`, `Interviewing`, `Rejected`. Plain string, not an enum — see "Known looseness" below. |
| `generated_cover_letter` | `str \| None` | `None` is the queue signal: `pending_llm_jobs()` selects rows where this is null. |
| `generated_cold_email` | `str \| None` | Generated alongside the cover letter, same LLM call cycle. |
| `contact_name` | `str \| None` | A contact found in the posting's own text, or `None`. See [`docs/hiring-manager-lookup.md`](./hiring-manager-lookup.md). |
| `contact_email` | `str \| None` | A published address or a flagged guess — never asserted as verified. `None` if none found. |
| `contact_confidence` | `str \| None` | `"published"` / `"pattern-guess"` / `"none"`. **Also the queue signal:** `pending_contact_jobs()` selects rows where this is null, so a looked-up row (even a miss, set to `"none"`) leaves the queue. Free-text, like `status`. |

## Dedup strategy

`job_board_id` is `f"{source}-{sha1(<key>)[:10]}"`. Re-running `make scrape` produces the same IDs for postings still live, so `upsert_job()` (`src/storage/database.py`) skips them instead of inserting duplicates. What goes into the hash depends on the source:

- **ATS boards (Greenhouse, Lever, Ashby)** and **SEEK** hash the posting URL directly.
- **Adzuna** hashes only the *normalized* redirect URL — scheme + host + path, with the query string and fragment dropped (`_dedup_key()` in `src/ingestion/adzuna.py`). Adzuna's `redirect_url` carries per-search tracking params (`se`, `utm_*`, `where`), so the same ad surfaced by two different searches (e.g. two spellings of a city) would otherwise hash to two IDs. Stripping the query collapses those into one row. The full URL is still stored in the `url` field for the link; only the dedup key is normalized. This assumes Adzuna keeps the job's identity in the URL *path* — see the TODO item to switch Adzuna's key to its own stable `id` field, which removes that assumption.

This means: if a posting's *identifying* URL changes (e.g. a board re-publishes it under a new listing ID), it's treated as a new job, not an update to an old one. That's a known limitation, not a bug — there's no canonical job identity across re-postings. Dedup is also per-source: the same role posted to two different boards is two rows (different URLs, different `<source>-` prefix).

## Status lifecycle

`To Apply → Applied → Interviewing → Rejected` is enforced only by the Streamlit dropdown (`STATUSES` list in `src/app/main.py`) — the database column is a free-text string with no `CHECK` constraint. Editing a row directly (e.g. via a SQLite browser) could set any string and the app wouldn't reject it.

## Known looseness (intentional, for now)

- No `CHECK` constraint or enum on `status`.
- No foreign keys — there's only one table.
- No column for which LLM model/version generated the materials — if you change `llm.model` in config and re-run `make process`, old rows are simply overwritten with no history.

## (TODO) Future tables

Not designed yet. Candidates if the project grows past one table:

- A `GeneratedMaterial` table (one-to-many with `JobPost`) to keep a history of generated cover letters/emails instead of overwriting in place.
- A `Resume` table if resume parsing gets built, instead of the current flat `resume_summary` string in `config.yaml`.
- An `Application` table if status tracking needs timestamps per transition (e.g. "applied on X, interview on Y") rather than a single current-status string.
