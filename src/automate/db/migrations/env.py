"""
db/migrations/env.py — Alembic environment configuration.

Loads the MySQL connection URL at runtime from environment variables
via DatabaseConfig — the password NEVER appears in alembic.ini or any
committed config file.

Supports both online (live DB) and offline (SQL-script) migration modes.
"""
import sys
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# ── Add project root to sys.path so `automate` package is importable ─────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))

from automate.config import DatabaseConfig
from automate.db.engine import Base

# Import ALL models so Alembic's autogenerate can detect them
import automate.db.models  # noqa: F401

# Alembic config object
config = context.config

# Inject the real DB URL from env — overrides the blank sqlalchemy.url in alembic.ini
DatabaseConfig.validate()
config.set_main_option("sqlalchemy.url", DatabaseConfig.url().replace("%", "%%"))

# Set up logging from the alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata object for autogenerate support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations without a live DB connection — produces a .sql script.
    Useful for reviewing what SQL will run before applying it.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations against the live database.
    Uses a plain connection (not a pool) to avoid connection-pool leaks
    during migration runs.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # no pooling during migrations
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
