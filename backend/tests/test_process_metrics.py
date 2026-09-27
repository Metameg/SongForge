"""App-process resource metrics are exposed on the shared registry (issue #18 follow-up).

The app registers its metrics on a *dedicated* ``CollectorRegistry`` rather than the
prometheus-client default, so the default process/platform/GC collectors are NOT picked
up automatically. Without them ``/metrics`` carries no process CPU/memory/fd/GC series,
which a production USE view needs. These tests pin the contract that the standard
process collectors are registered on ``songforge.metrics.REGISTRY``.
"""

from __future__ import annotations

from prometheus_client import generate_latest

from songforge.metrics import REGISTRY


def _rendered() -> str:
    return generate_latest(REGISTRY).decode()


def test_process_resource_metrics_are_exposed() -> None:
    text = _rendered()
    # ProcessCollector: cpu, resident/virtual memory, file descriptors, start time.
    assert "process_cpu_seconds_total" in text
    assert "process_resident_memory_bytes" in text
    assert "process_virtual_memory_bytes" in text
    assert "process_open_fds" in text
    assert "process_start_time_seconds" in text


def test_python_platform_and_gc_metrics_are_exposed() -> None:
    text = _rendered()
    # PlatformCollector exposes python_info; GCCollector exposes python_gc_* series.
    assert "python_info" in text
    assert "python_gc_collections_total" in text
