"""Prompt templates and output cleanup for generated application materials.

Keeping the templates as plain format strings (not f-strings baked into call
sites) means they can be tuned without touching cli.py or client.py.

A small local model ignores instructions often enough that the prompt alone is
not reliable, so `generate_material` also tidies the reply (`clean_output`) and
retries once when it still contains placeholders like "[Your Name]". Both the
dashboard (src/app/generate.py) and `make process` (src/llm/cli.py) go through
it, so the two paths cannot drift.
"""

import logging
import re

from src.ingestion.text_util import html_to_text

log = logging.getLogger("jobhunter.llm.prompts")

# Longer postings are cut here: the tail is usually benefits and legal
# boilerplate, and a shorter prompt is faster on a slow local model.
MAX_DESCRIPTION_CHARS = 3500

COVER_LETTER_TEMPLATE = """You are helping a job seeker write a specific, plain-spoken cover letter.

Rules:
1. Output only the letter itself. No introduction, no commentary, no subject line.
2. Never use placeholders or square brackets. If a fact is not given, leave it out.
3. Use only facts from the candidate background. Do not invent employers, tools,
   qualifications, or a number of years of experience that the background does not state.
4. The job description is the only source of facts about the employer. Do not
   praise the company's mission or values unless the description states them.
5. No buzzwords or filler ("passionate", "excited to leverage", "dynamic").

Structure, about 250 words in total:
- Start with the greeting line "Dear Hiring Manager,".
- Paragraph 1: the role, and the single strongest match between the
  background and the role.
- Paragraph 2: one or two concrete items from the background, each tied to a
  specific requirement in the job description.
- Paragraph 3: a short, direct close.
- {signature}

Job title: {title}
Company: {company}

Job description:
{description}

Candidate background:
{resume_summary}
"""

COLD_EMAIL_TEMPLATE = """Write a brief, direct cold email to a hiring manager at {company}
about the {title} role.

Rules:
1. Output only the email body. No introduction, no commentary, no subject line.
2. Open with this greeting line exactly: "{greeting}"
3. Under 120 words. Reference one specific detail from the job description and
   one matching fact from the candidate background, then make one clear ask.
4. Never use placeholders or square brackets. Use only facts from the candidate
   background; do not invent experience or a number of years.
5. No generic flattery, no filler, no company mission praise.
6. {signature}

Job description:
{description}

Candidate background:
{resume_summary}
"""


def cold_email_greeting(contact_name: str | None) -> str:
    """Greeting line for the cold email. Uses the looked-up contact's first
    name when we have one; otherwise the generic fallback (today's behaviour).

    Only the *name* is used here — the candidate email address lives on the
    JobPost row and in the dashboard, not in the body text."""
    if contact_name:
        first = contact_name.split()[0]
        return f"Hi {first},"
    return "Hi,"


# The materials the LLM writes: kind -> (JobPost column, template, label).
# `make process` writes both; the dashboard's job page can write either one.
MATERIALS = {
    "cover_letter": ("generated_cover_letter", COVER_LETTER_TEMPLATE, "cover letter"),
    "cold_email": ("generated_cold_email", COLD_EMAIL_TEMPLATE, "cold email"),
}


def _clip_description(description: str | None) -> str:
    """Plain text, cut to MAX_DESCRIPTION_CHARS at a paragraph or sentence
    boundary where one is near the limit."""
    text = html_to_text(description)
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    head = text[:MAX_DESCRIPTION_CHARS]
    floor = MAX_DESCRIPTION_CHARS * 6 // 10
    for boundary in ("\n\n", ". ", "\n", " "):
        cut = head.rfind(boundary)
        if cut >= floor:
            return head[: cut + (1 if boundary == ". " else 0)].rstrip()
    return head.rstrip()


def _signature_rule(kind: str, candidate_name: str) -> str:
    if candidate_name:
        if kind == "cover_letter":
            return (f'End with "Kind regards," on its own line, then the name '
                    f'"{candidate_name}" on the next line.')
        return f'End with the name "{candidate_name}" on its own line, with no sign-off word before it.'
    return "End after the final sentence. Add no sign-off and no signature line."


def build_prompt(kind: str, job, resume_summary: str, candidate_name: str = "") -> str:
    """The prompt for one material (a MATERIALS key) for one JobPost."""
    _, template, _ = MATERIALS[kind]
    return template.format(
        title=job.title,
        company=job.company,
        description=_clip_description(job.description),
        resume_summary=resume_summary,
        greeting=cold_email_greeting(job.contact_name),
        signature=_signature_rule(kind, candidate_name.strip()),
    )


# "Here is a 3-paragraph cover letter tailored to ...:" and the like.
_PREAMBLE = re.compile(r"^\s*here(?:'s|\s+is|\s+are)\b[^\n]*:\s*$", re.IGNORECASE)
_SUBJECT = re.compile(r"^\s*subject\s*:", re.IGNORECASE)
_SALUTATION = re.compile(r"^\s*(?:dear|hi|hello|hey)\b[^\n]*$", re.IGNORECASE)
_BRACKETED = re.compile(r"\[[^\]\n]{1,40}\]")
_NAMED = re.compile(r"\b(?:your name|current company)\b", re.IGNORECASE)


def clean_output(kind: str, text: str, greeting: str) -> str:
    """Drop the model's chatter around the letter and fix the cold email's
    opening line. Never invents content beyond that greeting."""
    lines = text.strip().splitlines()
    while lines and (not lines[0].strip() or _PREAMBLE.match(lines[0]) or _SUBJECT.match(lines[0])):
        lines.pop(0)
    if kind == "cold_email":
        if lines and _SALUTATION.match(lines[0]):
            lines[0] = greeting
        else:
            lines[:0] = [greeting, ""]
    return "\n".join(lines).strip()


def find_placeholders(text: str) -> list[str]:
    """Placeholders left in the text, e.g. "[Your Name]" or "[Number]"."""
    # Look for bare "Your Name" only outside brackets, so "[Your Name]" counts once.
    found = _BRACKETED.findall(text) + _NAMED.findall(_BRACKETED.sub(" ", text))
    return list(dict.fromkeys(found))


def generate_material(client, kind: str, job, resume_summary: str, candidate_name: str = "") -> str:
    """Generate one material for one job: build the prompt, call the model,
    tidy the reply. Retries once when placeholders remain, then keeps the
    cleaned text with a warning so one bad reply never blocks the queue.
    Returns "" when the model replies with nothing."""
    prompt = build_prompt(kind, job, resume_summary, candidate_name)
    greeting = cold_email_greeting(job.contact_name)
    raw = client.generate(prompt)
    if not raw.strip():
        return ""
    text = clean_output(kind, raw, greeting)

    left = find_placeholders(text)
    if left:
        shown = ", ".join(left)
        log.info("The %s for %s @ %s has placeholders (%s); asking again.", kind, job.title, job.company, shown)
        retry = client.generate(
            f"{prompt}\nYour previous draft contained placeholders ({shown}). Write it "
            "again with none: leave out any fact you were not given."
        )
        if retry.strip():
            text = clean_output(kind, retry, greeting)
            left = find_placeholders(text)
        if left:
            log.warning("The %s for %s @ %s still has placeholders (%s).", kind, job.title, job.company, ", ".join(left))
    return text
