"""Entry point for `make contacts`.

Queue consumer for contact lookup. Runs between `make scrape` and
`make process`: it reads jobs that haven't been looked up yet
(contact_confidence IS NULL), extracts an ethically-sourced contact from each
posting's own text (`src/contacts/extract.py`), and writes the result back.

Like the other stages it talks to nothing but the database, is safe to re-run,
and never aborts the batch on a single bad row — same shape as `src/llm/cli.py`.
A miss still sets confidence to "none" so the row leaves the queue.
"""

import logging
import sys

from src.config import ConfigError, load_config
from src.contacts.extract import find_contact
from src.logging_config import setup_logging
from src.storage.database import (
    get_session,
    init_db,
    pending_contact_jobs,
    set_database_url,
)

log = logging.getLogger("jobhunter.contacts")


def main() -> None:
    setup_logging()

    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    set_database_url(config.database.url)
    init_db()

    found = 0
    with get_session() as session:
        jobs = pending_contact_jobs(session)
        log.info("%d job(s) pending contact lookup.", len(jobs))

        for job in jobs:
            result = find_contact(job)
            job.contact_name = result.name
            job.contact_email = result.email
            job.contact_confidence = result.confidence
            session.add(job)
            session.commit()
            if result.confidence != "none":
                found += 1
            log.info(
                "%s @ %s -> %s (%s)",
                job.title,
                job.company,
                result.email or "no contact",
                result.confidence,
            )

    log.info("Contact lookup done: %d/%d with a contact.", found, len(jobs))


if __name__ == "__main__":
    main()
