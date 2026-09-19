"""Config module is the single env-driven source of truth (acceptance criterion #3)."""

from __future__ import annotations

import pytest

from songforge.config import Settings


def _base_env() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://u:p@db:5432/songforge",
        "REDIS_URL": "redis://redis:6379/0",
        "S3_ENDPOINT_URL": "http://minio:9000",
        "S3_ACCESS_KEY_ID": "minio",
        "S3_SECRET_ACCESS_KEY": "minio-secret",
        "S3_BUCKET": "songforge-audio",
    }


def test_settings_load_from_env() -> None:
    settings = Settings(_env=_base_env())
    assert settings.database_url == "postgresql+asyncpg://u:p@db:5432/songforge"
    assert settings.redis_url == "redis://redis:6379/0"
    assert settings.s3_bucket == "songforge-audio"


def test_enforce_rate_limits_defaults_true() -> None:
    """Dev matches prod by default; unlimited is deliberate (spec user story #29)."""
    settings = Settings(_env=_base_env())
    assert settings.enforce_rate_limits is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("false", False), ("0", False), ("no", False), ("true", True), ("1", True)],
)
def test_enforce_rate_limits_parses_booleans(raw: str, expected: bool) -> None:
    settings = Settings(_env={**_base_env(), "ENFORCE_RATE_LIMITS": raw})
    assert settings.enforce_rate_limits is expected


def test_rate_limit_values_have_single_source_defaults() -> None:
    settings = Settings(_env=_base_env())
    # Values from the spec (#28): all limits config-driven from one place.
    assert settings.anon_daily_songs_per_cookie == 2
    assert settings.anon_daily_songs_per_ip == 6
    assert settings.accounts_per_ip == 2
    assert settings.per_user_concurrent_jobs == 2
    assert settings.global_generation_concurrency == 1


def test_rate_limit_values_override_from_env() -> None:
    settings = Settings(_env={**_base_env(), "ANON_DAILY_SONGS_PER_COOKIE": "5"})
    assert settings.anon_daily_songs_per_cookie == 5


def test_sync_database_url_derived_for_alembic() -> None:
    """Alembic needs a sync driver; it is derived, not separately configured."""
    settings = Settings(_env=_base_env())
    assert settings.sync_database_url == "postgresql+psycopg://u:p@db:5432/songforge"


def test_missing_required_field_raises() -> None:
    env = _base_env()
    del env["DATABASE_URL"]
    with pytest.raises(Exception):
        Settings(_env=env)


def test_identity_and_job_queue_tunables_have_documented_defaults() -> None:
    """Issue #12: single config source for the identity cookie, prompt validation,
    semaphore keys, LISTEN/NOTIFY channel names, and the dispatch poll/backoff knobs
    (`.orchestrator/CONTEXT.md` "New config tunables")."""
    settings = Settings(_env=_base_env())
    assert settings.session_secret == "dev-insecure-change-me"
    assert settings.identity_cookie_name == "sf_uid"
    assert settings.identity_cookie_max_age_seconds == 60 * 60 * 24 * 365
    assert settings.prompt_max_length == 2000
    assert settings.lyrics_max_length == 5000
    assert settings.musicgpt_webhook_url == "http://web:8000/api/generation/webhook"
    assert settings.semaphore_global_key == "sem:gen:global"
    assert settings.semaphore_user_key_prefix == "sem:gen:user:"
    assert settings.jobs_new_channel == "new_job"
    assert settings.semaphore_release_channel == "sem_release"
    assert settings.dispatch_poll_backstop_seconds == 5.0
    assert settings.dispatch_requeue_backoff_seconds == 5.0


def test_identity_and_job_queue_tunables_override_from_env() -> None:
    settings = Settings(
        _env={
            **_base_env(),
            "SESSION_SECRET": "prod-secret",
            "IDENTITY_COOKIE_NAME": "uid",
            "IDENTITY_COOKIE_MAX_AGE_SECONDS": "3600",
            "PROMPT_MAX_LENGTH": "500",
            "LYRICS_MAX_LENGTH": "1500",
            "MUSICGPT_WEBHOOK_URL": "http://web:9000/hook",
            "SEMAPHORE_GLOBAL_KEY": "sem:g",
            "SEMAPHORE_USER_KEY_PREFIX": "sem:u:",
            "JOBS_NEW_CHANNEL": "nj",
            "SEMAPHORE_RELEASE_CHANNEL": "sr",
            "DISPATCH_POLL_BACKSTOP_SECONDS": "1.5",
            "DISPATCH_REQUEUE_BACKOFF_SECONDS": "10",
        }
    )
    assert settings.session_secret == "prod-secret"
    assert settings.identity_cookie_name == "uid"
    assert settings.identity_cookie_max_age_seconds == 3600
    assert settings.prompt_max_length == 500
    assert settings.lyrics_max_length == 1500
    assert settings.musicgpt_webhook_url == "http://web:9000/hook"
    assert settings.semaphore_global_key == "sem:g"
    assert settings.semaphore_user_key_prefix == "sem:u:"
    assert settings.jobs_new_channel == "nj"
    assert settings.semaphore_release_channel == "sr"
    assert settings.dispatch_poll_backstop_seconds == 1.5
    assert settings.dispatch_requeue_backoff_seconds == 10.0


def test_musicgpt_base_url_is_swappable() -> None:
    """Simulator swapped in by base-url config (spec #70)."""
    settings = Settings(_env={**_base_env(), "MUSICGPT_BASE_URL": "http://simulator:8080"})
    assert settings.musicgpt_base_url == "http://simulator:8080"


def test_radio_tunables_have_documented_defaults() -> None:
    """Issue #8: the coordinator's anti-repeat window, advisory-lock key, fallback
    track length, and retry backoff all come from this one config surface."""
    settings = Settings(_env=_base_env())
    assert settings.radio_recent_history_size == 5
    assert settings.radio_advisory_lock_key == 927_341
    assert settings.radio_default_track_seconds == 180
    assert settings.radio_coordinator_backoff_seconds == 5.0


def test_radio_tunables_override_from_env() -> None:
    settings = Settings(
        _env={
            **_base_env(),
            "RADIO_RECENT_HISTORY_SIZE": "10",
            "RADIO_ADVISORY_LOCK_KEY": "42",
            "RADIO_DEFAULT_TRACK_SECONDS": "240",
            "RADIO_COORDINATOR_BACKOFF_SECONDS": "2.5",
        }
    )
    assert settings.radio_recent_history_size == 10
    assert settings.radio_advisory_lock_key == 42
    assert settings.radio_default_track_seconds == 240
    assert settings.radio_coordinator_backoff_seconds == 2.5
