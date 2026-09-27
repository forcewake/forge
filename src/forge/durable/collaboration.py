"""The explicit collaboration target (R41-04, #359).

The disclosed debt this module retires: child round ids deliberately
reused the parent's first 8 hex chars so every ``factory_branch``
derivation landed on the same source branch
(``child_id = f"{run_id[:8]}{uuid4().hex[:24]}"``). That coupled work
identity (the run id), collaboration identity (the branch + MR a whole
delivery lineage shares) and display shorthand (the 8-char short id) —
and made short command ids ambiguous within a lineage.

From #359 the collaboration surface is a PERSISTED record — provider,
repository, source branch, target branch, MR identity — linked from
root and child work (``flow_runs.target_id`` →
``collaboration_targets``). New rounds get INDEPENDENT run ids; branch
identity resolves through the target in the publisher, the drift
checks, the harness dispatch, CI collection and the operator commands.
Legacy runs (admitted before the target column existed) get their
target derived ONCE by the adapter here — validated against recorded
MR/source information before anything is materialized; an unresolved
legacy topology records as a REFUSAL row, never an inferred branch.

Kept dependency-light like :mod:`forge.durable.identity` (identity
helpers only) plus the ORM row: the module is imported by the durable
package facade and by the services.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.durable.identity import factory_branch
from forge.durable.models import CollaborationTarget, FlowRun

__all__ = [
    "ACTIVE_TARGET_STATUS",
    "TARGET_MISMATCH_EVENT",
    "TARGET_REFUSED_EVENT",
    "CollaborationTargetError",
    "LegacyTargetEvidence",
    "ROUND_REFERENCE_RE",
    "TargetDecision",
    "admission_target",
    "derive_legacy_target",
    "round_reference",
    "target_for_run",
    "target_handle_mismatches",
]

#: The one live target status. A ``refused`` row is a durable record
#: that a legacy topology could NOT be resolved — it carries the typed
#: reason and never a branch.
ACTIVE_TARGET_STATUS = "active"

#: The outbox event type emitted when a recorded handle disagrees with
#: the target (run MR vs round MR vs provider MR vs target row) — the
#: observability dimension the review named.
TARGET_MISMATCH_EVENT = "collaboration.target_mismatch"

#: The outbox event type emitted when the legacy adapter records a
#: refusal (unresolved legacy topology).
TARGET_REFUSED_EVENT = "collaboration.target_refused"

#: ``round 2 of a1b2c3d4`` — the concise human round reference. It is
#: DISPLAY AND READ RESOLUTION ONLY: never an authorization token (the
#: destructive commands still require the full run id) and never a
#: publication input (publication keys on the target's recorded source
#: branch and the run ids, never on a label or a round number).
ROUND_REFERENCE_RE = re.compile(r"round\s+(\d+)\s+of\s+([0-9a-fA-F]{8,32})\b", re.IGNORECASE)


def round_reference(round_number: int, root_run_id: str) -> str:
    """``round <n> of <root-short>`` — the human reference for one round.

    Unambiguous within a repository because the pair (lineage root,
    round number) names exactly one delivery: round 1 is the root
    delivery itself, round N ≥ 2 the child of the (N−1)-th delivery.
    """
    return f"round {int(round_number)} of {root_run_id[:8]}"


class CollaborationTargetError(RuntimeError):
    """The typed refusal of the target contract.

    ``code`` is the stable machine-readable reason
    (``collaboration_target_unresolved`` for a refused legacy topology,
    ``collaboration.target_mismatch`` for disagreeing recorded
    handles); ``detail`` names the concrete disagreement.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class LegacyTargetEvidence:
    """The recorded MR/source information the legacy adapter validates
    a derivation against — everything the run ever PERSISTED about its
    collaboration surface (never a live provider read).

    ``branches`` — the branch names recorded on the run's own rows
    (``mr_reservations.branch``, publication-intent ``target_ref``s,
    succeeded commit ``correlation_id``s). ``mr_iids`` — the MR numbers
    those same rows recorded. Both are advisory-unless-present: absent
    evidence means the run never touched a branch; PRESENT evidence is
    binding.
    """

    branches: tuple[str, ...] = ()
    mr_iids: tuple[int, ...] = ()

    def branch_set(self) -> frozenset[str]:
        return frozenset(branch for branch in self.branches if branch)

    def mr_set(self) -> frozenset[int]:
        return frozenset(int(mr) for mr in self.mr_iids if mr is not None)


@dataclass(frozen=True)
class TargetDecision:
    """The pure outcome of the legacy validation: materialize, or refuse.

    A materialization carries the validated ``source_branch`` and the
    single agreeing ``mr_iid`` (``None`` when nothing recorded one). A
    refusal carries the typed reason and NEVER a branch — an unresolved
    legacy topology is recorded, not inferred.
    """

    source_branch: str | None = None
    target_branch: str | None = None
    mr_iid: int | None = None
    refusal_reason: str | None = None

    @property
    def refused(self) -> bool:
        return self.refusal_reason is not None


def derive_legacy_target(
    *,
    provider: str,
    project_id: int,
    issue_iid: int | None,
    run_id: str,
    run_mr_iid: int | None,
    evidence: LegacyTargetEvidence,
    target_branch: str,
) -> TargetDecision:
    """Derive ONE legacy run's collaboration target — validated first.

    The candidate is the run's OWN historical derivation
    (:func:`forge.durable.identity.factory_branch` — the exact
    expression every pre-#359 call site evaluated for this run, prefix
    reuse included). It is only materialized when the run's RECORDED
    MR/source information does not CONTRADICT it:

    - recorded branches must ALL equal the candidate — a disagreeing
      record (hand-migrated data, a foreign reservation) is an
      unresolved topology, refused;
    - the recorded MR numbers (reservations, intents, the run row)
      must agree on at most one MR — two different MRs for one run is
      a topology mismatch, refused;
    - a run with NO contradicting evidence keeps its own deterministic
      derivation — that IS the topology its admission minted (delivery
      1 always derived from its own full id; a legacy round child's
      prefix reuse made its own-id derivation the lineage branch by
      construction). Absent evidence is not disagreement: a delivery-1
      run records its branch only as publication intents / journal
      correlation ids once it commits, and its MR row alone pins no
      branch — materialized with provenance ``legacy``.

    This function is PURE (no session, no I/O): migration 032 applies
    the same decision in SQL terms, and the runtime adapter calls this
    one directly — one validation, two entry points.
    """
    if provider != "gitlab":
        # The GitHub/Azure lanes keep their own per-run branch twins
        # (no review rounds exist there); the legacy adapter is the
        # GitLab lane's alone this cycle.
        return TargetDecision(refusal_reason=f"no_legacy_adapter_for_provider:{provider}")
    candidate = factory_branch(issue_iid, run_id)
    recorded_branches = evidence.branch_set()
    recorded_mrs = evidence.mr_set()
    if run_mr_iid is not None:
        recorded_mrs = recorded_mrs | {int(run_mr_iid)}
    if recorded_branches and recorded_branches != frozenset({candidate}):
        listed = ", ".join(sorted(recorded_branches))
        return TargetDecision(
            refusal_reason=(
                f"recorded_branch_mismatch: derivation {candidate} but recorded [{listed}]"
            )
        )
    if len(recorded_mrs) > 1:
        listed = ", ".join(f"!{mr}" for mr in sorted(recorded_mrs))
        return TargetDecision(refusal_reason=f"recorded_mr_mismatch: {listed}")
    return TargetDecision(
        source_branch=candidate,
        target_branch=target_branch,
        mr_iid=next(iter(recorded_mrs)) if recorded_mrs else None,
    )


async def _recorded_evidence(session: AsyncSession, run: FlowRun) -> LegacyTargetEvidence:
    """Everything *run* ever persisted about its branch/MR — no I/O."""
    from forge.durable.models import ActionLog, MRReservation, PublicationIntent

    branches: list[str] = []
    mr_iids: list[int] = []
    reservations = (
        await session.execute(
            select(MRReservation.branch, MRReservation.mr_iid).where(
                MRReservation.flow_run_id == run.id
            )
        )
    ).all()
    for branch, mr_iid in reservations:
        if branch:
            branches.append(str(branch))
        if mr_iid is not None:
            mr_iids.append(int(mr_iid))
    intents = (
        (
            await session.execute(
                select(PublicationIntent.target_ref).where(
                    PublicationIntent.run_id == run.id,
                    PublicationIntent.operation == "commit",
                )
            )
        )
        .scalars()
        .all()
    )
    branches.extend(str(ref) for ref in intents if ref)
    commits = (
        (
            await session.execute(
                select(ActionLog.correlation_id).where(
                    ActionLog.flow_run_id == run.id,
                    ActionLog.action_kind == "commit",
                    ActionLog.status == "succeeded",
                )
            )
        )
        .scalars()
        .all()
    )
    branches.extend(str(ref) for ref in commits if ref)
    return LegacyTargetEvidence(branches=tuple(branches), mr_iids=tuple(mr_iids))


async def _lineage_root(session: AsyncSession, run: FlowRun) -> str:
    """The run's lineage root (delivery 1) via its round linkage."""
    from forge.durable.models import ReviewRound

    own = (
        (
            await session.execute(
                select(ReviewRound.root_run_id)
                .where(ReviewRound.child_run_id == run.id)
                .order_by(ReviewRound.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return str(own) if own else run.id


async def target_for_run(
    session: AsyncSession,
    run: FlowRun,
    *,
    target_branch: str = "main",
) -> CollaborationTarget:
    """The run's collaboration target — the live link, or the ONE
    legacy materialization.

    - a run already linked to an ACTIVE target returns it (the fast
      path every modern admission rides; read-only, no commit);
    - a run linked to a REFUSED target raises
      :class:`CollaborationTargetError` — the refusal is durable, the
      topology is never re-derived on later attempts;
    - an unlinked run (admitted before the target column existed, or by
      a worker from before a staged rollout) gets the legacy adapter:
      derive ONCE, validate against recorded MR/source information,
      find-or-create the row by its unique (provider, project,
      source-branch) key, link the run and COMMIT — this session's own
      transaction, complete when the call returns. Call it on a
      dedicated session (idempotent: the unique branch key collapses
      racing materializers, the loser re-reads the winner's row).

    The admission path resolves the lineage target BEFORE opening its
    transaction, then re-reads the linked row inside it — so the
    materialization never interleaves with the admission's commit.
    """
    if run.target_id:
        target = await session.get(CollaborationTarget, run.target_id)
        if target is None:
            raise CollaborationTargetError(
                "collaboration_target_unresolved",
                f"run {run.id[:8]} links target {run.target_id[:8]} which does not exist",
            )
        if target.status != ACTIVE_TARGET_STATUS:
            raise CollaborationTargetError(
                "collaboration_target_unresolved",
                f"run {run.id[:8]} target is refused: {target.refusal_reason or 'unknown'}",
            )
        return target
    evidence = await _recorded_evidence(session, run)
    decision = derive_legacy_target(
        provider=str(run.provider or "gitlab"),
        project_id=int(run.project_id),
        issue_iid=run.issue_iid,
        run_id=run.id,
        run_mr_iid=run.mr_iid,
        evidence=evidence,
        target_branch=target_branch,
    )
    if decision.refused or decision.source_branch is None:
        refusal = CollaborationTarget(
            id=uuid4().hex,
            provider=str(run.provider or "gitlab"),
            project_ref=str(run.project_id),
            issue_iid=run.issue_iid,
            root_run_id=await _lineage_root(session, run),
            status="refused",
            refusal_reason=(decision.refusal_reason or "unresolved")[:300],
            provenance="legacy",
        )
        session.add(refusal)
        await session.flush()
        run.target_id = refusal.id
        session.add(_refusal_outbox(run, refusal.refusal_reason or "unresolved"))
        await session.commit()
        raise CollaborationTargetError(
            "collaboration_target_unresolved", refusal.refusal_reason or "unresolved"
        )
    existing = (
        (
            await session.execute(
                select(CollaborationTarget)
                .where(
                    CollaborationTarget.provider == str(run.provider or "gitlab"),
                    CollaborationTarget.project_ref == str(run.project_id),
                    CollaborationTarget.source_branch == decision.source_branch,
                    CollaborationTarget.status == ACTIVE_TARGET_STATUS,
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        # A lineage sibling (or the migration) materialized this branch's
        # target already — link and go; the recorded MR identity only
        # strengthens (never contradicts: validation refused mismatches).
        run.target_id = existing.id
        if existing.mr_iid is None and decision.mr_iid is not None:
            existing.mr_iid = decision.mr_iid
        await session.commit()
        return existing
    root = await _lineage_root(session, run)
    target = CollaborationTarget(
        id=uuid4().hex,
        provider=str(run.provider or "gitlab"),
        project_ref=str(run.project_id),
        issue_iid=run.issue_iid,
        root_run_id=root,
        source_branch=decision.source_branch,
        target_branch=decision.target_branch or target_branch,
        mr_iid=decision.mr_iid,
        status=ACTIVE_TARGET_STATUS,
        provenance="legacy",
    )
    session.add(target)
    await session.flush()
    run.target_id = target.id
    await session.commit()
    return target


def _refusal_outbox(run: FlowRun, reason: str):
    """The observability row for a recorded refusal (no branch, ever)."""
    from forge.durable.models import Outbox

    return Outbox(
        flow_run_id=run.id,
        event_type=TARGET_REFUSED_EVENT,
        payload={
            "run_id": run.id,
            "reason": reason[:300],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def admission_target(
    *,
    provider: str,
    project_id: int,
    issue_iid: int | None,
    run_id: str,
    target_branch: str,
) -> CollaborationTarget:
    """The target row a NEW admission materializes (unpersisted).

    The ONE live derivation site: the source branch of a delivery-1
    admission is minted from the run's own identity exactly once, here
    and in :func:`derive_legacy_target` — every consumer afterwards
    resolves through the persisted row (the run id NEVER re-derives a
    branch downstream; R41-04's boundary rule confines exactly this).
    """
    return CollaborationTarget(
        id=uuid4().hex,
        provider=provider,
        project_ref=str(project_id),
        issue_iid=issue_iid,
        root_run_id=run_id,
        source_branch=factory_branch(issue_iid, run_id),
        target_branch=target_branch,
        status=ACTIVE_TARGET_STATUS,
        provenance="live",
    )


def target_handle_mismatches(
    *,
    target: CollaborationTarget,
    run_mr_iid: int | None,
    round_mr_iid: int | None = None,
    provider_mr_source_branch: str = "",
    project_ref: str | None = None,
) -> list[str]:
    """Every disagreement between the recorded handles — the pre-write
    guard (R41-04's ``collaboration.target_mismatch``).

    Compares what each recorded surface says about the collaboration
    identity: the target row's MR, the run row's MR, the round's MR,
    the provider's live MR document (its source branch, when it
    reports one) and the repository identity. Any disagreement is a
    mismatch string; an empty list means the handles agree and the
    write may proceed. Provider-scoped subjects keep two repositories
    with the same MR number and branch label apart by construction
    (``project_ref``), so they never reach a mismatch — they are
    different targets outright.
    """
    mismatches: list[str] = []
    recorded: dict[str, int] = {}
    if target.mr_iid is not None:
        recorded["target"] = int(target.mr_iid)
    if run_mr_iid is not None:
        recorded["run"] = int(run_mr_iid)
    if round_mr_iid is not None:
        recorded["round"] = int(round_mr_iid)
    if len(set(recorded.values())) > 1:
        listed = ", ".join(f"{where}=!{mr}" for where, mr in sorted(recorded.items()))
        mismatches.append(f"mr identity disagreement ({listed})")
    source = str(target.source_branch or "")
    provider_source = str(provider_mr_source_branch or "").strip()
    if provider_source and source and provider_source != source:
        mismatches.append(f"provider MR source branch {provider_source!r} != target {source!r}")
    if project_ref is not None and str(target.project_ref) != str(project_ref):
        mismatches.append(f"repository identity {project_ref!r} != target {target.project_ref!r}")
    return mismatches
