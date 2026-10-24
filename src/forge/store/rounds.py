"""Atomic child admission on one root Draft without copying active authority."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from uuid import UUID, uuid4, uuid5

from forge_protocol import CandidateBundle, digest
from pydantic import TypeAdapter
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.domain.approved_input import ApprovedInput
from forge.domain.run_spec import RunPolicy, RunSpec2
from forge.domain.states import Handler, State, TERMINAL
from forge.domain.verdicts import Committed
from forge.engine.plan_values import PlanIntent
from forge.port_values import Actor, Applied, DraftOutcome, RepoRef, ReviewRef
from forge.store.approval import load_prepared
from forge.store.approval_values import ApprovalClaim
from forge.store.command_runs import target_runs
from forge.store.commands import command_fence, resolve_in
from forge.store.models_execution import EngineCandidate, EngineEffect, EngineStep
from forge.store.models_lane import EngineReviewRound
from forge.store.models_commands import EngineCommand
from forge.store.models_runs import EngineApprovedInput, EngineRun, EngineRunSpec
from forge.store.records import RunRequest, StepRequest, StoreConflict
from forge.store.runs import subject_key
from forge.store.steps import enqueue_in
from forge.store.transactions import SessionFactory, locked_run, transaction


@dataclass(frozen=True)
class RoundWindow:
    root_id: str
    parent_id: str
    review: ReviewRef
    base_sha: str
    intent: PlanIntent
    spec: RunSpec2

    @property
    def branch(self) -> str:
        return f"forge/{self.root_id}"


def review_key(review: ReviewRef) -> str:
    return f"{review.repo.identity.digest}:{review.number}"


async def original(session: AsyncSession, run_id: str) -> PlanIntent:
    row = await session.scalar(select(EngineStep).where(EngineStep.key == f"{run_id}:plan"))
    if row is None:
        raise StoreConflict("review_window_closed")
    return PlanIntent.model_validate_json(row.payload_json)


async def specification(session: AsyncSession, run_id: str) -> RunSpec2:
    row = await session.scalar(select(EngineRunSpec).where(EngineRunSpec.run_id == run_id))
    if row is None:
        raise StoreConflict("review_window_closed")
    spec = RunSpec2.model_validate_json(row.document_json)
    if (spec.spec_json, spec.digest) != (row.document_json, row.digest):
        raise StoreConflict("frozen round policy is corrupt")
    return spec


async def draft_proof(session: AsyncSession, root_id: str, review: ReviewRef) -> None:
    row = await session.scalar(
        select(EngineEffect).where(EngineEffect.key == f"{root_id}:ensure_draft")
    )
    if (
        row is None
        or row.run_id != root_id
        or row.kind != "ensure_draft"
        or row.state not in ("committed", "adopted")
        or row.outcome_json is None
        or row.repo_key != review.repo.identity.digest
        or row.head_branch != f"forge/{root_id}"
        or digest(json.loads(row.payload_json)) != row.payload_digest
    ):
        raise StoreConflict("review_window_closed")
    outcome: DraftOutcome = TypeAdapter(DraftOutcome).validate_json(row.outcome_json)
    payload = json.loads(row.payload_json)
    if (
        not isinstance(outcome, Applied)
        or outcome.value.repo.identity != review.repo.identity
        or outcome.value.number != review.number
        or RepoRef.model_validate(payload.get("repo")).identity != review.repo.identity
        or payload.get("head_branch") != f"forge/{root_id}"
    ):
        raise StoreConflict("run_scope_mismatch")


async def candidate_proof(session: AsyncSession, row: EngineCandidate, root_id: str) -> None:
    key = f"{row.run_id}:{row.attempt}:{row.generation}:publish:commit"
    effect = await session.scalar(select(EngineEffect).where(EngineEffect.key == key))
    approval = await session.scalar(
        select(EngineApprovedInput).where(
            EngineApprovedInput.run_id == row.run_id,
            EngineApprovedInput.digest == row.approved_digest,
        )
    )
    if (
        effect is None
        or effect.run_id != row.run_id
        or effect.kind != "commit"
        or effect.state not in ("committed", "adopted")
        or effect.outcome_json is None
        or row.validation_json is None
        or approval is None
        or digest(json.loads(effect.payload_json)) != effect.payload_digest
    ):
        raise StoreConflict("no_committed_candidate")
    approved = ApprovedInput.model_validate_json(approval.document_json)
    if (
        approved.canonical_bytes.decode() != approval.document_json
        or approved.approved_digest != approval.digest
        or approved.spec_digest != approval.spec_digest
        or approved.base_sha != approval.base_sha
        or approved.revision != approval.revision
    ):
        raise StoreConflict("no_committed_candidate")
    candidate = CandidateBundle.model_validate_json(row.manifest_json)
    payload = json.loads(effect.payload_json)
    validated = json.loads(row.validation_json).get("validated")
    if (
        candidate.forge_run_id != row.run_id
        or candidate.approved_digest != row.approved_digest
        or (candidate.attempt, candidate.generation) != (row.attempt, row.generation)
        or digest(candidate) != row.digest
        or candidate.base_sha != row.base_sha
        or validated is None
        or payload.get("candidate") != validated
        or payload.get("expected_head") != row.base_sha
        or validated.get("changeset", {}).get("branch") != f"forge/{root_id}"
        or Committed.model_validate_json(effect.outcome_json).sha != row.published_sha
    ):
        raise StoreConflict("no_committed_candidate")


async def window_in(
    session: AsyncSession, run_id: str, review: ReviewRef, *, correction: bool = True
) -> RoundWindow:
    selected = await session.get(EngineRun, run_id)
    if selected is None:
        raise StoreConflict("run_not_found")
    root = await locked_run(session, selected.root_run_id)
    if selected.id != root.id:
        selected = await locked_run(session, selected.id)
    identity = review.repo.identity
    if (root.provider, root.connection_id, root.repository_id) != (
        identity.provider,
        identity.connection_id,
        identity.repository_id,
    ) or root.subject_kind != "issue":
        raise StoreConflict("run_scope_mismatch")
    await draft_proof(session, root.id, review)
    rows = await session.scalars(select(EngineRun).where(EngineRun.root_run_id == root.id))
    family = list(rows)
    if correction and any(State(row.state) not in TERMINAL for row in family):
        raise StoreConflict("round_already_open")
    if root.cancel_requested:
        raise StoreConflict("cancelled")
    unresolved = await session.scalar(
        select(EngineEffect.id)
        .where(
            EngineEffect.run_id.in_([row.id for row in family]),
            EngineEffect.kind.in_(("commit", "ensure_draft", "dispatch_lane")),
            EngineEffect.state.in_(("requested", "dispatched", "unknown")),
        )
        .limit(1)
    )
    if correction and unresolved is not None:
        raise StoreConflict("review_window_closed")
    candidate = await session.scalar(
        select(EngineCandidate)
        .where(
            EngineCandidate.run_id.in_([row.id for row in family]),
            EngineCandidate.state == "published",
            EngineCandidate.published_sha.is_not(None),
        )
        .order_by(EngineCandidate.created_at.desc(), EngineCandidate.id.desc())
        .limit(1)
    )
    if candidate is None or candidate.published_sha is None:
        raise StoreConflict("no_committed_candidate")
    await candidate_proof(session, candidate, root.id)
    spec = await specification(session, candidate.run_id)
    if ApprovedInput.from_spec(spec).approved_digest != candidate.approved_digest:
        raise StoreConflict("no_committed_candidate")
    intent = await original(session, root.id)
    root_spec = await specification(session, root.id)
    if intent.freeze(root_spec.plan) != root_spec or (
        intent.task.subject.kind,
        str(intent.task.subject.number),
        intent.task.subject.repo.identity,
    ) != (root.subject_kind, root.subject_id, identity):
        raise StoreConflict("run_scope_mismatch")
    return RoundWindow(
        root.id,
        candidate.run_id,
        review,
        candidate.published_sha,
        intent,
        spec,
    )


def revision_intent(window: RoundWindow, actor: Actor, envelope: CommandEnvelope) -> PlanIntent:
    description = (
        window.spec.task_description
        + "\n\nReview correction on Draft #"
        + str(window.review.number)
        + " from native feedback "
        + envelope.native_id
        + ":\n"
        + envelope.command.argument
    )
    if not envelope.command.argument.strip() or len(description) > 131072:
        raise StoreConflict("malformed_command")
    policy = RunPolicy.model_validate_json(
        json.dumps(
            {
                **window.spec.model_dump(mode="json", include=set(RunPolicy.model_fields)),
                "target_branch": window.branch,
            }
        )
    )
    task = window.intent.task.model_copy(
        update={
            "description": description,
            "version": "review:" + envelope.source_key,
        }
    )
    primary = window.intent.snapshot.primary.model_copy(update={"sha": window.base_sha})
    return PlanIntent.model_validate(
        {
            **window.intent.model_dump(),
            "actor": actor,
            "task": task,
            "policy": policy,
            "snapshot": window.intent.snapshot.model_copy(update={"primary": primary}),
            "revision": window.spec.revision + 1,
        }
    )


async def scoped_command(
    session: AsyncSession, lease: CommandLease, run_id: str, now: datetime
) -> tuple[EngineCommand, CommandEnvelope]:
    command = await command_fence(session, lease, now)
    envelope = CommandEnvelope.model_validate_json(command.document_json)
    if run_id not in {row.id for row in await target_runs(session, envelope)}:
        raise StoreConflict("run_scope_mismatch")
    return command, envelope


async def release_terminal_slot(session: AsyncSession, key: str) -> None:
    row = await session.scalar(
        select(EngineReviewRound)
        .where(EngineReviewRound.root_review_ref == key, EngineReviewRound.state == "open")
        .with_for_update()
    )
    if row is not None:
        child = await locked_run(session, row.child_run_id)
        if State(child.state) not in TERMINAL:
            raise StoreConflict("round_already_open")
        row.state = "closed"
        await session.flush()


class RoundStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def approval_window(
        self, run_id: str, step_id: str, claim: ApprovalClaim
    ) -> tuple[RoundWindow, CommandEnvelope, CommandEnvelope]:
        from forge.store.round_approval import approval_window

        async with transaction(self.factory) as session:
            return await approval_window(session, run_id, step_id, claim)

    async def feedback(self, run_id: str, review: ReviewRef) -> CommandEnvelope:
        async with self.factory() as session:
            slot = await session.scalar(
                select(EngineReviewRound).where(
                    EngineReviewRound.child_run_id == run_id,
                    EngineReviewRound.state == "open",
                    EngineReviewRound.root_review_ref == review_key(review),
                )
            )
            command = (
                await session.scalar(
                    select(EngineCommand).where(EngineCommand.source_key == slot.feedback_key)
                )
                if slot is not None
                else None
            )
            if command is None or command.result_json is None or command.run_id != run_id:
                raise StoreConflict("review_window_closed")
            envelope = CommandEnvelope.model_validate_json(command.document_json)
            if envelope.target != review or envelope.command.kind != "fix":
                raise StoreConflict("run_scope_mismatch")
            return envelope

    async def window(
        self, run_id: str, review: ReviewRef, *, correction: bool = True
    ) -> RoundWindow:
        async with transaction(self.factory) as session:
            return await window_in(session, run_id, review, correction=correction)

    async def stage(
        self,
        lease: CommandLease,
        run_id: str,
        window: RoundWindow,
        intent: PlanIntent,
        *,
        now: datetime,
    ) -> CommandResult:
        async with transaction(self.factory) as session:
            command, envelope = await scoped_command(session, lease, run_id, now)
            if command.result_json is not None:
                return CommandResult.model_validate_json(command.result_json)
            if envelope.command.kind != "fix" or envelope.target != window.review:
                raise StoreConflict("run_scope_mismatch")
            if intent != revision_intent(window, envelope.actor, envelope):
                raise StoreConflict("round input differs from its frozen authority")
            if await window_in(session, run_id, window.review) != window:
                raise StoreConflict("branch_drift")
            await release_terminal_slot(session, review_key(window.review))
            await self.capacity(session, intent)
            child_id = uuid5(UUID(window.root_id), "feedback:" + envelope.source_key).hex
            request = RunRequest(
                child_id,
                window.review.repo.identity.provider,
                window.review.repo.identity.connection_id,
                window.review.repo.identity.repository_id,
                "issue",
                str(intent.task.subject.number),
                window.root_id,
                window.parent_id,
            )
            child = EngineRun(
                id=child_id,
                provider=request.provider,
                connection_id=request.connection_id,
                repository_id=request.repository_id,
                subject_kind=request.subject_kind,
                subject_id=request.subject_id,
                subject_key=subject_key(request),
                root_run_id=window.root_id,
                parent_run_id=window.parent_id,
            )
            session.add(child)
            await session.flush()
            session.add(
                EngineReviewRound(
                    id=uuid4().hex,
                    run_id=child_id,
                    root_run_id=window.root_id,
                    parent_run_id=window.parent_id,
                    child_run_id=child_id,
                    root_review_ref=review_key(window.review),
                    feedback_key=envelope.source_key,
                    base_sha=window.base_sha,
                )
            )
            await enqueue_in(
                session,
                child,
                StepRequest(
                    child_id, Handler.PLAN, f"{child_id}:plan", intent.canonical_bytes.decode(), now
                ),
            )
            result = CommandResult(status="accepted", reason="revision_staged", run_id=child_id)
            resolve_in(command, result)
            return result

    @staticmethod
    async def capacity(session: AsyncSession, intent: PlanIntent) -> None:
        repo = intent.task.subject.repo.identity
        if session.get_bind().dialect.name == "postgresql":
            lock = int.from_bytes(
                hashlib.sha256((repo.provider + "\x00" + repo.connection_id).encode()).digest()[:8],
                signed=True,
            )
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        count = await session.scalar(
            select(func.count())
            .select_from(EngineRun)
            .where(
                EngineRun.provider == repo.provider,
                EngineRun.connection_id == repo.connection_id,
                EngineRun.state.not_in(tuple(state.value for state in TERMINAL)),
            )
        )
        if count is None or count >= intent.max_active:
            raise StoreConflict("another_run_active")

    async def clarify(
        self, lease: CommandLease, run_id: str, window: RoundWindow, *, now: datetime
    ) -> CommandResult:
        async with transaction(self.factory) as session:
            command, envelope = await scoped_command(session, lease, run_id, now)
            if envelope.command.kind != "ask" or envelope.target != window.review:
                raise StoreConflict("run_scope_mismatch")
            if await window_in(session, run_id, window.review, correction=False) != window:
                raise StoreConflict("branch_drift")
            result = CommandResult(
                status="observed",
                reason="clarification_recorded",
                run_id=run_id,
                data_json=json.dumps(
                    {
                        "question": envelope.command.argument,
                        "review": window.review.number,
                        "candidate_sha": window.base_sha,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            )
            resolve_in(command, result)
            return result

    async def approve(
        self, lease: CommandLease, run_id: str, review: ReviewRef, *, now: datetime
    ) -> CommandResult:
        async with transaction(self.factory) as session:
            command, envelope = await scoped_command(session, lease, run_id, now)
            if command.result_json is not None:
                return CommandResult.model_validate_json(command.result_json)
            if envelope.command.kind != "approve_revision" or envelope.target != review:
                raise StoreConflict("run_scope_mismatch")
            run = await locked_run(session, run_id)
            slot = await session.scalar(
                select(EngineReviewRound).where(
                    EngineReviewRound.child_run_id == run_id,
                    EngineReviewRound.state == "open",
                    EngineReviewRound.root_review_ref == review_key(review),
                )
            )
            prepared = await load_prepared(session, run_id)
            if slot is None or prepared is None or run.state != State.AWAITING_APPROVAL.value:
                raise StoreConflict("review_window_closed")
            if (
                prepared.spec.base_sha != slot.base_sha
                or prepared.spec.target_branch != f"forge/{run.root_run_id}"
            ):
                raise StoreConflict("branch_drift")
            approved = prepared.approved
            claim = ApprovalClaim(
                gate_id=prepared.gate_id,
                actor=envelope.actor,
                approved_digest=approved.approved_digest,
                plan_digest=approved.plan_digest,
                spec_digest=approved.spec_digest,
                base_sha=approved.base_sha,
                revision=approved.revision,
            )
            await enqueue_in(
                session,
                run,
                StepRequest(
                    run.id,
                    Handler.APPROVE,
                    f"{run.id}:approve:{command.id}",
                    claim.canonical_bytes.decode(),
                    now,
                ),
            )
            result = CommandResult(
                status="accepted", reason="revision_approval_scheduled", run_id=run.id
            )
            resolve_in(command, result)
            return result
