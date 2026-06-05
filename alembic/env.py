"""Alembic env. Uses the SYNC psycopg2 URL because Alembic is sync.

Importing `rootsign.models` registers all 7 ORM models on `Base.metadata`,
which Alembic uses for autogenerate diff support.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import side effect: register all models on Base.metadata.
from rootsign import models  # noqa: F401
from rootsign.config import settings
from rootsign.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow `-x db=test` to target the test database.
x_args = context.get_x_argument(as_dictionary=True)
target_db = x_args.get("db", "dev")
if target_db == "test":
    sync_url = settings.TEST_DATABASE_URL_SYNC
else:
    sync_url = os.environ.get("ALEMBIC_DATABASE_URL_SYNC", settings.DATABASE_URL_SYNC)

config.set_main_option("sqlalchemy.url", sync_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=sync_url,
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
