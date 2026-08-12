from src.contacts.extract import ContactResult, find_contact
from src.llm.prompts import cold_email_greeting
from src.storage.models import JobPost


def _job(description):
    return JobPost(
        job_board_id="x", title="Data Analyst", company="Acme",
        location="Remote", description=description, url="u",
    )


def test_published_personal_email_wins():
    r = find_contact(_job("Questions? Email dana.lee@acme.com to chat."))
    assert r.email == "dana.lee@acme.com"
    assert r.confidence == "published"


def test_role_inbox_is_published_but_impersonal():
    r = find_contact(_job("Apply now — send your CV to careers@acme.com."))
    assert r.email == "careers@acme.com"
    assert r.confidence == "published"
    assert r.name is None


def test_personal_email_preferred_over_role_inbox():
    desc = "Send applications to jobs@acme.com or reach dana.lee@acme.com directly."
    r = find_contact(_job(desc))
    assert r.email == "dana.lee@acme.com"  # personal beats the role inbox
    assert r.confidence == "published"


def test_pattern_guess_from_name_plus_published_domain():
    # A printed role inbox gives the domain; a cue gives the name; no personal
    # address exists -> one flagged guess.
    desc = "Send your CV to careers@acme.com. Reporting to Dana Lee."
    r = find_contact(_job(desc))
    assert r.email == "dana.lee@acme.com"
    assert r.name == "Dana Lee"
    assert r.confidence == "pattern-guess"


def test_no_contact_is_none_not_null():
    # A miss still gets a confidence so the row leaves the queue.
    r = find_contact(_job("A great role on a great team. No contact details."))
    assert r == ContactResult(name=None, email=None, confidence="none")


def test_name_only_no_domain_gives_no_email():
    # Name present, but the company printed no address -> we do NOT invent a
    # domain. Name is kept, email stays None.
    r = find_contact(_job("Reporting to Dana Lee. Apply via our website."))
    assert r.name == "Dana Lee"
    assert r.email is None
    assert r.confidence == "none"


def test_capitalised_noise_is_not_grabbed_as_a_name():
    # No cue phrase -> the capitalised pair "Data Engineer" must not be a name.
    r = find_contact(_job("Senior Data Engineer wanted. Email careers@acme.com."))
    assert r.name is None


def test_greeting_uses_first_name_or_falls_back():
    assert cold_email_greeting("Dana Lee") == "Hi Dana,"
    assert cold_email_greeting(None) == "Hi,"
