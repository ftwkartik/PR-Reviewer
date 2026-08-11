from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def make_worker_sessionmaker() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Fresh engine per Celery task: each task runs in its own event loop (asyncio.run)."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    if _sessionmaker is None:
        raise RuntimeError("sessionmaker not initialised")
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
