"""Canonical subject and connection-capacity admission, atomic with the first step."""

from datetime import datetime
import hashlib

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.states import TERMINAL, Handler
from forge.command_values import CommandLease, CommandResult
from forge.store.initial_commands import admission_command
from forge.store.issue_lifecycle import supersede_pending
from forge.store.commands import resolve_in
from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineRun
from forge.store.records import RunRecord, RunRequest, StepRequest, StoreConflict
from forge.store.runs import run_record, subject_key
from forge.store.steps import enqueue_in
from forge.store.transactions import SessionFactory, aware, canonical_json, transaction


async def connection_lock(session: AsyncSession, request: RunRequest) -> None:
    if session.get_bind().dialect.name == "postgresql":
        connection = request.provider + "\x00" + request.connection_id
        lock = int.from_bytes(hashlib.sha256(connection.encode()).digest()[:8], signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})


class AdmissionStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def admit(
        self,
        request: RunRequest,
        payload_json: str,
        *,
        max_active: int,
        now: datetime,
        command_lease: CommandLease | None = None,
        supersede_run_id: str | None = None,
    ) -> RunRecord:
        key = subject_key(request)
        canonical_json(payload_json)
        now = aware(now)
        if request.parent_run_id is not None or request.root_run_id not in (None, request.id):
            raise StoreConflict("initial admission cannot manufacture review lineage")
        if not 0 < max_active <= 1000:
            raise ValueError("connection capacity must be bounded")
        try:
            async with transaction(self.factory) as session:
                command = (
                    await admission_command(session, command_lease, request, payload_json, now)
                    if command_lease is not None
                    else None
                )
                await connection_lock(session, request)
                existing = await session.get(EngineRun, request.id)
                if existing is not None:
                    if existing.subject_key != key:
                        raise StoreConflict("run identity changed")
                    initial = await session.scalar(
                        select(EngineStep).where(EngineStep.key == f"{request.id}:plan")
                    )
                    if initial is None or initial.payload_json != payload_json:
                        raise StoreConflict("admission input changed")
                    return run_record(existing)
                if supersede_run_id is not None:
                    await supersede_pending(
                        session, command, supersede_run_id, key, payload_json, max_active, now
                    )
                occupied = await session.scalar(
                    select(func.count())
                    .select_from(EngineRun)
                    .where(
                        EngineRun.provider == request.provider,
                        EngineRun.connection_id == request.connection_id,
                        EngineRun.state.not_in(tuple(state.value for state in TERMINAL)),
                    )
                )
                if occupied is None or occupied >= max_active:
                    raise StoreConflict("connection capacity exhausted")
                row = EngineRun(
                    id=request.id,
                    provider=request.provider,
                    connection_id=request.connection_id,
                    repository_id=request.repository_id,
                    subject_kind=request.subject_kind,
                    subject_id=request.subject_id,
                    subject_key=key,
                    root_run_id=request.id,
                )
                session.add(row)
                await session.flush()
                await enqueue_in(
                    session,
                    row,
                    StepRequest(row.id, Handler.PLAN, f"{row.id}:plan", payload_json, now),
                )
                if command is not None:
                    resolve_in(
                        command,
                        CommandResult(
                            status="accepted", reason="implementation_queued", run_id=row.id
                        ),
                    )
                return run_record(row)
        except IntegrityError as exc:
            raise StoreConflict("subject already has an active run") from exc
