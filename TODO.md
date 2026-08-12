# TODO

Working tracker for what's left and what's next. Kept updated as things change.
(This file is gitignored on purpose.)

Status key: `[ ]` not started · `[~]` partial · `[x]` done (kept briefly for context)

The pipeline works end to end (scrape → store → process → Streamlit app), now
across Adzuna (the live search source — SEEK's RSS feed is dead) + two ATSs
(Greenhouse, Lever), verified against live boards. Do "Up next" top to bottom,
then pull from the backlog.

---

## Process

- [~] **Stop committing straight to `main`.** Solo project, but branch-per-change
  is still the right habit — cheap insurance against a bad commit landing on the
  branch everything builds from. `git checkout -b <feature>` before the next
  round, merge back once a chunk is done and tested.
  - 2026-06-21: first feature branch `feat/ats-pipeline-expansion` created +
    pushed (the big batch: Ashby/Lever/Adzuna, discover, validate, contacts).
    The `checkout -b` crashed mid-write and truncated `.git/HEAD` + corrupted
    the index; repaired by hand (`git symbolic-ref HEAD`, rebuild index). Still
    open: open a PR and merge back to `main`, then keep the habit going.

---

## Up next (in priority order)

1. [ ] **Removing Jobs**: Jobs may not remain listed on the websites forever. 
    Another script needs to be included into the workflow that would check for 
    jobs that no longer exist and depopulate the DB.

1. [ ] **Adding jobs.**: Streamlit UI should allow adding jobs to `jobs.db`
   in order to facilitate cover letter and cold email generation. Most of 
   cover letters and cold emails will be for these kind of manually added 
   jobs found on linkedin/seek/naukri etc.

2. [x] **Paste-a-careers-URL source auto-detection.** DONE — `source_from_url()`
   in `src/config.py` detects ATS + token from a pasted careers URL, wired into
   `sources_file` line parsing (`_source_from_line`). Covers greenhouse (both
   hosts), lever, and ashby; scheme optional; deep links resolve to the org
   token; unknown host → clear `ConfigError`. 12 new tests in
   `tests/test_sources_file.py`. *(original note below)*

   **Paste-a-careers-URL source auto-detection.** The painless way to add
   more boards: detect the ATS + token from a pasted careers URL instead of
   needing to know both. `boards.greenhouse.io/x` or `job-boards.greenhouse.io/x`
   → `greenhouse x`; `jobs.lever.co/x` → `lever x`; unknown host → a clear error.
   Wire it into `sources_file` parsing and/or `config.yaml`. This is the answer
   to "I want more boards" — far better than scraping a 20k-company token list
   (slow, noisy, mostly irrelevant; see the boards discussion).

3. [x] **`sources.txt` created + wired.** Now holds 19 ATS boards (17 Greenhouse,
   2 Lever) + seek, each confirmed live on 2026-06-20; `config.yaml` points at it
   via `sources_file: "sources.txt"` (the two inline ATS sources moved into it).
   `sources.candidates.txt` holds a wider, unconfirmed pool to re-validate.

4. [x] **Switch Adzuna dedup key to Adzuna's own `id` field (with URL fallback).**
   DONE (2026-06-22). The scraper now keys each job via `dedup_key(entry)` in
   `src/ingestion/adzuna.py`: `id_dedup_key(id)` (= `adzuna-<sha1(id)[:10]>`)
   when the result carries an `id`, else `url_dedup_key(redirect_url)` (the old
   scheme+host+path hash) as a belt-and-suspenders fallback. Same
   `adzuna-<sha1>[:10]` 17-char shape as every other scraper, so the source
   prefix and id length are unchanged (`_source_of` / the dashboard LIKE filters
   are unaffected).
   - **Docs-verified instead of live-eyeballed.** Adzuna's Search ads reference
     (developer.adzuna.com/docs/search) shows `id` as a top-level string field on
     every Job result in both worked examples (`"id":"129698749"` /
     `"126977586"`), and it equals the id in the redirect_url path
     (`.../land/ad/<id>`). `adref` is NOT in the search response schema — the
     "id vs adref" worry was unfounded for this endpoint. So the fallback covers
     the documented-as-rare empty-`id` case; a live eyeball is now confirmation,
     not a blocker.
   - **Migration handled (no re-insert).** `_migrate_adzuna_dedup_keys()` in
     `src/storage/database.py`, wired into `init_db()` next to
     `_migrate_sqlite_columns`, re-keys legacy `adzuna-` rows IN PLACE on the next
     run — preserves PK / status / generated materials, collapses a legacy+new
     duplicate onto the id-keyed row, idempotent, SQLite-only. Derives the id
     from the stored URL via `adzuna_id_from_url()`; rows it can't derive an id
     for are left alone.
   - **Tests written:** id-preferred / url-fallback / uniform format /
     `adzuna_id_from_url` extraction in `tests/test_adzuna.py`; in-place re-key /
     idempotency / collision-collapse / init_db-triggers-it in new
     `tests/test_db_migration.py`.
   - **Caveat — NOT run in-session (same old story):** the build sandbox mount
     served truncated copies of the source files (even unedited ones like
     `config.py`) and then the VM dropped, so `make test` / `make lint` could not
     run here. Logic verified by inspection; re-run both on a real machine to
     confirm green before trusting it.

   *(original plan below)*

   **Switch Adzuna dedup key to Adzuna's own `id` field (with URL fallback).**
   HIGH PRIORITY. The current `job_board_id` is `adzuna-<sha1(scheme+host+path of
   redirect_url)>` (`_dedup_key()` in `src/ingestion/adzuna.py`). That works but
   *assumes* the job's identity lives in the URL path and the query string is
   disposable — if Adzuna ever changes the redirect URL shape, dedup silently
   breaks (splits real dupes or, worse, merges distinct jobs). Each Adzuna result
   already carries Adzuna's own primary key, the `id` field, which is stable
   regardless of which search surfaced the ad — the robust dedup key. Plan:
   - Use `entry["id"]` for the key (`adzuna-{id}`, optionally hashed for
     uniformity with the other scrapers' `<source>-<sha1>` ids), falling back to
     `_dedup_key(redirect_url)` when `id` is missing/empty. Belt-and-suspenders,
     so it complements rather than replaces the URL normalization.
   - Blocked on one live check first: confirm each Adzuna result actually has a
     populated `id` (vs `adref`) — couldn't verify from the build sandbox (no
     network). Eyeball one real response after a scrape, then wire it up.
   - Note the one-time migration wrinkle: existing `adzuna-` rows keyed by the
     old URL hash get re-inserted once under the new scheme unless migrated.

4. [ ] Then pull from the backlog below.

---

## Backlog

### Scrapers / sources

- [~] **Ashby scraper.** Code done — `src/ingestion/ashby.py` (3rd ATS). Public
  board API (`api.ashbyhq.com/posting-api/job-board/<org>`); skips unlisted
  postings and folds the `isRemote` flag into the location so the remote-passes
  filter works. Wired into config (`AshbySource`), the URL auto-detect
  (`jobs.ashbyhq.com/<org>`), the planner, sources files, examples + docs.
  - **Open / why it's not actually usable yet:** `sources.txt` has ZERO `ashby`
    boards (and `sources.candidates.txt` has none either) — the engine runs but
    has nothing to point at. This is the "no boards" gap.
  - **Live status unproven:** "verified against Ashby's own board" can't be
    re-confirmed from the sandbox (no ATS network — `api.ashbyhq.com` 403s
    through the proxy). Re-verify on a real machine with `make validate` before
    trusting it.
  - Next: add a few real Ashby orgs to `sources.txt`, run `make validate`, then
    flip back to `[x]`.
- [~] **Workday scraper — scoped, not built** (`docs/workday.md`). The channel
  most likely to surface local AU/India roles, but the hard ATS: 3 identifiers
  (tenant/dc/site), POST + pagination + N+1 descriptions, needs an `http_util`
  POST helper, and its API shape couldn't be live-verified from the build sandbox.
  First step is a one-board verification spike on a real tenant. Workday is also
  manual-add only (data-center subdomain isn't name-derivable).
- [~] **Board discovery — `make discover`** (`src/ingestion/discover.py`,
  `docs/board-discovery.md`). SEEK-driven, propose-then-approve: slugifies the
  companies in your SEEK results, validates Greenhouse/Lever/Ashby candidates
  live, writes commented proposals to `sources.discovered.txt`. No false
  positives, many false negatives (only finds token==name-slug; excludes
  Workday). 8 tests in `tests/test_discover.py`. Open: a weekly scheduled
  re-validate to prune dead boards (run on your machine — sandbox has no ATS
  network).
- [x] **SEEK is region-scoped; India = ATS + filter.** `SeekSource` gained an
  optional `locations:` (AU/NZ search scope, falls back to `filters.locations`)
  so SEEK doesn't run pointless searches for non-AU cities. Decided against a
  Naukri/Indeed source: neither has a public feed (only ToS-violating/paid
  scrapers), so India coverage is ATS boards (Greenhouse/Lever/Ashby) + the
  global location filter, by design. Documented in `docs/configuration.md`.
- [ ] More scrapers beyond SEEK / Greenhouse / Lever / Ashby as needed (Workday).
- [x] **SEEK disabled — RSS feed is dead.** Confirmed: every SEEK scrape dump is
  empty (0 jobs) across all queries while ATS dumps are MBs. No in-scope fix
  (HTML/internal-API scraping crosses the ToS line). Commented out in sources.txt;
  was costing ~52 dead HTTP calls per scrape.
- [x] **Adzuna scraper — SEEK's replacement** (`src/ingestion/adzuna.py`).
  Sanctioned search API, free tier, per-country so it reaches India too. Search
  source like SEEK (planner pairs locations to country via `country_of`); creds
  in top-level `adzuna:` block, threaded through `plan_scrapes`. Revives
  `make discover` (now mines seek-/adzuna- rows). 6 tests in `tests/test_adzuna.py`.
  Limit: description is a snippet only.
  - 2026-06-21: **LIVE.** Free app_id/app_key registered + added to the
    `adzuna:` block in `config.yaml` (gitignored); `adzuna au` / `adzuna in`
    uncommented in `sources.txt`. Confirmed returning jobs against the live API.
    This is now the working search source that replaces the dead SEEK feed, and
    it reaches AU + India.
  - 2026-06-21: **dedup hardened + location list de-aliased.** `job_board_id`
    now hashes only scheme+host+path of `redirect_url` (new `_dedup_key()`),
    dropping the per-search query string (se/utm/where) so the same ad fetched
    under different searches collapses to one row instead of N. Full URL still
    stored for the link. 2 new tests in `tests/test_adzuna.py`. Also trimmed
    `filters.locations` to canonical names only (dropped Bangalore→Bengaluru,
    Gurgaon→Gurugram, Bombay→Mumbai, + a duplicate Mumbai) so Adzuna isn't
    billed twice for one city. Open: (a) re-run `make test` on a real machine —
    sandbox mount was stale, new tests verified standalone only; (b) confirm one
    live `redirect_url` keeps the job id in the PATH (the normalization's
    assumption); (c) consider keying off Adzuna's own `id` field instead of the
    URL for a parse-free, fully robust dedup key (see below).
- [x] **Board validator** — `make validate` / `src/ingestion/validate.py` checks
  tokens against the live APIs (match / live / dead), reusing `plan_scrapes` +
  `job_matches`; `--out` writes a clean `sources.txt`, `--require-match` keeps
  only boards with a current matching role. 7 tests in `tests/test_validate.py`.
  Chose curated-and-validated over a scraped aggregator dump (the firehose is
  slow/noisy/mostly-irrelevant for a role-targeted hunt).
- [~] (Partly addressed) the ATS-board supply skews remote/global; local AU/India
  data roles now come from Adzuna (live, reaches AU + India). A Workday/bespoke
  scraper is still the way to widen local coverage further, but it's no longer the
  only option (SEEK is dead, so it's no longer a fallback).

### LLM layer

- [x] **Batch LLM processing.** `make process` can now bound a run via
  `llm.batch_size` — a `LIMIT` on `pending_llm_jobs()`. Default `0` = unbounded
  (whole queue), so behaviour is unchanged unless you opt in; a positive value
  is for unattended/cron runs that should stop and free the GPU. Companion
  `count_pending_llm_jobs()` lets the CLI log "processed N of M, K remaining" so
  a bounded pass isn't mistaken for an empty queue.
  - 2026-06-21: incremental save is what actually delivers interruptibility
    (each job commits independently; Ctrl-C is now caught and exits cleanly with
    a progress line). Batching is the *opt-in* stop-point on top of that — not a
    throughput feature. Concurrency was considered and dropped: single local GPU
    serializes inference, so parallel requests just queue at the device. Not
    extended to `pending_contact_jobs` — that path is pure regex over stored
    text, no GPU/network to free.
- [x] **Error handling / retry around LLM calls.** `OllamaClient.generate()`
  retries transient `requests` failures with exponential backoff
  (`llm.max_retries=2`, `llm.retry_backoff=1.0`). If a call still fails after
  retries, the CLI does `session.rollback()` (drops the job's partial state so
  the next commit can't flush it) and skips to the next job — one bad job no
  longer aborts the batch. Both materials still generate before the single
  per-job commit, so a row never leaves the queue half-done.
- [x] Keep the client API open: `LLMClient` ABC + `get_llm_client()` factory
  unchanged; `OllamaClient` stays the only implementation (retry params threaded
  through the factory).
  - 2026-06-21: tests added — `tests/test_llm_queue.py` (limit/count) and
    `tests/test_llm_client.py` (retry/backoff, factory wiring), plus config
    field/validation cases in `tests/test_config.py`. Full suite: 126 passing,
    ruff clean. Caveat: sandbox mount was stale, so verified against a
    reconstructed copy — re-run `make test` on a real machine to confirm.
- [ ] Hosted-LLM option (OpenAI-compatible API) — would add `llm.api_key`
  (makes `config.yaml` genuinely sensitive; already gitignored).
- [ ] **CI coverage for the LLM layer.** The tests now exist
  (`test_llm_queue.py`, `test_llm_client.py`, LLM cases in `test_config.py`) but
  only run if someone runs them — wire them into CI so the batching/retry
  behaviour can't silently regress. Low-effort: all mock `requests` and use temp
  SQLite, so nothing hits a live model. Also closes the recurring "sandbox mount
  was stale, re-run on a real machine" caveat by running the suite somewhere
  trusted on every push.
- [ ] **Verify `make process` edge cases.** Confirm two paths produce sane
  output rather than a stack-trace wall: (a) empty queue (0 pending jobs) and
  (b) Ollama totally unreachable so *every* job fails its retries — should log a
  clean per-job skip + the final "N generated, K failed, M pending" summary and
  exit 0, not crash. Cheap to check, embarrassing if wrong.
- [ ] Fill in a real `resume_summary` in `config.yaml` (placeholder gives
  generic output). User task, not code.
- [ ] **Validate individual jobs** Validator needs to check if each individual 
  links are live (if 404 found, then remove). Currently it only checks if 
  boards are live which is assumed to be true.

### Features

- [~] **Hiring-manager lookup.** v1 shipped: `make contacts`
  (`src/contacts/`) extracts a contact from each posting's *own published text*
  only — a printed email, or a name + the company's printed domain → one flagged
  `pattern-guess`. No brokers, no login (design + ethics:
  `docs/hiring-manager-lookup.md`). Feeds the cold-email greeting and shows in the
  dashboard with its confidence. Deliberately narrow — most postings yield
  nothing or a generic `careers@`. Open follow-ups: (a) optionally derive a domain
  when the posting prints none (needs the careers-URL work / a non-broker source —
  the load-bearing open question in the design doc); (b) `contact_confidence` as
  enum/CHECK, folded into the `status` enum TODO.
- [ ] Generated-material history (`GeneratedMaterial` table) instead of
  overwrite-in-place on re-run. Open question whether it's worth it.
- [ ] `status` as enum / CHECK constraint (free-text today, enforced only by the
  Streamlit dropdown).

### Security & infrastructure

- [ ] **Security hardening (ongoing).** Baseline done (dashboard bound to
  localhost). Next: a committed `.streamlit/config.toml` so the binding isn't
  flag-dependent (note `.streamlit/` is gitignored); dashboard auth; secrets
  handling once a hosted LLM / API key is in play.
- [ ] **Real Postgres verification** — only if ever needed. Pin a driver
  (`psycopg`), run the suite against live Postgres, audit SQLite-isms. Until then
  it's a documented open-but-unimplemented escape hatch. SQLite is the product.
- [ ] UI test harness for `app/main.py` (no automated coverage of the Streamlit
  layer — the `import src` bug slipped through because of this).

### Misc
- [ ] **Scope change** — Current architecture allows personalised on-device 
  everything. Having a centralised database and users referencing to that db for 
  accessing job postings. This opens a whole new can of worms though. Not user 
  how plausible making an entire service would be. Also, probably some legal and
  ethical issues.

---

## Done recently (context, prune when stale)

- [x] **config.yaml migrated to the `filters` + `sources` schema** and ver

