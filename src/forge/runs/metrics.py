"""Delivery-acceptance metrics (R24) — measure accepted work honestly.

The F34 ladder counted a run's CURRENT status only, which read as "READY is
the end of the story": a merged MR was invisible (the bot never merges), a
superseded sibling's failure vanished behind its successful replan, and the
``ci_passed`` rung lit up for a transient status regardless of whether any
verification evidence existed. This module is the R24 correction, computed
entirely from data the run loop already persists — no new instrumentation:

- **Acceptance linkage** — when a run's native MR/PR is observed merged
  (the ``evaluate_accepted`` reconciler pass in :mod:`forge.runs.service`
  reads the provider API), the run carries an ``acceptance`` evidence
  fragment and buckets at the new top ladder rung ``accepted``. A run
  superseded by another run of the same issue records ``rework_of`` (and
  its successor learns ``superseded_by``) — both plain evidence writes.
- **Denominator honesty** — :func:`delivery_counts` exposes
  ``accepted_count``, ``rejected_count`` (failed/blocked/cancelled — they
  stay in the denominator forever; a later successful sibling never erases
  a failure), ``rework_count`` and ``total_attempts``.
- **ci_passed is VERIFIED-only** — the rung buckets a run there only when
  its recorded verification fragment is a ``passed`` verdict bound to the
  candidate sha (:func:`forge.runs.consistency.verified_verdict`, the
  R02/R27 evidence distinction). Harness pipeline success and
  unverified-ready runs never count.
- **Per-run time decomposition** — :func:`run_decomposition` derives
  ``llm_wait`` (sum of ``llm_calls.duration_ms``), ``step_wait`` (gaps
  between consecutive bounded steps), ``ci_wait`` (``waiting_ci`` →
  ``evaluating_ci``), ``human_wait`` (``ready_for_human`` → merged, only
  once merged) and ``repairs`` (``commit_cycle - 1``) from existing
  timestamps: the transition outbox, the step rows and the LLM ledger.

The exposition style matches the gateway's hand-rolled Prometheus text:
plain gauge families, one series per label tuple (see
``forge.gateway.router``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.durable import TRANSITION_EVENT_TYPE, FlowRun, LLMCall, Outbox, StepRun
from forge.durable.controller import as_aware_utc
from forge.runs.consistency import verified_verdict

__all__ = [
    "LADDER_RUNGS",
    "REWORK_OF_KEY",
    "RunFacts",
    "SUPERSEDED_BY_KEY",
    "TERMINAL_FAILURE_STATUSES",
    "acceptance_state",
    "collect_delivery_metrics",
    "delivery_counts",
    "ladder_counts",
    "run_decomposition",
    "run_rung",
]

#: The acceptance ladder (F34 rungs + the R24 ``accepted`` rung on top).
#: Bucketed by the FURTHEST rung each run has reached: ``accepted`` outranks
#: ``ready_for_human``, which outranks the evidence-proven ``ci_passed``.
LADDER_RUNGS: tuple[str, ...] = (
    "started",
    "planned",
    "gate_approved",
    "candidate_published",
    "ci_passed",
    "ready_for_human",
    "accepted",
)

#: Terminal statuses that stay in the attempts denominator forever (R24):
#: a run parked here is a rejected attempt even when a later sibling of the
#: same issue succeeds.
TERMINAL_FAILURE_STATUSES: frozenset[str] = frozenset({"failed", "blocked", "cancelled"})

#: Evidence key on the SUCCESSOR run naming the prior attempt it reworks.
REWORK_OF_KEY = "rework_of"

#: Evidence key on the SUPERSEDED run naming the run that replaced it.
SUPERSEDED_BY_KEY = "superseded_by"

#: Furthest rung each lifecycle status proves ON ITS OWN. ``reviewing``
#: deliberately maps to ``candidate_published`` — the ``ci_passed`` rung is
#: reserved for runs with a VERIFIED verdict (R24/R02), which
#: :func:`run_rung` grants from the evidence.
_RUNG_BY_STATUS: dict[str, str] = {
    "waiting_approval": "planned",
    "proposing": "gate_approved",
    "validating": "gate_approved",
    "committing": "gate_approved",
    "waiting_harness": "candidate_published",
    "ensuring_draft_mr": "candidate_published",
    "waiting_ci": "candidate_published",
    "evaluating_ci": "candidate_published",
    "reviewing": "candidate_published",
    "ready_for_human": "ready_for_human",
}


class RunFacts(NamedTuple):
    """The per-run fields the delivery metrics read (no ORM objects)."""

    status: str
    evidence: Mapping[str, Any] = {}
    candidate_shas: Sequence[str] = ()
    commit_cycle: int = 1


def acceptance_state(evidence: Mapping[str, Any] | None) -> str | None:
    """The recorded acceptance decision for a run, or None.

    Only an explicit ``"merged"``/``"closed"`` fragment counts — an empty,
    malformed or foreign-shaped ``acceptance`` block is invisible to the
    metrics rather than guessed into acceptance.
    """
    if not isinstance(evidence, Mapping):
        return None
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, Mapping):
        return None
    state = str(acceptance.get("state") or "").strip().lower()
    return state or None


def is_accepted(evidence: Mapping[str, Any] | None) -> bool:
    """Whether the run's native MR/PR was observed MERGED (R24 acceptance)."""
    return acceptance_state(evidence) == "merged"


def run_rung(
    status: str,
    evidence: Mapping[str, Any] | None = None,
    candidate_shas: Sequence[str] = (),
) -> str:
    """The furthest ladder rung one run has reached.

    ``ci_passed`` requires a VERIFIED verdict bound to the candidate sha —
    never harness success, never an unverified-ready run (R02/R24). The
    ``accepted`` rung outranks everything: the human merged the work.
    """
    rung = _RUNG_BY_STATUS.get(status, "started")
    if status == "reviewing":
        verification = evidence.get("verification") if isinstance(evidence, Mapping) else None
        candidate_sha = candidate_shas[-1] if candidate_shas else ""
        if verified_verdict(verification, candidate_sha):
            rung = "ci_passed"
    if is_accepted(evidence):
        rung = "accepted"
    return rung


def ladder_counts(facts: Iterable[RunFacts]) -> dict[str, int]:
    """Bucket runs by the furthest ladder rung each has reached (F34/R24)."""
    ladder = {rung: 0 for rung in LADDER_RUNGS}
    for fact in facts:
        ladder[run_rung(fact.status, fact.evidence, fact.candidate_shas)] += 1
    return ladder


def delivery_counts(facts: Iterable[RunFacts]) -> dict[str, int]:
    """The honest denominator and its numerators (R24).

    ``total_attempts`` counts EVERY run ever created — failed, blocked and
    cancelled runs stay counted, so a successful sibling never erases a
    failure. ``rework_count`` counts runs that superseded a prior attempt
    (the ``rework_of`` linkage); it deliberately overlaps the other views:
    a rework that merged is both accepted and a rework.
    """
    total = 0
    accepted = 0
    rejected = 0
    rework = 0
    for fact in facts:
        total += 1
        if is_accepted(fact.evidence):
            accepted += 1
        if fact.status in TERMINAL_FAILURE_STATUSES:
            rejected += 1
        if str(fact.evidence.get(REWORK_OF_KEY) or ""):
            rework += 1
    return {
        "total_attempts": total,
        "accepted_count": accepted,
        "rejected_count": rejected,
        "rework_count": rework,
    }


def _clamp_seconds(value: float) -> float:
    return value if value > 0 else 0.0


def _step_wait_seconds(steps: Sequence[tuple[datetime | None, datetime | None]]) -> float:
    """Sum of the gaps BETWEEN consecutive bounded steps (queue/step wait).

    Each step row carries ``started_at`` (when it was created) and
    ``finished_at``; the wait is the idle time between one step finishing
    and the next starting. Broken/missing timestamps contribute nothing.
    """
    wait = 0.0
    previous_finish: datetime | None = None
    for started_at, finished_at in steps:
        if previous_finish is not None and started_at is not None and finished_at is not None:
            gap = (as_aware_utc(started_at) - as_aware_utc(previous_finish)).total_seconds()
            wait += _clamp_seconds(gap)
        if finished_at is not None:
            previous_finish = finished_at
    return wait


def _transition_times(
    transitions: Sequence[tuple[datetime | None, Mapping[str, Any]]], target: str
) -> datetime | None:
    """The timestamp of the FIRST journaled transition INTO *target*."""
    for created_at, payload in transitions:
        if str(payload.get("to") or "") == target and created_at is not None:
            return as_aware_utc(created_at)
    return None


def run_decomposition(
    *,
    commit_cycle: int = 1,
    llm_duration_ms: float | None = None,
    transitions: Sequence[tuple[datetime | None, Mapping[str, Any]]] = (),
    steps: Sequence[tuple[datetime | None, datetime | None]] = (),
    acceptance: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    """Per-run time decomposition from existing timestamps (R24).

    Returns phase-name → seconds (plus ``repairs`` = ``commit_cycle - 1``).
    ``human_wait`` is present ONLY for merged runs — an unmerged run has no
    human wait yet, and a zero would be a lie about work still in review.
    All derived waits clamp at zero: clock skew degrades to 0, never
    negative.
    """
    phases: dict[str, float] = {
        "llm_wait": (llm_duration_ms or 0.0) / 1000.0,
        "step_wait": _step_wait_seconds(steps),
        "repairs": float(max(int(commit_cycle or 1) - 1, 0)),
    }

    ci_wait = 0.0
    waiting_at = _transition_times(transitions, "waiting_ci")
    evaluating_at = _transition_times(transitions, "evaluating_ci")
    if waiting_at is not None and evaluating_at is not None:
        ci_wait = _clamp_seconds((evaluating_at - waiting_at).total_seconds())
    phases["ci_wait"] = ci_wait

    if is_accepted(acceptance):
        ready_at = _transition_times(transitions, "ready_for_human")
        merged_raw = ""
        observed_raw = ""
        if isinstance(acceptance, Mapping):
            merged_raw = str(acceptance.get("merged_at") or "")
            observed_raw = str(acceptance.get("observed_at") or "")
        merged_at = _parse_iso(merged_raw) or _parse_iso(observed_raw)
        if ready_at is not None and merged_at is not None:
            phases["human_wait"] = _clamp_seconds((merged_at - ready_at).total_seconds())
    return phases


def _parse_iso(raw: str) -> datetime | None:
    try:
        return as_aware_utc(datetime.fromisoformat(raw))
    except (TypeError, ValueError):
        return None


async def collect_delivery_metrics(session: AsyncSession) -> dict[str, Any]:
    """One read pass over the durable tables → the delivery snapshot.

    Reads every run's status/evidence plus the three timestamp sources the
    decomposition derives from (the transition outbox, the bounded-step
    rows, the LLM call ledger). Returns::

        {
            "ladder":  {rung: count, ...},          # forge_delivery_ladder
            "counts":  {total_attempts, accepted_count,
                        rejected_count, rework_count},
            "runs":    {run_id: {phase: seconds, "repairs": n}, ...},
        }
    """
    run_rows = (
        await session.execute(
            select(
                FlowRun.id,
                FlowRun.status,
                FlowRun.evidence,
                FlowRun.commit_cycle,
                FlowRun.candidate_shas,
            )
        )
    ).all()
    facts = [
        RunFacts(
            status=str(status),
            evidence=evidence if isinstance(evidence, Mapping) else {},
            candidate_shas=list(candidate_shas or []),
            commit_cycle=int(commit_cycle or 1),
        )
        for _, status, evidence, commit_cycle, candidate_shas in run_rows
    ]

    llm_totals: dict[str, float] = {}
    for run_id, total_ms in (
        await session.execute(
            select(LLMCall.flow_run_id, func.sum(LLMCall.duration_ms)).group_by(LLMCall.flow_run_id)
        )
    ).all():
        if run_id is not None:
            llm_totals[str(run_id)] = float(total_ms or 0)

    steps_by_run: dict[str, list[tuple[datetime | None, datetime | None]]] = {}
    for run_id, started_at, finished_at in (
        await session.execute(
            select(StepRun.flow_run_id, StepRun.started_at, StepRun.finished_at).order_by(
                StepRun.flow_run_id, StepRun.id
            )
        )
    ).all():
        if run_id is not None:
            steps_by_run.setdefault(str(run_id), []).append((started_at, finished_at))

    transitions_by_run: dict[str, list[tuple[datetime | None, Mapping[str, Any]]]] = {}
    for run_id, created_at, payload in (
        await session.execute(
            select(Outbox.flow_run_id, Outbox.created_at, Outbox.payload)
            .where(Outbox.event_type == TRANSITION_EVENT_TYPE)
            .order_by(Outbox.flow_run_id, Outbox.id)
        )
    ).all():
        if run_id is not None and isinstance(payload, Mapping):
            transitions_by_run.setdefault(str(run_id), []).append((created_at, payload))

    runs: dict[str, dict[str, float]] = {}
    for run_id, _status, evidence, commit_cycle, _shas in run_rows:
        acceptance = evidence.get("acceptance") if isinstance(evidence, Mapping) else None
        runs[str(run_id)] = run_decomposition(
            commit_cycle=int(commit_cycle or 1),
            llm_duration_ms=llm_totals.get(str(run_id)),
            transitions=transitions_by_run.get(str(run_id), ()),
            steps=steps_by_run.get(str(run_id), ()),
            acceptance=acceptance if isinstance(acceptance, Mapping) else None,
        )

    return {
        "ladder": ladder_counts(facts),
        "counts": delivery_counts(facts),
        "runs": runs,
    }
