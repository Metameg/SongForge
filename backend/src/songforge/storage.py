"""S3-API object-storage abstraction: MinIO locally, Cloudflare R2 in prod.

Audio the platform generates is ingested here so it survives the provider's URL expiry
and plays forever (spec #42, #47). Objects are **immutable** and long-cached — a song
re-entering rotation is already warm at the edge (spec #45). Dev and prod differ only by
the configured endpoint (spec #73).
"""

from __future__ import annotations

import functools
import json
import re
from typing import Any
from urllib.parse import quote

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from songforge.config import Settings, get_settings
from songforge.logging_setup import get_logger

log = get_logger(__name__)

# Immutable objects → tell the CDN it can cache them effectively forever.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
DEFAULT_AUDIO_CONTENT_TYPE = "audio/mpeg"


def audio_key(song_id: str, ext: str = "mp3") -> str:
    """Canonical, immutable object key for a song's audio."""
    return f"audio/{song_id}.{ext}"


_UNSAFE_FILENAME_CHARS = re.compile(r'["\\\x00-\x1f\x7f]')
MAX_TITLE_CHARS = 150


def download_filename(title: str, fallback: str) -> str:
    """``<title>.mp3``: unsafe chars dropped, title bounded, ``fallback`` if nothing is left."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("", title).strip()[:MAX_TITLE_CHARS].strip()
    return f"{cleaned or fallback}.mp3"


def content_disposition(filename: str) -> str:
    """Attachment header with an ASCII ``filename=`` fallback and RFC 5987 ``filename*=``."""
    safe = _UNSAFE_FILENAME_CHARS.sub("", filename)
    ascii_name = safe.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe, safe='')}"


class ObjectStorage:
    """Thin wrapper over an S3 client scoped to one bucket."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        region: str = "auto",
        public_base_url: str | None = None,
        public_read: bool = True,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        presigned_download_ttl: int = 300,
        presign_endpoint_url: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url.rstrip("/")
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._public_read = public_read
        self._presigned_download_ttl = presigned_download_ttl
        # Path-style addressing works uniformly for MinIO and R2. Explicit socket
        # timeouts (quality report MED finding, issue #13 review): without these,
        # a hung MinIO/R2 connection blocks the calling thread indefinitely --
        # config-driven so local/staging/prod can tune independently.
        client_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )

        def _make_client(endpoint: str) -> Any:
            return boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region,
                config=client_config,
            )

        self._client = _make_client(endpoint_url)
        # Presigned download URLs must be SIGNED against a browser-reachable host: the
        # SigV4 signature covers the host, so it only validates when the browser connects
        # to that same host (issue #39). In dev `endpoint_url` is the internal
        # `minio:9000`; `presign_endpoint_url` points at the host-mapped MinIO instead.
        # Unset (prod R2, already browser-reachable) → reuse the main client.
        self._presign_client = (
            _make_client(presign_endpoint_url)
            if presign_endpoint_url and presign_endpoint_url != endpoint_url
            else self._client
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ObjectStorage":
        settings = settings or get_settings()
        return cls(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            public_base_url=settings.s3_public_base_url,
            public_read=settings.s3_public_bucket,
            connect_timeout=settings.s3_connect_timeout_seconds,
            read_timeout=settings.s3_read_timeout_seconds,
            presigned_download_ttl=settings.s3_presigned_download_ttl_seconds,
            presign_endpoint_url=settings.s3_presign_endpoint_url,
        )

    def public_url(self, key: str) -> str:
        """CDN/public URL an object is served from (spec #44)."""
        base = self._public_base_url or f"{self.endpoint_url}/{self.bucket}"
        return f"{base}/{key}"

    def ensure_bucket(self) -> None:
        """Create the bucket only if it is genuinely absent (idempotent).

        Only a 404 means "missing" → create. Any other error (e.g. a 403 where the
        bucket exists but the R2 token lacks HeadBucket permission) is re-raised rather
        than masked by a blind create_bucket, so a real auth problem surfaces instead of
        the abstraction silently diverging between MinIO and R2.
        """
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 404:
                raise
            self._client.create_bucket(Bucket=self.bucket)
        if self._public_read:
            self._ensure_public_read()

    def _ensure_public_read(self) -> None:
        """Grant anonymous ``s3:GetObject`` on the audio objects (PRD public bucket).

        Audio is streamed straight to browsers and CDN edges, so objects must be
        publicly readable (spec #44/#48). On MinIO this is a bucket policy set via the
        S3 API; on Cloudflare R2 public access is configured out-of-band (dashboard /
        custom domain) and this call may be unsupported — so it is **best-effort**: a
        failure is logged, never fatal, and boot proceeds. Idempotent (overwrites).
        """
        policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "PublicReadAudio",
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{self.bucket}/*"],
                    }
                ],
            }
        )
        try:
            self._client.put_bucket_policy(Bucket=self.bucket, Policy=policy)
        except Exception as exc:  # noqa: BLE001 - public policy is best-effort (see docstring)
            log.warning("bucket_public_policy_unset", bucket=self.bucket, error=str(exc))

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return False
        return True

    def put(
        self, key: str, data: bytes, content_type: str = DEFAULT_AUDIO_CONTENT_TYPE
    ) -> str:
        """Upload immutable object bytes; returns its public URL."""
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl=IMMUTABLE_CACHE_CONTROL,
        )
        return self.public_url(key)

    def presigned_download_url(
        self, key: str, filename: str, expires_in: int | None = None
    ) -> str:
        """Short-TTL presigned GET URL forcing an attachment download (issue #39).

        Signed with ``self._presign_client`` so the URL targets a browser-reachable host
        (see ``presign_endpoint_url`` in ``__init__``)."""
        url: str = self._presign_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": content_disposition(filename),
            },
            ExpiresIn=(
                expires_in if expires_in is not None else self._presigned_download_ttl
            ),
        )
        return url

    def download(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        body: bytes = response["Body"].read()
        return body


@functools.lru_cache(maxsize=1)
def get_storage() -> ObjectStorage:
    return ObjectStorage.from_settings()
