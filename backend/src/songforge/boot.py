"""Boot sequence: migrate the schema and seed the static library *before* serving.

Run as a container entrypoint step ahead of uvicorn/the worker (``songforge-boot``), so
the app never serves traffic against an unmigrated schema or an empty library
(acceptance criterion #2, spec #76). Every step is idempotent and safe to re-run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

from songforge.config import Settings, get_settings
from songforge.db import get_sessionmaker
from songforge.logging_setup import configure_logging, get_logger
from songforge.seed import (
    discover_static_tracks,
    seed_static_library,
    upload_static_audio,
)
from songforge.storage import ObjectStorage

log = get_logger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    return cfg


def run_migrations() -> None:
    """Upgrade the database to the latest schema revision.

    Issue #19 Phase-5 fix: the actual connection URL used is
    ``Settings.sync_worker_database_url`` (set in ``migrations/env.py``), a DIRECT
    connection that bypasses PgBouncer -- not the web tier's pooled ``database_url``.
    This matters because ``songforge-boot`` (this function's caller) is Railway's
    ``releaseCommand``, which runs inside the ``web`` service and would otherwise
    inherit its pooled ``DATABASE_URL``, running Alembic DDL through a transaction-mode
    pooler.
    """
    log.info("migrations_started")
    command.upgrade(_alembic_config(), "head")
    log.info("migrations_complete")


async def seed(settings: Settings | None = None) -> int:
    """Upload + catalog the static library. Returns the number of new songs inserted."""
    settings = settings or get_settings()
    tracks = discover_static_tracks(settings.static_library_dir)

    storage = ObjectStorage.from_settings(settings)
    upload_static_audio(storage, tracks)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        inserted = await seed_static_library(session, tracks)
    return inserted


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level)
    log.info("boot_started", environment=settings.environment)
    run_migrations()
    inserted = asyncio.run(seed(settings))
    log.info("boot_complete", static_songs_inserted=inserted)


if __name__ == "__main__":
    main()
