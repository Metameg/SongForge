"""Unit tests for the bot-check gate (issue #15, criterion #3).

``songforge/web/bot_check.py`` does not exist yet -- every test below is RED at
collection time (``ModuleNotFoundError``), the correct RED signature for a brand-new
module. No network/CAPTCHA provider involved: the default implementation is a simple
shared-secret header check, injectable behind a ``BotCheck`` protocol so a real
CAPTCHA/provider can drop in later without touching ``POST /create``.

Bare ``starlette.requests.Request`` objects are built directly from an ASGI scope (no
app, no TestClient, no socket) since ``verify`` only reads request headers.
"""

from __future__ import annotations

from starlette.requests import Request

from songforge.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _make_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/create",
        "headers": raw_headers,
        "client": ("203.0.113.5", 12345),
    }
    return Request(scope)


async def test_verify_passes_when_the_configured_header_matches_the_token() -> None:
    from songforge.web.bot_check import HeaderTokenBotCheck

    settings = _settings(bot_check_header="X-Bot-Check", bot_check_token="shh-secret")
    check = HeaderTokenBotCheck(settings)
    request = _make_request({"X-Bot-Check": "shh-secret"})

    assert await check.verify(request) is True


async def test_verify_fails_when_the_header_is_missing() -> None:
    from songforge.web.bot_check import HeaderTokenBotCheck

    settings = _settings(bot_check_header="X-Bot-Check", bot_check_token="shh-secret")
    check = HeaderTokenBotCheck(settings)
    request = _make_request()

    assert await check.verify(request) is False


async def test_verify_fails_when_the_header_value_is_wrong() -> None:
    from songforge.web.bot_check import HeaderTokenBotCheck

    settings = _settings(bot_check_header="X-Bot-Check", bot_check_token="shh-secret")
    check = HeaderTokenBotCheck(settings)
    request = _make_request({"X-Bot-Check": "wrong-value"})

    assert await check.verify(request) is False


async def test_verify_passes_regardless_of_header_when_no_token_is_configured() -> None:
    """Local-dev seam (documented in the module): an empty ``bot_check_token`` (the
    default) disables the check entirely rather than blocking every request with no way
    to pass it."""
    from songforge.web.bot_check import HeaderTokenBotCheck

    settings = _settings(bot_check_header="X-Bot-Check", bot_check_token="")
    check = HeaderTokenBotCheck(settings)
    request = _make_request()  # no header sent at all

    assert await check.verify(request) is True


async def test_verify_reads_the_configured_header_name() -> None:
    from songforge.web.bot_check import HeaderTokenBotCheck

    settings = _settings(bot_check_header="X-Custom-Check", bot_check_token="tok")
    check = HeaderTokenBotCheck(settings)
    request = _make_request({"X-Custom-Check": "tok"})

    assert await check.verify(request) is True
