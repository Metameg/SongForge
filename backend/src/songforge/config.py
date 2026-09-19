"""Single env-driven configuration module — the one source of truth for all tunables.

Everything the system reads at runtime (datastore URLs, object-storage endpoint, rate
limits, generation concurrency, the ``ENFORCE_RATE_LIMITS`` kill switch, observability
knobs) is declared here and read from the environment. No other module should call
``os.environ`` directly. This satisfies spec user stories #28 (single config source) and
#29 (one boolean disables rate-limit enforcement; dev matches prod by default).
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "staging", "prod"]

# Serializes the environment swap in `Settings(_env=...)` so concurrent construction
# (e.g. parallel test workers) cannot race on the process-wide os.environ.
_env_swap_lock = threading.Lock()


class Settings(BaseSettings):
    """Typed, validated view of the process environment.

    Construct with no arguments to read the real environment. Pass ``_env=<mapping>``
    to load from an explicit mapping instead (used by tests for determinism).
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        env_file=None,
    )

    # ── Runtime identity ────────────────────────────────────────────────────
    environment: Environment = "local"
    service_name: str = "songforge"
    log_level: str = "INFO"

    # ── Datastores ──────────────────────────────────────────────────────────
    # Async driver (asyncpg) is the runtime default; Alembic derives a sync URL.
    database_url: str
    redis_url: str

    # ── Object storage (S3 API: MinIO locally, Cloudflare R2 in prod) ───────
    s3_endpoint_url: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_bucket: str
    s3_region: str = "auto"
    # Public/CDN base URL audio is served from; falls back to the endpoint+bucket.
    s3_public_base_url: str | None = None
    # Grant anonymous read on the audio bucket at boot so browsers/CDN can stream it
    # (PRD: public bucket + CDN, spec #44/#48). Applied best-effort against MinIO's S3
    # API; on R2 public access is configured out-of-band, so a failure is non-fatal.
    # Set false to keep the bucket private (e.g. if serving via signed URLs instead).
    s3_public_bucket: bool = True

    # ── Static library (seeded at boot; spec #76) ───────────────────────────
    # Directory of curated *.mp3 files uploaded + cataloged on boot. Bind-mounted
    # locally; an object-storage prefix in prod.
    static_library_dir: str = "/app/static-library"

    # ── Generation API (real MusicGPT, or the fault-injectable simulator) ───
    musicgpt_base_url: str = "http://simulator:8080"
    musicgpt_api_key: str = ""

    # ── Rate limits & concurrency (single source; spec #28, #4, #10) ────────
    enforce_rate_limits: bool = True
    anon_daily_songs_per_cookie: int = 2
    anon_daily_songs_per_ip: int = 6
    accounts_per_ip: int = 2
    authed_daily_songs: int = 10
    per_user_concurrent_jobs: int = 2
    global_generation_concurrency: int = 1

    # ── Worker ──────────────────────────────────────────────────────────────
    worker_heartbeat_path: str = "/tmp/songforge-worker.heartbeat"  # noqa: S108
    worker_loop_interval_seconds: float = 5.0

    # ── Radio (issue #8: static radio plays) ────────────────────────────────
    # Redis capped-list anti-repeat window: how many recently-played static song ids
    # to avoid when picking the next one.
    radio_recent_history_size: int = 5
    # Fixed pg_advisory_lock key the single-leader coordinator holds.
    radio_advisory_lock_key: int = 927_341
    # Fallback song duration when a Song row has no duration_seconds recorded.
    radio_default_track_seconds: int = 180
    # Backoff between coordinator retries when idle (no songs yet) or on a
    # transient error, so a broken loop doesn't spin hot.
    radio_coordinator_backoff_seconds: float = 5.0

    # ── Radio pointer cache (issue #9: Redis pointer cache + cold-start warming) ────
    # Redis string key the coordinator writes the resolved `/now-playing` view to
    # after every successful init/advance, and the web tier reads to serve requests
    # without touching Postgres (design D1/D2).
    radio_pointer_redis_key: str = "radio:pointer"
    # TTL of each web instance's process-local pointer cache entry (design D3):
    # short enough that a song boundary is reflected promptly, long enough that a
    # request storm within the window costs 0 datastore reads.
    radio_pointer_cache_ttl_seconds: float = 1.0

    # ── Identity (issue #12: signed-cookie anon identity, criterion #1) ─────
    # HMAC key signing the identity cookie (stdlib hmac; see web/identity.py). MUST be
    # overridden in prod -- a default/leaked secret lets a client forge another
    # identity's cookie and, e.g., exhaust its rate-limit quota or read its jobs.
    session_secret: str = "dev-insecure-change-me"
    identity_cookie_name: str = "sf_uid"
    identity_cookie_max_age_seconds: int = 60 * 60 * 24 * 365  # ~1 year

    # ── Prompt validation (issue #12, criterion #1) ──────────────────────────
    prompt_max_length: int = 2000
    # `lyrics` is the same attacker-controlled, unauthenticated-request field as
    # `prompt` (security report MEDIUM finding) -- capped the same way so it can't
    # be left as an unbounded resource-abuse vector (oversized DB rows / oversized
    # outbound generation-API bodies) just because `prompt`'s cap doesn't cover it.
    lyrics_max_length: int = 5000

    # ── Generation job pipeline (issue #12) ───────────────────────────────────
    # Callback base URL sent as `webhook_url` on the generation API's create call; the
    # receiving handler is a later issue (see .orchestrator/CONTEXT.md scope) -- #12
    # only sends a plausible, config-driven URL.
    musicgpt_webhook_url: str = "http://web:8000/api/generation/webhook"
    # Redis semaphore keys (criterion #3: global + per-user in-flight generation caps).
    semaphore_global_key: str = "sem:gen:global"
    semaphore_user_key_prefix: str = "sem:gen:user:"
    # Postgres LISTEN/NOTIFY channel names (criterion #5): new-job wakes dispatch
    # without polling; semaphore-release wakes a dispatcher waiting on a full cap.
    jobs_new_channel: str = "new_job"
    semaphore_release_channel: str = "sem_release"
    # Slow-poll backstop for the dispatch loop, in case a NOTIFY is missed (e.g. a
    # dispatcher was down when it fired).
    dispatch_poll_backstop_seconds: float = 5.0
    # Backoff applied to a job's `available_at` on a 429/5xx/timeout requeue, so
    # dispatch doesn't hot-loop re-claiming the same job immediately.
    dispatch_requeue_backoff_seconds: float = 5.0

    # ── Observability (always on, every environment; spec #77) ──────────────
    metrics_enabled: bool = True
    correlation_id_header: str = "X-Correlation-ID"

    def __init__(self, _env: Mapping[str, str] | None = None, **data: Any) -> None:
        if _env is None:
            super().__init__(**data)
            return
        # Load *exclusively* from the provided mapping (deterministic for tests):
        # swap the process environment for the duration of construction so the normal
        # env source reads only `_env`, then restore it. A truly missing required field
        # therefore raises, rather than being satisfied by an ambient variable. The lock
        # keeps this global swap safe under concurrent construction.
        with _env_swap_lock:
            saved = dict(os.environ)
            os.environ.clear()
            os.environ.update(_env)
            try:
                super().__init__(**data)
            finally:
                os.environ.clear()
                os.environ.update(saved)

    @property
    def sync_database_url(self) -> str:
        """Sync SQLAlchemy URL for Alembic (migrations run synchronously).

        Derived from :attr:`database_url` rather than separately configured, so the
        two can never drift.
        """
        return self.database_url.replace("+asyncpg", "+psycopg", 1)

    @property
    def public_audio_base_url(self) -> str:
        """Base URL that immutable audio objects are served from."""
        if self.s3_public_base_url:
            return self.s3_public_base_url.rstrip("/")
        return f"{self.s3_endpoint_url.rstrip('/')}/{self.s3_bucket}"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings read from the real environment."""
    return Settings()
