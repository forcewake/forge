"""Disposable current-engine stores without importing retired model owners."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.store.base import Base
from forge.store import models


@pytest.fixture
async def native_store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'native-engine.db'}")
    owned = {getattr(models, name).__table__ for name in models.__all__}
    tables = [table for table in Base.metadata.sorted_tables if table in owned]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
