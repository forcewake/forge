"""Connection-scoped command target lookup and credential-free operator snapshots."""

import json
import re

from pydantic import TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandEnvelope
from forge.operator_values import redact
from forge.store.operator_evidence import read_evidence
from forge.domain.budget import Pool
from forge.budget.projection import pool_projection
from forge.store.budget_command_projection import amendment_snapshot
from forge.domain.states import TERMINAL
from forge.port_values import Applied, DraftOutcome, SubjectRef, TaskSnapshot
from forge.store.models_budget import EngineBudgetPool
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_runs import EngineRun
from forge.store.records import RunRecord, StoreConflict
from forge.store.runs import run_record
from forge.store.transactions import SessionFactory


async def target_runs(session: AsyncSession, envelope: CommandEnvelope) -> list[EngineRun]:
    target = envelope.target
    repo = target.repo.identity
    query = select(EngineRun).where(
        EngineRun.provider == repo.provider,
        EngineRun.connection_id == repo.connection_id,
        EngineRun.repository_id == repo.repository_id,
    )
    if target.kind == "issue":
        query = query.where(
            EngineRun.subject_kind == "issue", EngineRun.subject_id == str(target.number)
        )
    else:
        effects = await session.scalars(
            select(EngineEffect).where(
                EngineEffect.kind == "ensure_draft",
                EngineEffect.repo_key == repo.digest,
                EngineEffect.state.in_(("committed", "adopted")),
            )
        )
        roots = []
        for effect in effects:
            if effect.outcome_json is None or effect.run_id is None:
                continue
            result: DraftOutcome = TypeAdapter(DraftOutcome).validate_json(effect.outcome_json)
            if (
                isinstance(result, Applied)
                and result.value.repo.identity == repo
                and result.value.number == target.number
            ):
                run = await session.get(EngineRun, effect.run_id)
                if run is not None and effect.head_branch == f"forge/{run.root_run_id}":
                    roots.append(run.root_run_id)
        query = query.where(EngineRun.root_run_id.in_(roots))
    return list(
        (await session.scalars(query.order_by(EngineRun.created_at.desc(), EngineRun.id))).all()
    )


class CommandRuns:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def locate(self, envelope: CommandEnvelope, requested: str = "") -> RunRecord | None:
        if requested and not re.fullmatch(r"[0-9a-f]{32}", requested):
            raise StoreConflict("malformed_command")
        async with self.factory() as session:
            rows = await target_runs(session, envelope)
            if requested:
                found = next((row for row in rows if row.id == requested), None)
                if found is None and await session.get(EngineRun, requested) is not None:
                    raise StoreConflict("run_scope_mismatch")
                return run_record(found) if found is not None else None
            active = next(
                (row for row in rows if row.state not in {s.value for s in TERMINAL}), None
            )
            chosen = active or next(iter(rows), None)
            return run_record(chosen) if chosen is not None else None

    async def subject(self, run_id: str) -> SubjectRef:
        async with self.factory() as session:
            row = await session.scalar(select(EngineStep).where(EngineStep.key == f"{run_id}:plan"))
            if row is None:
                from forge.store.models_review import EngineReactiveReview

                request = await session.scalar(
                    select(EngineReactiveReview).where(EngineReactiveReview.run_id == run_id)
                )
                if request is not None:
                    row = await session.scalar(
                        select(EngineStep).where(EngineStep.key == f"{request.root_run_id}:plan")
                    )
            if row is None:
                raise StoreConflict("run has no original admitted issue")
            task = TaskSnapshot.model_validate_json(
                json.dumps(json.loads(row.payload_json)["task"])
            )
            return task.subject

    async def snapshot(self, run: RunRecord) -> str:
        async with self.factory() as session, session.begin():
            if session.get_bind().dialect.name == "sqlite":
                await session.execute(text("BEGIN"))
            else:
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
            current = await session.scalar(select(EngineRun).where(EngineRun.id == run.id))
            if current is None:
                raise StoreConflict("operator run is no longer available")
            run = run_record(current)
            pools = (
                await session.scalars(
                    select(EngineBudgetPool).where(EngineBudgetPool.run_id == run.id)
                )
            ).all()
            effects = (
                await session.scalars(
                    select(EngineEffect)
                    .where(EngineEffect.run_id == run.id)
                    .order_by(EngineEffect.created_at.desc())
                    .limit(51)
                )
            ).all()
            return json.dumps(
                redact(
                    {
                        **(await read_evidence(session, run)),
                        "state": run.state.value,
                        "reason": run.reason,
                        "attempt": run.attempt,
                        "generation": run.generation,
                        "run_version": run.version,
                        "cancel_requested": run.cancel_requested,
                        "failed_handler": run.failed_handler.value if run.failed_handler else None,
                        "budgets": {
                            row.purpose: pool_projection(Pool.model_validate_json(row.state_json))
                            for row in pools
                        },
                        "budget_amendments": await amendment_snapshot(session, run.id),
                        "effects": [
                            {"kind": row.kind, "state": row.state, "key": row.key}
                            for row in effects[:50]
                        ],
                        "effects_complete": len(effects) <= 50,
                    }
                ),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
