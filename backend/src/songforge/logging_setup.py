"""Structured JSON logging via structlog, always on in every environment.

Every emitted line is a JSON object carrying a level, an ISO timestamp, the event, any
bound key/values, and the current correlation ID (spec #77). Configuring twice is safe
and idempotent, so both the web app and the worker can call :func:`configure_logging`
at startup.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO, cast

import structlog
from structlog.typing import EventDict, WrappedLogger

from songforge import correlation


def _add_correlation_id(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: merge the context-bound correlation ID onto every event."""
    event_dict["correlation_id"] = correlation.get_correlation_id()
    return event_dict


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Configure structlog + stdlib logging to emit JSON to ``stream`` (default stdout)."""
    out = stream if stream is not None else sys.stdout
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=out, level=log_level, force=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _add_correlation_id,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
