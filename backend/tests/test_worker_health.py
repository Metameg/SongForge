"""Worker heartbeat freshness drives the compose healthcheck."""

from __future__ import annotations

import os
import time
from pathlib import Path

from songforge.worker import health


def test_missing_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    assert health.heartbeat_age_seconds(path) is None
    assert health.is_healthy(path, max_age_seconds=10) is False


def test_fresh_heartbeat_is_healthy(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    health.touch_heartbeat(path)
    assert path.exists()
    assert health.is_healthy(path, max_age_seconds=10) is True


def test_stale_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    health.touch_heartbeat(path)
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert health.is_healthy(path, max_age_seconds=10) is False


def test_touch_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "hb"
    health.touch_heartbeat(path)
    assert path.exists()
