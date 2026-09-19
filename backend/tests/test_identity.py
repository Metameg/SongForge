"""Unit tests for ``songforge.web.identity``: sign/unsign round-trip (issue #12, #1).

Production change that turns these green: implementing ``sign``/``unsign`` in
``songforge/web/identity.py`` with a real HMAC-SHA256 digest and a constant-time
compare (``hmac.compare_digest``). No datastore involved — pure function tests.
"""

from __future__ import annotations

from songforge.web.identity import mint, sign, unsign

SECRET = "test-secret-key"


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
