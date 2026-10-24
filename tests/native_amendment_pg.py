"""Move only this proof's seed into independent real PostgreSQL transactions."""

from datetime import datetime, timezone

from sqlalchemy import select

from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineRun, EngineRunSpec


async def move_case(case, factory):
    old = case["factory"]
    async with old() as source:
        documents = []
        for model in (EngineRun, EngineStep, EngineRunSpec):
            rows = (await source.scalars(select(model))).all()
            documents.append(
                (
                    model,
                    [
                        {
                            column.name: value.replace(tzinfo=timezone.utc)
                            if isinstance(value := getattr(row, column.name), datetime)
                            and value.tzinfo is None
                            else value
                            for column in model.__table__.columns
                        }
                        for row in rows
                    ],
                )
            )
    async with factory.begin() as session:
        for model, rows in documents:
            for document in rows:
                await session.execute(model.__table__.insert().values(**document))
    case["factory"] = factory
    # All existing services keep their actual gateway transport and independent SQL owners.
    case["keys"].factory = case["store"].factory = case["effects"].store.factory = factory
