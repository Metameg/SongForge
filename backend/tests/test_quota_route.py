"""Edge test for `GET /quota` (issue #15, criterion #5: songs-left display backend).

Drives the FastAPI app through real HTTP (`TestClient`), overriding the injectable
rate-limiter dependency with a fake so this test needs no live Redis -- mirrors
`tests/test_create_route.py`'s dependency-override edge pattern.

`GET /quota` doesn't exist yet (404 today), and the `get_rate_limiter` dependency it is
expected to share with `POST /create` doesn't exist on `songforge.web.routes.create`
either -- imported locally inside each test/helper below (mirrors
`tests/test_now_playing.py`'s NOTE-documented convention) so a missing symbol doesn't
prevent other test files from collecting.

Response shape assumed here (per `.orchestrator/CONTEXT.md`: "report remaining under
the anon cookie + IP, return the tighter/effective remaining"): `limit` is the tighter
(min) of the two anonymous daily caps -- the cap that explains the reported `remaining`
count for the currently-anonymous-only identity resolver.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from songforge.config import get_settings
from songforge.web.app import create_app
from songforge.web.identity import mint, sign


class _FakeQuotaLimiter:
    def __init__(self, *, remaining_value: int) -> None:
        self._remaining_value = remaining_value

    async def consume(self, identity: Any, ip: str) -> Any:
        raise AssertionError("GET /quota must not consume a slot, only report it")

    async def remaining(self, identity: Any, ip: str) -> int:
        return self._remaining_value

    async def refund(self, identity: Any, ip: str) -> None:
        return None


def _build_quota_client(*, remaining_value: int) -> TestClient:
    from songforge.web.routes.create import get_rate_limiter

    app = create_app()
    app.dependency_overrides[get_rate_limiter] = lambda: _FakeQuotaLimiter(
        remaining_value=remaining_value
    )
    return TestClient(app)


def test_quota_reports_remaining_enforced_and_limit_for_the_current_identity() -> None:
    client = _build_quota_client(remaining_value=2)
    settings = get_settings()
    expected_limit = min(settings.anon_daily_songs_per_cookie, settings.anon_daily_songs_per_ip)

    resp = client.get("/quota")

    assert resp.status_code == 200
    assert resp.json() == {
        "remaining": 2,
        "enforced": settings.enforce_rate_limits,
        "limit": expected_limit,
    }


def test_quota_mints_and_sets_an_identity_cookie_when_absent() -> None:
    client = _build_quota_client(remaining_value=2)
    settings = get_settings()

    resp = client.get("/quota")

    assert resp.status_code == 200
    assert settings.identity_cookie_name in resp.cookies


def test_quota_reuses_an_existing_valid_identity_cookie_without_reminting() -> None:
    client = _build_quota_client(remaining_value=2)
    settings = get_settings()
    existing_user_id = mint()
    signed = sign(existing_user_id, secret=settings.session_secret)
    client.cookies.set(settings.identity_cookie_name, signed)

    resp = client.get("/quota")

    assert resp.status_code == 200
    assert settings.identity_cookie_name not in resp.cookies
