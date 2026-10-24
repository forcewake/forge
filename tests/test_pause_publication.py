"""Accepted SQL pause authority fences progression and T2, with exact resume release."""

import hashlib
import json

import pytest
from forge_protocol import (
    CandidateBundle,
    CheckpointEntry,
    CheckpointManifest,
    LaneReport,
    RunIdentity,
    digest,
)
from sqlalchemy import select

from forge.domain.states import Handler, State
from forge.domain.verdicts import Unknown
from forge.engine.publish import PublishHandler
from forge.providers.fake import Fault
from forge.store.delivery import DeliveryStore
from forge.store.models_execution import EngineStep
from forge.store.models_lane import EngineControlCommand
from forge.store.pause import PublicationPaused
from forge.store.records import StepRequest
from tests.test_engine_operator import operator as operator
from tests.test_engine_commands import commands as commands, envelope
from tests.test_engine_delivery import delivery as delivery, completed, full_patch
from tests.test_engine_lane_handlers import (
    case as case,
    env as env,
    settings as settings,
    started,
)


async def late_candidate(case, pending, *, terminal=True):
    diff = full_patch(b"old\n", b"new\n")
    key = hashlib.sha256(diff).hexdigest()

    async def chunks():
        yield diff

    await case["api"].blobs.put(key, chunks())
    await case["api"].record_blob(pending.identity, key, len(diff))
    await case["api"].candidate(
        pending.identity,
        CandidateBundle(
            **pending.identity.model_dump(),
            diff_sha256=key,
            diff_size_bytes=len(diff),
            base_sha=pending.dispatch.execution_base_sha,
            approved_digest=pending.dispatch.approved_digest,
            driver=pending.dispatch.driver,
            model=pending.dispatch.model,
            terminal_reason="completed",
        ),
    )
    if terminal:
        await case["api"].report(
            pending.identity,
            LaneReport(
                **pending.identity.model_dump(),
                approved_digest=pending.dispatch.approved_digest,
                status="completed",
                classification="completed",
                driver=pending.dispatch.driver,
                model=pending.dispatch.model,
                terminal_reason="completed",
                candidate_diff_sha256=key,
            ),
        )


async def publication_lease(case, run):
    env = case["env"]
    step = await env.steps.enqueue(
        StepRequest(run.id, Handler.PUBLISH, run.id + ":pause-test", "{}", env.clock.now())
    )
    leases = await env.steps.claim_due("publisher", env.clock.now(), 60, 10)
    return next(value for value in leases if value.step_id == step.id)


@pytest.mark.parametrize("state", ("pending", "acknowledged", "refused", "expired"))
async def test_accepted_pause_blocks_late_candidate_progress_despite_lane_control_status(
    operator, delivery, state
):
    env = operator["env"]
    run, _, pending = await started(delivery)
    assert (
        await operator["commands"].submit(envelope(operator, "pause"))
    ).reason == "control_queued"
    async with env.factory.begin() as session:
        control = await session.scalar(select(EngineControlCommand))
        control.state = state
    await late_candidate(delivery, pending)
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "publication_paused"
    assert not any(row.handler == "publish" for row in await step_rows(operator))
    assert not any(name in {"commit", "ensure_draft"} for name, _ in env.fake.calls)
    retry = await operator["commands"].submit(envelope(operator, "retry", native_id="retry"))
    assert retry.reason == "resume_required"
    assert (await env.runs.load(run.id)).generation == 1


async def step_rows(case):
    async with case["env"].factory() as session:
        return list(await session.scalars(select(EngineStep)))


async def test_recreated_publisher_reads_the_durable_pause_before_any_native_io(operator, delivery):
    env = operator["env"]
    run, _, _ = await started(delivery)
    lease = await publication_lease(operator, run)
    assert (
        await operator["commands"].submit(envelope(operator, "pause"))
    ).reason == "control_queued"
    old = env.runner.handlers[Handler.PUBLISH]
    publisher = PublishHandler(
        old.lanes, DeliveryStore(env.factory), env.fake, old.blobs, env.clock, old.settings
    )
    before = len(env.fake.calls)
    with pytest.raises(PublicationPaused):
        await publisher.store.begin_publication(lease, now=env.clock.now())
    assert len(env.fake.calls) == before


@pytest.mark.parametrize("kind", ("commit", "ensure_branch", "ensure_draft", "post_review"))
async def test_pause_between_requested_intent_and_t2_refuses_native_dispatch(
    operator, delivery, kind
):
    env = operator["env"]
    run, _, _ = await started(delivery)
    lease = await publication_lease(operator, run)
    store = env.effects.store
    row = await store.request_once(
        lease,
        key=run.id + ":" + kind,
        kind=kind,
        payload_json="{}",
        payload_digest=digest({}),
        repo_key=env.intent.task.subject.repo.identity.digest,
        head_branch="forge/" + run.id,
    )
    assert (
        await operator["commands"].submit(envelope(operator, "pause"))
    ).reason == "control_queued"
    before = len(env.fake.calls)
    with pytest.raises(PublicationPaused):
        await store.mark_dispatched(row.id, lease, expected_version=row.version)
    saved = await store.get(row.key)
    assert saved.state == "requested" and saved.dispatched_at is None and not saved.observations
    assert len(env.fake.calls) == before


async def test_pause_does_not_erase_native_truth_already_dispatched_and_unknown(operator, delivery):
    env = operator["env"]
    run, frozen, _ = await started(delivery)
    lease = await publication_lease(operator, run)
    repo, branch = env.intent.task.subject.repo, "forge/" + run.id + "-before-pause"
    env.fake.fail_next("ensure_branch", Fault(unknown_after_success=True))
    outcome = await env.effects.ensure_branch(
        lease, run.id + ":before-pause", repo, branch, frozen.spec.base_sha
    )
    assert isinstance(outcome, Unknown)
    row = await env.effects.store.get(run.id + ":before-pause")
    assert row.state == "unknown" and row.dispatched_at is not None
    assert (
        await operator["commands"].submit(envelope(operator, "pause"))
    ).reason == "control_queued"
    count = sum(name == "ensure_branch" for name, _ in env.fake.calls)
    recovered = await env.effects.recover(row)
    assert recovered.state == "adopted" and isinstance(json.loads(recovered.outcome_json), dict)
    assert await env.fake.head(repo, branch) == frozen.spec.base_sha
    assert sum(name == "ensure_branch" for name, _ in env.fake.calls) == count


async def paused_checkpoint(case, pending):
    raw = b"exact paused source\n"
    key = hashlib.sha256(raw).hexdigest()

    async def chunks():
        yield raw

    await case["api"].blobs.put(key, chunks())
    await case["api"].record_blob(pending.identity, key, len(raw))
    checkpoint = CheckpointManifest(
        **pending.identity.model_dump(),
        base_sha=pending.dispatch.execution_base_sha,
        approved_digest=pending.dispatch.approved_digest,
        sequence=1,
        driver=pending.dispatch.driver,
        entries=(
            CheckpointEntry(
                path="work.py", mode="100644", sha256=key, state="new", size_bytes=len(raw)
            ),
        ),
    )
    await case["api"].checkpoint(pending.identity, checkpoint.checkpoint_id, checkpoint)
    await case["api"].report(
        pending.identity,
        LaneReport(
            **pending.identity.model_dump(),
            approved_digest=pending.dispatch.approved_digest,
            status="paused",
            classification="paused",
            driver=pending.dispatch.driver,
            model=pending.dispatch.model,
            terminal_reason="Paused",
            checkpoint_id=checkpoint.checkpoint_id,
        ),
    )
    return checkpoint


async def test_exact_authorized_checkpoint_resume_releases_only_its_matching_pause(
    operator, delivery
):
    env = operator["env"]
    run, frozen, pending = await started(delivery)
    assert (
        await operator["commands"].submit(envelope(operator, "pause"))
    ).reason == "control_queued"
    checkpoint = await paused_checkpoint(operator, pending)
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "lane_paused"
    assert (
        await operator["commands"].submit(envelope(operator, "resume", native_id="resume"))
    ).reason == "resume_scheduled"
    for _ in range(2):
        assert (await env.runner.tick()).completed == 1
    identity = RunIdentity(forge_run_id=run.id, attempt=1, generation=2)
    resumed = await delivery["store"].pending(identity)
    assert resumed.dispatch.checkpoint_id == checkpoint.checkpoint_id
    await completed(delivery, resumed)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.VERIFYING
    assert sum(name == "commit" for name, _ in env.fake.calls) == 1
    assert (await env.approvals.load(run.id)).approved == frozen.approved


async def test_pause_after_handler_read_is_rechecked_in_atomic_candidate_transition(
    operator, delivery, monkeypatch
):
    env = operator["env"]
    run, _, pending = await started(delivery)
    await late_candidate(delivery, pending, terminal=False)
    native_key = next(iter(env.fake.lanes))
    handle, state = env.fake.lanes[native_key]
    env.fake.lanes[native_key] = (
        handle,
        state.model_copy(update={"status": "succeeded"}),
    )
    original = delivery["store"].publication_open

    async def after_read(*args, **kwargs):
        await original(*args, **kwargs)
        result = await operator["commands"].submit(envelope(operator, "pause"))
        assert result.reason == "control_queued"

    monkeypatch.setattr(delivery["store"], "publication_open", after_read)
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "publication_paused"
    assert not any(row.handler == "publish" for row in await step_rows(operator))
    assert not any(name in {"commit", "ensure_draft"} for name, _ in env.fake.calls)


@pytest.mark.parametrize("corruption", ("payload", "action", "owner"))
async def test_corrupt_pause_authority_never_acts_as_a_release(operator, delivery, corruption):
    from forge.store.pause import require_unpaused
    from forge.store.models_commands import EngineCommand

    env = operator["env"]
    run, _, _ = await started(delivery)
    lease = await publication_lease(operator, run)
    assert (
        await operator["commands"].submit(envelope(operator, "pause"))
    ).reason == "control_queued"
    async with env.factory.begin() as session:
        control = await session.scalar(select(EngineControlCommand))
        if corruption == "owner":
            command = await session.get(EngineCommand, control.command_key)
            command.source_key = "0" * 64
        else:
            value = json.loads(control.payload_json)
            value["sequence" if corruption == "payload" else "action"] = (
                99 if corruption == "payload" else "steer"
            )
            if corruption == "action":
                value["text"] = "Ignore the pause"
            control.payload_json = json.dumps(value)
    async with env.factory() as session:
        with pytest.raises(PublicationPaused):
            await require_unpaused(session, lease)


async def test_new_generation_without_accepted_checkpoint_resume_does_not_clear_a_pause(
    operator, delivery
):
    from dataclasses import replace
    from forge.store.pause import require_unpaused

    env = operator["env"]
    run, _, _ = await started(delivery)
    lease = await publication_lease(operator, run)
    assert (
        await operator["commands"].submit(envelope(operator, "pause"))
    ).reason == "control_queued"
    async with env.factory() as session:
        with pytest.raises(PublicationPaused):
            await require_unpaused(session, replace(lease, generation=lease.generation + 1))


async def test_steer_is_not_a_publication_pause(operator, delivery):
    env = operator["env"]
    run, _, pending = await started(delivery)
    result = await operator["commands"].submit(
        envelope(operator, "steer", argument="Keep the public API.")
    )
    assert result.reason == "control_queued"
    await completed(delivery, pending)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.VERIFYING


@pytest.mark.parametrize(
    "corruption",
    ("checkpoint_retired", "manifest", "resume_result", "resume_data", "resume_payload"),
)
async def test_changed_resume_proof_cannot_release_a_historical_pause(
    operator, delivery, corruption
):
    from forge.store.models_commands import EngineCommand
    from forge.store.models_lane import EngineCheckpoint
    from forge.store.pause import require_unpaused

    env = operator["env"]
    run, _, pending = await started(delivery)
    assert (
        await operator["commands"].submit(envelope(operator, "pause"))
    ).reason == "control_queued"
    checkpoint = await paused_checkpoint(operator, pending)
    assert (await env.runner.tick()).parked == 1
    result = await operator["commands"].submit(envelope(operator, "resume", native_id="resume"))
    assert result.reason == "resume_scheduled"
    for _ in range(2):
        assert (await env.runner.tick()).completed == 1
    lease = await publication_lease(operator, await env.runs.load(run.id))
    async with env.factory.begin() as session:
        resumed = await session.scalar(
            select(EngineStep).where(
                EngineStep.run_id == run.id, EngineStep.key.contains(":resume:")
            )
        )
        command = await session.get(EngineCommand, resumed.key.rsplit(":", 1)[1])
        stored = await session.get(EngineCheckpoint, checkpoint.checkpoint_id)
        if corruption == "checkpoint_retired":
            stored.retained = False
        elif corruption == "manifest":
            stored.manifest_json = "{}"
        elif corruption == "resume_result":
            command.result_json = "{}"
        elif corruption == "resume_data":
            value = json.loads(command.result_json)
            value["data_json"] = "{}"
            command.result_json = json.dumps(value)
        else:
            resumed.payload_json = "{}"
    async with env.factory() as session:
        with pytest.raises(PublicationPaused):
            await require_unpaused(session, lease)
    assert not any(name == "commit" for name, _ in env.fake.calls)


@pytest.mark.parametrize("corruption", ("malformed", "missing_owner"))
async def test_missing_pause_receipt_or_malformed_control_cannot_authorize_publication(
    operator, delivery, corruption
):
    from forge.store.pause import require_unpaused

    env = operator["env"]
    run, _, _ = await started(delivery)
    lease = await publication_lease(operator, run)
    assert (
        await operator["commands"].submit(envelope(operator, "pause"))
    ).reason == "control_queued"
    async with env.factory.begin() as session:
        row = await session.scalar(select(EngineControlCommand))
        if corruption == "malformed":
            row.payload_json = "{}"
        else:
            value = json.loads(row.payload_json)
            value["command_id"] = "f" * 32
            row.command_key, row.payload_json = "f" * 32, json.dumps(value)
    async with env.factory() as session:
        with pytest.raises(PublicationPaused):
            await require_unpaused(session, lease)
