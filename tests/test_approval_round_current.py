"""Correction approval re-proves the native notes and cannot use root go authority."""

import json

import pytest
from sqlalchemy import select

from forge.command_values import CommandEnvelope
from forge.domain.states import State
from forge.ingress_values import Command
from forge.providers.fake import Blob
from forge.providers.fake.state import commit_value, ref_key
from forge.runtime.actions import InitialActions
from forge.runtime.routing import Registry
from forge.store.models_commands import EngineCommand
from forge.store.models_runs import EngineApprovedInput
from forge.store.records import StoreConflict
from tests.runtime_fixture import configuration
from tests.test_engine_rounds import (
    case as case,
    delivery as delivery,
    env as env,
    feedback,
    planned_child,
    rounds as rounds,
    settings as settings,
    submit,
)


@pytest.mark.parametrize(
    "fault",
    (
        "fix_body",
        "approval_body",
        "fix_missing",
        "approval_missing",
        "fix_author",
        "approval_author",
        "fix_id",
        "approval_id",
        "task",
        "head",
        "receipt_kind",
        "receipt_result",
    ),
)
async def test_child_gate_rechecks_native_and_durable_authority_after_scheduling(rounds, fault):
    env = rounds["env"]
    child, frozen = await planned_child(rounds)
    approval = feedback(rounds, "approve_revision", child.id, ident="20")
    assert (await submit(rounds, approval, run_id=child.id)).status == "accepted"
    notes = rounds["draft"].feedback
    if fault.startswith(("fix_", "approval_")):
        native_id = "11" if fault.startswith("fix_") else "20"
        index = next(i for i, item in enumerate(notes) if item.ref.native_id == native_id)
        note = notes[index]
        if fault.endswith("missing"):
            notes.pop(index)
        elif fault.endswith("body"):
            notes[index] = note.model_copy(update={"body": "/fix Unapproved replacement."})
        elif fault.endswith("author"):
            notes[index] = note.model_copy(update={"author": env.fake.bot_identity()})
        else:
            notes[index] = note.model_copy(
                update={"ref": note.ref.model_copy(update={"native_id": "999"})}
            )
    elif fault == "task":
        env.fake.tasks[ref_key(env.intent.task.subject)] = env.intent.task.model_copy(
            update={"description": "Unapproved issue edit"}
        )
    elif fault == "head":
        repo = env.fake.repo(approval.target.repo)
        human = commit_value({"a.py": Blob(b"human\n")}, rounds["sha"], "human")
        repo.commits[human.sha], repo.branches["forge/" + child.root_run_id] = human, human.sha
    else:
        async with env.factory.begin() as session:
            command = await session.scalar(
                select(EngineCommand).where(EngineCommand.source_key == approval.source_key)
            )
            if fault == "receipt_kind":
                value = json.loads(command.document_json)
                value["command"]["kind"] = "go"
                command.document_json = json.dumps(value)
            else:
                value = json.loads(command.result_json)
                value["reason"] = "implementation_queued"
                command.result_json = json.dumps(value)
    before = len(env.fake.lanes), len(rounds["native"].writes)
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(child.id)
    assert current.state == State.BLOCKED
    assert current.reason == (
        "approval_task_changed" if fault == "task" else "approval_revision_changed"
    )
    assert (await env.approvals.load(child.id)).state == "open"
    assert (await env.approvals.load(child.id)).spec == frozen.spec
    assert await env.count(EngineApprovedInput) == 1
    assert (len(env.fake.lanes), len(rounds["native"].writes)) == before


async def test_direct_approval_step_has_no_native_revision_command_authority(rounds):
    env = rounds["env"]
    child, frozen = await planned_child(rounds)
    await env.approve(child.id, await env.claim(child, frozen), command_key="go-shortcut")
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(child.id)).reason == "approval_revision_changed"
    assert (await env.approvals.load(child.id)).state == "open"
    assert await env.count(EngineApprovedInput) == 1 and len(env.fake.lanes) == 1


async def test_root_go_action_and_store_refuse_child_before_enqueuing(rounds, tmp_path):
    env = rounds["env"]
    child, frozen = await planned_child(rounds)
    envelope = CommandEnvelope(
        source="webhook",
        native_id="issue_comment:30",
        actor=env.intent.actor,
        target=env.intent.task.subject,
        command=Command(kind="go", argument=child.id),
    )
    store = rounds["command_store"]
    accepted = await store.accept(envelope, now=env.clock.now())
    lease = await store.claim("root-go", now=env.clock.now(), command_id=accepted.id)
    assert lease is not None
    config, _ = configuration(tmp_path)
    result = await InitialActions(config, Registry({}), env.factory, env.clock).approve(
        lease, envelope, child
    )
    assert result.status == "refused" and result.reason == "revision_approval_required"
    with pytest.raises(StoreConflict, match="original subject or actor"):
        await env.approve(
            child.id,
            await env.claim(child, frozen),
            command_key=lease.command_id,
            command_lease=lease,
        )
    assert await env.steps.get(f"{child.id}:approve:{lease.command_id}") is None
    assert (await env.runs.load(child.id)).state == State.AWAITING_APPROVAL
    assert (await env.approvals.load(child.id)).state == "open"
    assert await env.count(EngineApprovedInput) == 1 and len(env.fake.lanes) == 1
