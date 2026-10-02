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
from src.llm.prompts import (
    COLD_EMAIL_TEMPLATE,
    COVER_LETTER_TEMPLATE,
    cold_email_greeting,
)
from src.logging_config import setup_logging
from src.storage.database import (
    count_pending_llm_jobs,
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

    # batch_size == 0 means "no limit" — drain the whole queue.
    limit = config.llm.batch_size or None

    succeeded = 0
    failed = 0
    with get_session() as session:
        jobs = pending_llm_jobs(session, limit=limit)
        total_pending = count_pending_llm_jobs(session)
        log.info(
            "Processing %d of %d pending job(s)%s.",
            len(jobs),
            total_pending,
            f" (batch_size={config.llm.batch_size})" if limit else "",
        )

        for job in jobs:
            fmt_kwargs = dict(
                title=job.title,
                company=job.company,
                description=job.description,
                resume_summary=resume_summary,
                greeting=cold_email_greeting(job.contact_name),
            )
            try:
                # Generate both materials before committing so a row never
                # leaves the queue (cover letter set) with a missing cold
                # email. The client already retries transient failures; if it
                # still raises, this job is skipped and the batch continues.
                cover_letter = client.generate(
                    COVER_LETTER_TEMPLATE.format(**fmt_kwargs)
                )
                cold_email = client.generate(
                    COLD_EMAIL_TEMPLATE.format(**fmt_kwargs)
                )
            except KeyboardInterrupt:
                # Ctrl-C mid-job: discard this job's partial state and stop.
                # Everything committed before now is already saved.
                session.rollback()
                log.warning(
                    "Interrupted — stopping. %d generated, %d failed, "
                    "%d still pending.",
                    succeeded,
                    failed,
                    total_pending - succeeded,
                )
                return
            except Exception:
                # Drop the dirty (partial) state for this job so the next
                # commit doesn't accidentally flush it, then move on.
                session.rollback()
                failed += 1
                log.exception(
                    "Generation failed for %s @ %s — skipping.",
                    job.title,
                    job.company,
                )
                continue

            job.generated_cover_letter = cover_letter
            job.generated_cold_email = cold_email
            session.add(job)
            session.commit()
            succeeded += 1
            log.info("Generated materials for: %s @ %s", job.title, job.company)

    remaining = total_pending - succeeded
    log.info(
        "Done. %d generated, %d failed, %d still pending.",
        succeeded,
        failed,
        remaining,
    )


if __name__ == "__main__":
    main()
