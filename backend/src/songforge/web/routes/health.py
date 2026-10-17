"""Liveness and readiness probes used by docker-compose healthchecks."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from songforge import __version__, db, redis_client
from songforge.logging_setup import get_logger

router = APIRouter(tags=["health"])
log = get_logger(__name__)


@router.get("/health")
async def health() -> dict[str, str]:
    """Simple aggregate liveness for humans and load balancers."""
    return {"status": "ok", "service": "songforge-web", "version": __version__}


@router.get("/health/live")
async def live() -> dict[str, str]:
    """Liveness: the process is up and serving. Always 200 while the app runs."""
    return {"status": "alive"}


@router.get("/health/ready")
async def ready(response: Response) -> dict[str, object]:
    """Readiness: dependencies reachable. 503 if Postgres or Redis is down.

    docker-compose gates traffic on this so the stack only reports healthy once its
    datastores answer.
    """
    checks: dict[str, bool] = {}
    try:
        checks["database"] = await db.check_database()
    except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
        checks["database"] = False
        log.warning("readiness_check_failed", component="database", error=str(exc))
    try:
        checks["redis"] = await redis_client.check_redis()
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = False
        log.warning("readiness_check_failed", component="redis", error=str(exc))

    ok = all(checks.values())
    response.status_code = status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ok else "not_ready", "checks": checks}
