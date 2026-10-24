"""Signed lifecycle facts replace pending proposals without changing approved history."""

import json
from uuid import uuid4

import pytest

from forge.domain.states import State
from forge.providers.fake.state import ref_key
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineGate, EngineRunSpec
from tests.github_webhook_fixture import hooks as hooks, payload, planned, post, rows
from tests.github_webhook_fixture import native_store as native_store
from tests.runtime_fixture import OPERATOR_TOKEN


async def go(hooks, *, native_id=9011):
    doc = payload()
    doc["comment"]["id"] = native_id
    doc["comment"]["body"] = "/go"
    response = await post(hooks, doc, delivery=str(uuid4()))
    assert response.status_code == 202, response.text
    return response


def edit(hooks, *, title="Changed title", description="Changed body", version="issue-2"):
    hooks.fake.tasks[ref_key(hooks.task.subject)] = hooks.task.model_copy(
        update={"title": title, "description": description, "version": version}
    )
    document = payload("issues_edited.json")
    document["issue"].update(title=title, body=description, updated_at="2026-10-24T10:01:00Z")
    return document


@pytest.mark.parametrize("changed", ["title", "description"])
async def test_go_reads_current_native_task_even_when_edit_hook_is_delayed(hooks, changed):
    run, frozen = await planned(hooks)
    hooks.fake.tasks[ref_key(hooks.task.subject)] = hooks.task.model_copy(
        update={changed: "Unapproved edit"}
    )
    await go(hooks)
    assert (await hooks.runner.tick()).parked == 1
    stopped = await hooks.runs.load(run.id)
    assert stopped.state == State.BLOCKED and stopped.reason == "approval_task_changed"
    assert (await hooks.approvals.load(run.id)).spec == frozen.spec
    assert not any(step.handler == "dispatch" for step in await rows(hooks, EngineStep))


async def test_newer_native_timestamp_with_same_task_text_still_consumes_gate(hooks):
    run, frozen = await planned(hooks)
    hooks.fake.tasks[ref_key(hooks.task.subject)] = hooks.task.model_copy(
        update={"version": "new-comment-timestamp"}
    )
    await go(hooks)
    assert (await hooks.runner.tick()).completed == 1
    assert (await hooks.runs.load(run.id)).state == State.RUNNING
    assert (await hooks.approvals.load(run.id)).state == "consumed"
    assert (await hooks.approvals.load(run.id)).spec == frozen.spec


async def test_signed_edit_atomically_withdraws_old_gate_and_admits_original_policy(hooks):
    run, frozen = await planned(hooks)
    before = {
        row.run_id: (row.document_json, row.digest) for row in await rows(hooks, EngineRunSpec)
    }
    doc = edit(hooks)
    response = await post(hooks, doc, "issues")
    assert response.status_code == 202, response.text
    new_id = response.json()["commands"][0]["run_id"]
    assert new_id != run.id
    assert (await hooks.runs.load(run.id)).state == State.CANCELLED
    (old_gate,) = [row for row in await rows(hooks, EngineGate) if row.run_id == run.id]
    assert old_gate.state == "refused"
    (old_spec,) = await rows(hooks, EngineRunSpec)
    assert (old_spec.document_json, old_spec.digest) == before[run.id]
    initial = await hooks.steps.get(f"{new_id}:plan")
    assert (
        json.loads(initial.payload_json)["policy"]
        == json.loads((await hooks.steps.get(f"{run.id}:plan")).payload_json)["policy"]
    )
    assert json.loads(initial.payload_json)["task"]["title"] == "Changed title"
    assert (await post(hooks, doc, "issues", delivery=str(uuid4()))).json() == response.json()
    assert len(await rows(hooks, EngineCommand)) == 2
    assert (await hooks.runner.tick()).completed == 1
    assert (await hooks.runner.tick()).completed == 1
    fresh = await hooks.approvals.load(new_id)
    assert fresh.gate_id != frozen.gate_id and fresh.spec.limits == frozen.spec.limits


async def test_label_off_acts_only_before_consumption(hooks):
    run, _ = await planned(hooks)
    document = payload("issues_unlabeled.json")
    first = await post(hooks, document, "issues")
    assert first.json()["commands"][0]["reason"] == "cancelled"
    assert (await hooks.runs.load(run.id)).state == State.CANCELLED
    assert (await post(hooks, document, "issues")).json() == first.json()


async def test_post_go_label_off_and_edit_preserve_the_exact_consumed_spec(hooks):
    run, frozen = await planned(hooks)
    await go(hooks)
    assert (await hooks.runner.tick()).completed == 1
    response = await post(hooks, payload("issues_unlabeled.json"), "issues")
    assert response.json()["commands"][0]["reason"] == "run_past_gate"
    response = await post(hooks, edit(hooks), "issues", delivery=str(uuid4()))
    assert response.json()["commands"][0]["reason"] == "issue_no_longer_pending"
    assert (await hooks.runs.load(run.id)).state == State.RUNNING
    assert (await hooks.approvals.load(run.id)).spec.spec_json == frozen.spec.spec_json


async def test_operator_request_cannot_manufacture_an_issue_refresh(hooks):
    run, _ = await planned(hooks)
    body = {
        "repository": "project",
        "number": 42,
        "request_id": "forged-native",
        "command": {"kind": "implement", "argument": "", "issue_action": "edited"},
    }
    response = await hooks.client.post(
        "/api/v1/commands", json=body, headers={"Authorization": "Bearer " + OPERATOR_TOKEN}
    )
    assert response.status_code == 422
    assert (await hooks.runs.load(run.id)).state == State.AWAITING_APPROVAL
    assert len(await rows(hooks, EngineCommand)) == 1


async def test_native_task_read_failure_retries_before_consuming_the_gate(hooks, monkeypatch):
    from forge.port_values import ReadUnavailable
    from datetime import timedelta

    run, _ = await planned(hooks)
    original = hooks.fake.get_task

    async def unavailable(subject):
        raise ReadUnavailable("native task unavailable", "synthetic")

    monkeypatch.setattr(hooks.fake, "get_task", unavailable)
    await go(hooks)
    assert (await hooks.runner.tick()).rescheduled == 1
    assert (await hooks.approvals.load(run.id)).state == "open"
    assert not any(step.handler == "dispatch" for step in await rows(hooks, EngineStep))
    monkeypatch.setattr(hooks.fake, "get_task", original)
    hooks.clock.instant += timedelta(seconds=61)
    assert (await hooks.runner.tick()).completed == 1
    assert (await hooks.runs.load(run.id)).state == State.RUNNING


async def test_refresh_rollback_restores_the_old_gate_and_retry_admits_once(hooks, monkeypatch):
    from datetime import timedelta
    import forge.store.admission as admission

    run, _ = await planned(hooks)
    original = admission.supersede_pending

    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("worker lost before fresh admission")

    monkeypatch.setattr(admission, "supersede_pending", lost)
    document = edit(hooks)
    assert (await post(hooks, document, "issues")).status_code == 503
    assert (await hooks.runs.load(run.id)).state == State.AWAITING_APPROVAL
    assert (await hooks.approvals.load(run.id)).state == "open"
    assert len(await rows(hooks, EngineRunSpec)) == 1
    monkeypatch.setattr(admission, "supersede_pending", original)
    hooks.clock.instant += timedelta(seconds=121)
    response = await post(hooks, document, "issues")
    assert response.status_code == 202 and response.json()["commands"][0]["status"] == "accepted"
    assert (await hooks.runs.load(run.id)).state == State.CANCELLED
    new_id = response.json()["commands"][0]["run_id"]
    steps = await rows(hooks, EngineStep)
    assert len([step for step in steps if step.run_id == new_id]) == 1
    assert len({step.run_id for step in steps}) == 2
    assert (await post(hooks, document, "issues")).json() == response.json()
    assert len([step for step in await rows(hooks, EngineStep) if step.run_id == new_id]) == 1


async def test_refresh_keeps_original_policy_after_live_configuration_changes(hooks):
    from forge.runtime.settings import RuntimeSettings

    run, _ = await planned(hooks)
    actions = hooks.commands.actions["implement"].__self__
    document = actions.config.model_dump(mode="json", by_alias=True)
    document["connections"][0]["repositories"][0]["policy"]["limits"]["usd"] = "999"
    actions.config = RuntimeSettings.model_validate_json(json.dumps(document))
    response = await post(hooks, edit(hooks), "issues")
    new_id = response.json()["commands"][0]["run_id"]
    old = json.loads((await hooks.steps.get(f"{run.id}:plan")).payload_json)
    fresh = json.loads((await hooks.steps.get(f"{new_id}:plan")).payload_json)
    assert fresh["policy"] == old["policy"]
    assert fresh["policy"]["limits"]["usd"] != "999"
