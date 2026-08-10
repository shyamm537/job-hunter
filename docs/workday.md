# Workday scraper (design)

> Status: **design only, nothing built.** Workday is the hardest ATS to add and
> bends several assumptions the Greenhouse/Lever/Ashby scrapers share, so the
> decisions are settled here first. Sections marked **(proposed)** are intent.

## Why Workday matters here

The other ATS scrapers skew remote/global tech, because that's who exposes
Greenhouse/Lever/Ashby public APIs. Many **local AU/India** data employers
(banks, consultancies, large enterprises) run **Workday** instead. So Workday is
the channel most likely to surface the local roles SEEK + the current ATS boards
miss. That's the whole reason to take on the extra complexity.

## Why it's not "one file like Lever"

Greenhouse/Lever/Ashby each take a **single token** and one **GET**. Workday
breaks all three of those:

1. **Three identifiers, not one token.** A board lives at
   `https://<tenant>.<dc>.myworkdayjobs.com/<lang>/<site>`, e.g.
   `acme.wd5.myworkdayjobs.com/en-US/Careers`. You need the **tenant** (`acme`),
   the **data-center subdomain** (`wd1`/`wd3`/`wd5`/`wd103`/…), and the **site**
   name (`Careers`). The data-center isn't guessable from the company name — you
   have to read it off a real careers URL.
2. **The jobs endpoint is a POST with a JSON body**, not a GET:

   ```
   POST https://<tenant>.<dc>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
   body: {"appliedFacets":{}, "limit":20, "offset":0, "searchText":""}
   ```

   `src/ingestion/http_util.py`'s `get_json()` only does GET, so this needs a
   `post_json()` sibling (or the scraper uses `requests` directly).
3. **Pagination.** The response carries `total` and up to ~20 `jobPostings` per
   call (`title`, `externalPath`, `locationsText`, `postedOn`, `bulletFields`).
   A full board is many POSTs (`offset += 20` until `offset >= total`).
4. **Descriptions are an N+1.** The list response has no full description; each
   posting needs a second GET to
   `…/wday/cxs/<tenant>/<site>/job/<externalPath>` to fill `JobPost.description`.
   A 200-job board is ~10 list POSTs + 200 detail GETs.

So Workday is a **multi-call, paginated, POST-based** scraper — fundamentally
chattier than the one-shot boards. The `BaseScraper` contract still holds (it
returns `List[JobPost]`), but the politeness story matters far more here.

## Verification gap (read this before trusting the design)

I could live-verify Greenhouse/Lever because they're GET endpoints. Workday is
**POST**, and its careers pages are JS-rendered, so the API shape above is the
**known public pattern, not something confirmed against a live tenant from this
environment.** Field names (`jobPostings`, `externalPath`, `locationsText`) and
the exact body schema must be checked against one real Workday board before the
scraper is trusted. That check needs a machine that can POST to the endpoint —
i.e. yours, not the build sandbox.

## Proposed shape

### Config (`src/config.py`)

A `WorkdaySource` with three fields — the union stops being "type + one token":

```python
class WorkdaySource(BaseModel):
    type: Literal["workday"]
    tenant: str        # acme
    datacenter: str    # wd5
    site: str          # Careers
```

`source_from_url()` needs a Workday branch: parse host
`<tenant>.<dc>.myworkdayjobs.com` (split tenant + dc off the host) and take the
**site** from the path, skipping the `en-US`-style language segment. This is more
involved than the current "first path segment is the token" rule. `source_to_line`
gains a `workday <tenant> <dc> <site>` form.

### Scraper (`src/ingestion/workday.py`)

`WorkdayScraper(tenant, datacenter, site)` implementing `scrape()`:
1. POST the jobs endpoint, paginating on `offset`/`total`.
2. For each posting, GET the detail endpoint for the description (or, v1, skip
   descriptions and store the list fields only — see open questions).
3. Map to `JobPost` with `workday-<sha1(url)[:10]>` ids, like the others.

### http_util

Add `post_json(url, body, …)` mirroring `get_json()`'s retry/backoff/polite-UA,
so Workday's POSTs get the same transient-failure handling. Same 4xx-is-fatal
rule (a 404 = dead tenant/site).

### Planner

One `elif source.type == "workday"` branch, `post_filter=True` like the other
ATS boards. Nothing in `base_scraper.py` or `cli.py` changes.

## Open questions to resolve before building

- **Descriptions: N+1 or skip?** Fetching every description doubles+ the request
  count and slows scrapes a lot. v1 option: store the list-level fields and a URL,
  fetch the description lazily only for jobs that pass the filter. (Filtering is
  on title/location, which the list response *has* — so we can filter *before*
  paying for descriptions. That's the efficient ordering.)
- **Rate limiting.** This is the first scraper that genuinely needs a throttle
  ([`docs/scrapers.md`](./scrapers.md) notes none exists yet). A per-request delay + the existing
  backoff is the minimum; Workday tenants can be touchy about burst traffic.
- **Data-center discovery.** Since `wd5` isn't derivable from the company name,
  Workday boards can only be added from a **pasted careers URL**, never from a
  bare name. This directly limits how much of the discovery automation
  ([`docs/board-discovery.md`](./board-discovery.md)) can reach Workday — name→token guessing can't work
  for it. Worth stating plainly: Workday boards are a manual-add path.
- **Field-shape verification** (above) — a one-board spike on your machine is the
  first concrete step, before any of this is written.

## Not building yet

Design only. The first move, if greenlit, is the verification spike: POST one
real Workday board's jobs endpoint, confirm the field names and body schema, and
measure how slow a full paginated + descriptions scrape actually is. If it's
acceptably fast and the schema matches, the scraper is a known quantity; if not,
the design above changes.
