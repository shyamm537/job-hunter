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
hiring manager at {company} about the {title} role. Reference one specific
detail from the job description below. No generic flattery, no filler
sign-off.

Job description:
{description}

Candidate background:
{resume_summary}
"""
