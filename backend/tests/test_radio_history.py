"""Characterization tests for `RedisRecentHistoryStore` (issue #8, criterion #3).

Criterion #3 ("a capped Redis recently-played list drives anti-repeat selection") was
already built by #8 and is wired into `worker/radio_coordinator.py`, but its docstring
notes it "is not exercised by any unit test" -- live Redis was treated as an integration
concern. Per the issue #9 context pack ("verify/keep... do not re-architect"), these tests
PIN its current LPUSH+LTRIM capped-window behavior against a minimal in-memory async Redis
list double, so a Phase 2/3 refactor around it (the coordinator gaining a Redis pointer
writer) cannot silently break the anti-repeat window. These are expected to PASS already
-- they are not part of the RED set for #9's new behavior.
"""

from __future__ import annotations

from songforge.radio.history import RedisRecentHistoryStore


class _FakeListRedis:
    """In-memory async double of the narrow Redis list API this store uses
    (`lpush`, `ltrim`, `lrange`) -- enough to characterize newest-first + capping."""

    def __init__(self) -> None:
        self._lists: dict[str, list[str]] = {}

    async def lpush(self, key: str, value: str) -> None:
        self._lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        items = self._lists.get(key, [])
        # redis LTRIM end is inclusive.
        self._lists[key] = items[start : end + 1]

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self._lists.get(key, [])
        return items[start : end + 1]


async def test_recent_ids_is_empty_before_anything_is_recorded() -> None:
    store = RedisRecentHistoryStore(_FakeListRedis(), max_len=3)  # type: ignore[arg-type]
    assert await store.recent_ids() == set()


async def test_record_adds_the_song_to_recent_ids() -> None:
    redis = _FakeListRedis()
    store = RedisRecentHistoryStore(redis, max_len=3)  # type: ignore[arg-type]

    await store.record("song-1")

    assert await store.recent_ids() == {"song-1"}


async def test_window_is_capped_at_max_len_newest_first() -> None:
    redis = _FakeListRedis()
    store = RedisRecentHistoryStore(redis, max_len=2)  # type: ignore[arg-type]

    await store.record("song-1")
    await store.record("song-2")
    await store.record("song-3")  # pushes "song-1" out of the capped window

    assert await store.recent_ids() == {"song-2", "song-3"}


async def test_a_max_len_of_zero_yields_an_always_empty_window() -> None:
    redis = _FakeListRedis()
    store = RedisRecentHistoryStore(redis, max_len=0)  # type: ignore[arg-type]

    await store.record("song-1")

    assert await store.recent_ids() == set()


async def test_uses_a_distinct_key_when_configured() -> None:
    redis = _FakeListRedis()
    store = RedisRecentHistoryStore(redis, max_len=3, key="radio:recent_history:test")  # type: ignore[arg-type]

    await store.record("song-1")

    assert await store.recent_ids() == {"song-1"}
    assert "radio:recent_history:test" in redis._lists
