"""Durable lane identity binding using the run row and the inbox unique index."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal, Protocol, cast

from forge_protocol import Brief, LaneDispatch, RunIdentity
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable.inbox import ingest_event, mark_processed
from forge.durable.models import EventInbox, FlowRun

AUTH_NAMESPACE = "lane_v1_auth"
_TERMINAL = ("cancelled", "failed", "blocked", "ready_for_human")


class LaneAuthError(ValueError):
    """An authentication request was refused without exposing its credentials."""


def identity_of(value: RunIdentity) -> RunIdentity:
    return RunIdentity(
        forge_run_id=value.forge_run_id, attempt=value.attempt, generation=value.generation
    )


class PendingDispatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatch: LaneDispatch
    brief: Brief
    expires_at: AwareDatetime
    cancellation_generation: int = Field(strict=True, ge=0)
    github_workflow_ref: str | None = None
    github_caller_ref: str | None = None
    github_workflow_claim: Literal["workflow_ref", "job_workflow_ref"] = "job_workflow_ref"
    gitlab_pipeline_id: str | None = None
    provider_run_id: str | None = None
    provider_attempt: str | None = None
    gitlab_job_id: str | None = None
    nonce_consumed: bool = False

    @property
    def identity(self) -> RunIdentity:
        return identity_of(self.dispatch)


def check_current(row: FlowRun | None, identity: RunIdentity) -> PendingDispatch:
    """Recheck inside an API write transaction, before its conditional UPDATE."""
    if row is None or row.cancel_requested or row.status in _TERMINAL:
        raise LaneAuthError("lane run is not active")
    try:
        pending = PendingDispatch.model_validate((row.evidence or {})[AUTH_NAMESPACE])
    except (KeyError, ValidationError, TypeError):
        raise LaneAuthError("lane dispatch is not registered") from None
    if (
        row.id != identity.forge_run_id
        or pending.identity != identity_of(identity)
        or pending.cancellation_generation != row.cancellation_generation
        or pending.dispatch.provider != row.provider
    ):
        raise LaneAuthError("lane dispatch is no longer current")
    return pending


def current_conditions(identity: RunIdentity, cancellation_generation: int) -> tuple:
    """SQL authorization predicates; combine with a mutation's own CAS predicates."""
    auth = FlowRun.evidence[AUTH_NAMESPACE]
    return (
        FlowRun.id == identity.forge_run_id,
        FlowRun.cancel_requested.is_(False),
        FlowRun.status.not_in(_TERMINAL),
        FlowRun.cancellation_generation == cancellation_generation,
        auth["cancellation_generation"].as_integer() == cancellation_generation,
        auth["dispatch"]["forge_run_id"].as_string() == identity.forge_run_id,
        auth["dispatch"]["attempt"].as_integer() == identity.attempt,
        auth["dispatch"]["generation"].as_integer() == identity.generation,
    )


class LaneAuthStore(Protocol):
    async def load(self, identity: RunIdentity) -> PendingDispatch: ...

    async def register(self, pending: PendingDispatch) -> PendingDispatch: ...

    async def claim(
        self, identity: RunIdentity, *, jti_hash: str, expires_at: int, binding: dict[str, str]
    ) -> PendingDispatch: ...


def replay_hash(value: str) -> str:
    return hashlib.sha256(("lane-oidc-jti:" + value).encode()).hexdigest()


class SqlLaneAuthStore:
    """Only database transactions arbitrate nonce, provider binding and JWT replay."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def _row(self, session: AsyncSession, identity: RunIdentity) -> FlowRun:
        row = await session.scalar(
            select(FlowRun).where(FlowRun.id == identity.forge_run_id).with_for_update()
        )
        if row is None:
            raise LaneAuthError("lane run does not exist")
        return row

    async def load(self, identity: RunIdentity) -> PendingDispatch:
        async with self.session_factory() as session:
            return check_current(await self._row(session, identity), identity)

    async def register(self, pending: PendingDispatch) -> PendingDispatch:
        async with self.session_factory() as session, session.begin():
            row = await self._row(session, pending.identity)
            if row.cancel_requested or row.status in _TERMINAL:
                raise LaneAuthError("lane run is not active")
            if row.provider != pending.dispatch.provider:
                raise LaneAuthError("lane provider does not match run")
            pending = pending.model_copy(
                update={"cancellation_generation": row.cancellation_generation}
            )
            previous = (row.evidence or {}).get(AUTH_NAMESPACE)
            if previous:
                old = PendingDispatch.model_validate(previous)
                if old.identity == pending.identity:
                    immutable = {
                        "provider_run_id",
                        "provider_attempt",
                        "gitlab_job_id",
                        "nonce_consumed",
                    }
                    if old.model_dump(exclude=immutable) != pending.model_dump(exclude=immutable):
                        raise LaneAuthError("dispatch identity is already registered differently")
                    return check_current(row, pending.identity)
                if (pending.identity.attempt, pending.identity.generation) <= (
                    old.identity.attempt,
                    old.identity.generation,
                ):
                    raise LaneAuthError("cannot restore an older dispatch")
            if pending.dispatch.provider == "github":
                if pending.dispatch.nonce is None:
                    raise LaneAuthError("GitHub dispatch requires a nonce")
                nonce_hash = hashlib.sha256(
                    ("lane-oidc-nonce:" + pending.dispatch.nonce).encode()
                ).hexdigest()
                event, created = await ingest_event(
                    session,
                    source_event_id=nonce_hash,
                    project_id=row.project_id,
                    event_type="lane.oidc.nonce",
                    payload={"expires_at": int(pending.expires_at.timestamp())},
                )
                if not created:
                    raise LaneAuthError("dispatch nonce was already registered")
                await mark_processed(session, event)
            await self._save(session, row, pending, registering=True)
            return pending

    async def _save(
        self,
        session: AsyncSession,
        row: FlowRun,
        pending: PendingDispatch,
        *,
        registering: bool = False,
    ) -> None:
        evidence = {**(row.evidence or {}), AUTH_NAMESPACE: pending.model_dump(mode="json")}
        conditions = (
            FlowRun.id == row.id,
            FlowRun.updated_at == row.updated_at,
            FlowRun.cancel_requested.is_(False),
            FlowRun.cancellation_generation == pending.cancellation_generation,
            FlowRun.status.not_in(_TERMINAL),
        )
        if not registering:
            conditions += current_conditions(pending.identity, pending.cancellation_generation)
        result = await session.execute(
            update(FlowRun)
            .where(*conditions)
            .values(evidence=evidence)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult, result).rowcount != 1:
            raise LaneAuthError("lane run changed during authentication")

    async def claim(
        self, identity: RunIdentity, *, jti_hash: str, expires_at: int, binding: dict[str, str]
    ) -> PendingDispatch:
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session, session.begin():
            row = await self._row(session, identity)
            pending = check_current(row, identity)
            if not pending.nonce_consumed and pending.expires_at <= now:
                raise LaneAuthError("dispatch has expired")
            if pending.nonce_consumed:
                for name, value in binding.items():
                    if getattr(pending, name) != value:
                        raise LaneAuthError("OIDC provider run does not match first session")
            await session.execute(
                delete(EventInbox).where(
                    EventInbox.event_type == "lane.oidc.jti",
                    EventInbox.payload["expires_at"].as_integer() < int(now.timestamp()),
                )
            )
            event, created = await ingest_event(
                session,
                source_event_id=jti_hash,
                project_id=row.project_id,
                event_type="lane.oidc.jti",
                payload={"expires_at": expires_at},
            )
            if not created:
                raise LaneAuthError("OIDC token has already been used")
            await mark_processed(session, event)
            pending = pending.model_copy(update={**binding, "nonce_consumed": True})
            await self._save(session, row, pending)
            return pending
