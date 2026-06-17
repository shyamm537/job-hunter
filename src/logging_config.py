"""Minimal structured-logging setup.

The CLIs used bare `print()` for output, flagged as a gap in
docs/contributing.md and docs/roadmap.md. This replaces it with the stdlib
`logging` module so output has levels and timestamps and can be silenced or
redirected without editing code. Level is controlled by the
`JOBHUNTER_LOG_LEVEL` environment variable (default INFO).

This is intentionally tiny — it's not a logging framework, just one
`basicConfig` call in one place so every module logs consistently.
"""

import logging
import os

_CONFIGURED = False


def setup_logging(level: str | None = None) -> None:
    """Configure root logging once. Safe to call from every CLI entry point."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    resolved = level or os.environ.get("JOBHUNTER_LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=resolved.upper(),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    _CONFIGURED = True
