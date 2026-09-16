"""Object storage is accessed through one S3-API abstraction (acceptance criterion #5).

The same client talks to MinIO locally and Cloudflare R2 in prod — code differs only by
endpoint config (spec #73). Key/URL logic is pure and unit-tested; the round-trip is
exercised against an in-process S3 fake (moto).
"""

from __future__ import annotations

import importlib.util

import pytest
from botocore.exceptions import ClientError

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


class _FakeS3:
    """Minimal S3 client stub for exercising ensure_bucket's error handling."""

    def __init__(self, head_status: int | None) -> None:
        self.head_status = head_status  # None => bucket exists (no error)
        self.created = False

    def head_bucket(self, Bucket: str) -> None:  # noqa: N803 - boto3 kwarg name
        if self.head_status is not None:
            raise ClientError(
                {"Error": {"Code": str(self.head_status)},
                 "ResponseMetadata": {"HTTPStatusCode": self.head_status}},
                "HeadBucket",
            )

    def create_bucket(self, Bucket: str) -> None:  # noqa: N803
        self.created = True


def test_ensure_bucket_creates_when_missing() -> None:
    storage = _storage()
    fake = _FakeS3(head_status=404)
    storage._client = fake  # type: ignore[assignment]
    storage.ensure_bucket()
    assert fake.created is True


def test_ensure_bucket_reraises_on_forbidden() -> None:
    """A 403 (exists, no HeadBucket permission) must not be masked by a blind create."""
    storage = _storage()
    fake = _FakeS3(head_status=403)
    storage._client = fake  # type: ignore[assignment]
    with pytest.raises(ClientError):
        storage.ensure_bucket()
    assert fake.created is False


def test_ensure_bucket_noop_when_present() -> None:
    storage = _storage()
    fake = _FakeS3(head_status=None)
    storage._client = fake  # type: ignore[assignment]
    storage.ensure_bucket()
    assert fake.created is False


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
