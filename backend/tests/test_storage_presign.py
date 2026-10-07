"""`ObjectStorage.presigned_download_url` (issue #39): a short-TTL presigned GET URL that
forces an attachment download named after the song. Pure signing -- no network."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from songforge.storage import (
    MAX_TITLE_CHARS,
    ObjectStorage,
    content_disposition,
    download_filename,
)


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
    assert _query(url)["response-content-disposition"] == [
        "attachment; filename=\"My Song.mp3\"; filename*=UTF-8''My%20Song.mp3"
    ]


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


def test_unicode_filename_gets_ascii_fallback_and_rfc5987_name() -> None:
    url = _storage().presigned_download_url("audio/s.mp3", "Café 夜.mp3")
    assert _query(url)["response-content-disposition"] == [
        "attachment; filename=\"Caf_ _.mp3\"; filename*=UTF-8''Caf%C3%A9%20%E5%A4%9C.mp3"
    ]


def test_crlf_and_quotes_are_neutralised_in_disposition() -> None:
    header = content_disposition('a"b\r\nc\\d.mp3')
    assert header == "attachment; filename=\"abcd.mp3\"; filename*=UTF-8''abcd.mp3"


def test_download_filename_bounds_title_length() -> None:
    name = download_filename("x" * 1000, "song-1")
    assert name == "x" * MAX_TITLE_CHARS + ".mp3"


def test_download_filename_falls_back_when_title_empty() -> None:
    assert download_filename(' "" ', "song-1") == "song-1.mp3"


def test_zero_expiry_is_not_upgraded_to_default() -> None:
    url = _storage().presigned_download_url("audio/s.mp3", "s.mp3", expires_in=0)
    assert _query(url)["X-Amz-Expires"] == ["0"]


def test_presign_signs_against_browser_reachable_endpoint_when_set() -> None:
    """In dev the S3 client talks to the internal ``minio:9000`` host the browser can't
    reach; a configured ``presign_endpoint_url`` signs the download URL against the
    host-mapped MinIO (``localhost:59000``) instead, so the 302 target is reachable and
    the SigV4 signature matches the host the browser actually connects to (issue #39)."""
    storage = ObjectStorage(
        endpoint_url="http://minio:9000",
        access_key_id="test",
        secret_access_key="test-secret",
        bucket="songforge-audio",
        region="auto",
        presign_endpoint_url="http://localhost:59000",
    )
    url = storage.presigned_download_url("audio/song-1.mp3", "My Song.mp3")
    parsed = urlparse(url)
    assert parsed.netloc == "localhost:59000"
    assert parsed.path.endswith("/songforge-audio/audio/song-1.mp3")
    # The signature must cover that host, so the usual SigV4 params are still present.
    assert "X-Amz-Signature" in _query(url)


def test_presign_defaults_to_main_endpoint_when_unset() -> None:
    """Prod R2's ``S3_ENDPOINT_URL`` is already browser-reachable, so with no override the
    download URL is signed against the main endpoint (unchanged behavior)."""
    url = _storage().presigned_download_url("audio/song-1.mp3", "My Song.mp3")
    assert urlparse(url).netloc == "minio:9000"


def test_from_settings_threads_presign_endpoint(monkeypatch) -> None:
    from songforge.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("S3_PRESIGN_ENDPOINT_URL", "http://localhost:59000")
    try:
        storage = ObjectStorage.from_settings()
    finally:
        get_settings.cache_clear()
    url = storage.presigned_download_url("audio/s.mp3", "s.mp3")
    assert urlparse(url).netloc == "localhost:59000"
