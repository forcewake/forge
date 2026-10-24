"""Freeze model DDL and observed dialect signatures for the one final baseline."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Callable, cast

from sqlalchemy import MetaData, inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

import forge.store.models  # noqa: F401
from forge.store.base import Base
from forge.store.schema.baseline import REVISION
from forge.store.schema.signature import inspect_schema


def statements(metadata: MetaData, name: str) -> list[str]:
    factory = sqlite.dialect if name == "sqlite" else postgresql.dialect
    dialect = cast(Callable[[], Dialect], factory)()
    result = [
        str(CreateTable(table).compile(dialect=dialect)).strip() for table in metadata.sorted_tables
    ]
    result.extend(
        str(CreateIndex(index).compile(dialect=dialect)).strip()
        for table in metadata.sorted_tables
        for index in sorted(table.indexes, key=lambda value: str(value.name))
    )
    return result


async def freeze(postgres_url: str) -> dict[str, object]:
    result: dict[str, object] = {"revision": REVISION, "dialects": {}}
    dialects = {}
    for name, url in (("sqlite", "sqlite+aiosqlite:///:memory:"), ("postgresql", postgres_url)):
        ddl = statements(Base.metadata, name)
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                if await conn.run_sync(lambda sync: bool(inspect(sync).get_table_names())):
                    raise RuntimeError(
                        "schema rendering requires an explicitly empty disposable database"
                    )
                for statement in ddl:
                    await conn.exec_driver_sql(statement)
                await conn.exec_driver_sql(
                    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                )
                await conn.exec_driver_sql(
                    "INSERT INTO alembic_version (version_num) VALUES ('" + REVISION + "')"
                )
                signature = await conn.run_sync(inspect_schema)
            dialects[name] = {"ddl": ddl, "signature": signature}
        finally:
            await engine.dispose()
    result["dialects"] = dialects
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument("--output", type=Path, default=Path("src/forge/store/schema/baseline.json"))
    args = parser.parse_args()
    rendered = asyncio.run(freeze(args.postgres_url))
    args.output.write_text(json.dumps(rendered, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
