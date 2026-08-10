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

# `streamlit run src/app/main.py` puts src/app/ on sys.path (not the project
# root), so the `import src...` lines below would fail with ModuleNotFoundError.
# Add the repo root explicitly so imports resolve the same way they do under
# `python -m src...`. This must run before the `src.` imports.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

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


def render_home() -> None:
    """List page: filterable, lightweight table. No heavy columns loaded."""
    st.title("Job Hunter AI")

    # Filter option values are themselves cheap single-column reads.
    with get_session() as session:
        source_options = present_sources(session)
        company_options = distinct_companies(session)
        location_options = distinct_locations(session)

    st.sidebar.header("Filters")
    sel_sources = st.sidebar.multiselect("Source", source_options)
    sel_companies = st.sidebar.multiselect("Company", company_options)
    sel_statuses = st.sidebar.multiselect("Status", STATUSES)
    sel_locations = st.sidebar.multiselect("Location", location_options)
    title_query = st.sidebar.text_input("Title contains")

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
    table = pd.DataFrame(
        {
            "Title": [s.title for s in summaries],
            "Company": [s.company for s in summaries],
            "Status": [s.status for s in summaries],
            "Location": [s.location for s in summaries],
        }
    )

    event = st.dataframe(
        table,
        hide_index=True,
        use_container_width=True,
        selection_mode="single-row",
        on_select="rerun",
        key="jobs_table",
    )

    selected_rows = event.selection.rows
    if selected_rows:
        _selected_job_panel(summaries[selected_rows[0]])
    else:
        st.caption("Select a row to edit its status or open it in a new tab.")


def _selected_job_panel(job) -> None:
    """Quick actions for the row clicked in the list: edit status inline, or
    open the full detail page in a new tab. `job` is a JobSummary (no heavy
    text)."""
    st.divider()
    st.subheader(job.title)
    st.write(f"{job.company}  ·  {job.location}")

    edit_col, open_col = st.columns([2, 1])
    with edit_col:
        new_status = st.selectbox(
            "Status",
            STATUSES,
            index=_status_index(job.status),
            key=f"home_status_{job.id}",
        )
        if new_status != job.status:
            with get_session() as session:
                set_job_status(session, job.id, new_status)
            st.rerun()
    with open_col:
        # A plain HTML anchor is the only native way to force a new tab
        # (st.button / st.link_button can't). The URL is root-relative, which
        # assumes the app is served at the site root (the default for
        # `make app`); behind a base-path proxy this would need prefixing.
        st.write("")  # align with the selectbox
        st.markdown(
            f'<a href="/job?job={job.id}" target="_blank" rel="noopener" '
            f'style="display:inline-block;padding:0.4rem 0.9rem;'
            f'border:1px solid rgba(128,128,128,0.4);border-radius:0.5rem;'
            f'text-decoration:none;">Open in new tab</a>',
            unsafe_allow_html=True,
        )


def render_detail() -> None:
    """Detail page: the ONLY place a full row (with description, cover letter,
    cold email) is loaded — and only ever for the one selected job. Reached at
    /job?job=<id>, typically opened in a new tab from the list."""
    if st.button("Back to list"):
        st.query_params.clear()
        st.switch_page(HOME_PAGE)

    raw_id = st.query_params.get("job")
    if not raw_id:
        st.info("No job selected. Pick one from the list.")
        return
    try:
        job_id = int(raw_id)
    except (TypeError, ValueError):
        st.error("Invalid job id in the URL.")
        return

    with get_session() as session:
        job = get_job(session, job_id)

    if job is None:
        st.error("That job no longer exists.")
        return

    st.title(job.title)
    st.write(f"**Company:** {job.company}")
    st.write(f"**Location:** {job.location}")
    if job.url:
        st.markdown(f"**Source URL:** [{job.url}]({job.url})")

    new_status = st.selectbox(
        "Status",
        STATUSES,
        index=_status_index(job.status),
        key=f"detail_status_{job.id}",
    )
    if new_status != job.status:
        with get_session() as session:
            set_job_status(session, job.id, new_status)
        st.rerun()

    # Contact lookup result. Always show the confidence so a guessed address is
    # never mistaken for a verified one (see docs/hiring-manager-lookup.md).
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
                f"**Contact:** {job.contact_email}{who}  ·  _{job.contact_confidence}_"
            )
    elif job.contact_name:
        st.write(f"**Contact name:** {job.contact_name} (no email found)")
    elif job.contact_confidence is None:
        st.caption("Contact lookup not run yet — run `make contacts`.")

    if job.description:
        with st.expander("Job description"):
            st.write(job.description)

    if job.generated_cover_letter:
        st.text_area(
            "Cover letter", job.generated_cover_letter, height=300,
            key=f"cover_{job.id}",
        )
    else:
        st.caption("No cover letter generated yet — run `make process`.")

    if job.generated_cold_email:
        st.text_area(
            "Cold email", job.generated_cold_email, height=200,
            key=f"email_{job.id}",
        )
    else:
        st.caption("No cold email generated yet — run `make process`.")


# Pages are defined after their render functions so the functions can reference
# the page objects (for st.switch_page) as module globals at call time.
HOME_PAGE = st.Page(render_home, title="Jobs", url_path="jobs", default=True)
DETAIL_PAGE = st.Page(render_detail, title="Job details", url_path="job")

st.navigation([HOME_PAGE, DETAIL_PAGE]).run()
