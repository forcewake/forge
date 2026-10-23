"""Portable run contracts retained after the third provider's retirement.

The maintenance branch owns the removed provider's API-specific cases.
These cases exercise the supported CI lane, durable gate and frozen base.
"""

from sqlalchemy import select
from forge.durable import FlowStatus, GateApproval
from forge.execution.github_actions import ActionsHandle
from tests.test_github_harness import (
    db as db,
    fake as fake,
    get_run,
    go,
    make_service,
    start,
    BASE_HEAD,
)


async def test_approved_run_dispatches_without_in_process_publication(db, fake):
    service = make_service(db, fake)
    run_id = await start(service)
    await go(service, run_id)
    run = await get_run(db, run_id)
    assert run.status == FlowStatus.WAITING_HARNESS.value
    assert fake.calls_of("create_commit_on_branch") == []
    assert fake.calls_of("create_draft_pr") == []
    handle = ActionsHandle.from_json(run.evidence["harness"]["handle"])
    assert handle.forge_run_id == run_id
    assert run.evidence["harness"]["attempt_base"] == BASE_HEAD


async def test_replayed_approval_has_one_dispatch_and_one_consumption(db, fake):
    service = make_service(db, fake)
    run_id = await start(service)
    await go(service, run_id)
    await go(service, run_id)
    assert len(fake.dispatch_inputs) == 1
    async with db() as session:
        gates = (
            (await session.execute(select(GateApproval).where(GateApproval.flow_run_id == run_id)))
            .scalars()
            .all()
        )
    assert len(gates) == 1
    assert gates[0].consumed_at is not None


async def test_non_approver_cannot_dispatch_the_lane(db, fake):
    service = make_service(db, fake)
    run_id = await start(service)
    await go(service, run_id, author="mallory")
    assert fake.dispatch_inputs == []
    assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value
