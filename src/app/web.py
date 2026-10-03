"""Routes for the dashboard: the jobs list, a job's page, status changes and
the "Add a job" form.

The list never loads the heavy text columns (description, cover letter, cold
email); list_job_summaries selects only what the table shows, with every
filter applied in SQL. A job's page is the one place a full row is loaded.

Local-only guards (see check_request): the app answers only to localhost
Host headers, which blocks DNS-rebinding, and refuses a POST that comes from
another origin, so a web page you visit can't submit forms to it.
"""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from src.storage.database import (
    add_manual_job,
    distinct_companies,
    distinct_locations,
    get_job,
    get_session,
    list_job_summaries,
    present_sources,
    set_job_status,
)

STATUSES = ["To Apply", "Applied", "Interviewing", "Rejected"]

bp = Blueprint("web", __name__)


@bp.before_app_request
def check_request():
    host = (request.host or "").rsplit(":", 1)[0].strip("[]")
    if host not in current_app.config["ALLOWED_HOSTS"]:
        abort(400, "This dashboard only answers on localhost.")
    if request.method == "POST":
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source and urlsplit(source).netloc != request.host:
            abort(403, "Cross-origin form submissions are refused.")


def _safe_next(target: str) -> str:
    """A local path to go back to, never another site."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("web.jobs")


@bp.app_template_filter("http_url")
def http_url(url: str) -> str:
    """The URL if it's http(s), else "" — so a scraped or pasted
    `javascript:` link can never become a clickable href."""
    url = (url or "").strip()
    return url if urlsplit(url).scheme in ("http", "https") else ""


class _TextOnly(HTMLParser):
    """Collects the text of an HTML fragment: block tags become line breaks,
    list items get a bullet, and script/style contents are dropped."""

    BLOCKS = {"p", "div", "br", "ul", "ol", "li", "tr", "section", "article",
              "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table"}
    SKIP = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n\n• " if tag == "li" else "\n\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag in self.BLOCKS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


@bp.app_template_filter("plain_text")
def plain_text(value: str) -> str:
    """Readable text from a scraped description.

    Greenhouse sends HTML that is itself HTML-escaped (`&lt;p&gt;...`), so
    unescape until the markup shows (at most a few rounds), then keep only the
    text. The template still escapes the result: this never emits HTML.
    """
    text = value or ""
    for _ in range(3):
        if "&lt;" not in text and "&amp;" not in text:
            break
        text = html.unescape(text)
    if "<" in text:
        parser = _TextOnly()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@bp.get("/")
def jobs():
    selected = {
        "sources": request.args.getlist("source"),
        "statuses": request.args.getlist("status"),
        "company": request.args.get("company", ""),
        "location": request.args.get("location", ""),
        "q": request.args.get("q", "").strip(),
    }
    with get_session() as session:
        options = {
            "sources": present_sources(session),
            "companies": distinct_companies(session),
            "locations": distinct_locations(session),
        }
        summaries = list_job_summaries(
            session,
            sources=selected["sources"],
            statuses=selected["statuses"],
            companies=[selected["company"]] if selected["company"] else None,
            locations=[selected["location"]] if selected["location"] else None,
            title_query=selected["q"],
        )
    return render_template(
        "jobs.html",
        jobs=summaries,
        selected=selected,
        options=options,
        statuses=STATUSES,
        any_filter=any(selected.values()),
        here=request.full_path.rstrip("?"),
    )


@bp.get("/jobs/<int:job_id>")
def job(job_id: int):
    with get_session() as session:
        found = get_job(session, job_id)
    if found is None:
        abort(404)
    return render_template(
        "job.html", job=found, statuses=STATUSES, here=request.full_path.rstrip("?")
    )


@bp.post("/jobs/<int:job_id>/status")
def update_status(job_id: int):
    status = request.form.get("status", "")
    if status not in STATUSES:
        abort(400, f"Unknown status {status!r}.")
    with get_session() as session:
        if get_job(session, job_id) is None:
            abort(404)
        set_job_status(session, job_id, status)
    return redirect(_safe_next(request.form.get("next", "")))


@bp.route("/jobs/new", methods=["GET", "POST"])
def add_job():
    if request.method == "GET":
        return render_template("add_job.html", form={}, error=None)

    form = {
        name: request.form.get(name, "")
        for name in ("title", "company", "location", "url", "description")
    }
    try:
        with get_session() as session:
            added, created = add_manual_job(session, **form)
            job_id, title, company = added.id, added.title, added.company
    except ValueError as exc:
        return render_template("add_job.html", form=form, error=f"Couldn't add the job: {exc}."), 400

    if created:
        flash(f"Added {title} at {company}. The next `make process` run writes its cover letter first.", "success")
    else:
        flash(f"{title} at {company} is already in your list.", "info")
    return redirect(url_for("web.job", job_id=job_id))


@bp.app_errorhandler(400)
@bp.app_errorhandler(403)
@bp.app_errorhandler(404)
def error_page(err):
    return render_template("error.html", error=err), err.code
