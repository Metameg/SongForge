"""Signed-cookie anonymous identity (issue #12, criterion #1; spec #24).

Anonymous listeners/creators are identified by a UUID minted on first ``POST /create``
and carried in a long-lived cookie, signed with an HMAC keyed by
``Settings.session_secret`` so a client cannot forge or guess another user's identity
(PRD "Redesign identity & abuse": optional auth, anon listen+create). Uses stdlib
``hmac``/``hashlib`` rather than pulling in a new dependency like itsdangerous — the
wire format is intentionally tiny: ``"<user_id>.<hex hmac-sha256 digest>"``.

A tampered, malformed, or wrong-secret cookie must be rejected, not raise — the caller
(``web/routes/create.py``) mints a fresh identity on ``None`` rather than erroring the
request.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid


def mint() -> str:
    """Generate a new random identity (a UUID4, hex-encoded, no dashes)."""
    return uuid.uuid4().hex


def sign(user_id: str, *, secret: str) -> str:
    """Sign ``user_id`` for cookie storage. Returns ``"<user_id>.<digest>"``.

    The digest is an HMAC-SHA256 of ``user_id`` keyed by ``secret``, hex-encoded so the
    whole value is cookie-safe ASCII.
    """
    return f"{user_id}.{_digest(user_id, secret=secret)}"


def unsign(cookie_value: str, *, secret: str) -> str | None:
    """Verify and extract the user id from a signed cookie value.

    Returns the user id if the signature is valid, else ``None`` — covers a missing
    separator, an empty/malformed value, and a digest that doesn't match (wrong secret
    or tampering). Comparison MUST be constant-time (``hmac.compare_digest``) so a
    timing side-channel can't be used to forge a valid signature byte-by-byte.
    """
    user_id, separator, digest = cookie_value.rpartition(".")
    if not user_id or not separator or not digest:
        return None
    expected = _digest(user_id, secret=secret)
    if not hmac.compare_digest(expected, digest):
        return None
    return user_id


def _digest(user_id: str, *, secret: str) -> str:
    """The raw hex HMAC-SHA256 digest of ``user_id`` keyed by ``secret``."""
    return hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
