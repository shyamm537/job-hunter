"""Contact extraction from a posting — public, in-posting text only.

Scope (see docs/hiring-manager-lookup.md): this looks ONLY at text the company
already published in the listing itself (`JobPost.description`). It makes no
network calls, logs into nothing, and queries no data broker. The two clean
signals it uses:

1. **Published email addresses** in the posting (`careers@`, `jobs@`, or a named
   person's address the company chose to print). Confidence "published".
2. **A contact name** next to a cue phrase ("contact ...", "reach out to ...").

From those two it may form ONE marginal guess: if the posting published a
company domain (via any address) AND a person's name, but no direct address for
that person, it guesses `first.last@domain`. Confidence "pattern-guess" — a
guess, flagged as such, never asserted. The domain is never invented; it only
ever comes from an address the company itself printed.

Everything is a pure function: `find_contact(job)` returns a `ContactResult`.
The CLI (`src/contacts/cli.py`) is what writes it back, mirroring how scrapers
stay pure fetchers and the CLI persists.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

# Generic, role-based local-parts. A published role inbox is a perfectly good
# contact, but it's not a *person*, so we still try to personalize when we also
# found a name.
ROLE_LOCALPARTS = {
    "careers", "career", "jobs", "job", "recruiting", "recruitment", "recruiter",
    "talent", "hr", "people", "hiring", "apply", "applications", "work",
    "info", "hello", "contact",
}

# Permissive but standard email shape. Lower-cased before matching.
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")

# The cue is case-insensitive (scoped `(?i:...)`), but the captured name stays
# case-sensitive so a capitalised non-name ("Data Engineer") isn't grabbed.
_NAME_CUE_RE = re.compile(
    r"(?i:contact|reach out to|reach|speak (?:to|with)|questions?(?: to)?|"
    r"reporting to|reports? to|hiring manager(?: is)?|recruiter(?: is)?|"
    r"get in touch with|email)\s*:?\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+))",
)


@dataclass
class ContactResult:
    """Outcome of a lookup. `confidence` is always set (incl. 'none' on a miss),
    so the row leaves the queue and isn't retried forever."""

    name: Optional[str]
    email: Optional[str]
    confidence: str  # "published" | "pattern-guess" | "none"


def _find_emails(text: str) -> List[str]:
    """All published addresses, de-duplicated, order preserved."""
    seen: List[str] = []
    for match in _EMAIL_RE.findall((text or "").lower()):
        if match not in seen:
            seen.append(match)
    return seen


def _local_part(email: str) -> str:
    return email.split("@", 1)[0]


def _is_role_inbox(email: str) -> bool:
    return _local_part(email) in ROLE_LOCALPARTS


def _find_name(text: str) -> Optional[str]:
    match = _NAME_CUE_RE.search(text or "")
    return match.group(1).strip() if match else None


def _guess_local_part(name: str) -> Optional[str]:
    """`First Last` -> `first.last`. The most common corporate pattern; still a
    guess. Returns None if the name doesn't yield two alpha parts."""
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in name.split()]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[-1]}"


def find_contact(job) -> ContactResult:
    """Best ethically-sourced contact for a posting, from its own text.

    Priority:
    1. A published *personal* address (named inbox)         -> "published"
    2. Name + a published domain, no personal address       -> "pattern-guess"
    3. A published *role* inbox (careers@, jobs@, ...)       -> "published"
    4. Nothing                                              -> "none"
    """
    text = job.description or ""
    emails = _find_emails(text)
    name = _find_name(text)

    personal = [e for e in emails if not _is_role_inbox(e)]
    role = [e for e in emails if _is_role_inbox(e)]

    # 1. A real published personal address beats everything — no guessing needed.
    if personal:
        return ContactResult(name=name, email=personal[0], confidence="published")

    # 2. We have a company domain (from any printed address) and a name, but no
    #    address for that person — form the one marginal, clearly-flagged guess.
    if name and emails:
        domain = emails[0].split("@", 1)[1]
        local = _guess_local_part(name)
        if local:
            return ContactResult(
                name=name, email=f"{local}@{domain}", confidence="pattern-guess"
            )

    # 3. A published role inbox is a valid, if impersonal, contact.
    if role:
        return ContactResult(name=name, email=role[0], confidence="published")

    # 4. Nothing usable. Still record a name if we found one.
    return ContactResult(name=name, email=None, confidence="none")
