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
from typing import TYPE_CHECKING

from songforge.web.rate_limit import Identity

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from songforge.config import Settings


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


# ── Request identity resolution (issue #15, criterion #1) ────────────────────────────
#
# The single identity-resolution path shared by ``POST /create`` and ``GET /quota``: a
# valid signed cookie is reused; an absent/malformed/tampered one mints a fresh identity
# (whose caller then sets the signed cookie on the response). ``is_authenticated`` is
# always ``False`` for now -- the accounts seam (no accounts system exists yet).


def resolve_identity(request: Request, settings: Settings) -> Identity:
    """Resolve the request's identity from its signed cookie, minting a fresh one if the
    cookie is absent, malformed, or tampered. Never authenticated yet (accounts seam)."""
    raw = request.cookies.get(settings.identity_cookie_name)
    if raw is not None:
        user_id = unsign(raw, secret=settings.session_secret)
        if user_id is not None:
            return Identity(user_id=user_id, is_authenticated=False, minted=False)
    return Identity(user_id=mint(), is_authenticated=False, minted=True)


def client_ip(request: Request, settings: Settings) -> str:
    """The client's IP for the per-IP cap.

    Uses the direct socket peer by default. A forwarded header
    (``settings.client_ip_header``) is trusted ONLY when the operator has configured its
    name -- otherwise a client-supplied header is ignored, so nobody can spoof their IP
    to dodge the per-IP cap. When the configured header is absent on a request, falls
    back to the socket peer.
    """
    if settings.client_ip_header:
        forwarded = request.headers.get(settings.client_ip_header)
        if forwarded:
            # A proxy may append a comma-separated chain; the first hop is the client.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client is not None else ""


def set_identity_cookie(response: Response, user_id: str, settings: Settings) -> None:
    """Set the signed identity cookie for a freshly-minted identity.

    ``secure`` is config-driven off ``environment``: this cookie is the sole identity
    token, so outside local dev (where plain HTTP is expected) it must never be sent over
    an unencrypted connection.
    """
    response.set_cookie(
        settings.identity_cookie_name,
        sign(user_id, secret=settings.session_secret),
        max_age=settings.identity_cookie_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.environment != "local",
    )
