"""One bounded operator view, scoped before reading any run evidence."""

from sqlalchemy import select, tuple_

from forge.runtime.authority import Principal, NotAuthorized
from forge.runtime.settings import RuntimeSettings
from forge.store.command_runs import CommandRuns
from forge.store.models_runs import EngineRun
from forge.store.runs import run_record
from forge.store.records import RunRecord
from forge.store.transactions import SessionFactory


def public_run(run: RunRecord) -> dict[str, object]:
    return {
        "id": run.id,
        "provider": run.provider,
        "connection_id": run.connection_id,
        "repository_id": run.repository_id,
        "subject_kind": run.subject_kind,
        "subject_id": run.subject_id,
        "state": run.state.value,
        "root_run_id": run.root_run_id,
        "parent_run_id": run.parent_run_id,
        "attempt": run.attempt,
        "generation": run.generation,
        "cancel_requested": run.cancel_requested,
        "version": run.version,
        "failed_handler": run.failed_handler.value if run.failed_handler else None,
        "reason": run.reason,
    }


class Projection:
    def __init__(self, factory: SessionFactory, config: RuntimeSettings) -> None:
        self.factory, self.repositories = factory, config.repositories()
        self.commands = CommandRuns(factory)

    def scope(self, principal: Principal) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (
                self.repositories[key][0].provider,
                self.repositories[key][0].key,
                self.repositories[key][1].native_id,
            )
            for key in principal.actors
        )

    async def list(
        self, principal: Principal, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            raise ValueError("run list bounds refused")
        async with self.factory() as session:
            query = (
                select(EngineRun)
                .where(
                    tuple_(
                        EngineRun.provider, EngineRun.connection_id, EngineRun.repository_id
                    ).in_(self.scope(principal))
                )
                .order_by(EngineRun.created_at.desc(), EngineRun.id)
                .limit(limit)
                .offset(offset)
            )
            rows = (await session.scalars(query)).all()
            return [public_run(run_record(row)) for row in rows]

    async def get(self, principal: Principal, run_id: str) -> RunRecord | None:
        async with self.factory() as session:
            row = await session.get(EngineRun, run_id)
            if row is not None and (
                row.provider,
                row.connection_id,
                row.repository_id,
            ) not in self.scope(principal):
                raise NotAuthorized("run access refused")
            return run_record(row) if row is not None else None

    async def detail(self, principal: Principal, run_id: str) -> dict[str, object] | None:
        import json

        run = await self.get(principal, run_id)
        return (
            {**public_run(run), "evidence": json.loads(await self.commands.snapshot(run))}
            if run is not None
            else None
        )

    async def metrics(self, principal: Principal) -> str:
        from forge.runtime.metrics import Metrics

        return await Metrics(self.factory, self.scope(principal)).render()
