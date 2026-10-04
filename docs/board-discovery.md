# Growing the board list

A job hunt runs for months; a static board list goes stale and caps your reach.
But the fix is **not** "add more boards" — auto-adding random boards is the
firehose `TODO.md` rejects: it grows the pipeline's noise (more pending jobs to
LLM-process, more dashboard clutter) without growing relevant supply. The goal is
to grow **relevance** and fight **decay**. Three mechanisms, in order of how
hands-off they are.

## 1. Manual add (always available)

The most precise way to add a board is to paste its careers URL. The parser
detects the ATS + token for you (`source_from_url`, `src/config.py`):

```
# in sources.txt — any of these forms work, one per line:
greenhouse acme
lever acme
ashby acme
workable acme                             # case-sensitive: as in apply.workable.com/acme
workday acme wd3 Careers                  # tenant, data centre, site (3 tokens)
https://job-boards.greenhouse.io/acme     # pasted URL → auto-detected
https://jobs.lever.co/acme
https://jobs.ashbyhq.com/acme
https://acme.wd3.myworkdayjobs.com/en-US/Careers   # Workday: locale optional
https://apply.workable.com/acme           # the board URL; a single posting's
                                          # apply.workable.com/j/<code> link is rejected
```

Then re-validate so you don't add a dead token:

```bash
make validate                  # checks everything your config resolves
```

Workable boards are manual-only too: `make discover` does not guess Workable
accounts (it still checks only Greenhouse, Lever and Ashby), so add them by hand.

Workday boards are **manual-only**: their data-center subdomain (`wd5`) isn't
derivable from a name, so they can only be added from a pasted careers URL
(`https://cba.wd3.myworkdayjobs.com/en-US/CommBank_Careers`) or the three-token form
(`workday cba wd3 CommBank_Careers`). See [`docs/workday.md`](./workday.md) for the
limits (one site per tenant, boards of 2,000+ postings can't be read).

## 2. Discovery from your Adzuna results (`make discover`)

Semi-automated, **propose-then-approve**. Your search source (Adzuna) already surfaces
companies hiring your exact roles. `make discover` (`src/ingestion/discover.py`):

1. Reads the distinct **companies** from Adzuna postings already in your DB.
2. Slugifies each name into candidate board tokens (drops legal suffixes like
   "Pty Ltd"; tries a joined and a hyphenated form).
3. Builds Greenhouse/Lever/Ashby candidates and **validates each live** (reusing
   `make validate`'s machinery), skipping boards you already have.
4. Writes the confirmed-live ones as **commented proposals** to
   `sources.discovered.txt`, matches first.

Nothing is added automatically. You review, uncomment the keepers, and move them
into `sources.txt`:

```bash
make scrape       # populate Adzuna companies first (if you haven't)
make discover     # writes sources.discovered.txt
# review it, uncomment the boards worth keeping, paste them into sources.txt
make validate     # sanity-check the merged list
```

### What discovery can and can't do (honest limits)

- **No false positives, plenty of false negatives.** Every proposal is validated,
  so a proposed board is real. But it only finds companies whose board **token
  equals the name slug** (`Acme` → `acme`). When the token differs (`acme-inc`,
  an acronym, a parent-company token) or the company is on Workday, it misses.
  Treat it as "free easy wins," not full coverage.
- **It's only as good as your Adzuna data.** Discovery proposes from companies Adzuna
  already returned for your titles/locations — relevant by construction, but
  bounded by what Adzuna surfaces.
- **It makes live API calls** (companies × ~2 slugs × 3 ATS). Bounded with
  `--limit N`. In-scope: same public endpoints as the scrapers, polite UA +
  backoff via `http_util`.

## 3. Maintenance: keep the list from rotting

Tokens go stale over a months-long hunt. `make validate` reports `match` / `live`
/ `dead`; re-run it periodically and drop the dead ones. To make it routine,
schedule it on your own machine (cron / Task Scheduler) — e.g. weekly:

```bash
python -m src.ingestion.validate --out sources.txt   # rewrites with live boards only
```

(Run validation where the network reaches the ATS APIs — i.e. your machine.)

## Why not fully automatic?

Auto-adding every discovered live board re-creates the firehose and lowers signal
(see `TODO.md`'s boards discussion). The curation gate — you approving proposals —
is cheap and is what keeps the pipeline pointed at roles you actually want. The
automation does the tedious part (detection + validation); you keep the judgment.
