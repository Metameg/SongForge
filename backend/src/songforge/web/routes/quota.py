"""``GET /quota`` — remaining daily "songs left" for the current identity (issue #15, #5).

Backs the frontend songs-left indicator (criterion #5). Resolves the request identity the
same way ``POST /create`` does -- minting and setting the signed cookie when absent so a
first-time visitor's quota is tracked under a stable identity -- and reports the tighter
of the applicable daily caps minus current use, plus whether enforcement is on.

Shares the injectable ``get_rate_limiter`` dependency with ``POST /create`` so a fake
substitutes cleanly in tests (no live Redis needed). This endpoint only *reads* the
quota: it calls ``remaining`` and never ``consume``, so viewing "songs left" never
charges a slot.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from songforge.config import get_settings
from songforge.web.identity import client_ip, resolve_identity, set_identity_cookie
from songforge.web.rate_limit import RateLimiter
from songforge.web.routes.create import get_rate_limiter

router = APIRouter(tags=["quota"])


class QuotaResponse(BaseModel):
    """``GET /quota`` response — the songs-left figure the UI renders."""

    remaining: int
    enforced: bool
    limit: int


@router.get("/quota", response_model=QuotaResponse)
async def get_quota(
    request: Request,
    response: Response,
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> QuotaResponse:
    """Report the current identity's remaining daily quota without consuming a slot."""
    settings = get_settings()
    identity = resolve_identity(request, settings)
    ip = client_ip(request, settings)

    remaining = await rate_limiter.remaining(identity, ip)
    # The identity resolver is anonymous-only for now (accounts seam), so the effective
    # ceiling is the tighter of the two anonymous daily caps -- the cap that explains the
    # reported `remaining`.
    limit = min(settings.anon_daily_songs_per_cookie, settings.anon_daily_songs_per_ip)

    if identity.minted:
        set_identity_cookie(response, identity.user_id, settings)

    return QuotaResponse(
        remaining=remaining, enforced=settings.enforce_rate_limits, limit=limit
    )
