from src.storage.models import JobPost


def test_jobpost_defaults():
    job = JobPost(
        job_board_id="seek-test123",
        title="Data Analyst",
        company="Acme",
        location="Adelaide",
        description="A great role.",
        url="https://example.com/job/123",
    )

    assert job.status == "To Apply"
    assert job.generated_cover_letter is None
    assert job.generated_cold_email is None
    assert job.id is None


def test_job_board_id_has_unique_constraint():
    column = JobPost.__table__.columns["job_board_id"]
    assert column.unique is True
