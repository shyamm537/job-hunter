# Workday scraper

> Status: **built** (KAN-14), `src/ingestion/workday.py`. The API shape below was
> checked against live Workday tenants on 2026-10-03 (Commonwealth Bank, Bunnings,
> Flinders University and NVIDIA). Workday's careers API is **not a published
> contract**, so it can change without notice; the scraper raises rather than
> guess when it does.

## Why Workday matters here

The other ATS scrapers skew remote/global tech, because that's who exposes
Greenhouse/Lever/Ashby public APIs. Many **local AU/India** employers (banks,
universities, retailers, enterprises) run **Workday** instead. So Workday is the
channel most likely to surface the local roles Adzuna and the other boards miss.

## Adding a board

Paste the careers site's URL into `sources.txt`, or write the three values out.
All three are read off the careers URL, and none can be guessed from a company
name, so Workday boards are always added by hand:

```
https://cba.wd3.myworkdayjobs.com/en-US/CommBank_Careers     # pasted URL
workday cba wd3 CommBank_Careers                              # the same board, spelled out
```

`cba` is the **tenant**, `wd3` the **data centre** (`wd1`, `wd3`, `wd5`, `wd103`,
...), `CommBank_Careers` the **site**, whose case is kept as given. A leading
locale segment (`en-US`) and any deeper posting path are ignored. Then run
`make validate`: a wrong site is a 404 and an unknown tenant or data centre a 422,
and `validate` reports both as dead.

Before adding a tenant, open `https://<tenant>.<dc>.myworkdayjobs.com/robots.txt`
and check it has no rule against `/wday/`. The four tenants checked on 2026-10-03
allowed it (they only disallowed `/refreshFacet/` and a few private sites).

### Limits

- **One site per tenant.** A board is identified by its tenant (the stored
  `company`), so two sites on one tenant would be reconciled as one board and each
  scrape would mark the other's postings "gone from board". `plan_scrapes()`
  refuses such a config with an error naming both sites. This is a real limit:
  Commonwealth Bank runs four sites (`CommBank_Careers`, `Bankwest_Careers`, ...),
  Bunnings three, and Flinders two (its employment site and a casual register).
  Add the one you want.
- **Boards of 2,000 or more postings cannot be read.** Workday caps the reported
  `total` at 2000 and answers any `offset` of 2000 or more with the first page
  again, so a larger list can never be complete. The scraper raises for such a
  board (NVIDIA is one) instead of returning a list that merely looks complete,
  which would mark every posting beyond it dead. Boards seen: Bunnings 251, CBA
  167, Flinders 38.
- **Other host form.** Some tenants use `<dc>.myworkdaysite.com/recruiting/...`
  instead of `myworkdayjobs.com`. That form is not supported (the URL parser
  rejects it); none of the four tenants checked used it.

### Place names and your location filter

Workday boards name places their own way (`Sydney CBD Area`, `SA Western Area`,
`Support Office SA`, `Bedford Park / Kaurna Country`), and none of 456 postings
captured on 2026-10-03 said "Adelaide". A city-name `filters.locations` therefore
drops most of a board. Put each board's place names in `filters.board_locations`
(see [`configuration.md`](./configuration.md)): it applies to boards only, matches
whole words, and does not add Adzuna searches.

## How it works

### The calls

```
list    POST https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
        body {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""}
detail  GET  https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site><externalPath>
```

Both need only `Content-Type`/`Accept: application/json` and the project
User-Agent: no key, cookie, CSRF token or `Referer`. `src/ingestion/http_util.py`'s
`post_json()` and `get_json()` give them retries on timeouts and 5xx and treat a
4xx as fatal. The scraper sleeps `REQUEST_DELAY` (0.5 s) between requests.

### Reading the list

- **20 per page.** `limit` above 20 is an HTTP 400, so `PAGE_SIZE` stays at 20.
- **`total` is on the first page only**; later pages report 0, so it is read once.
- **A complete list or an error.** The scraper counts unique postings (by
  `externalPath`, so a posting seen twice counts once) and raises if it ends with
  fewer than `total`, if the board is at the 2,000 ceiling, or if a posting has no
  path. `scrape()` must return the whole board, because the scrape CLI treats
  anything missing as gone and marks it dead.
- **`postedOn` is ignored**: it is display text ("Posted Today"), not a date, and
  some tenants omit it.

### Descriptions: only for title matches

The list has no description, and one detail call per posting would be hundreds of
requests (a 200-posting board is ~10 list calls plus 200 detail calls). The
planner therefore gives the scraper `filters.titles` as `detail_titles`, and the
detail is fetched only for postings whose title matches one. Every other posting
is still returned, with an empty description, so the board reconciles correctly.
With **no** title filters set, no details are fetched at all (it would otherwise
mean every posting on the board).

From the detail call the scraper takes the HTML description (converted to text by
`html_to_text()`), the clean location plus `additionalLocations` (so a posting the
list shows as "2 Locations" gets its real places), and `startDate`, which is the
posting date to within a day. A failed detail call is logged and leaves the list
values: one bad posting never fails the scrape. A posting stored with an empty
description is filled in by a later scrape (`upsert_job`).

Known cost: the scraper cannot see the database, so it repeats the detail call for
every title match on every scrape, including postings already stored. The count is
bounded by the number of title matches (8 on Commonwealth Bank's 167 postings).

### The stored fields

| `JobPost` field | From |
|---|---|
| `company` | the tenant (the board token) |
| `url` | `https://<tenant>.<dc>.myworkdayjobs.com/<site><externalPath>`, no locale segment |
| `location` | the detail's `location` and `additionalLocations`, joined with `"; "`; without a detail, `locationsText` cut at the first `\|` (some tenants pack the job level and closing date after it) |
| `posted_at` | the detail's `startDate`; `None` without a detail |
| `description` | the detail's `jobDescription` as text; empty without a detail |

### The id

`job_board_id` is `workday-<sha1(<tenant>/<site>/<requisition id>)[:10]>`, **not**
a hash of the URL. The URL's path holds location and title slugs that can change
while the posting stays open, and an id that followed them would re-insert the
posting as new and lose its status and generated materials.

Every `externalPath` ends in `_<requisition id>`, sometimes with a `-N` repost
marker (`..._JR0000017701-1`; 122 of Bunnings' 251 postings have one). One of the
posting's `bulletFields` is the base id (`JR0000017701`; its position varies by
tenant), so the scraper looks for a bullet that equals the suffix or the suffix
minus `-N` and uses it. If none matches it uses the whole suffix unchanged. It never
removes a trailing `-N` by pattern: an id written `R-12345` would become `R`, and
every posting on the board would share one id. If two postings still map to the
same id the scrape raises, so fix the rule rather than skipping the board.

**Not yet verified:** that the `-N` value, or the path slugs, actually change when
a posting is edited or refreshed; that needs a second look at the same boards after
some days. Back-to-back scrapes were checked (456 postings, then 0 new and 0 gone).

## Not covered

- **`make discover`** does not look for Workday boards: the tenant, data centre and
  site cannot be derived from a company name.
- The facets in the list response (company, job family, time type, country and
  region) are ignored; filtering is done after fetching, like every other board.
