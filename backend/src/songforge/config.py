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

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "staging", "prod"]

# The documented dev-safe placeholder (see `session_secret` below). Fine for
# local/staging/tests; must never reach a prod deploy (security report MEDIUM
# finding: an unenforced default silently lets a client forge any identity's
# cookie in prod).
_DEFAULT_SESSION_SECRET = "dev-insecure-change-me"

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
    # botocore socket timeouts for the underlying S3 client (quality report MED
    # finding): without these, a hung MinIO/R2 connection would block indefinitely --
    # unbounded, unlike the rest of this module's explicit timeouts.
    s3_connect_timeout_seconds: float = 10.0
    s3_read_timeout_seconds: float = 30.0

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

    # ── Rate-limit daily window + counter keys (issue #15, criteria #1/#4) ───
    # TTL stamped on each daily counter on its FIRST increment (default 24h). The key
    # also carries a per-day UTC bucket, so "songs left" resets both by TTL expiry and
    # by the day rolling over.
    rate_limit_window_seconds: int = 60 * 60 * 24
    rate_limit_cookie_prefix: str = "rl:cookie:"
    rate_limit_ip_prefix: str = "rl:ip:"
    rate_limit_account_prefix: str = "rl:account:"

    # ── Client IP resolution (issue #15, criterion #1) ──────────────────────
    # Trusted forwarded header carrying the real client IP behind a proxy/LB. Left
    # unset (``None``), the direct socket peer (``request.client.host``) is used and any
    # forwarded header is IGNORED -- trusting a client-supplied header with no known
    # proxy in front lets anyone spoof their IP and dodge the per-IP cap. Set to e.g.
    # ``X-Forwarded-For`` only when a trusted proxy always overwrites it.
    client_ip_header: str | None = None

    # ── Bot check (issue #15, criterion #3) ─────────────────────────────────
    # Shared-secret header gate on ``POST /create``. An empty ``bot_check_token`` (the
    # default) disables the check -- the documented local-dev seam -- so dev is not
    # walled off with no way to pass it; a real CAPTCHA/provider can replace
    # ``HeaderTokenBotCheck`` behind the ``BotCheck`` protocol without touching the
    # route. The gate is also bypassed entirely when ``enforce_rate_limits`` is false.
    bot_check_header: str = "X-Bot-Check"
    bot_check_token: str = ""

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
    session_secret: str = _DEFAULT_SESSION_SECRET
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

    # ── Async ingest (issue #13: webhook -> download -> R2 upload -> READY) ────
    # Postgres LISTEN/NOTIFY channel the webhook handler NOTIFYs after recording a
    # successful webhook (state -> INGEST_PENDING), and the ingest worker LISTENs on.
    ingest_channel: str = "ingest_pending"
    # Slow-poll backstop for the ingest loop, mirrors dispatch_poll_backstop_seconds.
    ingest_poll_backstop_seconds: float = 5.0
    # httpx timeout for downloading the finished audio from its (possibly expiring)
    # hint URL, or from the by-id-refreshed URL.
    ingest_download_timeout_seconds: float = 30.0
    # Backoff applied to a job's `available_at` on a download/by-id-lookup failure
    # that isn't resolved by the single in-claim refresh-and-retry.
    ingest_requeue_backoff_seconds: float = 10.0
    # Bounded ingest retries before giving up -> FAILED (this is NOT the watchdog's
    # job; see .orchestrator/CONTEXT.md OUT-of-scope).
    ingest_max_attempts: int = 5
    # Hard ceiling on a single audio download's size (security report MED finding:
    # memory-exhaustion / DoS via a hostile or oversized `audio_url`). 100 MiB is
    # generously above any real song-length MP3; exceeding it fails the download the
    # same way a network error would (bounded retry -> requeue/FAILED).
    ingest_max_download_bytes: int = 100 * 1024 * 1024

    # ── SSE + pub/sub (issue #10: push + gapless transitions) ───────────────
    # Redis pub/sub channel the coordinator publishes the resolved pointer to on
    # every genuine init/advance, and that each app instance's `PointerBroadcaster`
    # subscribes to for its `/events` fan-out (criterion #2).
    radio_pointer_channel: str = "radio:pointer:changed"
    # How often `/events` re-emits the current pointer to each connected client with
    # no new pub/sub message — covers a dropped pub/sub message where nobody
    # disconnected, and gives a reconnecting client a bound on staleness (criterion #4).
    sse_heartbeat_seconds: float = 30.0

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

    @model_validator(mode="after")
    def _reject_default_session_secret_in_prod(self) -> Settings:
        """Fail-closed (security report MEDIUM finding): a default/placeholder
        `session_secret` is fine for local/staging (and every test in this repo,
        which never sets `ENVIRONMENT=prod`), but must never silently reach a prod
        deploy -- it's a public constant, so anyone could forge any identity's
        signed cookie. Raising here turns a forgotten override into a boot-time
        failure instead of a silent vulnerability."""
        if self.environment == "prod" and self.session_secret == _DEFAULT_SESSION_SECRET:
            raise ValueError(
                "session_secret is still the default placeholder in a prod "
                "environment -- set a real SESSION_SECRET before deploying to prod"
            )
        return self

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
