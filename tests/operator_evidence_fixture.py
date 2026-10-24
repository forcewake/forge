"""Current SQL receipts for operator projections; no compatibility dictionaries."""

from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from forge_protocol import (
    CandidateBundle,
    CheckpointManifest,
    ControlAck,
    ControlCommand,
    ControlDelivery,
    digest,
)
from sqlalchemy import select

from forge.command_values import CommandEnvelope, CommandResult
from forge.domain.verdicts import CheckVerdict
from forge.ingress_values import Command
from forge.port_values import Refused, ReviewVerdict, SubjectRef
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineCandidate, EngineEffect, EngineStep
from forge.store.models_lane import (
    EngineCheckpoint,
    EngineControlCommand,
    EngineReviewRound,
)
from forge.store.models_runs import EngineRun
from forge.store.transactions import payload_digest
from tests.operator_surface_fixture import seed_run

SHA, BASE = "c" * 40, "b" * 40


async def edit_run(case, run, **values):
    async with case.service.factory() as session, session.begin():
        row = await session.get(EngineRun, run.id)
        for name, value in values.items():
            setattr(row, name, value)


async def candidate(
    case,
    run,
    *,
    sha=SHA,
    attempt=None,
    generation=None,
    state="published",
    verification=None,
    tested_sha=None,
    review=None,
):
    wire = CandidateBundle(
        forge_run_id=run.id,
        attempt=attempt or run.attempt,
        generation=generation or run.generation,
        diff_sha256=hashlib.sha256(b"bounded diff").hexdigest(),
        diff_size_bytes=12,
        base_sha=BASE,
        approved_digest="d" * 64,
        driver="claude_code",
        model="test-model",
        terminal_reason="raw prompt excluded",
    )
    facts = {}
    if verification:
        facts["verification"] = CheckVerdict(
            status=verification,
            tested_sha=tested_sha or sha,
            required=("tests",),
            successful=("tests",) if verification == "passed" else (),
            observed_at=case.service.budget.clock.now(),
            evidence='{"native":"sanitized"}',
        ).model_dump(mode="json")
    if review:
        facts["review"] = ReviewVerdict(
            candidate_sha=sha, decision=review, body="raw model text excluded", evidence="{}"
        ).model_dump(mode="json")
    row = EngineCandidate(
        id=uuid4().hex,
        run_id=run.id,
        attempt=wire.attempt,
        generation=wire.generation,
        digest=digest(wire),
        base_sha=wire.base_sha,
        approved_digest=wire.approved_digest,
        manifest_json=wire.model_dump_json(),
        state=state,
        published_sha=sha if state == "published" else None,
        validation_json=json.dumps(facts) if facts else None,
    )
    async with case.service.factory() as session, session.begin():
        session.add(row)
    return row


async def command_row(
    case, run, *, kind="pause", state="accepted", reason="control_queued", native_id=None
):
    actor = case.service.operators.authenticate(
        "Bearer " + case.values["SYNTHETIC_OPERATOR_PROJECT"]
    ).actor("project")
    ref = case.service.connections.registry.named("project").ref
    envelope = CommandEnvelope(
        source="api",
        native_id=native_id or uuid4().hex,
        actor=actor,
        target=SubjectRef(repo=ref, number=int(run.subject_id)),
        command=Command(kind=kind, argument="private request excluded" if kind == "steer" else ""),
    )
    result = (
        CommandResult(status="accepted", reason=reason, run_id=run.id)
        if state in ("resolved", "completed")
        else None
    )
    row = EngineCommand(
        id=uuid4().hex,
        source_key=envelope.source_key,
        kind=kind,
        document_json=envelope.canonical_bytes.decode(),
        run_id=run.id if result else None,
        state=state,
        result_json=result.canonical_bytes.decode() if result else None,
        due_at=case.service.budget.clock.now(),
    )
    async with case.service.factory() as session, session.begin():
        session.add(row)
    return row


async def control(
    case,
    run,
    *,
    action="pause",
    state="pending",
    sequence=1,
    checkpoint_id=None,
    ack_status="applied",
):
    wire = ControlCommand(
        forge_run_id=run.id,
        attempt=run.attempt,
        generation=run.generation,
        command_id=uuid4().hex,
        sequence=sequence,
        action=action,
        text="raw steering text excluded" if action == "steer" else None,
    )
    ack = (
        ControlAck(
            **wire.model_dump(
                include={"forge_run_id", "attempt", "generation", "command_id", "sequence"}
            ),
            delivery_id="d" * 32,
            status=ack_status,
            detail="raw ack detail excluded",
            checkpoint_id=checkpoint_id,
        )
        if state == "acknowledged"
        else None
    )
    row = EngineControlCommand(
        id=uuid4().hex,
        run_id=run.id,
        command_key=wire.command_id,
        attempt=run.attempt,
        generation=run.generation,
        sequence=sequence,
        world_version=run.version,
        payload_json=wire.model_dump_json(),
        state=state,
        deadline_at=case.service.budget.clock.now() + timedelta(seconds=60),
        delivery_json=ControlDelivery.model_validate(
            ack.model_dump(include=set(ControlDelivery.model_fields))
        ).model_dump_json()
        if ack
        else None,
        acknowledgement_json=ack.model_dump_json() if ack else None,
    )
    async with case.service.factory() as session, session.begin():
        session.add(row)
    return row


async def checkpoint(case, run, *, retained=True):
    wire = CheckpointManifest(
        forge_run_id=run.id,
        attempt=run.attempt,
        generation=run.generation,
        base_sha=BASE,
        approved_digest="d" * 64,
        sequence=1,
        entries=(),
        driver="claude_code",
        resume_session="opaque-private-session-excluded",
    )
    row = EngineCheckpoint(
        id=wire.checkpoint_id,
        run_id=run.id,
        attempt=run.attempt,
        generation=run.generation,
        digest=wire.checkpoint_id,
        base_sha=BASE,
        approved_digest="d" * 64,
        driver="claude_code",
        retained=retained,
        manifest_json=wire.model_dump_json(),
    )
    async with case.service.factory() as session, session.begin():
        session.add(row)
    return row


async def effect(case, run, *, kind="post_comment", state="unknown", key=None):
    async with case.service.factory() as session:
        step = await session.scalar(select(EngineStep).where(EngineStep.run_id == run.id))
    payload = '{"body":"raw prompt excluded"}'
    row = EngineEffect(
        id=uuid4().hex,
        key=key or uuid4().hex,
        kind=kind,
        run_id=run.id,
        step_id=step.id,
        lease_epoch=1,
        payload_json=payload,
        payload_digest=payload_digest(payload),
        state=state,
        outcome_json=Refused(reason="native_refused", evidence="{}").model_dump_json()
        if state == "failed"
        else None,
        observations_json="[]",
    )
    async with case.service.factory() as session, session.begin():
        session.add(row)
    return row


async def round_child(case, run, *, state="open"):
    # Terminal parent admission is separate from the new child subject lock.
    await edit_run(case, run, state="ready_for_human")
    child = await seed_run(
        case, number=int(run.subject_id), root_run_id=run.root_run_id, parent_run_id=run.id
    )
    row = EngineReviewRound(
        id=uuid4().hex,
        run_id=run.id,
        root_run_id=run.id,
        parent_run_id=run.id,
        child_run_id=child.id,
        root_review_ref="native-review:8",
        feedback_key="review_comment:918",
        base_sha=SHA,
        state=state,
    )
    async with case.service.factory() as session, session.begin():
        session.add(row)
    return child, row


async def mark_step(case, run, **changes):
    async with case.service.factory() as session, session.begin():
        row = await session.scalar(select(EngineStep).where(EngineStep.run_id == run.id))
        for name, value in changes.items():
            setattr(row, name, value)
