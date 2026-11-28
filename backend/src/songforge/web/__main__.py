"""Run the web app with uvicorn: ``python -m songforge.web`` (or ``songforge-web``)."""

from __future__ import annotations

import uvicorn

from songforge.config import get_settings
from songforge.fd_limit import raise_open_file_limit
from songforge.logging_setup import configure_logging, get_logger


def main() -> None:
    settings = get_settings()
    # Each held-open SSE listener is one file descriptor; lift Docker's common soft
    # limit of 1024 before accepting connections (see songforge/fd_limit.py).
    limit = raise_open_file_limit()
    configure_logging(level=settings.log_level)
    get_logger(__name__).info(
        "open_file_limit", soft_before=limit.before, soft=limit.after, hard=limit.hard
    )
    uvicorn.run(
        "songforge.web.app:app",
        host="0.0.0.0",  # noqa: S104 - containerised service binds all interfaces
        # Issue #19 (PRD #6 AC#4): Railway (and PaaS deploys generally) injects the
        # port to bind via the `$PORT` env var -- `Settings.web_port` is the single
        # place that's read (aliased to `PORT`; defaults to 8000 unchanged for local
        # compose, where nothing sets it).
        port=settings.web_port,
        log_config=None,  # structlog owns logging; don't let uvicorn reconfigure it
        access_log=False,  # our middleware emits the structured JSON access log instead
    )


if __name__ == "__main__":
    main()
