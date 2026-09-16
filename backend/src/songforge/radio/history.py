"""Redis-backed `RecentHistoryStore`: the hot anti-repeat window (issue #8, criterion #2).

Thin I/O only — the anti-repeat *decision* (candidate set + recent set -> chosen id)
lives entirely in `songforge.radio.selection.pick_static` and is exercised by the fast
unit tests via a stub `RecentHistoryStore` (see `test_radio_coordinator.py`). This
implementation is wired into the worker's leader loop only (`worker/radio_coordinator.py`);
it is not exercised by any unit test — live Redis is an integration concern, per the PRD
testing decision to keep the coordinator's decision logic on the fast unit path.
"""

from __future__ import annotations

from redis.asyncio import Redis

RECENT_HISTORY_KEY = "radio:recent_history"


class RedisRecentHistoryStore:
    """A `songforge.radio.coordinator.RecentHistoryStore` backed by a Redis capped list.

    Newest-first: `record` pushes onto the head and trims the list back down to
    `max_len`, so `recent_ids` always reflects at most the last `max_len` plays.
    """

    def __init__(
        self, redis: Redis, *, max_len: int, key: str = RECENT_HISTORY_KEY
    ) -> None:
        self._redis = redis
        self._max_len = max_len
        self._key = key

    async def recent_ids(self) -> set[str]:
        """The static song ids played within the current capped window."""
        if self._max_len <= 0:
            return set()
        raw = await self._redis.lrange(self._key, 0, self._max_len - 1)
        # `decode_responses=True` on the shared client means these are already `str`
        # at runtime; the redis-py stubs are generic over bytes|str, so normalize
        # explicitly rather than asserting the narrower type.
        return {item if isinstance(item, str) else item.decode() for item in raw}

    async def record(self, song_id: str) -> None:
        """Push `song_id` onto the window and trim it back down to `max_len`."""
        await self._redis.lpush(self._key, song_id)
        await self._redis.ltrim(self._key, 0, max(self._max_len - 1, 0))
