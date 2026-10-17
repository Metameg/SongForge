"""Worker liveness via a heartbeat file (the worker has no HTTP surface).

The worker touches a heartbeat file each loop; the docker-compose healthcheck runs
``python -m songforge.worker.health`` which exits 0 while the file is fresh, non-zero if
it is stale or missing.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from songforge.config import get_settings


def touch_heartbeat(path: str | Path) -> None:
    """Record a fresh heartbeat at ``path``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(time.time()))


def heartbeat_age_seconds(path: str | Path) -> float | None:
    """Seconds since the last heartbeat, or None if the file is missing."""
    p = Path(path)
    if not p.exists():
        return None
    return time.time() - p.stat().st_mtime


def is_healthy(path: str | Path, max_age_seconds: float) -> bool:
    """True if a heartbeat exists and is younger than ``max_age_seconds``."""
    age = heartbeat_age_seconds(path)
    return age is not None and age <= max_age_seconds


def main() -> int:
    settings = get_settings()
    # Allow a couple of missed loops before reporting unhealthy.
    max_age = settings.worker_loop_interval_seconds * 3
    healthy = is_healthy(settings.worker_heartbeat_path, max_age)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
