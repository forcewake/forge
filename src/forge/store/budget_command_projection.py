"""Accepted, pending and refused native cap updates stay visible without credentials."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.budget import Resources
from forge.store.models_budget import EngineBudgetAmendment


async def amendment_snapshot(session: AsyncSession, run_id: str) -> dict[str, object]:
    rows = (
        await session.scalars(
            select(EngineBudgetAmendment)
            .where(EngineBudgetAmendment.run_id == run_id)
            .order_by(EngineBudgetAmendment.created_at.desc(), EngineBudgetAmendment.id)
            .limit(101)
        )
    ).all()
    return {
        "complete": len(rows) <= 100,
        "latest": [
            {
                "command": row.command_key,
                "state": row.state,
                "amount": Resources.model_validate_json(row.amount_json).model_dump(mode="json"),
            }
            for row in rows[:100]
        ],
    }
