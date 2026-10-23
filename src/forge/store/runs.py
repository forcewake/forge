"""Canonical run admission and versioned lifecycle changes for the new engine."""

import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forge.domain.states import Handler, State, revive, transition
from forge.store.models_runs import EngineRun, EngineRunSpec
from forge.store.records import RunRecord, RunRequest, StoreConflict
from forge.store.transactions import (
    SessionFactory,
    canonical_json,
    locked_run,
    payload_digest,
    transaction,
)


def subject_key(request: RunRequest) -> str:
    fields = (
        request.provider,
        request.connection_id,
        request.repository_id,
        request.subject_kind,
        request.subject_id,
    )
    if request.provider not in {"github", "gitlab"} or request.subject_kind not in {
        "issue",
        "review",
    }:
        raise ValueError("unsupported subject identity")
    if not all(isinstance(item, str) and item and item.strip() == item for item in fields):
        raise ValueError("subject identity must be explicit")
    if (
        not request.repository_id.isdecimal()
        or int(request.repository_id) <= 0
        or str(int(request.repository_id)) != request.repository_id
    ):
        raise ValueError("repository_id must be a canonical native numeric ID")
    key = json.dumps(fields, separators=(",", ":"), ensure_ascii=False)
    if len(key) > 600 or len(request.id) != 32:
        raise ValueError("identity exceeds storage bounds")
    return key


def run_record(row: EngineRun) -> RunRecord:
    return RunRecord(
        row.id,
        row.provider,
        row.connection_id,
        row.repository_id,
        row.subject_kind,
        row.subject_id,
        State(row.state),
        row.root_run_id,
        row.parent_run_id,
        row.attempt,
        row.generation,
        row.cancellation_generation,
        row.cancel_requested,
        row.version,
        Handler(row.failed_handler) if row.failed_handler is not None else None,
        row.reason,
    )


class RunStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def admit(self, request: RunRequest) -> RunRecord:
        key = subject_key(request)
        try:
            async with transaction(self.factory) as session:
                row = await session.get(EngineRun, request.id)
                if row is not None:
                    if (row.subject_key, row.root_run_id, row.parent_run_id) != (
                        key,
                        request.root_run_id or request.id,
                        request.parent_run_id,
                    ):
                        raise StoreConflict("run identity changed")
                    return run_record(row)
                if request.parent_run_id is not None:
                    parent = await locked_run(session, request.parent_run_id)
                    if parent.root_run_id != request.root_run_id:
                        raise StoreConflict("review lineage changed")
                elif request.root_run_id not in (None, request.id):
                    raise StoreConflict("root run cannot name another root")
                row = EngineRun(
                    id=request.id,
                    provider=request.provider,
                    connection_id=request.connection_id,
                    repository_id=request.repository_id,
                    subject_kind=request.subject_kind,
                    subject_id=request.subject_id,
                    subject_key=key,
                    root_run_id=request.root_run_id or request.id,
                    parent_run_id=request.parent_run_id,
                )
                session.add(row)
                await session.flush()
                return run_record(row)
        except IntegrityError as exc:
            winner = await self.load(request.id)
            if winner is not None and (
                winner.provider,
                winner.connection_id,
                winner.repository_id,
                winner.subject_kind,
                winner.subject_id,
                winner.root_run_id,
                winner.parent_run_id,
            ) == (
                request.provider,
                request.connection_id,
                request.repository_id,
                request.subject_kind,
                request.subject_id,
                request.root_run_id or request.id,
                request.parent_run_id,
            ):
                return winner
            raise StoreConflict(
                "subject already has an active run or run identity collided"
            ) from exc

    async def load(self, run_id: str) -> RunRecord | None:
        async with self.factory() as session:
            row = await session.get(EngineRun, run_id)
            return run_record(row) if row is not None else None

    async def transition(
        self,
        run_id: str,
        *,
        expected_version: int,
        target: State,
        failed_handler: Handler | None = None,
        reason: str | None = None,
    ) -> RunRecord:
        async with transaction(self.factory) as session:
            row = await locked_run(session, run_id)
            self._version(row, expected_version)
            row.state = transition(State(row.state), target).value
            row.failed_handler = failed_handler.value if failed_handler is not None else None
            row.reason = reason
            row.version += 1
            await session.flush()
            return run_record(row)

    async def request_cancel(self, run_id: str, *, expected_version: int) -> RunRecord:
        async with transaction(self.factory) as session:
            row = await locked_run(session, run_id)
            self._version(row, expected_version)
            if not row.cancel_requested:
                row.cancel_requested = True
                row.cancellation_generation += 1
                row.version += 1
            await session.flush()
            return run_record(row)

    async def resume(self, run_id: str, *, expected_version: int, handler: Handler) -> RunRecord:
        async with transaction(self.factory) as session:
            row = await locked_run(session, run_id)
            self._version(row, expected_version)
            if row.cancel_requested or row.failed_handler != handler.value:
                raise StoreConflict("cancelled run or wrong failed-handler owner")
            row.state = revive(State(row.state), handler).value
            row.attempt += 1
            row.generation += 1
            row.version += 1
            row.failed_handler = None
            row.reason = None
            await session.flush()
            return run_record(row)

    async def put_spec_once(self, run_id: str, *, document_json: str, digest: str) -> str:
        canonical_json(document_json)
        if payload_digest(document_json) != digest:
            raise StoreConflict("spec digest disagrees with canonical document")
        async with transaction(self.factory) as session:
            await locked_run(session, run_id)
            row = await session.scalar(select(EngineRunSpec).where(EngineRunSpec.run_id == run_id))
            if row is not None:
                if (row.digest, row.document_json) != (digest, document_json):
                    raise StoreConflict("frozen run spec changed")
                return row.id
            row = EngineRunSpec(
                id=uuid4().hex, run_id=run_id, digest=digest, document_json=document_json
            )
            session.add(row)
            await session.flush()
            return row.id

    @staticmethod
    def _version(row: EngineRun, expected: int) -> None:
        if row.version != expected:
            raise StoreConflict("run version changed")
