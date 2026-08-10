# Hiring-manager lookup (design + v1)

> Status: **v1 implemented.** `make contacts` (`src/contacts/`) ships the
> in-posting-text path below: it reads only what the company already published
> in the listing, adds the three `JobPost` columns via a tiny SQLite migration,
> feeds the cold-email greeting, and shows the result + confidence in the
> dashboard. Tests in `tests/test_contacts.py`. What's **not** built is noted in
> "Open questions" — chiefly deriving a domain when the posting prints none.

## The goal, and why it's harder than it sounds

The cold-email template (`src/llm/prompts.py`, `COLD_EMAIL_TEMPLATE`) writes "to a
hiring manager at {company}" but there is no recipient — the generated email has
no `To:` line. The point of this feature is to fill that line: find a plausible
contact for a posting so the cold email is actually targeted rather than
theoretical.

The catch is that the goal sits in tension with the project's own ethic
([`docs/scrapers.md`](./scrapers.md) → "Scope"): **public, non-authenticated sources only; no
login, no anti-bot bypass.** That ethic is what rules out a `LinkedInScraper`,
and it rules out most of what would make this feature *easy*:

- **The ATS APIs we already scrape don't expose contacts, by design.** Greenhouse
  and Lever return job fields, not the hiring manager's email. Companies route
  applications through an ATS *specifically* so candidates don't cold-email the
  hiring manager. Our best existing data source structurally can't help here.
- **The sources that do have individual emails are off-limits or dirty.**
  LinkedIn is behind auth (already out of scope). Hunter.io, Apollo, RocketReach,
  ZoomInfo, Clearbit are data brokers — "it's an API on our side" doesn't fix
  that the data is scraped personal information of provenance we can't audit, and
  wiring one in forces an API key into `config.yaml` (the "genuinely sensitive
  config" path `TODO.md` is wary of). See "Out of scope" below.

So the feature cannot honestly be "find the hiring manager's email." What's
achievable from clean public sources is weaker, and the design names that
honestly rather than pretending otherwise.

## Decision: in-scope sources

Two clean public signals, combined into a **candidate** contact that is always
flagged as unverified:

1. **Company email *pattern* inference.** From the posting's company domain
   (derivable from the careers/ATS URL or the company's own site), infer the
   common local-part pattern — `first.last@`, `flast@`, `first@`, etc. Patterns
   are a property of the *company*, not the person, so inferring one is not
   personal-data collection. The pattern can come from any non-broker public
   signal (e.g. a published `press@`/`careers@` address on the company site,
   which reveals the domain and sometimes the shape).
2. **A contact *name* from public, non-authenticated text.** Some postings name a
   recruiter or hiring manager ("Reporting to…", "Questions? Contact…"), and some
   company team/careers pages list names publicly. Where a name is present in
   public text, capture it.

Combine the two → a **candidate** address (`first.last@company.com`) with a
**confidence** marker. Never assert it's correct. If only the pattern is known
(no name), output the generic queue address the company already publishes
(`careers@`, `jobs@`) rather than guessing a person.

This is the option chosen because it stays consistent with the scraper ethic
instead of quietly breaking it.

## Out of scope (explicit, same spirit as `docs/scrapers.md`)

- **Data-broker / email-finder APIs** (Hunter, Apollo, RocketReach, ZoomInfo,
  Clearbit). Crosses the public-sources ethic on provenance grounds and forces a
  secret key into config.
- **LinkedIn or any logged-in source.** Same rule that excludes `LinkedInScraper`.
- **CAPTCHA / anti-bot bypass.** Same rule as the scrapers.
- **Scraping personal contact details at scale.** The feature surfaces, at most,
  one already-public name per posting plus a company-level pattern — not a
  harvested contact database.

If you fork and go further, that's your call and your risk — exactly the framing
`docs/scrapers.md` already uses.

## Be honest about reliability

A pattern-guessed address is a *guess*. It will sometimes bounce, sometimes land
in a catch-all, and occasionally reach the wrong person. Guessed-address cold
email also has mediocre ROI and can annoy recruiters who deliberately route
through the ATS. The feature's value is "a reasonable starting point you can
choose to verify," not "a verified inbox." The `confidence` flag and the
candidate framing exist so the UI and the user never treat a guess as a fact.

## Data-model change (proposed)

Three nullable columns on `JobPost` (`src/storage/models.py`) — additive, no
change to existing rows, consistent with the single-table model in
[`docs/data-model.md`](./data-model.md):

```python
contact_name: Optional[str] = None        # e.g. "Dana Lee", or None
contact_email: Optional[str] = None        # candidate address, or a published careers@ inbox
contact_confidence: Optional[str] = None   # "verified" | "pattern-guess" | "generic" | None
```

`contact_confidence` is deliberately free-text for now, mirroring how `status` is
handled today (enforced by the UI, not a DB `CHECK` — see `docs/data-model.md` →
"Known looseness"). A real enum/`CHECK` can come later alongside the existing
`status` enum TODO.

Why columns on `JobPost` rather than a new table: there's exactly one contact per
posting and no history requirement, so a one-to-many table would be premature —
same reasoning that's keeping `GeneratedMaterial` unbuilt.

## Where it plugs into the pipeline

The architecture ([`docs/architecture.md`](./architecture.md)) is `ingestion → storage → llm → app`,
with the database as the queue between stages. The lookup is a **new stage
between scrape and process**, queue-driven the same way `make process` is:

```
make scrape   →  JobPost rows (contact_* NULL)
make contacts →  fills contact_name / contact_email / contact_confidence   (proposed)
make process  →  cold email uses contact_* for the To: line + greeting
make app      →  shows the candidate contact + confidence, lets you edit/clear it
```

- **Queue signal:** rows where `contact_confidence IS NULL`, exactly mirroring how
  `pending_llm_jobs()` selects `generated_cover_letter IS NULL`
  (`src/storage/database.py`).
- **Pure-fetcher shape:** the lookup logic shouldn't touch the DB directly — it
  takes a `JobPost` and returns the three values, the CLI writes them back. This
  mirrors the `BaseScraper` contract (a scraper is a pure fetcher; the CLI
  persists).
- **Prompt change:** `COLD_EMAIL_TEMPLATE` gains an optional recipient. When
  `contact_name` is present, the email opens to that person; otherwise it falls
  back to today's generic "hiring manager at {company}." No call site changes —
  the template stays a format string (`src/llm/prompts.py` rationale).

Order matters: `make contacts` runs **before** `make process` so the cold email
can use the contact. If contacts are missing, `make process` still works with the
generic fallback — the stages stay independently re-runnable, per the queue
design.

## Ethics & legal boundary (state it in the doc, not just in code)

- Surface only what is already public and non-authenticated. No login, no
  brokers, no bypass.
- Company email *patterns* are company-level, not personal data. A single
  publicly-listed name is the most personal thing captured, and only when the
  source already published it.
- Never present a guess as verified — `contact_confidence` is mandatory and the
  UI must show it.
- This stays clear of the most fraught zone (bulk personal-data harvesting), but
  cold-emailing a guessed address still has GDPR/CAN-SPAM implications depending
  on jurisdiction; that's a user-judgment call the feature should not hide.

## Open questions to resolve before building

- **Domain resolution.** How reliably can we get a company's real email domain
  from an ATS posting? Greenhouse/Lever give a company *name*, not always a clean
  domain. Worth a small spike before committing to pattern inference.
- **Pattern source without a broker.** Is there a non-broker public signal good
  enough to infer the pattern, or does honesty mean we usually fall back to the
  generic `careers@` inbox? If it's mostly the fallback, the feature is thinner
  than it sounds — decide whether that's still worth building.
- **Verification step.** Do we ever attempt SMTP/MX validation of a candidate
  address, or is that over-reach / unreliable enough to skip? Default: skip.
- **`contact_confidence` as enum vs. free-text.** Fold into the existing `status`
  enum/`CHECK` TODO, or leave loose for now (current lean: leave loose).

## Not building yet

Per the scope decision, this is a design doc only. Next concrete step, if/when
greenlit, is the domain-resolution spike above — it's the load-bearing
assumption, and if it's weak the whole feature collapses to "generic inbox," at
which point it may not be worth the schema change.
