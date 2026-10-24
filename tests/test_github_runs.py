"""Retained lifecycle lessons execute one native engine for GitHub and GitLab."""

import asyncio
from datetime import timedelta
import json

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from forge_protocol import digest
from forge.domain.states import State
from forge.providers.fake import Blob, Fault
from forge.providers.fake.state import commit_value, ref_key
from forge.runtime.settings import RuntimeSettings
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineCandidate, EngineStep
from forge.store.models_runs import EngineApprovedInput, EngineGate, EngineRun, EngineRunSpec
from tests.lifecycle_fixture import (
    advance,
    case as case,
    check,
    command,
    completed,
    delivery as delivery,
    env as env,
    lifecycle as lifecycle,
    planned,
    published,
    ready,
    rows,
    settings as settings,
    started,
)


async def test_native_plan_comment_and_durable_spec_have_one_exact_implementation(lifecycle):
    env = lifecycle["env"]
    run, frozen = await planned(lifecycle)
    comment = next(iter(env.fake.comments.values()))
    document = comment.body.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert document == frozen.spec.spec_json
    assert json.loads(document)["plan"] == lifecycle["template_plan"].model_dump(mode="json")
    assert frozen.spec.plan.digest == digest(lifecycle["template_plan"].model_dump(mode="json"))
    assert frozen.approved.approved_digest in comment.body
    assert frozen.approved.base_sha in comment.body and comment.marker == frozen.marker
    assert (await rows(lifecycle, EngineRunSpec))[0].document_json == document
    assert (await rows(lifecycle, EngineGate))[0].state == "open"
    assert len(await rows(lifecycle, EngineCommand)) == 1
    assert not await rows(lifecycle, EngineApprovedInput)
    assert len(env.planner.calls) == 1 and not env.fake.lanes and not env.fake.drafts
    assert (await env.runs.load(run.id)).state == State.AWAITING_APPROVAL


@pytest.mark.parametrize(
    "fault", ("unauthorized", "bot_flag", "bot_id", "bot_login", "no_approvers")
)
async def test_admission_denies_foreign_or_bot_authority_before_planning(lifecycle, fault):
    env = lifecycle["env"]
    actor = env.intent.actor
    if fault == "unauthorized":
        actor = actor.model_copy(update={"native_id": "777"})
    elif fault == "bot_flag":
        actor = actor.model_copy(update={"is_bot": True})
    elif fault == "bot_id":
        actor = actor.model_copy(update={"native_id": "99"})
    elif fault == "bot_login":
        actor = actor.model_copy(update={"login": env.fake.bot_identity().login.upper()})
    else:
        env.fake.approvers.clear()
    result = await lifecycle["commands"].submit(command(lifecycle, "implement", actor=actor))
    assert result.reason == (
        "actor_not_authorized" if fault in {"unauthorized", "no_approvers"} else "bot_authored"
    )
    assert not await rows(lifecycle, EngineRun) and not env.planner.calls
    assert not env.fake.comments and not env.fake.lanes


async def test_redelivery_and_concurrent_new_admission_share_one_active_subject(lifecycle):
    env = lifecycle["env"]
    original = command(lifecycle, "implement", native_id="issue_comment:admit")
    accepted = await lifecycle["commands"].submit(original)
    assert await lifecycle["commands"].submit(original) == accepted
    outcomes = await asyncio.gather(
        *(lifecycle["commands"].submit(command(lifecycle, "implement")) for _ in range(2))
    )
    assert all(result.reason == "another_run_active" for result in outcomes)
    assert len(await rows(lifecycle, EngineRun)) == 1
    assert len(await rows(lifecycle, EngineStep)) == 1
    for _ in range(2):
        assert (await env.runner.tick()).completed == 1
    assert len(env.planner.calls) == len(env.fake.comments) == 1


async def test_native_database_unique_admission_remains_an_independent_arbiter(lifecycle):
    run, _ = await planned(lifecycle)
    env = lifecycle["env"]
    async with env.factory() as session:
        existing = await session.get(EngineRun, run.id)
        data = {
            column.name: getattr(existing, column.name) for column in EngineRun.__table__.columns
        }
    data.update(id="f" * 32, root_run_id="f" * 32)
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as session:
            session.add(EngineRun(**data))
    assert len(await rows(lifecycle, EngineRun)) == 1


@pytest.mark.parametrize(
    "fault", ("builtin", "fallback", "neighbor", "approvers", "waiver", "provider", "closing_policy")
)
async def test_closed_trusted_configuration_refuses_retired_or_unbound_authority(lifecycle, fault):
    document = lifecycle["config"].model_dump(mode="json", by_alias=True)
    repo = document["connections"][0]["repositories"][0]
    if fault == "builtin":
        repo["policy"]["driver"] = "builtin"
    elif fault == "fallback":
        repo["policy"]["fallback_drivers"] = ["claude_code", "copilot_cli"]
    elif fault == "neighbor":
        repo["neighbors"] = [{"repository": "unconfigured"}]
    elif fault == "approvers":
        repo["approver_ids"] = []
    elif fault == "waiver":
        repo["policy"]["verification"]["allow_skipped"] = True
    elif fault == "closing_policy":
        del repo["policy"]["closing_reserve"]
    else:
        document["connections"][0]["provider"] = "retired-provider"
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(document))
    assert not lifecycle["env"].planner.calls


async def test_admission_snapshot_does_not_reread_a_moving_base_for_planning(lifecycle):
    env = lifecycle["env"]
    result = await lifecycle["commands"].submit(command(lifecycle, "implement"))
    initial = await env.steps.get(result.run_id + ":plan")
    base = json.loads(initial.payload_json)["snapshot"]["primary"]["sha"]
    repo = env.fake.repo(env.intent.task.subject.repo)
    human = commit_value({"a.py": Blob(b"human\n")}, base, "human")
    repo.commits[human.sha], repo.branches["main"] = human, human.sha
    for _ in range(2):
        assert (await env.runner.tick()).completed == 1
    frozen = await env.approvals.load(result.run_id)
    assert frozen.spec.base_sha == base
    assert env.planner.calls[0][1][1].primary.sha == base
    await lifecycle["commands"].submit(command(lifecycle, "go", argument=result.run_id))
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(result.run_id)).reason == "approval_base_drift"
    assert not env.fake.lanes


@pytest.mark.parametrize("argument", ("short", "f" * 8, "f" * 31, "f" * 32))
async def test_go_accepts_full_native_run_identity_and_rejects_prefixes_or_unknown(
    lifecycle, argument
):
    run, _ = await planned(lifecycle)
    before = len(await rows(lifecycle, EngineStep))
    result = await lifecycle["commands"].submit(command(lifecycle, "go", argument=argument))
    assert result.reason == ("run_not_found" if len(argument) == 32 else "malformed_command")
    assert len(await rows(lifecycle, EngineStep)) == before
    assert (await lifecycle["env"].runs.load(run.id)).state == State.AWAITING_APPROVAL


@pytest.mark.parametrize(
    "fault",
    ("wrong_issue", "unauthorized", "revoked_before_execution", "expired", "edited_display"),
)
async def test_go_never_consumes_changed_subject_actor_expiry_or_display(lifecycle, fault):
    env = lifecycle["env"]
    run, frozen = await planned(lifecycle)
    target, actor = env.intent.task.subject, env.intent.actor
    if fault == "wrong_issue":
        target = target.model_copy(update={"number": 2})
    elif fault == "unauthorized":
        actor = actor.model_copy(update={"native_id": "777"})
    elif fault == "expired":
        advance(lifecycle, 3601)
    elif fault == "edited_display":
        next(iter(env.fake.comments.values())).body += "\nDifferent instructions."
    result = await lifecycle["commands"].submit(
        command(lifecycle, "go", argument=run.id, target=target, actor=actor)
    )
    if fault in {"wrong_issue", "unauthorized"}:
        assert result.reason == (
            "run_scope_mismatch" if fault == "wrong_issue" else "actor_not_authorized"
        )
    else:
        if fault == "revoked_before_execution":
            env.fake.approvers.clear()
        tick = await env.runner.tick()
        assert tick.completed == 1 if fault == "revoked_before_execution" else tick.parked == 1
    assert (await env.approvals.load(run.id)).state != "consumed"
    assert not await rows(lifecycle, EngineApprovedInput) and not env.fake.lanes
    assert (await env.approvals.load(run.id)).spec == frozen.spec


@pytest.mark.parametrize("argument", ("", "full"))
async def test_native_go_replay_consumes_one_gate_and_schedules_one_dispatch(lifecycle, argument):
    env = lifecycle["env"]
    run, frozen = await planned(lifecycle)
    value = command(
        lifecycle, "go", argument=run.id if argument else "", native_id="issue_comment:go"
    )
    result = await lifecycle["commands"].submit(value)
    assert result.status == "accepted" and await lifecycle["commands"].submit(value) == result
    assert (await env.runner.tick()).completed == 1
    assert await lifecycle["commands"].submit(value) == result
    assert (await env.approvals.load(run.id)).state == "consumed"
    assert len(await rows(lifecycle, EngineApprovedInput)) == 1
    steps = [row for row in await rows(lifecycle, EngineStep) if row.handler == "dispatch"]
    assert (
        len(steps) == 1
        and json.loads(steps[0].payload_json)["approved_digest"] == frozen.approved.approved_digest
    )


async def test_dispatch_keeps_frozen_scope_budget_and_bytes_after_live_config_drift(lifecycle):
    env = lifecycle["env"]
    run, frozen, pending = await started(lifecycle)
    config = lifecycle["initial"].config.model_dump(mode="json", by_alias=True)
    live = config["connections"][0]["repositories"][0]["policy"]
    live["limits"]["usd"], live["verification"]["required_checks"] = "999", ["other"]
    lifecycle["initial"].config = RuntimeSettings.model_validate_json(json.dumps(config))
    assert pending.brief.approved_digest == frozen.approved.approved_digest
    assert pending.brief.brief_text == frozen.approved.brief_text
    assert pending.dispatch.driver == frozen.spec.driver == "claude_code"
    assert pending.dispatch.execution_base_sha == frozen.spec.base_sha
    assert pending.dispatch.checkpoint_id is None
    assert (await env.approvals.load(run.id)).spec == frozen.spec
    pool = await lifecycle["budgets"].load(run.id, "implementation")
    assert pool.capacity.usd == frozen.spec.limits.usd - frozen.spec.closing_reserve.usd
    await completed(lifecycle, pending)
    assert (await env.runner.tick()).completed == 1
    sha = await env.fake.head(env.intent.task.subject.repo, "forge/" + run.id)
    check(lifecycle, sha)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.REVIEWING
    assert frozen.spec.verification.required_checks == ("unit",)


async def test_actual_collected_candidate_green_ci_and_native_comment_end_at_one_draft(lifecycle):
    env = lifecycle["env"]
    run, frozen, pending, sha = await ready(lifecycle)
    candidates = await rows(lifecycle, EngineCandidate)
    assert len(candidates) == 1 and candidates[0].published_sha == sha
    facts = json.loads(candidates[0].validation_json)
    assert facts["verification"]["tested_sha"] == facts["review"]["candidate_sha"] == sha
    assert len(env.fake.drafts) == 1 and len(lifecycle["reviewer"].calls) == 1
    assert sum(name == "commit" for name, _ in env.fake.calls) == 1
    assert sum(name == "ensure_draft" for name, _ in env.fake.calls) == 1
    assert sum(name == "post_review" for name, _ in env.fake.calls) == 1
    assert pending.brief.approved_digest == frozen.approved.approved_digest
    assert (await lifecycle["keys"].get(f"forge:{run.id}:1:1")).state == "purged"
    assert not any("merge" == name for name, _ in env.fake.calls)


@pytest.mark.parametrize(
    "observation",
    (
        "optional",
        "lane",
        "missing",
        "pending",
        "skipped",
        "cancelled",
        "unknown",
        "stale_sha",
        "stale_time",
    ),
)
async def test_non_proving_native_checks_cannot_start_paid_closing(lifecycle, observation):
    env = lifecycle["env"]
    run, _, _, sha = await published(lifecycle)
    value = check(lifecycle, sha)
    updates = {
        "optional": {"name": "docs"},
        "lane": {"role": "lane"},
        "pending": {"status": "pending"},
        "skipped": {"status": "skipped"},
        "cancelled": {"status": "cancelled"},
        "unknown": {"status": "unknown"},
        "stale_sha": {"tested_sha": "f" * 40},
        "stale_time": {"observed_at": env.clock.now() - timedelta(days=1)},
    }
    env.fake.checks[env.intent.task.subject.repo.identity.digest] = (
        () if observation == "missing" else (value.model_copy(update=updates[observation]),)
    )
    result = await env.runner.tick()
    assert result.rescheduled + result.parked == 1
    assert not lifecycle["reviewer"].calls
    assert (await env.runs.load(run.id)).state != State.READY_FOR_HUMAN
    if result.rescheduled:
        advance(lifecycle, 60)
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).reason == "verification_timeout"
    assert len(env.fake.lanes) == 1


@pytest.mark.parametrize("order", (False, True))
@pytest.mark.parametrize(
    "kind", ("new_failure", "same_time_conflict", "same_time_distinct", "new_success")
)
async def test_native_attempt_and_identity_are_order_independent_without_name_collapse(
    lifecycle, order, kind
):
    env = lifecycle["env"]
    run, _, _, sha = await published(lifecycle)
    first = check(lifecycle, sha)
    second = first.model_copy(update={"native_id": "check-2"})
    if kind == "new_failure":
        second = second.model_copy(update={"attempt": 2, "status": "failed"})
    elif kind == "same_time_conflict":
        second = second.model_copy(update={"status": "failed"})
    elif kind == "new_success":
        first = first.model_copy(update={"attempt": 1, "status": "failed"})
        second = second.model_copy(update={"attempt": 2})
    values = (first, second)
    env.fake.checks[env.intent.task.subject.repo.identity.digest] = (
        values[::-1] if order else values
    )
    result = await env.runner.tick()
    assert (result.completed == 1) == (kind == "new_success")
    if kind != "new_success":
        assert result.parked == 1 and not lifecycle["reviewer"].calls
    else:
        candidate = (await rows(lifecycle, EngineCandidate))[0]
        evidence = json.loads(json.loads(candidate.validation_json)["verification"]["evidence"])
        assert evidence["checks"][0]["native_id"] == "check-2"
    assert len(env.fake.lanes) == 1


@pytest.mark.parametrize("failure", ("infrastructure", "configuration", "unknown", "code"))
async def test_only_explicit_native_code_failure_creates_fresh_bounded_repair(lifecycle, failure):
    env = lifecycle["env"]
    run, frozen, _, sha = await published(lifecycle)
    check(lifecycle, sha, status="failed", failure_kind=failure)
    tick = await env.runner.tick()
    current = await env.runs.load(run.id)
    if failure != "code":
        assert tick.parked == 1 and current.reason == "verification_non_code_failure"
        assert (current.attempt, current.generation) == (1, 1)
        return
    assert tick.completed == 1 and (current.attempt, current.generation) == (2, 2)
    repair = await env.steps.get(f"{run.id}:2:2:repair")
    authority = json.loads(repair.payload_json)
    assert authority["repair"]["candidate_sha"] == sha
    assert "check-1" in authority["repair"]["failure_context"]
    assert authority["approved_digest"] == frozen.approved.approved_digest
    assert not lifecycle["reviewer"].calls


async def test_planner_failure_never_opens_an_unproven_gate_or_creates_execution(lifecycle):
    env = lifecycle["env"]
    env.planner.script.clear()
    env.planner.script.append(("plan", RuntimeError("synthetic planner failure")))
    accepted = await lifecycle["commands"].submit(command(lifecycle, "implement"))
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(accepted.run_id)).state == State.BLOCKED
    assert not env.fake.comments and not await rows(lifecycle, EngineGate)
    assert not env.fake.lanes and not lifecycle["native"].writes


@pytest.mark.parametrize("method", ("get_task", "head"))
async def test_unavailable_native_admission_data_never_falls_back_and_recovers_same_command(
    lifecycle, method
):
    from forge.port_values import ReadUnavailable

    env = lifecycle["env"]
    original = command(lifecycle, "implement", native_id="issue_comment:read-recovery")
    env.fake.fail_next(method, Fault(http_5xx=True))
    with pytest.raises(ReadUnavailable):
        await lifecycle["commands"].submit(original)
    assert not await rows(lifecycle, EngineRun) and not env.planner.calls
    assert len(await rows(lifecycle, EngineCommand)) == 1
    advance(lifecycle, 121)
    assert await lifecycle["commands"].tick() == 1
    run = (await rows(lifecycle, EngineRun))[0]
    assert run.state == "queued" and len(await rows(lifecycle, EngineCommand)) == 1
    for _ in range(2):
        assert (await env.runner.tick()).completed == 1
    assert len(env.planner.calls) == 1 and len(env.fake.comments) == 1
    assert (await lifecycle["commands"].submit(original)).run_id == run.id


async def test_original_command_and_frozen_proposal_remain_separate_for_two_issues(lifecycle):
    env = lifecycle["env"]
    first, original = await planned(lifecycle)
    target = env.intent.task.subject.model_copy(update={"number": 2})
    env.fake.tasks[ref_key(target)] = env.intent.task.model_copy(
        update={"subject": target, "title": "Second issue"}
    )
    env.fake.approvers.add((ref_key(target), env.intent.actor.native_id))
    changed = lifecycle["template_plan"].model_copy(update={"summary": "Independent second plan"})
    env.planner.script.append(("plan", changed))
    second, proposal = await planned(
        lifecycle, envelope=command(lifecycle, "implement", target=target)
    )
    assert first.id != second.id and original.spec.plan != proposal.spec.plan
    assert original.spec.task_title != proposal.spec.task_title
    assert (await env.approvals.load(first.id)).spec == original.spec
    assert proposal.spec.plan == changed
    assert len(env.fake.comments) == 2 and len(env.planner.calls) == 2


async def test_capacity_is_decided_before_paid_planning_and_never_consumes_an_attempt(lifecycle):
    env = lifecycle["env"]
    lifecycle["initial"].config = lifecycle["config"].model_copy(update={"max_active": 1})
    run, _ = await planned(lifecycle)
    target = env.intent.task.subject.model_copy(update={"number": 2})
    env.fake.tasks[ref_key(target)] = env.intent.task.model_copy(update={"subject": target})
    env.fake.approvers.add((ref_key(target), env.intent.actor.native_id))
    refused = await lifecycle["commands"].submit(command(lifecycle, "implement", target=target))
    assert refused.reason == "another_run_active"
    assert len(await rows(lifecycle, EngineRun)) == 1 and len(env.planner.calls) == 1
    current = await env.runs.load(run.id)
    assert (current.attempt, current.generation) == (1, 1)
    assert not lifecycle["native"].writes and not env.fake.lanes


@pytest.mark.parametrize("cross_provider", (False, True))
async def test_explicit_neighbor_connections_freeze_exact_read_only_source_set(
    lifecycle, cross_provider
):
    from tests.lifecycle_fixture import neighbor

    env = lifecycle["env"]
    other = "gitlab" if env.fake.provider == "github" else "github"
    first = neighbor(lifecycle, "neighbor1", provider=other if cross_provider else None)
    second = neighbor(lifecycle, "neighbor2", native_id="44")
    run, _ = await planned(lifecycle)
    initial = await env.steps.get(run.id + ":plan")
    snapshot = json.loads(initial.payload_json)["snapshot"]
    assert len(snapshot["neighbors"]) == 2
    assert [item["sha"] for item in snapshot["neighbors"]] == [first[2], second[2]]
    assert all(item["allowed_globs"] == ["b.py"] for item in snapshot["neighbors"])
    assert snapshot["primary"]["repo"][
        "identity"
    ] == env.intent.task.subject.repo.identity.model_dump(mode="json")
    for fake, ref, _ in (first, second):
        assert fake.calls and all(name == "head" for name, _ in fake.calls)
        assert not fake.comments and not fake.lanes and not fake.drafts
    assert not env.fake.lanes


async def test_optional_pending_and_identical_native_observation_do_not_hold_required_green(
    lifecycle,
):
    env = lifecycle["env"]
    run, _, _, sha = await published(lifecycle)
    required = check(lifecycle, sha)
    optional = required.model_copy(
        update={
            "name": "docs",
            "status": "pending",
            "observed_at": env.clock.now() - timedelta(days=1),
            "native_id": "optional",
        }
    )
    env.fake.checks[env.intent.task.subject.repo.identity.digest] = (optional, required, required)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.REVIEWING
    assert not lifecycle["reviewer"].calls


@pytest.mark.parametrize("path", (".forge.yml", ".github/workflows/ci.yml"))
async def test_real_collected_candidate_cannot_publish_a_denied_path(lifecycle, path):
    from tests.test_engine_delivery import full_patch

    env = lifecycle["env"]
    repo = env.fake.repo(env.intent.task.subject.repo)
    base = commit_value(
        {"a.py": Blob(b"old\n"), path: Blob(b"old\n")}, repo.branches["main"], "metadata"
    )
    repo.commits[base.sha], repo.branches["main"] = base, base.sha
    run, _, pending = await started(lifecycle)
    diff = (
        full_patch(b"old\n", b"new\n")
        .replace(b"a/a.py", ("a/" + path).encode())
        .replace(b"b/a.py", ("b/" + path).encode())
    )
    await completed(lifecycle, pending, diff=diff)
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "candidate_write_policy_refused"
    assert not any(name in {"commit", "ensure_draft"} for name, _ in env.fake.calls)


async def test_cancel_during_native_base_read_fences_the_final_publication_boundary(
    lifecycle, monkeypatch
):
    env = lifecycle["env"]
    run, _, pending = await started(lifecycle)
    await completed(lifecycle, pending)
    original = env.fake.read_blob

    async def cancel_before_return(*args):
        result = await original(*args)
        cancelled = await lifecycle["commands"].submit(
            command(lifecycle, "cancel", argument=run.id)
        )
        assert cancelled.reason == "cancelled"
        return result

    monkeypatch.setattr(env.fake, "read_blob", cancel_before_return)
    assert (await env.runner.tick()).lost == 1
    assert (await env.runs.load(run.id)).state == State.CANCELLED
    assert not any(name in {"commit", "ensure_draft"} for name, _ in env.fake.calls)


@pytest.mark.parametrize("conclusion", ("neutral", "skipped", "unknown"))
def test_native_neutral_and_skipped_conclusions_never_become_green(conclusion):
    from forge.providers.github.ci import job_status
    from tests.test_verification_checks import check as observation, reduce

    status = job_status({"status": "completed", "conclusion": conclusion})
    result = reduce(observation(status=status))
    assert result.status == "unknown" and not result.successful
