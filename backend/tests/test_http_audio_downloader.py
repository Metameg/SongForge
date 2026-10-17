"""Unit tests for `songforge.jobs.ingest.HttpAudioDownloader` in isolation (issue #13
phase-5 review, security report MED finding: unbounded in-memory download buffering).

`tests/test_ingest.py`'s `_FakeDownloader` never touches this class at all -- it's a
hand-authored stand-in for the `Downloader` port. This file exercises the REAL
`HttpAudioDownloader.download()` against a fake HTTP transport (`httpx.MockTransport`,
no real sockets) to prove the streaming max-bytes ceiling actually bounds memory use
and maps an oversized response to the same `AudioDownloadError` any other download
failure produces (routing through the normal bounded-retry -> requeue/FAILED path,
not a special case).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from songforge.jobs.ingest import AudioDownloadError, HttpAudioDownloader


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_download_returns_bytes_under_the_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"small-audio-bytes")

    async with _client(handler) as client:
        downloader = HttpAudioDownloader(client, timeout=5.0, max_bytes=1024)
        data = await downloader.download("http://audio.test/track.mp3")

    assert data == b"small-audio-bytes"


async def test_download_raises_on_a_non_200_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"expired")

    async with _client(handler) as client:
        downloader = HttpAudioDownloader(client, timeout=5.0, max_bytes=1024)
        with pytest.raises(AudioDownloadError):
            await downloader.download("http://audio.test/expired-token")


async def test_download_aborts_once_the_response_exceeds_max_bytes() -> None:
    """The cap fix: an oversized (or hostile) response must not be buffered
    unbounded into memory -- it fails the same way any other download failure does."""
    oversized = b"a" * 2048

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    async with _client(handler) as client:
        downloader = HttpAudioDownloader(client, timeout=5.0, max_bytes=1024)
        with pytest.raises(AudioDownloadError, match="exceeded max size"):
            await downloader.download("http://audio.test/huge-file.mp3")


async def test_download_succeeds_when_body_is_exactly_at_the_cap() -> None:
    """Boundary check: the cap rejects strictly-greater-than, not equal-to."""
    exactly_at_cap = b"a" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=exactly_at_cap)

    async with _client(handler) as client:
        downloader = HttpAudioDownloader(client, timeout=5.0, max_bytes=1024)
        data = await downloader.download("http://audio.test/exact.mp3")

    assert data == exactly_at_cap
