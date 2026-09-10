"""S3-API object-storage abstraction: MinIO locally, Cloudflare R2 in prod.

Audio the platform generates is ingested here so it survives the provider's URL expiry
and plays forever (spec #42, #47). Objects are **immutable** and long-cached — a song
re-entering rotation is already warm at the edge (spec #45). Dev and prod differ only by
the configured endpoint (spec #73).
"""

from __future__ import annotations

import functools

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from songforge.config import Settings, get_settings

# Immutable objects → tell the CDN it can cache them effectively forever.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
DEFAULT_AUDIO_CONTENT_TYPE = "audio/mpeg"


def audio_key(song_id: str, ext: str = "mp3") -> str:
    """Canonical, immutable object key for a song's audio."""
    return f"audio/{song_id}.{ext}"


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
    ) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url.rstrip("/")
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            # Path-style addressing works uniformly for MinIO and R2.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
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

    def download(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        body: bytes = response["Body"].read()
        return body


@functools.lru_cache(maxsize=1)
def get_storage() -> ObjectStorage:
    return ObjectStorage.from_settings()
