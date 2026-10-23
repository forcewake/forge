"""Durable control causality, without the retired scripted vendor grader.

A real mailbox restart recovers dispatch intent and uncertainty. These are
control-plane observations; no assertion claims a live model applied a steer.
"""

import pytest

from forge.adaptive.mailbox_db import PostgresMailbox
from tests.test_adaptive_mailbox_db import SCOPES, _command
from tests.test_checkpoint_repository import _sqlite_factory

pytestmark = pytest.mark.slow


async def test_restart_keeps_unobserved_steer_unknown_until_observation():
    engine, factory = await _sqlite_factory()
    try:
        mailbox = PostgresMailbox(factory)
        await mailbox.submit(
            _command(kind="steer", expected_plan_revision=2, expected_execution_epoch=4)
        )
        await mailbox.authorize("cmd-1", SCOPES)
        dispatched = await mailbox.dispatch(
            "cmd-1",
            current_plan_revision=2,
            current_execution_epoch=4,
            vendor_correlation_id="session:turn:7",
        )
        assert dispatched.status == "dispatching"
        with pytest.raises(ValueError):
            await mailbox.checkpoint("cmd-1")
        await mailbox.outcome_unknown("cmd-1")
        restarted = PostgresMailbox(factory)
        recovered = await restarted.get("cmd-1")
        assert recovered.status == "outcome_unknown"
        assert await restarted.pending(recovered.work_id) == []
        observed = await restarted.observe("cmd-1")
        assert observed.status == "applied"
        assert (await restarted.checkpoint("cmd-1")).status == "checkpointed"
    finally:
        await engine.dispose()


async def test_stale_revision_expires_before_any_vendor_dispatch_intent():
    engine, factory = await _sqlite_factory()
    try:
        mailbox = PostgresMailbox(factory)
        await mailbox.submit(
            _command(kind="steer", expected_plan_revision=1, expected_execution_epoch=4)
        )
        await mailbox.authorize("cmd-1", SCOPES)
        command = await mailbox.dispatch(
            "cmd-1", current_plan_revision=2, current_execution_epoch=4
        )
        assert command.status == "expired"
        assert (await PostgresMailbox(factory).get("cmd-1")).status == "expired"
        with pytest.raises(ValueError):
            await mailbox.vendor_accepted("cmd-1")
    finally:
        await engine.dispose()
