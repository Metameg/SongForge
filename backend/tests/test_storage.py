"""Object storage is accessed through one S3-API abstraction (acceptance criterion #5).

The same client talks to MinIO locally and Cloudflare R2 in prod — code differs only by
endpoint config (spec #73). Key/URL logic is pure and unit-tested; the round-trip is
exercised against an in-process S3 fake (moto).
"""

from __future__ import annotations

import importlib.util

import pytest

from songforge.storage import ObjectStorage, audio_key

_HAS_MOTO = importlib.util.find_spec("moto") is not None


def _storage() -> ObjectStorage:
    return ObjectStorage(
        endpoint_url="http://minio:9000",
        access_key_id="test",
        secret_access_key="test-secret",
        bucket="songforge-audio",
        region="auto",
        public_base_url="https://cdn.example.com/audio-bucket",
    )


def test_audio_key_is_immutable_and_namespaced() -> None:
    assert audio_key("song-123") == "audio/song-123.mp3"
    assert audio_key("song-123", ext="wav") == "audio/song-123.wav"


def test_public_url_uses_cdn_base() -> None:
    storage = _storage()
    assert (
        storage.public_url("audio/song-123.mp3")
        == "https://cdn.example.com/audio-bucket/audio/song-123.mp3"
    )


def test_public_url_falls_back_to_endpoint_and_bucket() -> None:
    storage = ObjectStorage(
        endpoint_url="http://minio:9000",
        access_key_id="test",
        secret_access_key="test-secret",
        bucket="songforge-audio",
        region="auto",
        public_base_url=None,
    )
    assert (
        storage.public_url("audio/x.mp3")
        == "http://minio:9000/songforge-audio/audio/x.mp3"
    )


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_MOTO, reason="moto not installed")
def test_put_exists_and_download_round_trip() -> None:
    from moto import mock_aws

    with mock_aws():
        # moto intercepts standard AWS endpoints; the abstraction is identical to the
        # MinIO/R2 config — only the endpoint URL differs (the point of criterion #5).
        storage = ObjectStorage(
            endpoint_url="https://s3.us-east-1.amazonaws.com",
            access_key_id="test",
            secret_access_key="test-secret",
            bucket="songforge-audio",
            region="us-east-1",
            public_base_url="https://cdn.example.com/audio-bucket",
        )
        storage.ensure_bucket()
        key = audio_key("round-trip")
        assert storage.exists(key) is False

        storage.put(key, b"ID3-fake-audio-bytes", content_type="audio/mpeg")

        assert storage.exists(key) is True
        assert storage.download(key) == b"ID3-fake-audio-bytes"
