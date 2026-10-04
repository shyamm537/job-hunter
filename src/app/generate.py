"""Generate one material for one job from the dashboard (KAN-43).

The job page's "Generate cover letter" and "Generate email" buttons start a
background thread here, so the page answers at once and you can keep triaging
while the LLM works (a generation can take minutes). The page asks
`running()` whether anything is still in progress and reloads when it's done.

State is in memory: a restart forgets what was running (the result of a
finished generation is already in the database) and any error not yet shown.
"""

import logging
import threading

import requests

from src.llm.client import get_llm_client
from src.llm.prompts import MATERIALS, build_prompt
from src.storage.database import get_job, get_session

log = logging.getLogger("jobhunter.app.generate")

_GONE = "the job no longer exists (it may have been archived)"


class Generator:
    def __init__(self, llm_settings: dict, resume_summary: str):
        self.llm_settings = llm_settings
        self.resume_summary = resume_summary
        self._lock = threading.Lock()
        self._running: set[tuple[int, str]] = set()
        self._errors: dict[tuple[int, str], str] = {}
        self._threads: list[threading.Thread] = []

    def start(self, job_id: int, kind: str) -> bool:
        """Start generating `kind` for the job; False if it's already running."""
        if kind not in MATERIALS:
            raise ValueError(f"Unknown material {kind!r}")
        key = (job_id, kind)
        with self._lock:
            if key in self._running:
                return False
            self._running.add(key)
            self._errors.pop(key, None)
        thread = threading.Thread(target=self._run, args=key, daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()
        return True

    def running(self, job_id: int) -> list[str]:
        """The kinds still generating for this job."""
        with self._lock:
            return sorted(kind for jid, kind in self._running if jid == job_id)

    def pop_errors(self, job_id: int) -> dict[str, str]:
        """Errors from this job's finished generations, cleared once read."""
        with self._lock:
            found = {kind: msg for (jid, kind), msg in self._errors.items() if jid == job_id}
            for kind in found:
                del self._errors[(job_id, kind)]
            return found

    def wait(self) -> None:
        """Block until every started generation has finished (for tests)."""
        for thread in list(self._threads):
            thread.join()

    def _run(self, job_id: int, kind: str) -> None:
        column, _, label = MATERIALS[kind]
        try:
            # Read, call the LLM, then write in a fresh session, so no
            # database connection is held during a minutes-long generation.
            with get_session() as session:
                job = get_job(session, job_id)
                if job is None:
                    raise LookupError(_GONE)
                prompt = build_prompt(kind, job, self.resume_summary)
            text = get_llm_client({"llm": self.llm_settings}).generate(prompt)
            if not text:
                raise ValueError("the model returned an empty response")
            with get_session() as session:
                job = get_job(session, job_id)
                if job is None:
                    raise LookupError(_GONE)
                setattr(job, column, text)
                session.add(job)
                session.commit()
            log.info("Generated the %s for job %d.", label, job_id)
        except Exception as exc:
            if isinstance(exc, requests.ConnectionError):
                host = self.llm_settings.get("host", "the LLM server")
                reason = f"couldn't reach {host}. Is Ollama running?"
                log.warning("Generating the %s for job %d failed: %s", label, job_id, exc)
            else:
                reason = str(exc)
                log.exception("Generating the %s for job %d failed.", label, job_id)
            with self._lock:
                self._errors[(job_id, kind)] = f"Couldn't generate the {label}: {reason}"
        finally:
            with self._lock:
                self._running.discard((job_id, kind))
