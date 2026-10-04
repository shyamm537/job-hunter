# Growing the board list

A job hunt runs for months, and companies change boards while a fixed list of
board-company pairs does not. Auto-adding *random* boards is the firehose
`TODO.md` rejects: it grows the pipeline's noise (more pending jobs to
LLM-process, more dashboard clutter) without growing relevant supply. So the
project keeps two lists and lets the program join them. **You choose the
companies; the program finds their boards.**

| List | Where | Who keeps it |
|---|---|---|
| **Companies** | `companies.txt` (gitignored; copy `companies.txt.example`) | You: the employers you want to follow, no board named |
| **Boards** | the registry in code (`RESOLVABLE`, `src/config.py`), switchable with `resolve.boards` in `config.yaml` | The project: the board types it can read (Greenhouse, Lever, Ashby, Workable; Workday from a URL) |

`make resolve` checks every company against every board and writes the result to
a generated **board map**, `data/board_map.yaml`. `make scrape` reads the map as
well as `sources.txt`. Boards you list in `sources.txt` by hand are **pinned**:
a pinned board always wins over the map, and the Adzuna lines stay in
`sources.txt`.

## 1. Companies and the board map (`make resolve`)

```
# companies.txt
Canva
Atlassian
Commonwealth Bank | cba, https://cba.wd3.myworkdayjobs.com/CommBank_Careers
Flinders University | flinders
- ashby amp
```

One employer per line; blank lines and `#` comments (whole line or trailing) are
ignored. Names may contain apostrophes (`Moody's`). Duplicate names are an error.

- **Name**: turned into board tokens by `slug_variants()` (`discover.py`): it
  drops "Pty Ltd" and similar, and tries a joined and a hyphenated form
  (`Foo Bar` gives `foobar` and `foo-bar`).
- **Aliases** after `|`, comma-separated. A plain word is an extra token to try,
  used exactly as written (Workable accounts are case-sensitive); you need one
  when the board token is not the name (`cba` for Commonwealth Bank). A careers
  URL is a **known board** for that company. This is how a Workday board gets
  in, since its data centre and site cannot be guessed from a name.
- **Block line** (`- <sources line>`): never use this board, for any company.
  Use it for a token that belongs to someone else (see the limits below).

Turn the feature on in `config.yaml`:

```yaml
companies_file: "companies.txt"     # unset = the whole feature is off
resolve:
  boards: [greenhouse, lever, ashby, workable]   # default: every resolvable type
  recheck_days: 14      # placeholder until the maintenance cycle is decided
  stale_after: 3        # separate days of "not here" before a mapped board is gone
# board_map_file: "data/board_map.yaml"          # default
```

Then:

```bash
make resolve                   # check the companies that are due, save the map
python -m src.ingestion.resolve --dry-run       # print the report, save nothing
python -m src.ingestion.resolve --company "Canva"   # one company, due or not
python -m src.ingestion.resolve --all --limit 5     # every company, at most 5
make validate                  # lists pinned and mapped boards
make scrape                    # scrapes pinned boards plus the map's live boards
```

What a run does:

1. Loads `companies.txt` and the map; drops map entries for companies no longer
   in the file.
2. Picks the companies that are **due**: not in the map yet, flagged for a
   check, or last checked more than `recheck_days` ago.
3. For each, tries every enabled board type with every token (aliases as
   written, then the name's slugs), plus each URL alias. Blocked boards are
   skipped; a board already pinned in `sources.txt` is reported as `pinned` and
   not added to the map.
4. Checks each candidate with `validate_source()` (the same call `make validate`
   makes, without the per-posting detail requests), pausing between requests.
5. Records the answers: **live** (has postings, scraped), **empty** (the board
   answers with no postings, not scraped), **gone** (was in the map and now does
   not answer, not scraped, kept so the report can say where a company moved
   from). Every company is checked against every board and all live boards are
   kept; it does not stop at the first hit.
6. Prints a report, one row per board with a flag (`NEW`, `MOVED`, `BACK`,
   `EMPTY`, `GONE`, `NOT FOUND` for a URL alias that did not answer, `UNREACHABLE`
   for a board that could not be asked). Each new
   board is followed by three sample postings, so a wrong company stands out.

**New matches go live without approval.** This replaces the old
propose-then-approve rule, which was written against auto-adding boards of
companies nobody chose. Here you choose the companies, so adding their boards is
the point. The safeguards are the report (every new board is flagged, with
samples) and the block line, which removes a wrong match for good.

The map is a YAML file the program rewrites on every save: comments and key
order do not survive, so do not edit it. Put names, aliases and block lines in
`companies.txt` instead. It is read and written only by
`src/ingestion/board_map.py`. With `companies_file` unset the map is ignored even
if it exists.

Cost: (2 slugs + aliases) x 4 boards requests per company, most of them quick
404s. Twenty companies is about 160 requests on the first run and almost none
after that until a company is due again. These are the same public endpoints
the scrapers use, with the same polite User-Agent and backoff.

`make resolve` is run by hand. There is no scheduled run: how often boards and
postings are re-checked belongs to a later maintenance cycle, so `recheck_days`
is one config value that work can change.

### What this can and can't do (honest limits)

- **A token can belong to someone else.** `amp` on Ashby is not the Australian
  AMP. The check proves a board exists, not whose it is. The sample postings in
  the report and the block line are the defence.
- **Token is not the name.** A company whose token differs from its name slug is
  missed until you add an alias or a careers URL. Workday is always in this
  group.
- **Empty is ambiguous.** An empty Workable account can be a real employer with
  no openings or an abandoned account. Either way it is not scraped.
- **A move is noticed after `stale_after` days, and only if the old board dies
  or empties.** A company that leaves its old board up and populated looks fine
  until its next routine re-check (`recheck_days`) finds the second board.
- **A returning board only restores matching rows.** When a retired board comes
  back, an archived posting is restored only if it passes your filters; the rest
  stay in the archive.
- **A move starts again from scratch.** The postings on the new board are new
  rows with new ids, so status and generated letters do not carry over.
- **Don't run `make resolve` and `make scrape` at the same time.** Both rewrite
  `board_map.yaml`. The atomic save prevents a broken file, not a lost update.

### Noticing a move

`make scrape` tells the map how each mapped board answered. For a board that
came from the map (never a pinned one):

- **postings returned**: the board's miss count goes back to 0.
- **a definite "not here"**: an HTTP 404, 410 or 422, or a scrape that returned
  no postings. This counts as one miss, **at most once per board per calendar
  day (UTC)**. After `resolve.stale_after` such days in a row the board becomes
  `gone`: it is no longer scraped, its stored postings are retired (marked dead
  with the reason `board retired`, then moved to the archive like any dead
  posting), and its company is flagged so the next `make resolve` checks it
  against every board again. If it turns up elsewhere the report says `MOVED`.
- **anything else is not a miss**: a timeout, a connection error, a server
  error, or a scraper's own error (such as a Workday board over its 2,000
  posting limit). The scrape logs it and leaves the counts alone, so one
  platform being down for a week retires nothing.

Because only a definite answer counts, and only once a day, three scrapes in one
afternoon never retire a board: `gone` means three separate days of the board
saying it is not there.

`make resolve` follows the same rule when it re-checks: a board that answers
404, 410 or 422 (or has no postings) is marked `gone` or `empty`, while one that
could not be asked (flagged `UNREACHABLE` in the report) keeps its status.

**Removing a company.** Deleting a company from `companies.txt` drops it from the
map at the next `make resolve`, and the stored postings of its boards are
retired (reason `company removed`; not on `--dry-run`). A board that is also
pinned in `sources.txt`, or that another company in the map still uses, is left
alone.

## 2. Adding a board by hand (always available)

To pin a board, put a line in `sources.txt`. The most precise way is to paste
its careers URL; the parser detects the ATS + token for you
(`source_from_url`, `src/config.py`):

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

A pinned board that is also in the map is scraped once, as the pinned line.

Workday boards cannot be found from a name: their data-center subdomain (`wd5`)
isn't derivable from one, so they come from a pasted careers URL (in
`sources.txt`, or as a URL alias in `companies.txt`) or the three-token form
(`workday cba wd3 CommBank_Careers`). See [`docs/workday.md`](./workday.md) for
the limits (one site per tenant, boards of 2,000+ postings can't be read).

## 3. Proposals from your Adzuna results (`make discover`)

The older, propose-then-approve route. Your search source (Adzuna) already
surfaces companies hiring your exact roles. `make discover`
(`src/ingestion/discover.py`):

1. Reads the distinct **companies** from Adzuna postings already in your DB.
2. Slugifies each name into candidate board tokens (the same `slug_variants()`).
3. Builds Greenhouse/Lever/Ashby/Workable candidates and **validates each live**,
   skipping boards you already have.
4. Writes the confirmed-live ones as **commented proposals** to
   `sources.discovered.txt`, matches first.

Nothing is added from here automatically: you review, uncomment the keepers and
move them into `sources.txt`, or add the company names you want to
`companies.txt` and let `make resolve` find them.

```bash
make scrape       # populate Adzuna companies first (if you haven't)
make discover     # writes sources.discovered.txt
# review it, uncomment the boards worth keeping, paste them into sources.txt
make validate     # sanity-check the merged list
```

Discovery has the same limits as resolving (no false positives, plenty of false
negatives, and a token can belong to another company), is only as good as your
Adzuna data, and makes live API calls (companies x ~2 slugs x 4 boards; bound it
with `--limit N`).

## 4. Maintenance: keep the list from rotting

Tokens go stale over a months-long hunt. `make validate` reports `match` / `live`
/ `dead`. For pinned boards, re-run it periodically and drop the dead ones:

```bash
python -m src.ingestion.validate --out sources.txt   # rewrites with live boards only
```

`--out` writes the **pinned** sources only (never the board map's boards, which
would otherwise be copied into `sources.txt` and pinned). For mapped boards,
re-run `python -m src.ingestion.resolve --all` (or just wait for a company to become due).

(Run validation where the network reaches the ATS APIs — i.e. your machine.)
