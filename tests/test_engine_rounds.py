"""Real SQL correction lineage, fresh approval and same-Draft publication."""

import asyncio
import json

import pytest
from sqlalchemy import select

from forge_protocol import RunIdentity
from forge.command_values import CommandEnvelope
from forge.domain.states import State
from forge.engine.rounds import RoundActions
from forge.ingress_values import Command
from forge.port_values import CommentRef, FeedbackNote
from forge.providers.fake import Blob
from forge.providers.fake.state import commit_value
from forge.store.commands import CommandStore
from forge.store.models_budget import EngineBudgetPool
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_lane import EngineReviewRound
from forge.store.models_runs import EngineApprovedInput, EngineRun
from forge.store.rounds import RoundStore
from tests.test_engine_delivery import (
    case as case,
    check,
    completed,
    delivery as delivery,
    env as env,
    published,
    settings as settings,
)


@pytest.fixture
async def rounds(delivery):
    env = delivery["env"]
    run, frozen, pending, sha = await published(delivery)
    check(delivery, sha)
    for _ in range(3):
        assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.READY_FOR_HUMAN
    store = RoundStore(env.factory)
    delivery.update(
        root=run,
        frozen=frozen,
        sha=sha,
        draft=next(iter(env.fake.drafts.values())),
        rounds=store,
        actions=RoundActions(store, env.fake, env.fake, env.clock, lambda repo: env.fake),
        command_store=CommandStore(env.factory),
    )
    return delivery


def feedback(case, kind="fix", argument="Correct `a.py`.", *, ident="11", actor=None):
    env, draft = case["env"], case["draft"]
    actor = actor or env.intent.actor
    envelope = CommandEnvelope(
        source="webhook",
        native_id="review_comment:" + ident,
        actor=actor,
        target=draft.ref,
        command=Command(kind=kind, argument=argument),
    )
    draft.feedback.append(
        FeedbackNote(
            ref=CommentRef(kind="review_comment", subject=draft.ref, native_id=ident),
            author=actor,
            body="/" + kind.replace("_", "-") + " " + argument,
            cursor=ident,
            created_at=env.clock.now(),
        )
    )
    return envelope


async def submit(case, envelope, *, run_id=None):
    env, store = case["env"], case["command_store"]
    receipt = await store.accept(envelope, now=env.clock.now())
    if receipt.result is not None:
        return receipt.result
    lease = await store.claim("rounds", now=env.clock.now(), command_id=receipt.id)
    assert lease is not None
    run = await env.runs.load(run_id or case["root"].id)
    outcome = await case["actions"].actions()[envelope.command.kind](lease, envelope, run)
    await store.resolve(lease, outcome, now=env.clock.now())
    await store.complete(lease, now=env.clock.now())
    return outcome


async def planned_child(case, envelope=None):
    env = case["env"]
    result = await submit(case, envelope or feedback(case))
    assert result.status == "accepted", result
    env.planner.script.append(("plan", case["frozen"].spec.plan))
    for _ in range(2):
        assert (await env.runner.tick()).completed == 1
    child = await env.runs.load(result.run_id)
    assert child.state == State.AWAITING_APPROVAL, child.reason
    return child, await env.approvals.load(child.id)


async def test_fix_keeps_original_issue_and_frozen_scope_and_requires_fresh_approval(rounds):
    env = rounds["env"]
    value = feedback(rounds)
    writes, lanes = len(env.fake.calls), len(env.fake.lanes)
    first = await submit(rounds, value)
    assert await submit(rounds, value) == first
    child = await env.runs.load(first.run_id)
    assert (child.subject_kind, child.subject_id, child.root_run_id, child.parent_run_id) == (
        "issue",
        "1",
        rounds["root"].id,
        rounds["root"].id,
    )
    assert child.id != child.root_run_id and child.state == State.QUEUED
    assert len(env.fake.lanes) == lanes
    assert not any(
        method in {"commit", "ensure_branch", "dispatch_lane", "ensure_draft"}
        for method, _ in env.fake.calls[writes:]
    )
    async with env.factory() as session:
        assert (
            await session.scalar(
                select(EngineBudgetPool).where(EngineBudgetPool.run_id == child.id)
            )
            is None
        )
        initial = await session.scalar(
            select(EngineStep).where(EngineStep.key == child.id + ":plan")
        )
        document = json.loads(initial.payload_json)
        assert document["task"]["subject"] == env.intent.task.subject.model_dump(mode="json")
        assert (
            document["policy"]["write_policy"]
            == json.loads(rounds["frozen"].spec.spec_json)["write_policy"]
        )
        assert document["snapshot"]["primary"]["sha"] == rounds["sha"]
        assert document["policy"]["target_branch"] == "forge/" + child.root_run_id
        assert document["revision"] == 2 and "Correct `a.py`." in document["task"]["description"]
        assert (
            await session.scalar(
                select(EngineApprovedInput).where(EngineApprovedInput.run_id == child.id)
            )
            is None
        )
    busy = await submit(rounds, feedback(rounds, ident="12"))
    assert busy.reason == "round_already_open"


async def test_correction_has_own_budget_and_consumed_gate_and_second_round_reuses_one_draft(
    rounds,
):
    env = rounds["env"]
    child, frozen = await planned_child(rounds)
    assert frozen.approved.revision == 2
    approval = feedback(rounds, "approve_revision", child.id, ident="20")
    accepted = await submit(rounds, approval, run_id=child.id)
    assert accepted.status == "accepted"
    assert await submit(rounds, approval, run_id=child.id) == accepted
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(child.id)).state == State.RUNNING
    for _ in range(2):
        assert (await env.runner.tick()).completed == 1
    identity = RunIdentity(forge_run_id=child.id, attempt=1, generation=1)
    pending = await rounds["store"].pending(identity)
    assert pending.dispatch.execution_base_sha == rounds["sha"]
    assert pending.brief.approved_digest == frozen.approved.approved_digest
    assert "Correct `a.py`." in pending.brief.brief_text
    assert sum(name == "ensure_branch" for name, _ in env.fake.calls) == 1
    pool = await rounds["budgets"].load(child.id, "implementation")
    parent_pool = await rounds["budgets"].load(rounds["root"].id, "implementation")
    assert pool.capacity == parent_pool.capacity and pool is not parent_pool
    await completed(rounds, pending, old=b"new\n", new=b"corrected\n")
    assert (await env.runner.tick()).completed == 1
    next_sha = await env.fake.head(env.intent.task.subject.repo, "forge/" + child.root_run_id)
    check(rounds, next_sha)
    for _ in range(3):
        assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(child.id)).state == State.READY_FOR_HUMAN
    second = await submit(rounds, feedback(rounds, ident="30"), run_id=child.id)
    assert second.status == "accepted" and second.run_id != child.id
    newest = await env.runs.load(second.run_id)
    assert newest.parent_run_id == child.id and newest.root_run_id == child.root_run_id
    assert len(env.fake.drafts) == 1
    assert sum(name == "ensure_draft" for name, _ in env.fake.calls) == 1
    async with env.factory() as session:
        slots = list(await session.scalars(select(EngineReviewRound)))
        assert sorted(row.state for row in slots) == ["closed", "open"]


async def test_ask_is_durable_clarification_without_child_coder_budget_or_dispatch(rounds):
    env = rounds["env"]
    before = await env.count(EngineRun), await env.count(EngineStep), len(rounds["native"].writes)
    value = feedback(rounds, "ask", "Why this implementation?")
    result = await submit(rounds, value)
    assert result.status == "observed" and result.reason == "clarification_recorded"
    assert await submit(rounds, value) == result
    assert (
        await env.count(EngineRun),
        await env.count(EngineStep),
        len(rounds["native"].writes),
    ) == before
    assert json.loads(result.data_json)["question"] == "Why this implementation?"


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("deleted", "native_feedback_unavailable"),
        ("edited", "native_feedback_changed"),
        ("unauthorized", "actor_not_authorized"),
        ("scope", "revision_out_of_scope"),
        ("human_head", "branch_drift"),
    ],
)
async def test_invalid_native_feedback_scope_or_head_refuses_before_any_new_work(
    rounds, fault, reason
):
    env = rounds["env"]
    value = feedback(
        rounds, argument="Change `.forge.yml`." if fault == "scope" else "Correct `a.py`."
    )
    if fault == "deleted":
        rounds["draft"].feedback.clear()
    elif fault == "edited":
        rounds["draft"].feedback[-1] = (
            rounds["draft"].feedback[-1].model_copy(update={"body": "/fix something else"})
        )
    elif fault == "unauthorized":
        env.fake.approvers.clear()
    elif fault == "human_head":
        repo = env.fake.repo(value.target.repo)
        human = commit_value({"a.py": Blob(b"human\n")}, rounds["sha"], "human")
        repo.commits[human.sha] = human
        repo.branches["forge/" + rounds["root"].id] = human.sha
    before = await env.count(EngineRun), len(rounds["native"].writes)
    outcome = await submit(rounds, value)
    assert outcome.status == "refused" and outcome.reason == reason
    assert (await env.count(EngineRun), len(rounds["native"].writes)) == before


async def test_changed_original_feedback_or_gate_drift_never_authorizes_a_child(rounds):
    env = rounds["env"]
    child, frozen = await planned_child(rounds)
    rounds["draft"].feedback.clear()
    result = await submit(
        rounds, feedback(rounds, "approve_revision", child.id, ident="20"), run_id=child.id
    )
    assert result.reason == "native_feedback_unavailable"
    assert (await env.runs.load(child.id)).state == State.AWAITING_APPROVAL
    assert await env.count(EngineApprovedInput) == 1


async def test_concurrent_fix_commands_share_one_root_review_slot(rounds):
    values = [feedback(rounds, ident=str(11 + i)) for i in range(2)]
    outcomes = await asyncio.gather(*(submit(rounds, value) for value in values))
    assert sum(value.status == "accepted" for value in outcomes) == 1
    assert sum(value.reason == "round_already_open" for value in outcomes) == 1
    assert await rounds["env"].count(EngineReviewRound) == 1


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("commit_digest", "no_committed_candidate"),
        ("commit_tree", "no_committed_candidate"),
        ("draft", "review_window_closed"),
        ("candidate", "no_committed_candidate"),
        ("original", "review_window_closed"),
        ("cancelled", "cancelled"),
        ("unresolved", "review_window_closed"),
    ],
)
async def test_missing_or_corrupt_durable_ownership_never_creates_correction(rounds, fault, reason):
    from forge_protocol import digest
    from forge.store.models_execution import EngineCandidate

    env = rounds["env"]
    async with env.factory.begin() as session:
        effect = await session.scalar(select(EngineEffect).where(EngineEffect.kind == "commit"))
        if fault == "commit_digest":
            effect.payload_digest = "0" * 64
        elif fault == "commit_tree":
            payload = json.loads(effect.payload_json)
            payload["candidate"]["changeset"]["branch"] = "forge/another"
            effect.payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            effect.payload_digest = digest(payload)
        elif fault == "draft":
            row = await session.scalar(
                select(EngineEffect).where(EngineEffect.kind == "ensure_draft")
            )
            row.state = "failed"
        elif fault == "candidate":
            row = await session.scalar(select(EngineCandidate))
            row.published_sha = None
        elif fault == "original":
            row = await session.scalar(
                select(EngineStep).where(EngineStep.key == rounds["root"].id + ":plan")
            )
            row.key += ":corrupted"
        elif fault == "cancelled":
            row = await session.get(EngineRun, rounds["root"].id)
            row.cancel_requested = True
        else:
            effect.state = "unknown"
    outcome = await submit(rounds, feedback(rounds))
    assert outcome.reason == reason and outcome.status == "refused"
    assert await env.count(EngineRun) == 1


@pytest.mark.parametrize("kind", ("fix", "ask", "approve_revision"))
async def test_unavailable_native_reads_never_create_authority(rounds, monkeypatch, kind):
    from forge.port_values import ReadUnavailable

    async def unavailable(*args):
        raise ReadUnavailable("synthetic outage", "test:unavailable")

    monkeypatch.setattr(rounds["env"].fake, "list_feedback", unavailable)
    outcome = await submit(rounds, feedback(rounds, kind))
    assert outcome.reason == "review_unavailable" and outcome.status == "refused"
    assert await rounds["env"].count(EngineRun) == 1


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("issue_target", "run_scope_mismatch"),
        ("empty", "malformed_command"),
        ("bad_body", "native_feedback_changed"),
        ("closed_draft", "review_window_closed"),
    ],
)
async def test_wrong_native_target_or_command_cannot_stage_work(rounds, monkeypatch, fault, reason):
    env = rounds["env"]
    value = feedback(rounds, argument="" if fault == "empty" else "Correct `a.py`.")
    if fault == "issue_target":
        value = value.model_copy(update={"target": env.intent.task.subject})
    elif fault == "bad_body":
        rounds["draft"].feedback[-1] = (
            rounds["draft"]
            .feedback[-1]
            .model_copy(update={"body": "A quote\n/fix Correct `a.py`."})
        )
    elif fault == "closed_draft":

        async def no_draft(*args):
            return None

        monkeypatch.setattr(env.fake, "find_draft", no_draft)
    result = await submit(rounds, value)
    assert result.reason == reason and result.status == "refused"


async def test_feedback_native_kind_keeps_equal_numeric_namespaces_distinct(rounds):
    from forge.engine.rounds import native_note
    from forge.store.records import StoreConflict

    value = feedback(rounds)
    note = rounds["draft"].feedback[-1]
    other = note.model_copy(update={"ref": note.ref.model_copy(update={"kind": "issue_comment"})})
    assert native_note((note, other), value) == note
    with pytest.raises(StoreConflict, match="native_feedback_unavailable"):
        native_note((note, other), value.model_copy(update={"native_id": note.ref.native_id}))


async def test_approval_after_head_moves_does_not_consume_the_new_gate(rounds):
    env = rounds["env"]
    child, frozen = await planned_child(rounds)
    value = feedback(rounds, "approve_revision", child.id, ident="20")
    repo = env.fake.repo(value.target.repo)
    human = commit_value({"a.py": Blob(b"human\n")}, rounds["sha"], "human")
    repo.commits[human.sha] = human
    repo.branches["forge/" + rounds["root"].id] = human.sha
    result = await submit(rounds, value, run_id=child.id)
    assert result.reason == "branch_drift"
    assert await env.count(EngineApprovedInput) == 1
    assert (await env.runs.load(child.id)).state == State.AWAITING_APPROVAL
