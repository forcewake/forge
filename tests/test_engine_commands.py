"""Command replay, independent reply authority and cancellation revocation proofs."""

import json

import pytest
from sqlalchemy import select

from forge.command_values import CommandEnvelope
from forge.domain.states import State
from forge.engine.commands import Commands
from forge.ingress_values import Command
from forge.store.command_effects import CommandEffectStore
from forge.store.command_runs import CommandRuns
from forge.store.commands import CommandStore
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.operator import OperatorStore
from forge.store.records import StaleLease, StoreConflict
from tests.test_engine_lane_handlers import (
    case as case,
    env as env,
    settings as settings,
    started,
)


@pytest.fixture
def commands(case):
    env = case["env"]
    store, authority = CommandStore(env.factory), CommandEffectStore(env.factory)
    service = Commands(
        store,
        CommandRuns(env.factory),
        OperatorStore(env.factory),
        env.effects,
        authority,
        case["keys"],
        lambda repo: env.fake,
        env.clock,
        actions={},
    )
    case.update(commands=service, command_store=store, command_authority=authority)
    return case


def envelope(case, kind, *, native_id="note-1", argument="", actor=None, reply=False):
    env = case["env"]
    return CommandEnvelope(
        source="webhook",
        native_id=native_id,
        actor=actor or env.intent.actor,
        target=env.intent.task.subject,
        command=Command(kind=kind, argument=argument),
        native_reply=reply,
    )


async def test_native_reply_has_its_own_idempotent_owner_without_a_run(commands):
    env = commands["env"]
    value = envelope(commands, "status", reply=True)
    one = await commands["commands"].submit(value)
    two = await commands["commands"].submit(value)
    assert one == two and one.reason == "run_not_found"
    assert sum(name == "post_comment" for name, _ in env.fake.calls) == 1
    async with env.factory() as session:
        command = await session.scalar(select(EngineCommand))
        effect = await session.scalar(select(EngineEffect))
        assert command.state == "completed" and command.run_id is None
        assert effect.command_id == command.id and effect.step_id is None and effect.run_id is None
        assert effect.state == "committed"
    changed = value.model_copy(update={"command": Command(kind="cancel")})
    with pytest.raises(StoreConflict, match="changed content"):
        await commands["commands"].submit(changed)


async def test_status_is_readonly_and_redacts_secret_material(commands, monkeypatch):
    env = commands["env"]
    run, _, _ = await started(commands)
    before = len(commands["native"].writes)
    comments_before = sum(name == "post_comment" for name, _ in env.fake.calls)

    async def forbidden(*args):
        raise AssertionError("read-only status must not request approver authority")

    monkeypatch.setattr(env.fake, "is_approver", forbidden)
    result = await commands["commands"].submit(envelope(commands, "status"))
    assert result.status == "observed" and result.run_id == run.id
    snapshot = json.loads(result.data_json)
    assert (
        snapshot["state"] == "running"
        and snapshot["budgets"]["implementation"]["held"]["usd"] == "2"
    )
    assert "synthetic-admin" not in result.data_json and "ciphertext" not in result.data_json
    assert len(commands["native"].writes) == before
    assert sum(name == "post_comment" for name, _ in env.fake.calls) == comments_before


async def test_cancel_revokes_and_withdraws_before_blocking_owned_key(commands, monkeypatch):
    from forge.api.lane_auth_values import LaneAuthError

    env = commands["env"]
    run, _, pending = await started(commands)
    (old_lease,) = await env.steps.claim_due("old-lane-worker", env.clock.now(), 60)
    original = env.effects._gateway.block
    observations = []

    async def blocked_after_revoke(alias):
        current = await env.runs.load(run.id)
        async with env.factory() as session:
            live = list(
                await session.scalars(
                    select(EngineStep).where(
                        EngineStep.run_id == run.id, EngineStep.state.in_(("scheduled", "running"))
                    )
                )
            )
        observations.append((current.cancel_requested, current.state, len(live)))
        return await original(alias)

    monkeypatch.setattr(env.effects._gateway, "block", blocked_after_revoke)
    value = envelope(commands, "cancel", reply=True)
    result = await commands["commands"].submit(value)
    assert result.status == "accepted" and result.reason == "cancelled"
    assert observations == [(True, State.CANCELLED, 0)]
    current = await env.runs.load(run.id)
    assert current.cancel_requested and current.cancellation_generation == 1
    assert (await commands["keys"].get(f"forge:{run.id}:1:1")).state == "purged"
    assert (await commands["commands"].submit(value)) == result
    assert len(observations) == 1
    with pytest.raises(StaleLease):
        await env.steps.assert_current(old_lease, now=env.clock.now())
    with pytest.raises(LaneAuthError):
        await commands["auth"].store.load(pending.identity)
    async with env.factory() as session:
        effects = list(
            await session.scalars(select(EngineEffect).where(EngineEffect.command_id.is_not(None)))
        )
        assert {row.kind for row in effects} == {"gw_block", "post_comment"}
        assert all(row.step_id is None for row in effects)
    assert not any(name == "commit" for name, _ in env.fake.calls)


async def test_bot_authored_and_mutable_permission_refusals_do_no_work(commands):
    env = commands["env"]
    bot = env.fake.bot_identity()
    result = await commands["commands"].submit(envelope(commands, "implement", actor=bot))
    assert result.reason == "bot_authored"
    result = await commands["commands"].submit(
        envelope(commands, "cancel", argument="<!-- forge:plan:example -->")
    )
    assert result.reason == "bot_authored"
    run, _, _ = await started(commands)
    env.fake.approvers.clear()
    result = await commands["commands"].submit(envelope(commands, "cancel", native_id="denied"))
    assert result.reason == "actor_not_authorized"
    assert not (await env.runs.load(run.id)).cancel_requested


async def test_command_authority_cannot_commit_dispatch_mint_or_block_foreign_alias(commands):
    env = commands["env"]
    run, _, _ = await started(commands)
    value = envelope(commands, "cancel")
    row = await commands["command_store"].accept(value, now=env.clock.now())
    lease = await commands["command_store"].claim(
        "operator", now=env.clock.now(), command_id=row.id
    )
    await OperatorStore(env.factory).cancel(lease, run.id, now=env.clock.now())
    for kind in ("commit", "ensure_draft", "dispatch_lane", "gw_mint", "gw_update"):
        with pytest.raises(StoreConflict, match="cannot perform"):
            await commands["command_authority"].request_once(
                lease, key=f"command:{row.id}:{kind}", kind=kind, payload_json="{}"
            )
    with pytest.raises(StoreConflict, match="persisted key"):
        await commands["command_authority"].request_once(
            lease,
            key=f"command:{row.id}:foreign",
            kind="gw_block",
            payload_json='{"alias":"another-run-key"}',
        )
    with pytest.raises(StoreConflict, match="fixed resolved reply"):
        await commands["command_authority"].request_once(
            lease,
            key=f"command:{row.id}:forged",
            kind="post_comment",
            payload_json='{"body":"unauthorized target"}',
        )


async def test_terminal_cancel_does_not_revoke_completed_work(commands):
    env = commands["env"]
    run, _, _ = await started(commands)
    current = await env.runs.load(run.id)
    await env.runs.transition(
        run.id,
        expected_version=current.version,
        target=State.BLOCKED,
        failed_handler=__import__("forge.domain.states", fromlist=["Handler"]).Handler.AWAIT_LANE,
        reason="driver_failed",
    )
    result = await commands["commands"].submit(envelope(commands, "cancel"))
    assert result.status == "observed" and result.reason == "run_terminal"
    current = await env.runs.load(run.id)
    assert current.state == State.BLOCKED and not current.cancel_requested


async def test_global_worker_resolves_authority_from_each_queued_repository(commands):
    env = commands["env"]
    original = env.intent.task.subject.repo
    other = original.model_copy(
        update={
            "identity": original.identity.model_copy(update={"repository_id": "43"}),
            "full_name": "owner/other",
        }
    )
    observed = []

    class ScopedActor:
        def __init__(self, repo, allowed):
            self.repo, self.allowed = repo, allowed

        def bot_identity(self):
            observed.append(("bot", self.repo.identity.repository_id))
            return env.fake.bot_identity()

        async def is_approver(self, actor, subject):
            assert subject.repo.identity == self.repo.identity
            observed.append(("approver", self.repo.identity.repository_id))
            return self.allowed

    actors = {
        original.identity.digest: ScopedActor(original, True),
        other.identity.digest: ScopedActor(other, False),
    }
    commands["commands"].actor_for = lambda repo: actors[repo.identity.digest]
    value = envelope(commands, "cancel").model_copy(
        update={"target": env.intent.task.subject.model_copy(update={"repo": other})}
    )
    receipt = await commands["command_store"].accept(value, now=env.clock.now())
    assert await commands["commands"].tick() == 1
    result = (await commands["command_store"].load(receipt.id)).result
    assert result.reason == "actor_not_authorized"
    assert observed == [("bot", "43"), ("approver", "43")]


async def test_queued_execution_rechecks_bot_identity_without_submit(commands):
    env = commands["env"]
    value = envelope(commands, "cancel", actor=env.fake.bot_identity())
    receipt = await commands["command_store"].accept(value, now=env.clock.now())
    assert await commands["commands"].tick() == 1
    assert (await commands["command_store"].load(receipt.id)).result.reason == "bot_authored"
    assert not env.fake.calls
