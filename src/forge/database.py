from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.models.base import Base

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(database_url: str):
    """Create (or return cached) async engine for *database_url*."""
    global _engine
    if _engine is None:
        connect_args = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_async_engine(database_url, connect_args=connect_args)
    return _engine


def get_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to the given database URL."""
    global _session_factory
    if _session_factory is None:
        engine = get_engine(database_url)
        _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _session_factory


async def init_db(database_url: str) -> None:
    """Create all tables. Used for dev / first-run bootstrap."""
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def reset_engine() -> None:
    """Reset cached engine and session factory (used in tests)."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
