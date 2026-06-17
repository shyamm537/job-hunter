"""Entry point for `make process`.

This is the queue consumer: it reads jobs with no generated cover letter
yet, calls the LLM client for each, and writes the results back. Streamlit
never calls the LLM directly — it only reads what this script produces.
"""

import yaml

from src.llm.client import get_llm_client
from src.llm.prompts import COLD_EMAIL_TEMPLATE, COVER_LETTER_TEMPLATE
from src.storage.database import get_session, init_db, pending_llm_jobs


def main() -> None:
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    init_db()
    client = get_llm_client(config)
    resume_summary = config.get("resume_summary", "")

    with get_session() as session:
        jobs = pending_llm_jobs(session)
        print(f"{len(jobs)} job(s) pending generation.")

        for job in jobs:
            fmt_kwargs = dict(
                title=job.title,
                company=job.company,
                description=job.description,
                resume_summary=resume_summary,
            )
            job.generated_cover_letter = client.generate(
                COVER_LETTER_TEMPLATE.format(**fmt_kwargs)
            )
            job.generated_cold_email = client.generate(
                COLD_EMAIL_TEMPLATE.format(**fmt_kwargs)
            )
            session.add(job)
            session.commit()
            print(f"Generated materials for: {job.title} @ {job.company}")


if __name__ == "__main__":
    main()
