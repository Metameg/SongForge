"""`ObjectStorage.presigned_download_url` (issue #39): a short-TTL presigned GET URL that
forces an attachment download named after the song. Pure signing -- no network."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from songforge.storage import ObjectStorage


def _storage() -> ObjectStorage:
    return ObjectStorage(
        endpoint_url="http://minio:9000",
        access_key_id="test",
        secret_access_key="test-secret",
        bucket="songforge-audio",
        region="auto",
        public_base_url="https://cdn.example.com/audio-bucket",
    )


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_url_targets_the_object_key() -> None:
    url = _storage().presigned_download_url("audio/song-1.mp3", "My Song.mp3")
    assert urlparse(url).path.endswith("/songforge-audio/audio/song-1.mp3")


def test_url_carries_attachment_disposition_with_filename() -> None:
    url = _storage().presigned_download_url("audio/song-1.mp3", "My Song.mp3")
    assert _query(url)["response-content-disposition"] == ['attachment; filename="My Song.mp3"']


def test_explicit_ttl_is_embedded() -> None:
    url = _storage().presigned_download_url("audio/s.mp3", "s.mp3", expires_in=120)
    assert _query(url)["X-Amz-Expires"] == ["120"]


def test_default_ttl_comes_from_settings() -> None:
    from songforge.config import get_settings

    url = _storage().presigned_download_url("audio/s.mp3", "s.mp3")
    assert _query(url)["X-Amz-Expires"] == [str(get_settings().s3_presigned_download_ttl_seconds)]


def test_explicit_ttl_overrides_configured_default() -> None:
    from songforge.config import get_settings

    configured = get_settings().s3_presigned_download_ttl_seconds
    url = _storage().presigned_download_url("audio/s.mp3", "s.mp3", expires_in=configured + 7)
    assert _query(url)["X-Amz-Expires"] == [str(configured + 7)]


def test_default_ttl_follows_overridden_setting(monkeypatch) -> None:
    from songforge.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("S3_PRESIGNED_DOWNLOAD_TTL_SECONDS", "42")
    try:
        storage = ObjectStorage.from_settings()
    finally:
        get_settings.cache_clear()
    url = storage.presigned_download_url("audio/s.mp3", "s.mp3")
    assert _query(url)["X-Amz-Expires"] == ["42"]


def test_unicode_filename_survives_in_disposition() -> None:
    url = _storage().presigned_download_url("audio/s.mp3", "Café 夜.mp3")
    assert _query(url)["response-content-disposition"] == ['attachment; filename="Café 夜.mp3"']
