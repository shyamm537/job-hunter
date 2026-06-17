"""Entry point for `make process`.

This is the queue consumer: it reads jobs with no generated cover letter
yet, calls the LLM client for each, and writes the results back. Streamlit
never calls the LLM directly — it only reads what this script produces.

Config and logging go through the shared layers (`src/config.py`,
`src/logging_config.py`) like the rest of the pipeline. The LLM client
itself (`src/llm/client.py`) is unchanged — it still takes a plain dict, so
we hand it one built from the validated config.
"""

import logging
import sys

from src.config import ConfigError, load_config
from src.llm.client import get_llm_client
from src.llm.prompts import COLD_EMAIL_TEMPLATE, COVER_LETTER_TEMPLATE
from src.logging_config import setup_logging
from src.storage.database import (
    get_session,
    init_db,
    pending_llm_jobs,
    set_database_url,
)

log = logging.getLogger("jobhunter.process")


def main() -> None:
    setup_logging()

    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    set_database_url(config.database.url)
    init_db()

    # get_llm_client still expects a raw dict with an "llm" key — pass one
    # built from the validated config rather than re-reading the YAML.
    client = get_llm_client({"llm": config.llm.model_dump()})
    resume_summary = config.resume_summary

    with get_session() as session:
        jobs = pending_llm_jobs(session)
        log.info("%d job(s) pending generation.", len(jobs))

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
            log.info("Generated materials for: %s @ %s", job.title, job.company)


if __name__ == "__main__":
    main()
