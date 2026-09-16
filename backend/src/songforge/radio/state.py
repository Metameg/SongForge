"""The ``radio_state`` pointer and the ``/now-playing`` read model (criteria #1, #3).

``radio_state`` is a single fixed-id row (``RADIO_STATE_SINGLETON_ID``) giving the
current song, its window on the shared server timeline (``started_at``/``ends_at``), and
the CAS ``version`` the coordinator's advance step relies on (see
``songforge.radio.coordinator``). This module owns reading that pointer — joined with its
song — and shaping it for the ``/now-playing`` HTTP response, including the server's
current time so the client can correct clock skew (criterion #3).

Issue #8, phase 3 (green) implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from songforge.models import RADIO_STATE_SINGLETON_ID, RadioState, Song, Source
from songforge.storage import get_storage


@dataclass(frozen=True)
class NowPlayingView:
    """Everything ``GET /now-playing`` needs, already joined with the song row."""

    song_id: str
    title: str
    source: Source
    object_key: str
    audio_url: str
    started_at: datetime
    ends_at: datetime
    duration_seconds: int | None
    playback_id: str
    version: int

    def to_response(self, *, server_time: datetime) -> dict[str, Any]:
        """Shape this pointer into the ``/now-playing`` JSON body (criterion #3).

        Body per the PRD: ``song_id, title, source, object_key, audio_url, started_at,
        ends_at, duration, playback_id, version, server_time`` — timestamps as ISO-8601
        UTC strings.
        """
        return {
            "song_id": self.song_id,
            "title": self.title,
            "source": self.source,
            "object_key": self.object_key,
            "audio_url": self.audio_url,
            "started_at": self.started_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "duration": self.duration_seconds,
            "playback_id": self.playback_id,
            "version": self.version,
            "server_time": server_time.isoformat(),
        }


async def get_now_playing(session: AsyncSession) -> NowPlayingView | None:
    """Read the current pointer joined with its song.

    Returns ``None`` when idle: no ``radio_state`` row yet (never initialized), or a
    pointer whose ``song_id`` is unset/no longer resolves to a song. The web route turns
    ``None`` into the not-playing HTTP response rather than a 500.
    """
    pointer = await session.get(RadioState, RADIO_STATE_SINGLETON_ID)
    if pointer is None or pointer.song_id is None:
        return None
    song = await session.get(Song, pointer.song_id)
    if song is None:
        return None
    if pointer.started_at is None or pointer.ends_at is None or pointer.playback_id is None:
        return None

    return NowPlayingView(
        song_id=song.id,
        title=song.title,
        source=song.source,
        object_key=song.object_key,
        audio_url=get_storage().public_url(song.object_key),
        started_at=pointer.started_at,
        ends_at=pointer.ends_at,
        duration_seconds=song.duration_seconds,
        playback_id=pointer.playback_id,
        version=pointer.version,
    )
