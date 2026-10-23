"""Historical data tests use the actual schema bootstrap and SQL connection."""

import pytest
from forge.database import init_db, get_engine, dispose_engine


@pytest.fixture()
async def historical_engine(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/historical.db"
    try:
        await init_db(url)
        yield get_engine(url)
    finally:
        await dispose_engine()
