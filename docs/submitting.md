# Submitting applications (manual)

Job Hunter AI finds postings and drafts materials. It does **not** submit
applications — submission almost always needs authentication, heterogeneous
web forms, file uploads, and free-text answers, and full automation both
violates most job-board ToS and backfires (recruiters flag identical
cover letters and burst-timing submissions). See the "automate applying"
discussion and `TODO.md`: the tool automates *up to* the submit; you press the
button.

This doc is the manual playbook — the submission flow differs by platform, so
here's what to expect for each, what to have ready, and the gotchas.

## The workflow

1. In the dashboard (`make app`), open a job. Its `url` deep-links to the
   posting / application.
2. Have your **application packet** ready:
   - Resume as a clean PDF (named e.g. `Firstname-Lastname-Resume.pdf`).
   - The tailored cover letter from `make process` (shown in the dashboard —
     copy it).
   - Stock answers you'll reuse: work authorization / visa / sponsorship,
     salary expectation, notice period, location / remote preference, and your
     LinkedIn + portfolio/GitHub URLs.
3. Open the posting URL, follow the platform's flow below, submit.
4. Back in the dashboard, move the job `To Apply` → `Applied`.

Tip: keep the stock answers in one place (a notes file or password manager) —
the same five or six questions recur on almost every form.

## Per-platform

### Greenhouse  (`boards.greenhouse.io/<co>` or the company careers page)

- **Account:** usually none — a single-page form.
- **Flow:** Apply → name, email, phone → upload resume → paste cover letter →
  LinkedIn/website → company-specific screening questions (free text) → work
  authorization / sponsorship → optional EEO/voluntary self-identification →
  Submit.
- **Gotcha:** resume auto-parse is weak; double-check any pre-filled fields.
  Screening questions vary a lot per company — that's where the drafted answers
  save time.

### Lever  (`jobs.lever.co/<co>/<id>`)

- **Account:** none.
- **Flow:** "Apply for this job" → upload resume (Lever parses it to pre-fill
  name/email/etc — **review the autofill**, it's often slightly wrong) → add
  profile links (LinkedIn) → "Additional information" custom questions → Submit.
  Some boards offer "Apply with LinkedIn".
- **Gotcha:** the autofill feels done but frequently mangles one field; scan
  before submitting.

### Ashby  (`jobs.ashbyhq.com/<org>/<id>`)

- **Account:** none.
- **Flow:** modern multi-step (still no login) → resume upload + autofill →
  questions → review → Submit. Often shows the compensation range up front.
- **Gotcha:** some Ashby boards carry a long list of custom questions; budget a
  bit more time for those.

### Workday  (`<co>.wdN.myworkdayjobs.com/...`)

- **Account:** **required, and separate per company.** This is the painful one.
- **Flow:** create account (email + password) → multi-step wizard: My
  Information → Experience (resume upload + parse, then usually re-checking or
  re-entering roles by hand) → Application Questions → Voluntary Disclosures →
  Self-Identify → Review → Submit.
- **Gotcha:** budget 15–20 min each. The resume parser fills the experience
  section messily — expect cleanup. Save each company's login to a password
  manager; you'll likely return.

### SEEK  (`seek.com.au`)

- **Account:** SEEK account for in-platform "Quick Apply".
- **Flow:** two kinds of listing:
  - **Quick Apply** — apply within SEEK using your profile + uploaded resume +
    optional cover letter.
  - **External apply** — SEEK hands you off to the employer's own site/ATS;
    you then follow that ATS's flow above (often Greenhouse/Workday).
- **Gotcha:** many SEEK results redirect externally, so you frequently end up on
  one of the ATSs anyway.

### LinkedIn  (not scraped by this tool, but you'll hit it)

- **Account:** LinkedIn.
- **Flow:** **Easy Apply** (fast, in-platform, uses your profile + a stored
  resume) vs **Apply** (redirects to the company site/ATS).
- **Note:** this project deliberately doesn't scrape LinkedIn (auth + ToS), but
  a role found elsewhere may also live here.

### Company custom sites / email

- **Account:** varies.
- **Flow:** some companies run their own form; others just say "email your
  resume and cover letter to careers@…". This is exactly where the generated
  **cold email** (from `make process`) and cover letter earn their keep.

## At-a-glance

| Platform | Account? | Resume autofill | Typical time | Main gotcha |
|---|---|---|---|---|
| Greenhouse | No | Weak | 5–10 min | Per-company screening questions |
| Lever | No | Yes (review it) | 5 min | Autofill mangles a field |
| Ashby | No | Yes | 5–10 min | Sometimes many custom questions |
| Workday | **Yes, per company** | Messy | 15–20 min | Separate login + manual cleanup |
| SEEK | For Quick Apply | Profile-based | 3–10 min | Often redirects to an ATS |
| LinkedIn | Yes | Profile-based | 2–5 min (Easy Apply) | "Apply" redirects out |
| Custom / email | Varies | — | Varies | No standard; read the posting |

## After submitting

Update the job's status in the dashboard (`To Apply` → `Applied`). When the
match-scoring / application-packet features land (see `TODO.md`), the goal is
that everything up to this point — the right jobs, the resume, the tailored
letter, and drafted answers — is prepared for you, and submitting stays a
deliberate, reviewed human action.
