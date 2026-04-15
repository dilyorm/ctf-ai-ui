"""Database wiring (async SQLAlchemy + Postgres)."""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _db_url() -> str:
    # Prefer explicit DB_URL; fallback to common Postgres env vars.
    url = (os.environ.get("DB_URL") or "").strip()
    if url:
        return url

    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "ctf_agent")
    user = os.environ.get("POSTGRES_USER", "ctf_agent")
    # Keep defaults aligned with scripts/start-postgres.sh and .env.example.
    pw = os.environ.get("POSTGRES_PASSWORD", "ctf_agent")

    # NOTE: password might be empty for local dev.
    # Avoid emitting an invalid URL like user:@host when pw is empty.
    if pw:
        return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"
    return f"postgresql+asyncpg://{user}@{host}:{port}/{db}"


engine: AsyncEngine = create_async_engine(_db_url(), pool_pre_ping=True)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
