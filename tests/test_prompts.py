"""Tests for the prompt templates, the reply cleanup and the placeholder retry.

The bad replies below are modelled on what llama3 actually wrote for stored
jobs: a "Here is..." preamble, a Subject line, "Dear Hiring Manager" instead of
the required greeting, and "[Your Name]" / "[Number] years" left in.
"""

from types import SimpleNamespace

import pytest

from src.llm import prompts
from src.llm.prompts import (
    MAX_DESCRIPTION_CHARS,
    build_prompt,
    clean_output,
    find_placeholders,
    generate_material,
)


def _job(**overrides):
    values = dict(title="Data Analyst", company="Acme", description="Build dashboards.",
                  contact_name=None)
    values.update(overrides)
    return SimpleNamespace(**values)


class _Scripted:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_prompt_carries_the_rules_the_greeting_and_the_inputs():
    prompt = build_prompt("cold_email", _job(contact_name="Dana Lee"), "Five years of SQL.")
    assert '"Hi Dana,"' in prompt
    assert "Build dashboards." in prompt and "Five years of SQL." in prompt
    assert "Never use placeholders" in prompt


def test_prompt_has_no_signature_placeholder_without_a_name():
    for kind in ("cover_letter", "cold_email"):
        prompt = build_prompt(kind, _job(), "SQL.")
        assert "{" not in prompt and "[" not in prompt
        assert "no signature line" in prompt


def test_prompt_uses_the_configured_name():
    assert '"Sam Rowe"' in build_prompt("cover_letter", _job(), "SQL.", "Sam Rowe")
    assert '"Sam Rowe"' in build_prompt("cold_email", _job(), "SQL.", "Sam Rowe")


def test_description_is_converted_and_clipped_at_a_sentence():
    sentence = "We build things people use every day. "
    long = sentence * 200
    prompt = build_prompt("cover_letter", _job(description=f"<p>{long}</p>"), "SQL.")
    body = prompt.split("Job description:\n")[1].split("\n\nCandidate background:")[0]
    assert "<p>" not in body
    assert len(body) <= MAX_DESCRIPTION_CHARS and body.endswith("every day.")


def test_a_short_description_is_left_alone():
    assert prompts._clip_description("Build dashboards.") == "Build dashboards."
    assert prompts._clip_description(None) == ""


def test_preamble_and_subject_lines_are_dropped():
    raw = "Here is a 3-paragraph cover letter tailored to the role:\n\nDear Hiring Manager,\nHello."
    assert clean_output("cover_letter", raw, "Hi,") == "Dear Hiring Manager,\nHello."
    raw = "Here's a brief email:\n\nSubject: Data Analyst at Acme\n\nHi Hiring Manager,\nBody."
    assert clean_output("cold_email", raw, "Hi Dana,") == "Hi Dana,\nBody."


def test_a_letter_that_merely_starts_with_here_is_kept():
    raw = "Here is why I fit this role: I write SQL daily."
    assert clean_output("cover_letter", raw, "Hi,") == "Here is why I fit this role: I write SQL daily."


def test_cold_email_opens_with_the_required_greeting():
    assert clean_output("cold_email", "Dear Hiring Manager,\nBody.", "Hi Dana,") == "Hi Dana,\nBody."
    assert clean_output("cold_email", "Body.", "Hi,") == "Hi,\n\nBody."
    assert clean_output("cold_email", "Hi Dana,\nBody.", "Hi Dana,") == "Hi Dana,\nBody."


def test_find_placeholders():
    text = "With [Number] years at [Current Company]. Thanks, [Your Name]"
    assert find_placeholders(text) == ["[Number]", "[Current Company]", "[Your Name]"]
    assert find_placeholders("Your Name here") == ["Your Name"]
    assert find_placeholders("Clean text, no brackets.") == []


def test_clean_reply_makes_one_call():
    llm = _Scripted("Dear Hiring Manager,\nI write SQL.")
    assert generate_material(llm, "cover_letter", _job(), "SQL.") == "Dear Hiring Manager,\nI write SQL."
    assert len(llm.prompts) == 1


def test_a_placeholder_triggers_one_retry_that_names_it():
    llm = _Scripted("I have [Number] years.", "I write SQL.")
    assert generate_material(llm, "cover_letter", _job(), "SQL.") == "I write SQL."
    assert len(llm.prompts) == 2
    assert "[Number]" in llm.prompts[1] and llm.prompts[1].startswith(llm.prompts[0])


def test_still_bad_after_the_retry_is_kept_with_a_warning(caplog):
    llm = _Scripted("Thanks, [Your Name]", "Thanks again, [Your Name]")
    with caplog.at_level("WARNING", logger="jobhunter.llm.prompts"):
        text = generate_material(llm, "cover_letter", _job(), "SQL.")
    assert text == "Thanks again, [Your Name]" and len(llm.prompts) == 2
    assert "still has placeholders" in caplog.text


def test_an_empty_retry_keeps_the_first_text():
    llm = _Scripted("Thanks, [Your Name]", "  ")
    assert generate_material(llm, "cover_letter", _job(), "SQL.") == "Thanks, [Your Name]"


def test_an_empty_reply_gives_an_empty_string_without_retrying():
    llm = _Scripted("   ")
    assert generate_material(llm, "cold_email", _job(), "SQL.") == ""
    assert len(llm.prompts) == 1


def test_unknown_kind_is_rejected():
    with pytest.raises(KeyError):
        build_prompt("poem", _job(), "SQL.")
