"""Injectable delay seam for the simulator's webhook scheduling.

Lets tests assert scheduled-webhook timing deterministically — via a fake ``SleepFn`` that
records the requested delay and returns immediately, or blocks on a test-controlled gate —
without ever using a real ``asyncio.sleep`` wait (issue #11 TDD discipline).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

SleepFn = Callable[[float], Awaitable[None]]


async def real_sleep(seconds: float) -> None:
    """Default sleep implementation: a real ``asyncio.sleep``."""
    await asyncio.sleep(seconds)
