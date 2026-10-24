"""Read-only review leases authorize one exact comment and their own closing key."""

from datetime import datetime
import json

from forge_protocol import RunIdentity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.port_values import GatewayKeyRef
from forge.store.models_budget import EngineGatewayKey
from forge.store.native_reviews import comment_body, comment_key, marker, request_row, work_value
from forge.store.records import Lease, StoreConflict
from forge.store.transactions import stored_time


async def authorize_native_review_effect(
    session: AsyncSession, lease: Lease, key: str, kind: str, payload: str, now: datetime
) -> None:
    row = await request_row(session, lease, now)
    work = await work_value(session, row)
    document = json.loads(payload)
    if kind == "post_comment":
        if (
            work.result is None
            or key != comment_key(lease.run_id)
            or document
            != {
                "subject": work.review.model_dump(mode="json"),
                "body": comment_body(work, work.result),
                "marker": marker(work),
            }
        ):
            raise StoreConflict("read-only native review permits only its exact stored comment")
        return
    if kind not in ("gw_mint", "gw_block"):
        raise StoreConflict("read-only native review cannot authorize implementation effects")
    identity = RunIdentity(
        forge_run_id=lease.run_id, attempt=lease.attempt, generation=lease.generation
    )
    alias = f"forge:{lease.run_id}:{lease.attempt}:{lease.generation}:closing"
    gateway = await session.scalar(select(EngineGatewayKey).where(EngineGatewayKey.alias == alias))
    if (
        gateway is None
        or gateway.run_id != lease.run_id
        or gateway.purpose != "closing"
        or gateway.attempt != lease.attempt
        or gateway.generation != lease.generation
        or gateway.reference_json is None
    ):
        raise StoreConflict("read-only review gateway grant is not its persisted closing key")
    if kind == "gw_block":
        if key != alias + ":block" or document != {"alias": alias}:
            raise StoreConflict("read-only review cannot block another key")
        return
    reference = GatewayKeyRef.model_validate_json(gateway.reference_json)
    expected = {
        "identity": identity.model_dump(mode="json"),
        "alias": alias,
        "model": work.policy.budget.model,
        "allowance_json": reference.allowance.model_dump_json(),
        "expires_at": stored_time(gateway.expires_at).isoformat(),
    }
    if (
        key != alias + ":mint"
        or document != expected
        or row.input_json is None
        or reference.model != work.policy.budget.model
        or not work.policy.requested.covers(reference.allowance)
        or reference.expires_at != work.expires_at
    ):
        raise StoreConflict("read-only review gateway request differs from frozen policy")
