"""Streamlit dashboard — read-only over the job queue, plus status edits.

This deliberately does no scraping and no LLM calls. Those run as separate
CLI steps (`make scrape`, `make process`) so the UI thread never blocks on
either. The app only reads from, and writes status updates to, the DB.
"""

import streamlit as st
from sqlmodel import select

from src.storage.database import get_session, init_db
from src.storage.models import JobPost

STATUSES = ["To Apply", "Applied", "Interviewing", "Rejected"]

st.set_page_config(page_title="Job Hunter AI", layout="wide")
st.title("Job Hunter AI")

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
