"""Engine-owned lane authentication, with shared durable hashed replay receipts."""

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from forge_protocol import Brief, LaneDispatch, RunIdentity, digest
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.api.lane_auth_values import LaneAuthError, LaneAuthPending, PendingDispatch, identity_of
from forge.domain.approved_input import ApprovedInput
from forge.domain.run_spec import RunSpec2
from forge.domain.states import TERMINAL, State
from forge.store.repair import repair_brief
from forge.store.inbox import ingest_event, mark_processed
from forge.store.inbox_models import EventInbox
from forge.store.models_lane import EngineLaneSession
from forge.store.models_execution import EngineEffect
from forge.store.models_runs import EngineApprovedInput, EngineRun, EngineRunSpec
from forge.store.transactions import SessionFactory, canonical_json, stored_time, transaction

_BINDING = (
    "github_workflow_ref",
    "github_caller_ref",
    "github_workflow_claim",
    "gitlab_pipeline_id",
    "repository_id",
)
_JOB = ("provider_run_id", "provider_attempt", "gitlab_job_id")


def encoded(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def current(
    run: EngineRun | None, row: EngineLaneSession | None, identity: RunIdentity
) -> PendingDispatch:
    if run is None or row is None or run.cancel_requested or State(run.state) in TERMINAL:
        raise LaneAuthError("lane run is not active")
    if (
        (run.id, run.attempt, run.generation)
        != (identity.forge_run_id, identity.attempt, identity.generation)
        or (row.run_id, row.attempt, row.generation) != (run.id, run.attempt, run.generation)
        or row.cancellation_generation != run.cancellation_generation
    ):
        raise LaneAuthError("lane dispatch is no longer current")
    try:
        pending = PendingDispatch(
            dispatch=LaneDispatch.model_validate_json(row.dispatch_json),
            connection_id=run.connection_id,
            brief=Brief.model_validate_json(row.brief_json),
            expires_at=stored_time(row.expires_at),
            cancellation_generation=row.cancellation_generation,
            nonce_consumed=row.nonce_consumed,
            **json.loads(row.provider_binding_json),
            **json.loads(row.first_job_json or "{}"),
        )
    except (ValidationError, ValueError, TypeError):
        raise LaneAuthError("stored lane session is invalid") from None
    if (
        pending.identity != identity_of(identity)
        or pending.dispatch.provider != run.provider
        or pending.repository_id != run.repository_id
    ):
        raise LaneAuthError("stored lane identity differs from its run")
    return pending


async def awaiting_job_binding(
    session: AsyncSession, run: EngineRun | None, identity: RunIdentity
) -> bool:
    if (
        run is None
        or run.cancel_requested
        or State(run.state) in TERMINAL
        or (run.id, run.attempt, run.generation)
        != (identity.forge_run_id, identity.attempt, identity.generation)
    ):
        return False
    effect = await session.scalar(
        select(EngineEffect).where(
            EngineEffect.key == f"{run.id}:{run.attempt}:{run.generation}:lane:dispatch",
            EngineEffect.run_id == run.id,
            EngineEffect.kind == "dispatch_lane",
            EngineEffect.state.in_(("requested", "dispatched", "unknown", "committed", "adopted")),
        )
    )
    if effect is None:
        return False
    try:
        raw = json.loads(effect.payload_json)
        dispatch = LaneDispatch.model_validate_json(raw["dispatch_json"])
        return (
            digest(raw) == effect.payload_digest
            and identity_of(dispatch) == identity
            and dispatch.provider == run.provider
            and len(dispatch.nonce or "") >= 32
        )
    except (ValueError, TypeError, KeyError):
        return False


async def locked_current(
    session: AsyncSession, identity: RunIdentity
) -> tuple[EngineRun, EngineLaneSession, PendingDispatch]:
    run = await session.scalar(
        select(EngineRun).where(EngineRun.id == identity.forge_run_id).with_for_update()
    )
    row = await session.scalar(
        select(EngineLaneSession)
        .where(
            EngineLaneSession.run_id == identity.forge_run_id,
            EngineLaneSession.attempt == identity.attempt,
            EngineLaneSession.generation == identity.generation,
        )
        .with_for_update()
    )
    if row is None and await awaiting_job_binding(session, run, identity):
        raise LaneAuthPending("lane dispatch is awaiting its provider job binding")
    pending = current(run, row, identity)
    assert run is not None and row is not None
    return run, row, pending


async def approved_brief(session: AsyncSession, identity: RunIdentity) -> tuple[Brief, RunSpec2]:
    spec_row = await session.scalar(
        select(EngineRunSpec).where(EngineRunSpec.run_id == identity.forge_run_id)
    )
    approved_row = await session.scalar(
        select(EngineApprovedInput)
        .where(EngineApprovedInput.run_id == identity.forge_run_id)
        .order_by(EngineApprovedInput.revision.desc())
        .limit(1)
    )
    if spec_row is None or approved_row is None:
        raise LaneAuthError("persisted approved input is required before dispatch")
    try:
        canonical_json(spec_row.document_json)
        canonical_json(approved_row.document_json)
        spec = RunSpec2.model_validate_json(spec_row.document_json)
        approved = ApprovedInput.model_validate_json(approved_row.document_json)
        if (
            spec.digest != spec_row.digest
            or approved.approved_digest != approved_row.digest
            or approved.spec_digest != approved_row.spec_digest
            or approved.base_sha != approved_row.base_sha
            or approved.revision != approved_row.revision
        ):
            raise ValueError("frozen approval metadata disagrees")
        return await repair_brief(session, identity, approved.to_brief(identity, spec)), spec
    except (ValueError, ValidationError):
        raise LaneAuthError("persisted approval or specification is invalid") from None


async def register_nonce(session: AsyncSession, pending: PendingDispatch) -> str:
    nonce = pending.dispatch.nonce
    if nonce is None or len(nonce) < 32:
        raise LaneAuthError("engine dispatch requires a random nonce")
    nonce_hash = hashlib.sha256(("lane-oidc-nonce:" + nonce).encode()).hexdigest()
    event, created = await ingest_event(
        session,
        source_event_id=nonce_hash,
        project_id=0,
        event_type="lane.oidc.nonce",
        payload={"expires_at": int(pending.expires_at.timestamp())},
    )
    if not created:
        raise LaneAuthError("dispatch nonce was already registered")
    await mark_processed(session, event)
    return nonce_hash


class EngineLaneAuthStore:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def load(self, identity: RunIdentity) -> PendingDispatch:
        async with transaction(self.session_factory) as session:
            _, _, pending = await locked_current(session, identity_of(identity))
            return pending

    async def register(self, pending: PendingDispatch) -> PendingDispatch:
        if pending.nonce_consumed or any(getattr(pending, name) is not None for name in _JOB):
            raise LaneAuthError("new dispatch cannot pre-bind a provider job")
        async with transaction(self.session_factory) as session:
            run = await session.scalar(
                select(EngineRun)
                .where(EngineRun.id == pending.identity.forge_run_id)
                .with_for_update()
            )
            if (
                run is None
                or run.cancel_requested
                or pending.connection_id != run.connection_id
                or State(run.state) in TERMINAL
                or (run.attempt, run.generation, run.provider)
                != (
                    pending.identity.attempt,
                    pending.identity.generation,
                    pending.dispatch.provider,
                )
            ):
                raise LaneAuthError("dispatch does not match the active engine identity")
            brief, spec = await approved_brief(session, pending.identity)
            if (
                pending.brief != brief
                or pending.dispatch.repair != brief.repair
                or (
                    pending.dispatch.base_sha,
                    pending.dispatch.approved_digest,
                    pending.dispatch.driver,
                    pending.dispatch.model,
                )
                != (brief.base_sha, brief.approved_digest, spec.driver, spec.model)
            ):
                raise LaneAuthError("dispatch differs from the persisted approved specification")
            pending = pending.model_copy(
                update={
                    "brief": brief,
                    "cancellation_generation": run.cancellation_generation,
                    "repository_id": run.repository_id,
                }
            )
            row = await session.scalar(
                select(EngineLaneSession).where(
                    EngineLaneSession.run_id == run.id,
                    EngineLaneSession.attempt == run.attempt,
                    EngineLaneSession.generation == run.generation,
                )
            )
            if row is not None:
                previous = current(run, row, pending.identity)
                if previous.model_dump(
                    exclude=set(_JOB) | {"nonce_consumed"}
                ) != pending.model_dump(exclude=set(_JOB) | {"nonce_consumed"}):
                    raise LaneAuthError("dispatch identity is already registered differently")
                return previous
            nonce_hash = await register_nonce(session, pending)
            session.add(
                EngineLaneSession(
                    id=uuid4().hex,
                    run_id=run.id,
                    attempt=run.attempt,
                    generation=run.generation,
                    cancellation_generation=run.cancellation_generation,
                    nonce_hash=nonce_hash,
                    nonce_consumed=False,
                    expires_at=pending.expires_at,
                    dispatch_json=encoded(pending.dispatch.model_dump(mode="json")),
                    brief_json=encoded(brief.model_dump(mode="json")),
                    provider_binding_json=encoded(
                        pending.model_dump(mode="json", include=set(_BINDING))
                    ),
                    api_state_json="{}",
                )
            )
            await session.flush()
            return pending

    async def claim(
        self, identity: RunIdentity, *, jti_hash: str, expires_at: int, binding: dict[str, str]
    ) -> PendingDispatch:
        now = datetime.now(timezone.utc)
        if (
            re.fullmatch(r"[0-9a-f]{64}", jti_hash) is None
            or type(expires_at) is not int
            or expires_at <= int(now.timestamp())
        ):
            raise LaneAuthError("invalid hashed token receipt or expiry")
        async with transaction(self.session_factory) as session:
            run, row, pending = await locked_current(session, identity_of(identity))
            names = (
                {"provider_run_id", "provider_attempt"}
                if run.provider == "github"
                else {"gitlab_job_id"}
            )
            if set(binding) != names or any(
                not isinstance(value, str) or not value for value in binding.values()
            ):
                raise LaneAuthError("invalid provider job binding")
            if not pending.nonce_consumed and pending.expires_at <= now:
                raise LaneAuthError("dispatch has expired")
            if pending.nonce_consumed and any(
                getattr(pending, name) != value for name, value in binding.items()
            ):
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
                project_id=0,
                event_type="lane.oidc.jti",
                payload={"expires_at": expires_at},
            )
            if not created:
                raise LaneAuthError("OIDC token has already been used")
            await mark_processed(session, event)
            row.first_job_json, row.nonce_consumed = encoded(binding), True
            await session.flush()
            return pending.model_copy(update={**binding, "nonce_consumed": True})
