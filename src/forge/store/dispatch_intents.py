"""Indexed lane correlation reads validate the original execution authority."""

import hashlib
import json

from forge_protocol import LaneDispatch
from sqlalchemy.ext.asyncio import AsyncSession

from forge.port_values import RepoRef
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_runs import EngineRun
from forge.store.records import Lease, StoreConflict
from forge.store.transactions import payload_digest


def nonce_digest(nonce: str) -> str:
    if not nonce or len(nonce) > 256:
        raise StoreConflict("dispatch nonce is absent or exceeds its bound")
    return hashlib.sha256(nonce.encode()).hexdigest()


def parse_dispatch(payload: str) -> tuple[RepoRef, LaneDispatch]:
    try:
        payload_digest(payload)
        data = json.loads(payload)
        if set(data) != {"repo", "dispatch_json", "correlation_id"}:
            raise ValueError("dispatch intent has unexpected fields")
        repo = RepoRef.model_validate(data["repo"])
        dispatch = LaneDispatch.model_validate_json(data["dispatch_json"])
        if (
            dispatch.nonce is None
            or dispatch.nonce != data["correlation_id"]
            or dispatch.provider != repo.identity.provider
            or dispatch.repository != repo.full_name
        ):
            raise ValueError("dispatch correlation or native repository changed")
        return repo, dispatch
    except (KeyError, TypeError, ValueError) as error:
        raise StoreConflict("dispatch intent is invalid") from error


async def validate_owner(
    session: AsyncSession,
    run_id: str | None,
    step_id: str | None,
    repo: RepoRef,
    dispatch: LaneDispatch,
) -> None:
    run = await session.get(EngineRun, run_id) if run_id is not None else None
    step = await session.get(EngineStep, step_id) if step_id is not None else None
    identity = repo.identity
    if (
        run is None
        or step is None
        or step.run_id != run.id
        or dispatch.forge_run_id != run.id
        or (dispatch.attempt, dispatch.generation) != (step.attempt, step.generation)
        or (run.provider, run.connection_id, run.repository_id)
        != (identity.provider, identity.connection_id, identity.repository_id)
    ):
        raise StoreConflict("dispatch intent differs from its original run and step authority")


async def derive_nonce(session: AsyncSession, lease: Lease, payload: str) -> str:
    repo, dispatch = parse_dispatch(payload)
    await validate_owner(session, lease.run_id, lease.step_id, repo, dispatch)
    assert dispatch.nonce is not None
    return nonce_digest(dispatch.nonce)


async def read_dispatch(
    session: AsyncSession, row: EngineEffect, repo: RepoRef, nonce: str
) -> LaneDispatch:
    saved_repo, dispatch = parse_dispatch(row.payload_json)
    if (
        row.kind != "dispatch_lane"
        or row.command_id is not None
        or row.payload_digest != payload_digest(row.payload_json)
        or row.dispatch_nonce_digest != nonce_digest(nonce)
        or dispatch.nonce != nonce
        or saved_repo.identity != repo.identity
    ):
        raise StoreConflict("dispatch nonce lookup differs from its exact committed intent")
    await validate_owner(session, row.run_id, row.step_id, saved_repo, dispatch)
    return dispatch
