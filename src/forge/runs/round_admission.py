"""The review-round CHILD ADMISSION — the one named application service
(R41-17 / #372, extracting the R40-02 #338 admission RunService carried).

The sequence lived inline in ``runs.service._admit_review_round`` and only
there: the child FlowRun created and FLUSHED FIRST, then walked to
``proposing`` over the legal graph, seeded with the round's active-plan
representation (R41-01/#356: the child exists before the budget that keys
on it opens), the copied frozen spec, the confirmed MR reservation on the
SAME branch, the child's OWN budget opened from that spec, the round row
and the admission outbox row — ONE transaction, or nothing at all. That
composition is a decision (the admission's write ORDER is its semantics),
so it has ONE owner here; the service keeps the eligibility ladder and
the reply/refusal rungs (every refusal is answered where the operator
surface lives) and CONSUMES this service for the write.

Two seams are EXPLICIT immutable context values (``RoundAdmissionSeams``),
never service-global mutable defaults: the service resolves its own module
globals when it builds the context, so the production mutation seams
(#356's ``forge.durable.open_budget_from_spec``, ``runs.service.Controller``
and ``runs.service.MRReservation`` patches in ``tests/test_review_rounds.py``)
keep binding exactly where they always did.

The module also owns the admission's OWN uniqueness arbiters
(``round_slot_integrity_conflict``): the lineage's ONE-outstanding-round
partial index, the ``(parent, note)`` request idempotency key and the
one-ACTIVE-run-per-issue index — the constraints whose violation means "a
racing correction won", never "a defect". Any OTHER integrity failure
inside this transaction is a genuine defect and re-raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from forge.adaptive.models import PlanRevision
from forge.adaptive.revisions import (
    ACTIVE_PLAN_KEY,
    REQUEST_DISPATCHED,
    REVISION_CONTENT_KEY,
    ReviewFeedbackRefused,
    ReviewFeedbackRequest,
    classic_spec_revision,
    correction_decision_id,
    correction_invalidation_set,
    plan_digest as revision_plan_digest,
    review_correction_revision,
    round_active_plan_seed,
)
from forge.durable import (
    CollaborationTarget,
    Controller,
    FlowRun,
    FlowStatus,
    MRReservation,
    Outbox,
    RunNotFound,
    RunSpec,
)
from forge.durable.budgets import budget_limits_from_spec
from forge.durable.collaboration import round_reference, target_handle_mismatches
from forge.durable.models import ReviewRound
from forge.runs.spec import EXECUTABLE_SPEC_SCHEMA_VERSION, ExecutableRunSpec

__all__ = [
    "ROUND_SLOT_CONSTRAINTS",
    "ROUND_SLOT_SQLITE_COLUMNS",
    "RoundAdmissionSeams",
    "RoundChildPlan",
    "admit_round_child",
    "plan_round_child",
    "round_slot_integrity_conflict",
]


#: The round admission's own uniqueness arbiters (R41-01): the lineage's
#: ONE-outstanding-round partial index, the ``(parent, note)`` request
#: idempotency key, and the one-ACTIVE-run-per-issue index — in a true
#: same-head race the loser's CHILD INSERT loses the per-issue slot first
#: (its round row never flushes), so that index is this admission's
#: arbiter just as much as the round slot. Any OTHER integrity failure
#: inside the admission transaction is a genuine defect — never a
#: competing correction.
ROUND_SLOT_CONSTRAINTS: tuple[str, ...] = (
    "uq_review_round_open_per_root",
    "uq_review_round_request",
    "uq_active_run_per_issue",
)
ROUND_SLOT_SQLITE_COLUMNS: tuple[str, ...] = (
    "review_rounds.root_run_id",
    "review_rounds.parent_run_id, review_rounds.note_id",
    "flow_runs.provider, flow_runs.project_id, flow_runs.issue_iid",
)


def round_slot_integrity_conflict(exc: IntegrityError) -> bool:
    """Is *exc* one of the round admission's own uniqueness arbiters?

    PostgreSQL names the constraint in the DBAPI error; SQLite's unique
    violations surface as ``UNIQUE constraint failed`` column lists (a
    partial index reports no name). Both dialects are matched on their
    own terms; a foreign-key, NOT NULL or CHECK failure — or another
    table's uniqueness — reads ``False`` and is re-raised by the caller
    as the defect it is, so an unrelated integrity error is never
    answered with a false conflicting-correction refusal.
    """
    detail = " ".join(
        str(part)
        for part in (exc.orig, exc, getattr(exc, "statement", None) or "")
        if part is not None
    )
    if any(name in detail for name in ROUND_SLOT_CONSTRAINTS):
        return True
    if "UNIQUE constraint failed" not in detail:
        return False
    return any(columns in detail for columns in ROUND_SLOT_SQLITE_COLUMNS)


@dataclass(frozen=True)
class RoundAdmissionSeams:
    """The admission's collaborators as explicit immutable context values.

    The service resolves ``controller`` / ``reservation_model`` from ITS
    OWN module globals at call time when constructing this context — the
    #356 mutation seams on ``forge.runs.service`` (and the
    ``forge.durable.open_budget_from_spec`` patch, imported call-time in
    :func:`admit_round_child`) therefore keep driving the REAL admission
    path, and the extraction introduces no service-global mutable default.
    """

    #: The session factory the admission transaction opens its ONE
    #: session through (the service's own factory, passed per call).
    session_factory: Any
    #: The run-graph controller class (``forge.durable.Controller``).
    controller: type[Controller]
    #: The MR reservation model (``forge.durable.MRReservation``).
    reservation_model: type[MRReservation]


@dataclass(frozen=True)
class RoundChildPlan:
    """The derived admission inputs — PURE (no I/O, no provider reads).

    Everything the transaction writes derives from these values plus the
    re-read parent/target rows inside the transaction itself; the plan is
    computed BEFORE the transaction opens and is immutable thereafter.
    """

    #: The child's INDEPENDENT run id (R41-04/#359: work identity and
    #: collaboration identity are decoupled — never a derivation).
    child_id: str
    #: ``len(rounds) + 2`` — delivery 1 is round 1 (never a row).
    round_number: int
    #: ``correction_decision_id(run_id, note_id)`` — the linkage key the
    #: parent's request carries before the dispatch legs run.
    decision_id: str
    #: The frozen spec document copied VERBATIM (never a re-serialization:
    #: the child's spec digest check recomputes over these exact bytes).
    spec_document: dict[str, Any]
    spec_digest: str
    spec_schema_version: int
    #: ``revision_plan_digest(round_revision)`` — the plan digest of the
    #: round's revision (the corrected plan the child executes).
    plan_digest: str
    #: The child's seeded evidence (review_round block, the round's own
    #: active-plan seed, the inherited request record).
    evidence: dict[str, Any]
    #: The correction invalidation set over the parent's last candidate.
    invalidation: dict[str, Any]


def plan_round_child(
    *,
    parent: FlowRun,
    spec: ExecutableRunSpec,
    spec_row: RunSpec | None,
    root: str,
    rounds_count: int,
    mr_iid: int | None,
    target_id: str,
    request: ReviewFeedbackRequest,
) -> RoundChildPlan:
    """Derive the child's admission plan from exactly two verified sources.

    The parent's frozen RunSpec (digest-verified read — *spec*) and the
    accepted request. A parent that staged an adaptive revision chains
    from its durable content instead — never a re-derivation. Raises the
    typed ``content_unreadable`` refusal when the parent's active
    revision does not parse; the caller answers it on the operator
    surface.
    """
    # Copy the frozen bytes VERBATIM (never a re-serialization): the
    # child's spec digest check recomputes over these exact bytes.
    spec_document = dict(spec_row.document) if spec_row is not None else spec.to_document()
    spec_digest = str(parent.spec_digest or (spec_row.digest if spec_row else "") or "")
    parent_evidence = parent.evidence if isinstance(parent.evidence, dict) else {}
    active_raw = parent_evidence.get(ACTIVE_PLAN_KEY)
    content_raw = active_raw.get(REVISION_CONTENT_KEY) if isinstance(active_raw, dict) else None
    decision_id = correction_decision_id(parent.id, request.note_id)
    # R41-04 (#359): the child's run id is INDEPENDENT of the parent's —
    # work identity and collaboration identity are no longer coupled. The
    # branch/MR surface every leg lands on is the target's (linked on the
    # child by the admission transaction); parent and child 8-hex short
    # forms are therefore distinct too, so short command ids stay
    # unambiguous within the lineage.
    child_id = uuid4().hex
    if isinstance(content_raw, dict) and content_raw:
        try:
            base_revision = PlanRevision.model_validate(dict(content_raw))
        except Exception as exc:  # pydantic ValidationError — unreadable
            raise ReviewFeedbackRefused(
                "content_unreadable",
                f"the parent's active revision does not parse: {type(exc).__name__}",
            ) from exc
        round_revision = review_correction_revision(base_revision, request)
        revised_from = str(active_raw.get("plan_digest") or "") if active_raw else ""
    else:
        round_revision = classic_spec_revision(
            work_id=child_id, spec=spec_document, request=request
        )
        revised_from = str(spec.plan_digest or "")
    round_number = rounds_count + 2  # delivery 1 is round 1 (never a row)
    last_candidate = str(list(parent.candidate_shas or [])[-1]) if parent.candidate_shas else ""
    invalidation = (
        correction_invalidation_set(
            last_candidate,
            {
                "review": {"applicability": last_candidate},
                "verification": {"applicability": last_candidate},
                "pipeline": {"applicability": last_candidate},
            },
        )
        if last_candidate
        else {}
    )
    child_evidence = {
        "requested_by": request.actor,
        "backend": str(parent_evidence.get("backend") or "").strip(),
        "review_round": {
            "parent_run_id": parent.id,
            "root_run_id": root,
            "round_number": round_number,
            "note_id": request.note_id,
            "decision_id": decision_id,
            "base_head_sha": request.head_sha,
            "mr_iid": mr_iid,
            # R41-04 (#359): the persisted surface this round rides.
            "collaboration_target_id": target_id,
            "reference": round_reference(round_number, root),
        },
        "round_invalidation": invalidation,
        ACTIVE_PLAN_KEY: round_active_plan_seed(
            round_revision,
            revised_from_digest=revised_from,
            decision_id=decision_id,
        ),
        # The round inherits its OWN copy of the request: the child's
        # readiness gate then holds the candidate until the reviewer
        # resolves the originating discussion (the human decision —
        # forge never resolves it).
        "review_feedback_requests": {
            request.note_id: request.with_status(
                REQUEST_DISPATCHED, decision_id=decision_id
            ).document()
        },
    }
    return RoundChildPlan(
        child_id=child_id,
        round_number=round_number,
        decision_id=decision_id,
        spec_document=spec_document,
        spec_digest=spec_digest,
        spec_schema_version=(
            int(spec_row.schema_version) if spec_row is not None else EXECUTABLE_SPEC_SCHEMA_VERSION
        ),
        plan_digest=revision_plan_digest(round_revision),
        evidence=child_evidence,
        invalidation=invalidation,
    )


async def _require_run(session: AsyncSession, run_id: str) -> FlowRun:
    """Fetch the parent run row, or fail loudly (the service's
    ``_get_run`` semantics — an absent parent here is an invariant
    violation, never a tolerated outcome)."""
    run = await session.get(FlowRun, run_id)
    if run is None:
        raise RunNotFound(f"flow run {run_id!r} not found")
    return run


async def admit_round_child(
    seams: RoundAdmissionSeams,
    plan: RoundChildPlan,
    *,
    parent_run_id: str,
    project_id: int,
    mr_iid: int | None,
    root: str,
    target: CollaborationTarget,
    request: ReviewFeedbackRequest,
) -> str:
    """Run the child-admission TRANSACTION; return the child run id.

    ONE session, ONE commit — a failure before it leaves NOTHING partial.
    The typed refusals (``parent_moved`` / ``collaboration_target_moved``)
    are re-raises the service logs; an ``IntegrityError`` propagates for
    the caller to arbitrate through :func:`round_slot_integrity_conflict`.
    """
    async with seams.session_factory() as session:
        controller = seams.controller(session)
        parent = await _require_run(session, parent_run_id)
        if parent.status != FlowStatus.READY_FOR_HUMAN.value:
            # The run moved between the read and the admission — the
            # pre-ready staged route (or the window answer) owns the
            # request now; nothing is admitted.
            raise ReviewFeedbackRefused(
                "parent_moved",
                f"run {parent_run_id!r} is {parent.status!r}, not ready_for_human",
            )
        # R41-04 (#359): re-read the target INSIDE the admission
        # transaction — the row the child links must be the committed
        # one, active, with its handle agreement still holding (a
        # concurrent refusal/move refuses the round, never rides the
        # child onto a moved surface).
        txn_target = await session.get(CollaborationTarget, target.id)
        if (
            txn_target is None
            or txn_target.status != "active"
            or txn_target.source_branch != target.source_branch
            or target_handle_mismatches(
                target=txn_target,
                run_mr_iid=parent.mr_iid,
                round_mr_iid=mr_iid,
                project_ref=str(project_id),
            )
        ):
            raise ReviewFeedbackRefused(
                "collaboration_target_moved",
                f"target {target.id[:8]} moved between resolution and admission",
            )
        if txn_target.mr_iid is None and mr_iid is not None:
            # The lineage's MR identity, recorded once — the first
            # recorded handle wins and never moves.
            txn_target.mr_iid = mr_iid
        # R41-01 (#356): the child FlowRun is created and its identity
        # FLUSHED before the child budget opens. ``open_budget`` loads
        # the run and raises ``RunNotFound`` for an absent one — an
        # application guard, not a database surprise — so budget-before-
        # child broke EVERY finite spec (max_calls / max_tokens /
        # wallclock_s) before the child existed; only the unbudgeted
        # fixture skipped the guard. The flush lands the row in the
        # still-open transaction (identity map + INSERT emitted, FK
        # target visible), and everything below rides the SAME single
        # commit — a failure before it leaves NOTHING partial.
        session.add(
            FlowRun(
                id=plan.child_id,
                project_id=parent.project_id,
                issue_iid=parent.issue_iid,
                provider="gitlab",
                mr_iid=mr_iid,
                base_sha=request.head_sha,
                spec_digest=parent.spec_digest,
                config_digest=parent.config_digest,
                plan_digest=plan.plan_digest,
                evidence=plan.evidence,
                # R41-04 (#359): the child links the lineage's target —
                # its INDEPENDENT id never re-derives the branch.
                target_id=txn_target.id,
            )
        )
        await session.flush()
        # The child's OWN budget admission (R40-04 discipline): a new
        # round is never funded by an amendment — it opens its own ledger
        # from the SAME frozen spec ceilings, closing share partitioned
        # up front. The seams import CALL-TIME (the #356 mutation matrix
        # patches the durable package attribute).
        from forge.adaptive.closing_budget import ClosingReservePolicy, closing_partition
        from forge.durable import open_budget_from_spec

        spec_limits = budget_limits_from_spec(plan.spec_document)
        partition = (
            closing_partition(spec_limits, ClosingReservePolicy.from_env())
            if spec_limits is not None
            else None
        )
        if spec_limits is not None:
            await open_budget_from_spec(
                session,
                run_id=plan.child_id,
                spec_document=plan.spec_document,
                spec_digest=plan.spec_digest or None,
                closing_reserved_calls=partition.calls if partition else None,
                closing_reserved_tokens=partition.tokens if partition else None,
                closing_partition_policy=(partition.policy_version if partition else None),
            )
        reason = f"review round {plan.round_number} admitted (note {request.note_id})"
        await controller.transition(plan.child_id, FlowStatus.PREFLIGHT, reason=reason)
        await controller.transition(plan.child_id, FlowStatus.PLANNING, reason=reason)
        await controller.transition(plan.child_id, FlowStatus.WAITING_APPROVAL, reason=reason)
        await controller.transition(plan.child_id, FlowStatus.PROPOSING, reason=reason)
        session.add(
            RunSpec(
                id=uuid4().hex,
                run_id=plan.child_id,
                schema_version=plan.spec_schema_version,
                document=plan.spec_document,
                digest=plan.spec_digest or str(parent.spec_digest or ""),
            )
        )
        session.add(
            seams.reservation_model(
                flow_run_id=plan.child_id,
                # R41-04 (#359): the TARGET's recorded source branch — the
                # confirmed reservation on the SAME branch the lineage
                # already collaborates on (never an id re-derivation).
                branch=str(txn_target.source_branch),
                status="confirmed",
                mr_iid=mr_iid,
            )
        )
        session.add(
            ReviewRound(
                id=uuid4().hex,
                parent_run_id=parent_run_id,
                child_run_id=plan.child_id,
                root_run_id=root,
                round_number=plan.round_number,
                note_id=request.note_id,
                mr_iid=mr_iid,
                base_head_sha=request.head_sha,
                decision_id=plan.decision_id,
                requested_by=request.actor,
                status="admitted",
            )
        )
        session.add(
            Outbox(
                flow_run_id=plan.child_id,
                event_type="review_round.admitted",
                payload={
                    "run_id": plan.child_id,
                    "parent_run_id": parent_run_id,
                    "root_run_id": root,
                    "round_number": plan.round_number,
                    "note_id": request.note_id,
                    "decision_id": plan.decision_id,
                    "base_head_sha": request.head_sha,
                    "requested_by": request.actor,
                    # R41-04 (#359): the persisted surface the round rides
                    # + the human reference for it.
                    "collaboration_target_id": txn_target.id,
                    "source_branch": txn_target.source_branch,
                    "reference": round_reference(plan.round_number, root),
                },
            )
        )
        await session.commit()
    return plan.child_id
