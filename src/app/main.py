"""Streamlit dashboard — read-only over the job queue, plus status edits.

This deliberately does no scraping and no LLM calls. Those run as separate
CLI steps (`make scrape`, `make process`) so the UI thread never blocks on
either. The app only reads from, and writes status updates to, the DB.
"""

import sys
from pathlib import Path

# `streamlit run src/app/main.py` puts src/app/ on sys.path (not the project
# root), so the `import src...` lines below would fail with ModuleNotFoundError.
# Add the repo root explicitly so imports resolve the same way they do under
# `python -m src...`. This must run before the `src.` imports.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st  # noqa: E402
from sqlmodel import select  # noqa: E402

from src.config import ConfigError, load_config  # noqa: E402
from src.logging_config import setup_logging  # noqa: E402
from src.storage.database import get_session, init_db, set_database_url  # noqa: E402
from src.storage.models import JobPost  # noqa: E402

STATUSES = ["To Apply", "Applied", "Interviewing", "Rejected"]

setup_logging()

st.set_page_config(page_title="Job Hunter AI", layout="wide")
st.title("Job Hunter AI")

# Reach the database the same way the CLIs do: load + validate config, then
# point the storage layer at the configured URL. Without this the app would
# fall back to database.py's silent default and could quietly read a
# different database than `make scrape` / `make process` wrote to.
try:
    config = load_config()
except ConfigError as exc:
    st.error(str(exc))
    st.stop()

set_database_url(config.database.url)
init_db()

with get_session() as session:
    jobs = session.exec(select(JobPost).order_by(JobPost.date_scraped.desc())).all()

if not jobs:
    st.info("No jobs yet. Run `make scrape` to populate the database.")
else:
    st.caption(f"{len(jobs)} job(s) tracked.")

    for job in jobs:
        with st.expander(f"{job.title} — {job.company}  ·  {job.status}"):
            st.write(f"**Location:** {job.location}")
            st.write(f"**Source URL:** {job.url}")

            # Contact lookup result. Always show the confidence so a guessed
            # address is never mistaken for a verified one (see
            # docs/hiring-manager-lookup.md).
            if job.contact_email:
                who = f" ({job.contact_name})" if job.contact_name else ""
                if job.contact_confidence == "pattern-guess":
                    st.warning(
                        f"**Candidate contact:** {job.contact_email}{who} — "
                        "**guessed** from the company email pattern, unverified. "
                        "Check before sending."
                    )
                else:
                    st.write(
                        f"**Contact:** {job.contact_email}{who}  "
                        f"·  _{job.contact_confidence}_"
                    )
            elif job.contact_name:
                st.write(f"**Contact name:** {job.contact_name} (no email found)")
            elif job.contact_confidence is None:
                st.caption("Contact lookup not run yet — run `make contacts`.")

            new_status = st.selectbox(
                "Status",
                STATUSES,
                index=STATUSES.index(job.status) if job.status in STATUSES else 0,
                key=f"status_{job.id}",
            )
            if new_status != job.status:
                with get_session() as update_session:
                    db_job = update_session.get(JobPost, job.id)
                    db_job.status = new_status
                    update_session.add(db_job)
                    update_session.commit()
                st.rerun()

            if job.generated_cover_letter:
                st.text_area(
                    "Cover letter", job.generated_cover_letter, height=200,
                    key=f"cover_{job.id}",
                )
            else:
                st.caption("No cover letter generated yet — run `make process`.")

            if job.generated_cold_email:
                st.text_area(
                    "Cold email", job.generated_cold_email, height=150,
                    key=f"email_{job.id}",
                )
