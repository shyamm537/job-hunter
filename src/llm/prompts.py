"""Prompt templates for generated application materials.

Keeping these as plain format strings (not f-strings baked into call sites)
means they can be tuned without touching cli.py or client.py.
"""

COVER_LETTER_TEMPLATE = """You are helping a job seeker write a concise, specific cover letter.

Job title: {title}
Company: {company}
Job description:
{description}

Candidate background:
{resume_summary}

Write a 3-paragraph cover letter. Be specific to the role, avoid generic
filler, and do not invent experience that isn't present in the candidate
background.
"""

COLD_EMAIL_TEMPLATE = """Write a brief, direct cold email (under 150 words) to a
hiring manager at {company} about the {title} role. Open the email with this
greeting line exactly: "{greeting}". Reference one specific detail from the job
description below. No generic flattery, no filler sign-off.

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
