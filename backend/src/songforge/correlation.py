"""Correlation ID propagated through a request/job via a context variable.

A single song's journey (submit → webhook → ingest → play) is traceable by stamping
one correlation ID and carrying it on every log line (spec #77). The ID lives in a
:class:`contextvars.ContextVar` so it is isolated per async task / request without being
threaded through every function signature.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Return the correlation ID bound to the current context, if any."""
    return _correlation_id.get()


def set_correlation_id(value: str) -> Token[str | None]:
    """Bind ``value`` as the current correlation ID; returns a reset token."""
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restore the correlation ID to what it was before :func:`set_correlation_id`."""
    _correlation_id.reset(token)


def clear_correlation_id() -> None:
    """Unbind any correlation ID from the current context."""
    _correlation_id.set(None)


def new_correlation_id() -> str:
    """Generate, bind, and return a fresh correlation ID."""
    value = uuid.uuid4().hex
    _correlation_id.set(value)
    return value


@contextmanager
def bind_correlation_id(value: str | None) -> Iterator[str]:
    """Bind ``value`` as the correlation ID for the duration of the block, minting a
    fresh id if ``value`` is ``None`` (e.g. a pre-#18 row with no stored correlation
    id -- see ``songforge.models.Job.correlation_id``). Restores whatever was bound
    before on exit (issue #18, acceptance criterion #1) -- a caller nested inside a
    request's own ``CorrelationIdMiddleware`` binding (e.g.
    ``web/routes/webhook.py::receive_webhook``) gets that prior binding back, not
    ``None``, and a drain loop that binds one job after another never leaks one job's
    id into the next.

    Sets the contextvar directly (not via :func:`new_correlation_id`, which also binds)
    to avoid a redundant double-bind when minting a fresh id.
    """
    cid = value if value is not None else uuid.uuid4().hex
    token = set_correlation_id(cid)
    try:
        yield cid
    finally:
        reset_correlation_id(token)
