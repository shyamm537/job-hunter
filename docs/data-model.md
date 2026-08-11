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

## Dedup strategy

`job_board_id` is built as `f"{source}-{sha1(url)[:10]}"` (see `src/ingestion/seek.py`). Re-running `make scrape` against the same search will produce the same IDs for postings still live, so `upsert_job()` (`src/storage/database.py`) skips them instead of inserting duplicates.

This means: if a posting's URL changes (e.g. SEEK re-publishes it under a new listing ID), it will be treated as a new job, not an update to an old one. That's a known limitation, not a bug — there's no canonical job identity across re-postings.

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
