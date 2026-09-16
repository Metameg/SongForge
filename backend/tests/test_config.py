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


def test_musicgpt_base_url_is_swappable() -> None:
    """Simulator swapped in by base-url config (spec #70)."""
    settings = Settings(_env={**_base_env(), "MUSICGPT_BASE_URL": "http://simulator:8080"})
    assert settings.musicgpt_base_url == "http://simulator:8080"
