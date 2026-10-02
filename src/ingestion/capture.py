"""Capture unfiltered scrape output to disk, for debugging and test fixtures.

When a scrape returns nothing useful you usually want to see what the source
*actually* returned, before any title/location filtering. This writes each
scrape's raw JobPost list to a JSON file. The same files double as realistic
fixtures for offline tests later (real shapes, no network).

Off by default. Enable by setting the JOBHUNTER_DUMP_DIR environment variable:

- a directory path  -> dump there
- "temp" / "auto" / "1" -> create a fresh temp folder for the run

Nothing is written unless asked.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src.storage.models import JobPost

ENV_VAR = "JOBHUNTER_DUMP_DIR"
_AUTO_VALUES = {"1", "true", "temp", "auto"}


def new_temp_dump_dir() -> str:
    """Create and return a fresh temp folder for a dump session."""
    return tempfile.mkdtemp(prefix="jobhunter-scrape-")


def resolve_dump_dir() -> Optional[str]:
    """Resolve the dump directory from JOBHUNTER_DUMP_DIR, or None if unset.

    A magic value ("temp"/"auto"/"1") creates a fresh temp folder so all of a
    run's scrapes land together; any other value is treated as a path.
    """
    val = os.environ.get(ENV_VAR)
    if not val:
        return None
    if val.strip().lower() in _AUTO_VALUES:
        return new_temp_dump_dir()
    return val


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "scrape"


def dump_jobs(label: str, jobs: List[JobPost], dest_dir: str) -> str:
    """Write `jobs` (unfiltered) as JSON to dest_dir/<slug>-<timestamp>.json.

    Returns the path written. The payload is `{label, captured_at, count,
    jobs: [...]}` so a fixture file is self-describing.
    """
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(dest_dir) / f"{_slug(label)}-{ts}.json"
    payload = {
        "label": label,
        "captured_at": ts,
        "count": len(jobs),
        "jobs": [job.model_dump(mode="json") for job in jobs],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(path)


def load_jobs(path: str) -> List[JobPost]:
    """Re-hydrate JobPost objects from a dump file (for use as test fixtures)."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [JobPost(**row) for row in payload["jobs"]]
