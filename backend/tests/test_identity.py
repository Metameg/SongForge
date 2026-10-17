"""Unit tests for ``songforge.web.identity``: sign/unsign round-trip (issue #12, #1).

Production change that turns these green: implementing ``sign``/``unsign`` in
``songforge/web/identity.py`` with a real HMAC-SHA256 digest and a constant-time
compare (``hmac.compare_digest``). No datastore involved — pure function tests.
"""

from __future__ import annotations

from starlette.requests import Request

from songforge.config import Settings
from songforge.web.identity import mint, sign, unsign

SECRET = "test-secret-key"


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _make_request(
    *,
    cookie_header: str | None = None,
    extra_headers: dict[str, str] | None = None,
    client_host: str = "203.0.113.5",
) -> Request:
    """Build a bare ASGI ``Request`` -- no app, no TestClient, no socket -- enough for
    ``resolve_identity``/``client_ip``, which only read ``.cookies``, ``.headers``, and
    ``.client.host`` off the request."""
    headers: list[tuple[bytes, bytes]] = []
    if cookie_header is not None:
        headers.append((b"cookie", cookie_header.encode()))
    for name, value in (extra_headers or {}).items():
        headers.append((name.lower().encode(), value.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (client_host, 12345),
    }
    return Request(scope)


def test_sign_unsign_round_trips() -> None:
    user_id = mint()

    signed = sign(user_id, secret=SECRET)

    assert unsign(signed, secret=SECRET) == user_id


def test_unsign_rejects_a_tampered_signature() -> None:
    user_id = mint()
    signed = sign(user_id, secret=SECRET)
    flipped_last_char = "0" if signed[-1] != "0" else "1"
    tampered = signed[:-1] + flipped_last_char

    assert unsign(tampered, secret=SECRET) is None


def test_unsign_rejects_a_tampered_user_id_with_the_original_signature() -> None:
    user_id = mint()
    signed = sign(user_id, secret=SECRET)
    body, _, digest = signed.rpartition(".")
    forged = f"{body}extra.{digest}"

    assert unsign(forged, secret=SECRET) is None


def test_unsign_rejects_the_wrong_secret() -> None:
    user_id = mint()
    signed = sign(user_id, secret=SECRET)

    assert unsign(signed, secret="a-different-secret") is None


def test_unsign_rejects_malformed_values() -> None:
    assert unsign("not-a-signed-value", secret=SECRET) is None
    assert unsign("", secret=SECRET) is None
    assert unsign(".", secret=SECRET) is None


def test_sign_output_does_not_leak_the_secret() -> None:
    user_id = mint()

    signed = sign(user_id, secret=SECRET)

    assert SECRET not in signed


def test_mint_returns_unique_values() -> None:
    assert mint() != mint()


# ── Identity resolver + client IP (issue #15, criterion #1 seam) ────────────────
#
# `resolve_identity`/`client_ip` don't exist in `songforge.web.identity` yet -- imported
# locally inside each test below (mirrors `tests/test_now_playing.py`'s NOTE-documented
# convention: a missing symbol then fails only these new tests, not the pre-existing
# sign/unsign round-trip tests above).


def test_resolve_identity_mints_a_new_identity_when_no_cookie_is_present() -> None:
    from songforge.web.identity import resolve_identity

    settings = _settings()
    request = _make_request()

    identity = resolve_identity(request, settings)

    assert identity.minted is True
    assert identity.is_authenticated is False
    assert identity.user_id


def test_resolve_identity_reuses_a_valid_signed_cookie_without_minting() -> None:
    from songforge.web.identity import resolve_identity

    settings = _settings()
    original_user_id = mint()
    signed = sign(original_user_id, secret=settings.session_secret)
    request = _make_request(cookie_header=f"{settings.identity_cookie_name}={signed}")

    identity = resolve_identity(request, settings)

    assert identity.user_id == original_user_id
    assert identity.minted is False
    assert identity.is_authenticated is False


def test_resolve_identity_remints_when_the_cookie_is_tampered() -> None:
    from songforge.web.identity import resolve_identity

    settings = _settings()
    original_user_id = mint()
    signed = sign(original_user_id, secret=settings.session_secret)
    flipped_last_char = "0" if signed[-1] != "0" else "1"
    tampered = signed[:-1] + flipped_last_char
    request = _make_request(cookie_header=f"{settings.identity_cookie_name}={tampered}")

    identity = resolve_identity(request, settings)

    assert identity.minted is True
    assert identity.user_id != original_user_id


def test_resolve_identity_is_never_authenticated_yet() -> None:
    """Seam: there is no accounts system yet (`.orchestrator/CONTEXT.md`) -- every
    resolved identity is anonymous until a future accounts issue plugs into this seam."""
    from songforge.web.identity import resolve_identity

    settings = _settings()
    request = _make_request()

    identity = resolve_identity(request, settings)

    assert identity.is_authenticated is False


def test_client_ip_uses_the_direct_socket_by_default() -> None:
    from songforge.web.identity import client_ip

    settings = _settings()  # client_ip_header defaults to None
    request = _make_request(client_host="203.0.113.5")

    assert client_ip(request, settings) == "203.0.113.5"


def test_client_ip_ignores_a_forwarded_header_when_not_configured() -> None:
    """Spoof guard: an attacker-controlled forwarded header must be IGNORED unless the
    operator has explicitly configured a trusted proxy header name -- otherwise any
    client could claim any IP and dodge the per-IP cap."""
    from songforge.web.identity import client_ip

    settings = _settings()  # client_ip_header is None
    request = _make_request(
        client_host="203.0.113.5", extra_headers={"X-Forwarded-For": "9.9.9.9"}
    )

    assert client_ip(request, settings) == "203.0.113.5"


def test_client_ip_uses_the_configured_forwarded_header_when_set() -> None:
    from songforge.web.identity import client_ip

    settings = _settings(client_ip_header="X-Forwarded-For")
    request = _make_request(
        client_host="203.0.113.5", extra_headers={"X-Forwarded-For": "9.9.9.9"}
    )

    assert client_ip(request, settings) == "9.9.9.9"


def test_client_ip_falls_back_to_the_socket_when_the_configured_header_is_missing() -> None:
    from songforge.web.identity import client_ip

    settings = _settings(client_ip_header="X-Forwarded-For")
    request = _make_request(client_host="203.0.113.5")  # header not sent

    assert client_ip(request, settings) == "203.0.113.5"
