from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.db_models import Base


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _db_url() -> str:
    url = (os.environ.get("DB_URL") or "").strip()
    if not url:
        host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
        port = os.environ.get("POSTGRES_PORT", "5432")
        db = os.environ.get("POSTGRES_DB", "ctf_agent")
        user = os.environ.get("POSTGRES_USER", "ctf_agent")
        # Keep defaults aligned with scripts/start-postgres.sh and .env.example.
        pw = os.environ.get("POSTGRES_PASSWORD", "ctf_agent")
        if pw:
            url = f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"
        else:
            url = f"postgresql+asyncpg://{user}@{host}:{port}/{db}"
    return url


config.set_main_option("sqlalchemy.url", _db_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async def do_run_migrations() -> None:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations)
        await connectable.dispose()

    import asyncio

    asyncio.run(do_run_migrations())


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
