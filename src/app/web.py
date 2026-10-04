"""Routes for the dashboard: the jobs list, a job's page, status changes and
the "Add a job" form.

The list never loads the heavy text columns (description, cover letter, cold
email); list_job_summaries selects only what the table shows, with every
filter applied in SQL. A job's page is the one place a full row is loaded.

Triage (KAN-34): with no filters the list is the "To Apply" queue, newest
posted first. A job is unread until you open it or change its status, and
"new" if the latest `make scrape` run first stored it. "Not interested"
dismisses a job in one click.

A job's page can generate its cover letter or cold email on demand (KAN-43):
the work runs in the background (src/app/generate.py) and the page reloads
when it's done.

Local-only guards (see check_request): the app answers only to localhost
Host headers, which blocks DNS-rebinding, and refuses a POST that comes from
another origin, so a web page you visit can't submit forms to it.
"""

import html
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from src.storage.database import (
    MANUAL_SOURCE,
    SORTS,
    add_manual_job,
    distinct_companies,
    distinct_locations,
    get_job,
    get_session,
    latest_scrape_start,
    list_job_summaries,
    mark_opened,
    present_sources,
    set_job_status,
    utcnow,
)
from src.llm.prompts import MATERIALS
from src.storage.models import NOT_INTERESTED, STATUSES, TO_APPLY

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


def _filters(args) -> dict:
    """The list's filters and sort from query args (a MultiDict or a
    parse_qs dict). With no status chosen and no filter form submitted, the
    list is the triage queue: "To Apply" only. The filter form always sends
    f=1, so unticking every status there shows every status."""
    def many(name):
        return list(args.getlist(name)) if hasattr(args, "getlist") else list(args.get(name, []))

    def one(name):
        values = many(name)
        return values[0].strip() if values else ""

    statuses = many("status")
    if not statuses and not one("f"):
        statuses = [TO_APPLY]
    sort = one("sort")
    return {
        "sources": many("source"),
        "statuses": statuses,
        "company": one("company"),
        "location": one("location"),
        "q": one("q"),
        "sort": sort if sort in SORTS else "posted",
    }


def _summaries(session, selected: dict):
    return list_job_summaries(
        session,
        sources=selected["sources"],
        statuses=selected["statuses"],
        companies=[selected["company"]] if selected["company"] else None,
        locations=[selected["location"]] if selected["location"] else None,
        title_query=selected["q"],
        sort=selected["sort"],
    )


def _is_new(job, new_since: Optional[datetime]) -> bool:
    """First stored by the latest scrape run (jobs you added yourself don't count)."""
    return (
        new_since is not None
        and job.date_scraped >= new_since
        and job.source != MANUAL_SOURCE
    )


def _sort_url(sort: str) -> str:
    """This list with a different sort, every other filter kept."""
    args = request.args.to_dict(flat=False)
    args["sort"] = [sort]
    return url_for("web.jobs", **args)


@bp.app_template_filter("posted")
def posted(value: Optional[datetime]) -> str:
    """Short age for the list: "today", "3d", "5w", "4mo"."""
    if value is None:
        return ""
    days = (utcnow() - value).days
    if days < 1:
        return "today"
    if days < 14:
        return f"{days}d"
    if days < 60:
        return f"{days // 7}w"
    return f"{days // 30}mo"


@bp.get("/")
def jobs():
    selected = _filters(request.args)
    with get_session() as session:
        options = {
            "sources": present_sources(session),
            "companies": distinct_companies(session),
            "locations": distinct_locations(session),
        }
        summaries = _summaries(session, selected)
        new_since = latest_scrape_start(session)
    new_ids = {job.id for job in summaries if _is_new(job, new_since)}
    any_filter = bool(
        request.args.get("f") or request.args.getlist("status") or selected["sources"]
        or selected["company"] or selected["location"] or selected["q"]
    )
    return render_template(
        "jobs.html",
        jobs=summaries,
        new_ids=new_ids,
        unread=sum(1 for job in summaries if job.opened_at is None),
        selected=selected,
        options=options,
        statuses=STATUSES,
        not_interested=NOT_INTERESTED,
        has_jobs=bool(options["sources"]),
        # The status filter is the default "To Apply" queue (heading).
        queue_view=not request.args.getlist("status") and not request.args.get("f"),
        any_filter=any_filter,
        sort_url=_sort_url,
        here=request.full_path.rstrip("?"),
    )


@bp.post("/jobs/mark-read")
def mark_read():
    """Mark every job in the list you were looking at as read."""
    target = _safe_next(request.form.get("next", ""))
    selected = _filters(parse_qs(urlsplit(target).query))
    with get_session() as session:
        ids = [job.id for job in _summaries(session, selected) if job.opened_at is None]
        changed = mark_opened(session, ids)
        session.commit()
    flash(f"Marked {changed} job(s) as read.", "info")
    return redirect(target)


@bp.get("/jobs/<int:job_id>")
def job(job_id: int):
    with get_session() as session:
        found = get_job(session, job_id)
        if found is not None and found.opened_at is None:
            mark_opened(session, [job_id])  # opening a job marks it read
            session.commit()
            session.refresh(found)
    if found is None:
        abort(404)
    generator = _generator()
    return render_template(
        "job.html", job=found, statuses=STATUSES, here=request.full_path.rstrip("?"),
        generating=generator.running(job_id), errors=generator.pop_errors(job_id),
    )


def _generator():
    return current_app.extensions["generator"]


@bp.post("/jobs/<int:job_id>/generate")
def generate(job_id: int):
    """Start generating one material for this job in the background."""
    kind = request.form.get("kind", "")
    if kind not in MATERIALS:
        abort(400, f"Unknown material {kind!r}.")
    with get_session() as session:
        if get_job(session, job_id) is None:
            abort(404)
    label = MATERIALS[kind][2]
    if _generator().start(job_id, kind):
        flash(f"Generating the {label}. This can take a few minutes; the page "
              "updates when it's ready, and you can keep working meanwhile.", "info")
    else:
        flash(f"The {label} is already being generated.", "info")
    return redirect(url_for("web.job", job_id=job_id))


@bp.get("/jobs/<int:job_id>/generating")
def generating(job_id: int):
    """What's still generating for this job, for the page's reload check."""
    return jsonify(running=_generator().running(job_id))


@bp.post("/jobs/<int:job_id>/status")
def update_status(job_id: int):
    status = request.form.get("status", "")
    if status not in STATUSES:
        abort(400, f"Unknown status {status!r}.")
    with get_session() as session:
        if get_job(session, job_id) is None:
            abort(404)
        mark_opened(session, [job_id])  # acting on a job counts as reading it
        set_job_status(session, job_id, status)  # commits both
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
