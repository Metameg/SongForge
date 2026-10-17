"""Alembic environment — runs migrations synchronously against Postgres.

The DB URL comes from the single config module (a sync psycopg URL derived from the
async one), and target metadata is the ORM ``Base`` so autogenerate stays accurate.

Issue #19 Phase-5 fix: uses ``sync_worker_database_url`` (not ``sync_database_url``) so
migrations always run over a DIRECT connection, bypassing PgBouncer, even when
``DATABASE_URL`` (the web tier's own URL) points at a transaction-mode pooler --
matching compose's ``migrate`` service, which already gives itself a direct URL. Falls
back to ``database_url`` when no ``WORKER_DATABASE_URL`` override is set, so
local/single-node behavior is unchanged.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from songforge.config import get_settings
from songforge.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().sync_worker_database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
