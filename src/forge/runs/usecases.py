"""One lifecycle: the ObserveVerification use case (R27 slice 2, ADR-0027).

:func:`observe_verification` is the pure verdict semantics over
adapter-normalized evidence. It owns:

The caller (provider service) keeps everything that is genuinely adapter
duty:

- fetching the native evidence (transport/auth);
- normalizing it: translating native field names (``head_sha``/
  ``sourceVersion``, ``conclusion``/``result``), excluding the harness lane
  by spec-frozen identity, ordering by newest attempt, collapsing the
  newest-attempt-per-name observations, building the native-detail
  ``surface`` entries, and supplying its vocabulary sets;
- the timing guards that decide whether a pass observes at all — the
  registration grace window, the R17 deadline-before-I/O, and the cancel
  re-check — which need settings, wall clock and durable state, not
  verdict semantics;
- the provider-situational wording of the resulting *transition* reasons
  (the blocked/repair/timeout sentences name the native evidence —
  "checks" vs "builds") and the log lines.

**Where is GitLab?** Deliberately not a caller — its pipeline gate is
structurally different, and forcing it in would change behavior:

1. GitLab evaluates ONE FINISHED pipeline (an active pipeline keeps waiting
   upstream), so a missing required job there is a terminal
   ``quality_contract`` block — CI concluded; waiting is pointless. The A01
   lanes observe a surface that may still be registering and deliberately
   keep WAITING on unproven required checks.
2. The empty-contract semantics are intentionally opposite: GitLab's R02
   rule labels a green pipeline with no verification profile honestly
   ``unverified``; the A01 rule makes every OBSERVED check the proof set so
   a lone skipped workflow never verifies without the waiver.
3. GitLab's negative path classifies from failed JOBS
   (:func:`forge.runs.ci_contract.classify_failure`) over the whole
   pipeline status; the A01 contract classifies from required-check
   CONCLUSIONS.

GitLab joins the consolidation at the verdict level it already shares —
:class:`~forge.runs.verification.VerificationResult`,
:func:`forge.runs.consistency.ready_evidence` and the shared ready-reason
rules (ADR-0027 slice 1) — and its decision core is the profile contract in
:mod:`forge.runs.verification` (:func:`forge.runs.verification.evaluate`).

Import boundary (the enforceable half of R27): this module is core — it
must never import from ``forge.integrations.*`` or ``forge.gateway.*``
(``tests/test_usecases.py`` parses the imports), so the verification
decision stays provider-neutral."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from forge.runs.verification import (
    GITHUB_CODE_FAILURE_CONCLUSIONS,
    GITHUB_INFRA_CONCLUSIONS,
    GITHUB_SUCCESS_CONCLUSIONS,
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    STATUS_PASSED,
    STATUS_UNKNOWN,
    VerificationResult,
    evaluate_positive_proof,
)

__all__ = [
    "OUTCOME_BLOCK",
    "OUTCOME_REPAIR",
    "OUTCOME_REVIEW",
    "OUTCOME_WAIT",
    "VerificationContract",
    "VerificationDecision",
    "observe_verification",
]

#: The gate concluded that a required check is still running / still
#: unproven — keep waiting (bounded by the caller's R17 deadline), never
#: verify, never repair. A WAIT may carry the honest ``unknown`` verdict to
#: record (the unproven case) or none at all (the pending case).
OUTCOME_WAIT = "wait"

#: The gate concluded — walk to the review leg. Always carries a verdict:
#: ``passed`` (verified) or ``not_configured`` (honestly unverified, R02).
OUTCOME_REVIEW = "review"

#: A conclusion that BLAMES THE CHANGE — the bounded repair cycle (ADR-0008).
#: Carries the failed native verdict so the next repair retains the
#: candidate identity and checks that caused it.
OUTCOME_REPAIR = "repair"

#: The EXECUTION died (cancelled/timed_out/canceled/abandoned) — block
#: visibly, never repair (the repair budget is for code failures only).
#: Carries the honest ``unknown`` verdict naming the failed checks.
OUTCOME_BLOCK = "block"


class VerificationContract(Protocol):
    """The frozen spec surface ObserveVerification enforces.

    R04/A02: the required-checks contract comes from the digest-verified
    executable spec, never from live settings —
    :class:`~forge.runs.spec.ExecutableRunSpec` satisfies this.
    """

    @property
    def required_jobs(self) -> tuple[str, ...]: ...


#: The verified verdict summary — byte-identical on every provider (both
#: A01 gates already said exactly this).
VERIFIED_SUMMARY = "required checks succeeded for the tested sha"

NOT_CONFIGURED_SUMMARY = "no CI checks ran for the candidate commit"

#: The infra-failure verdict summary — the execution died, not the change.
INFRA_SUMMARY_PREFIX = "required checks cancelled or timed out: "

#: The ``evaluating_ci`` transition reasons (identical on both A01 gates).
VERIFIED_TRANSITION_REASON = "required checks passed"
NOT_CONFIGURED_TRANSITION_REASON = "no CI configured — unverified"


@dataclass(frozen=True)
class VerificationDecision:
    """The ObserveVerification outcome for one verification pass.

    ``verdict`` is the :class:`~forge.runs.verification.VerificationResult`
    to record under ``flow_runs.evidence["verification"]`` — ``None`` when
    the pass has no completed observation. A repair records its failed proof.
    ``failing`` names the offending checks for the caller's provider-worded
    blocked/repair reasons. ``reason`` is the
    ``evaluating_ci`` transition reason for REVIEW outcomes (provider-neutral
    wording; the blocked/repair/timeout sentences stay at the call site).
    """

    #: One of the ``OUTCOME_*`` constants.
    outcome: str
    #: The verdict to record, or None when the pass records nothing.
    verdict: VerificationResult | None
    #: The offending check names (REPAIR/BLOCK outcomes).
    failing: tuple[str, ...] = ()
    #: The ``evaluating_ci`` transition reason (REVIEW outcomes).
    reason: str = ""

    @property
    def verified(self) -> bool:
        """R02: only a passed verdict counts as verified — nothing else."""
        return self.verdict is not None and self.verdict.verified

    def evidence(self) -> dict:
        """The evidence fragment for the verdict; ``{}`` when none recorded."""
        return self.verdict.as_evidence() if self.verdict is not None else {}


def _verdict(
    status: str,
    tested_oid: str,
    producer: str,
    summary: str,
    surface: Sequence[Mapping[str, Any]],
    now: datetime,
) -> VerificationResult:
    """Stamp a VerificationResult with the caller's observation instant.

    Stamping with the pass's ``now`` (not the wall clock) is what makes two
    identical passes produce identical decisions — the property the
    cross-provider parity suite pins.
    """
    return VerificationResult(
        status,
        tested_oid,
        now.astimezone(timezone.utc).isoformat(),
        producer,
        summary,
        tuple(dict(entry) for entry in surface),
    )


def observe_verification(
    *,
    spec: VerificationContract,
    provider: str,
    observations: Mapping[str, str | None],
    candidate_sha: str,
    subject_head_oid: str = "",
    surface: Sequence[Mapping[str, Any]] = (),
    pending: bool = False,
    ambiguous_checks: Mapping[str, Sequence[str]] | None = None,
    success_conclusions: frozenset[str] = GITHUB_SUCCESS_CONCLUSIONS,
    code_failure_conclusions: frozenset[str] = GITHUB_CODE_FAILURE_CONCLUSIONS,
    infra_conclusions: frozenset[str] = GITHUB_INFRA_CONCLUSIONS,
    waived_conclusions: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> VerificationDecision:
    """The ObserveVerification decision over one candidate's observations.

    *observations* maps check name → the NEWEST conclusion observed for that
    name in the provider's native vocabulary (``None`` when the entry
    concluded with no verdict); the caller owns the newest-attempt rule.
    *pending* is the completeness flag — any observed check the provider
    still reports as running. *subject_head_oid* is the head oid the
    provider observed on the newest entry (``head_sha`` / ``sourceVersion``);
    the verdict binds there, falling back to *candidate_sha*. *provider* is
    the evidence producer identity (a ``PRODUCER_*`` constant). *surface*
    rides into the verdict's evidence detail as-is (the provider's native
    per-check identity). *now* stamps the verdict (default: wall clock)."""
    now = now or datetime.now(timezone.utc)

    if not observations:
        # Nothing observed for the candidate (B07): with a NON-EMPTY frozen
        # required list this is a MISSING MANDATORY GATE, not the no-CI
        # form — the run keeps waiting (bounded by the R17 deadline) and
        # blocks as verification_timeout, never an unverified READY. Only
        # a spec without required checks (best-effort mode) keeps the
        # honest not_configured handoff.
        if spec.required_jobs:
            return VerificationDecision(outcome=OUTCOME_WAIT, verdict=None)
        return VerificationDecision(
            outcome=OUTCOME_REVIEW,
            verdict=_verdict(
                STATUS_NOT_CONFIGURED,
                candidate_sha,
                provider,
                NOT_CONFIGURED_SUMMARY,
                (),
                now,
            ),
            reason=NOT_CONFIGURED_TRANSITION_REASON,
        )

    if pending:
        # Completeness: a check the provider still reports as running is no
        # verdict at all — the run keeps waiting (bounded by the R17
        # deadline), a late check may still register.
        return VerificationDecision(outcome=OUTCOME_WAIT, verdict=None)

    proof = evaluate_positive_proof(
        spec.required_jobs,
        observations,
        ambiguous_checks=ambiguous_checks,
        success_conclusions=success_conclusions,
        code_failure_conclusions=code_failure_conclusions,
        infra_conclusions=infra_conclusions,
        waived_conclusions=waived_conclusions,
    )
    # ADR-0008: the verdict binds to the sha the PROVIDER verified.
    tested_oid = subject_head_oid or candidate_sha

    if proof.infra_failures:
        # The execution died — infrastructure, never a code failure; the
        # run blocks visibly instead of spending the repair budget.
        return VerificationDecision(
            outcome=OUTCOME_BLOCK,
            verdict=_verdict(
                STATUS_UNKNOWN,
                tested_oid,
                provider,
                INFRA_SUMMARY_PREFIX + ", ".join(proof.infra_failures),
                surface,
                now,
            ),
            failing=proof.infra_failures,
        )

    if proof.code_failures:
        # ADR-0008: only a conclusion that blames the change drives the
        # bounded repair loop.
        return VerificationDecision(
            outcome=OUTCOME_REPAIR,
            verdict=_verdict(STATUS_FAILED, tested_oid, provider, "checks failed", surface, now),
            failing=proof.code_failures,
        )

    if proof.unproven:
        # Required checks absent/skipped/neutral (unwaived): NO proof — a
        # green optional check never substitutes. Honest unknown, keep
        # waiting (the R17 deadline is the bound).
        return VerificationDecision(
            outcome=OUTCOME_WAIT,
            verdict=_verdict(
                STATUS_UNKNOWN,
                tested_oid,
                provider,
                proof.summary(),
                surface,
                now,
            ),
        )

    # Positive proof: every required check present and successful (or an
    # explicitly waived inconclusive one) — the one verified verdict (R02).
    return VerificationDecision(
        outcome=OUTCOME_REVIEW,
        verdict=_verdict(STATUS_PASSED, tested_oid, provider, VERIFIED_SUMMARY, surface, now),
        reason=VERIFIED_TRANSITION_REASON,
    )
