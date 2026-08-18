"""Streamlit dashboard — a two-page, lazy-loading view over the job queue.

This deliberately does no scraping and no LLM calls. Those run as separate
CLI steps (`make scrape`, `make process`) so the UI thread never blocks on
either. The app only reads from, and writes status updates to, the DB.

Why two pages (see docs/data-model.md):

The list page must stay cheap no matter how big the table gets. It queries
ONLY the columns it renders (title/company/status/location, via
`list_job_summaries`) and never touches the heavy text — `description` and the
two generated blobs (cover letter, cold email). Those load only when you open
a single job's detail page, and only for that one row. Filters are pushed into
SQL, so the database returns just the matching rows rather than the whole table.

Opening a job's detail uses a plain link to `/job?job=<id>` (target=_blank), so
each job opens in its own tab at its own URL. The id rides in the URL, which is
why the detail page always receives it. (An earlier version set a query param
and called st.switch_page; switch_page jumps to the bare `/job` path and drops
the query string, so the detail page came up blank — hence the direct link.)
"""

import sys
from pathlib import Path
from pypdf import PdfReader
import docx

# `streamlit run src/app/main.py` puts src/app/ on sys.path (not the project
# root), so the `import src...` lines below would fail with ModuleNotFoundError.
# Add the repo root explicitly so imports resolve the same way they do under
# `python -m src...`. This must run before the `src.` imports.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import time
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from src.llm.client import get_llm_client  # noqa: E402
from src.llm.prompts import COVER_LETTER_TEMPLATE  # noqa: E402
from src.config import ConfigError, load_config  # noqa: E402
from src.logging_config import setup_logging  # noqa: E402
from src.storage.database import (  # noqa: E402
    distinct_companies,
    distinct_locations,
    get_job,
    get_session,
    init_db,
    list_job_summaries,
    present_sources,
    set_database_url,
    set_job_status,
)

STATUSES = ["To Apply", "Applied", "Interviewing", "Rejected"]

setup_logging()

st.set_page_config(page_title="Job Hunter AI", layout="wide")

# Reach the database the same way the CLIs do: load + validate config, then
# point the storage layer at the configured URL. Without this the app would
# fall back to database.py's silent default and could quietly read a
# different database than `make scrape` / `make process` wrote to. This runs on
# every rerun (cheap + idempotent) so both pages always hit the right DB.
try:
    _config = load_config()
except ConfigError as exc:
    st.error(str(exc))
    st.stop()

set_database_url(_config.database.url)
init_db()


def _status_index(status: str) -> int:
    return STATUSES.index(status) if status in STATUSES else 0

def _resume_to_text(uploaded) -> str:
    """Pull plain text out of an uploaded resume (PDF or DOCX)."""
    name = uploaded.name.lower()
    if name.endswith(".pdf"):
        reader = PdfReader(uploaded)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if name.endswith(".docx"):
        doc = docx.Document(uploaded)
        return "\n".join(p.text for p in doc.paragraphs)
    return uploaded.read().decode("utf-8", errors="ignore")

def render_home() -> None:
    """List page: filterable, lightweight table. No heavy columns loaded."""
    st.title("Job Hunter AI")

    # Filter option values are themselves cheap single-column reads.
    with get_session() as session:
        source_options = present_sources(session)
        company_options = distinct_companies(session)
        location_options = distinct_locations(session)

    with st.sidebar.expander("Filters", expanded=True):
        sel_sources = st.multiselect("Source", source_options)
        sel_companies = st.multiselect("Company", company_options)
        sel_statuses = st.multiselect("Status", STATUSES)
        sel_locations = st.multiselect("Location", location_options)
        title_query = st.text_input("Title contains")

    # Every filter is applied in SQL, so only matching rows come back.
    with get_session() as session:
        summaries = list_job_summaries(
            session,
            sources=sel_sources,
            companies=sel_companies,
            statuses=sel_statuses,
            locations=sel_locations,
            title_query=title_query,
        )

    any_filter = bool(
        sel_sources
        or sel_companies
        or sel_statuses
        or sel_locations
        or title_query
    )
    if not summaries:
        if any_filter:
            st.info("No jobs match the current filters.")
        else:
            st.info("No jobs yet. Run `make scrape` to populate the database.")
        return

    st.caption(f"{len(summaries)} job(s){' matching filters' if any_filter else ''}.")

    # Only the four requested columns are shown; `id` and `source` are kept in
    # `summaries` for navigation/filtering but never displayed.
    for s in summaries:
        c = st.columns([4, 3, 2, 3, 1])
        c[0].write(s.title)
        c[1].write(s.company)
        c[2].write(s.status)
        c[3].write(s.location)
        if c[4].button("Open", key=f"open_{s.id}"):
            _job_dialog(s.id)

@st.dialog("Job",width="large")
def _job_dialog(job_id: int) -> None:
    with get_session() as session:
        job = get_job(session, job_id)
    
    if job is None:
        st.error("That job no longer exists.")
        return
    st.subheader(job.title)
    st.write(f"**Company:** {job.company}")
    st.write(f"**Location:** {job.location}")
    st.markdown(f"[Apply]({job.url})")

    new_status = st.selectbox(
            "Status",
            STATUSES,
            index=_status_index(job.status),
            key=f"dlg_status_{job.id}",
        )
    
    if new_status != job.status:
        with get_session() as session:
            set_job_status(session, job.id, new_status)
        st.toast("Status updated")
    
    with st.expander("Job description"):
        st.write(f"{job.description}")

    resume = st.file_uploader(
        "Upload your resume",
        type=["pdf", "docx"],
        key=f"dlg_resume_{job.id}",
    )
    if resume is not None:
        st.success(f"Uploaded: {resume.name}")
        
    if resume is not None and st.button("Generate cover letter", key=f"dlg_gen_{job.id}"):
        resume_text = _resume_to_text(resume)
        prompt = COVER_LETTER_TEMPLATE.format(
            title=job.title, company=job.company,
            description=job.description, resume_summary=resume_text,
        )

        with st.spinner("Writing cover letter…"):
            client = get_llm_client({"llm": _config.llm.model_dump()})
            start = time.perf_counter()
            letter = client.generate(prompt)
            elapsed = time.perf_counter() - start
        st.session_state[f"dlg_letter_{job.id}"] = letter #save cover letter for this session only
        st.session_state[f"dlg_elapsed_{job.id}"] = elapsed

        saved = st.session_state.get(f"dlg_letter_{job.id}")
        if saved:
            elapsed = st.session_state.get(f"dlg_elapsed_{job.id}")
            if elapsed is not None:
                st.caption(f"Generated in {elapsed:.1f}s")
            st.code(saved, language=None, wrap_lines=True)

# Pages are defined after their render functions so the functions can reference
# the page objects (for st.switch_page) as module globals at call time.
HOME_PAGE = st.Page(render_home, title="Jobs", url_path="jobs", default=True)
# DETAIL_PAGE = st.Page(render_detail, title="Job details", url_path="job")

st.navigation([HOME_PAGE]).run()
