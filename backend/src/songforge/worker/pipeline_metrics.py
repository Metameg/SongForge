"""Refresh the fleet-wide pipeline gauges on a timer, exposed on the worker's ``/metrics``.

Computed here once per interval instead of by every web replica on every scrape; with
several worker replicas each reports the same value and the dashboard takes ``max()``.
"""

from __future__ import annotations

import asyncio

from songforge.config import Settings
from songforge.db import get_sessionmaker
from songforge.logging_setup import get_logger
from songforge.metrics_pipeline import refresh_pipeline_gauges
from songforge.redis_client import get_redis

log = get_logger(__name__)


async def run_pipeline_metrics(settings: Settings, stop: asyncio.Event) -> None:
    if not settings.metrics_enabled:
        return
    sessionmaker = get_sessionmaker()
    redis = get_redis()
    while not stop.is_set():
        await refresh_pipeline_gauges(sessionmaker, redis, settings)
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.pipeline_metrics_interval_seconds
            )
        except asyncio.TimeoutError:
            pass
