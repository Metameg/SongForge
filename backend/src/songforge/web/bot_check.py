"""Bot check gating ``POST /create`` (issue #15, criterion #3).

A pluggable gate that deters scripted farming of the create action. The default
implementation (``HeaderTokenBotCheck``) is a simple shared-secret header check kept
behind the ``BotCheck`` protocol, so a real CAPTCHA/challenge provider can drop in later
without touching the route.

Two documented bypasses, both intentional:
- an empty ``bot_check_token`` (the default) disables the check -- the local-dev seam, so
  developers are not walled off with no token to send;
- ``enforce_rate_limits`` being off disables the check along with every other limit
  (criterion #4: one boolean disables all enforcement).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from starlette.requests import Request

    from songforge.config import Settings


class BotCheck(Protocol):
    """The gate ``POST /create`` calls before persisting anything. ``verify`` returns
    whether the request is allowed to proceed (``True`` = human/allowed)."""

    async def verify(self, request: Request) -> bool:
        ...


class HeaderTokenBotCheck:
    """Default bot check: the request must carry ``bot_check_header`` equal to the
    configured ``bot_check_token``. Disabled when the token is empty (dev seam) or when
    ``enforce_rate_limits`` is off."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def verify(self, request: Request) -> bool:
        settings = self._settings
        if not settings.enforce_rate_limits:
            return True
        if not settings.bot_check_token:
            return True
        return request.headers.get(settings.bot_check_header) == settings.bot_check_token
