"""One lifecycle: the shared READY/finalization invariants (R27, ADR-0027).

The three provider services (``service.py`` GitLab, ``github_service.py``,
``azure_service.py``) hand-maintained parallel implementations of the same
finalization invariants. The copies had already drifted once — GitHub
recorded its verification fragment under a ``candidate_sha`` key while
GitLab/Azure used the unified ``tested_oid`` shape, and the unverified
GitLab ready reason claimed "checks passed" — which is exactly the
``/retry``-guard-miss class of bug R27 predicts from "three copies of the
factory". This module is the ONE source for the invariants that drifted:

- :func:`ready_evidence` — the unified R02
  :class:`~forge.runs.verification.VerificationResult` evidence fragment
  every provider records under ``flow_runs.evidence["verification"]``;
- :func:`ready_reason` — the ``ready_for_human`` transition reason
  (the "unverified — …" prefix rule and the "checks passed; merge is a
  human decision" tail, never combined: R02 says an unverified run is
  never presented as green);
- :func:`ready_closing_line` — the final-sentence pair of the
  "ready for human review" evidence comment (same honesty rule, comment
  surface);
- :func:`assert_ready_invariants` — the iron checks a service must pass
  before it may finalize: a terminal/superseded run never finalizes; a
  ready run carries a review bound to the exact candidate sha (ADR-0008);
  an unverified ready reason never claims checks passed;
- :func:`verified_verdict` / :func:`verification_bound_sha` — the one
  reading of a persisted verification fragment (both historical key
  spellings tolerated).

Import boundary (the enforceable half of R27 today): this module is core —
it must never import from ``forge.integrations.*`` or ``forge.gateway.*``
(``tests/test_consistency.py`` parses the imports), so the finalization
invariants stay provider-neutral and the provider packages stay pluggable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from forge.runs.verification import (
    STATUS_NOT_CONFIGURED,
    STATUS_PASSED,
    STATUS_UNVERIFIED,
    VerificationResult,
)

__all__ = [
    "ReadyInvariantError",
    "assert_ready_invariants",
    "ready_closing_line",
    "ready_evidence",
    "ready_reason",
    "verification_bound_sha",
    "verified_verdict",
]


class ReadyInvariantError(RuntimeError):
    """A finalization path tried to make a run ready while an iron check
    failed (ADR-0008 sha binding, R02 honesty, or a superseded/terminal
    target). Raised, never repaired — the caller's crash handling parks the
    run visibly instead of shipping a dishonest READY."""


#: The verdict word a reviewer uses to flag merge-worthy doubts.
REVIEW_CONCERNS = "concerns"

#: R02: an unverified ready reason ALWAYS starts with this prefix — the
#: human must be able to grep one token and see every not-actually-green
#: run. The detail after the prefix is provider-situational ("no
#: verification profile configured", "no CI configured").
UNVERIFIED_PREFIX = "unverified — "

#: The verified, no-concerns ready tail — one source (R27). Providers used
#: to spell three variants of this ("merge is a human decision" with and
#: without the "checks passed; " clause); now every verified ready says
#: exactly this.
CHECKS_PASSED_TAIL = "checks passed; merge is a human decision"

#: The concerns tail — a reviewer's doubts never block, they inform (the
#: merge decision stays human, ADR-0003).
CONCERNS_TAIL = "review raised concerns — merge is a human decision"

#: The unverified, no-concerns tail: the honest form. "checks passed" is
#: reserved for verified runs — an unverified run NEVER says it (R02).
MERGE_HUMAN_TAIL = "merge is a human decision"

#: The honest verification vocabulary (the non-``passed`` statuses from
#: :mod:`forge.runs.verification`): every value here reaches the human
#: labeled as what it is.
_HONEST_STATUSES = frozenset(
    {
        "pending",
        "failed",
        "unknown",
        STATUS_NOT_CONFIGURED,
        STATUS_UNVERIFIED,
    }
)

#: States a run never finalizes FROM/INTO — a superseded or terminal run is
#: dead; "finalizing" it would resurrect it past a cancel/repair decision.
_TERMINAL_FINALIZE_STATUSES = frozenset({"blocked", "failed", "cancelled", "superseded"})

#: The only status this module's iron checks govern.
READY_STATUS = "ready_for_human"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ready_evidence(
    verified: bool,
    tested_oid: str,
    producer: str,
    summary: str = "",
    *,
    surface: Iterable[Mapping[str, Any]] = (),
    status: str | None = None,
) -> dict:
    """The unified verification evidence fragment (R02, one shape per R27).

    Every provider records the SAME dict under
    ``flow_runs.evidence["verification"]`` — the
    :class:`~forge.runs.verification.VerificationResult` shape with keys
    ``status`` / ``tested_oid`` / ``observed_at`` / ``producer`` (plus the
    optional ``summary`` / ``surface`` detail). GitHub used to hand-roll a
    variant keyed ``candidate_sha`` with a ``checks`` list; the drift is
    why this function exists.

    ``verified`` picks the honest default status (``passed`` vs
    ``unverified``); ``status`` overrides for the other honest no-CI form
    (``not_configured``). Contradictory inputs raise — an honest label is
    the invariant, and a caller asking for ``verified=True`` with a
    non-passed status is a bug, not a nuance.
    """
    if status is None:
        status = STATUS_PASSED if verified else STATUS_UNVERIFIED
    if verified and status != STATUS_PASSED:
        raise ReadyInvariantError(
            f"verified=True is only ever the {STATUS_PASSED!r} verdict, got {status!r}"
        )
    if not verified and status == STATUS_PASSED:
        raise ReadyInvariantError("verified=False must never record a 'passed' verdict (R02)")
    verdict = VerificationResult(
        status,
        tested_oid,
        _utc_now_iso(),
        producer,
        summary,
        tuple(dict(entry) for entry in surface),
    )
    return verdict.as_evidence()


def ready_reason(verified: bool, review_verdict: str, summary: str = "") -> str:
    """The ``ready_for_human`` transition reason — one source (R27).

    Rules encoded here (and nowhere else):

    - an unverified run's reason starts with ``unverified — `` (the detail
      after the prefix is the caller's situational ``summary``);
    - an unverified run NEVER says "checks passed" (R02) — its tail is the
      plain "merge is a human decision";
    - a verified run with no concerns says
      "checks passed; merge is a human decision" — pipeline success is
      reported, the merge decision stays human (ADR-0003);
    - reviewer concerns never block: the tail becomes
      "review raised concerns — merge is a human decision".

    Parts are joined with " · " in prefix-then-tail order.
    """
    parts: list[str] = []
    if not verified:
        detail = " ".join(str(summary).split())
        parts.append(UNVERIFIED_PREFIX + detail)
    if review_verdict == REVIEW_CONCERNS:
        parts.append(CONCERNS_TAIL)
    elif verified:
        parts.append(CHECKS_PASSED_TAIL)
    else:
        parts.append(MERGE_HUMAN_TAIL)
    return " · ".join(parts)


def ready_closing_line(verified: bool) -> str:
    """The closing sentence of the "ready for human review" comment.

    Same honesty rule as :func:`ready_reason`, comment surface: a verified
    run says its checks passed for the exact SHA; an unverified run is
    labeled **unverified** and never implied green (R02).
    """
    if verified:
        return "All checks passed for this exact SHA. Merging is a human decision."
    return (
        "This run is **unverified** — no verification profile is configured "
        "(pipeline success only). Merging is a human decision."
    )


def verification_bound_sha(verification: Mapping[str, Any] | None) -> str:
    """The commit oid a persisted verification fragment is bound to.

    Tolerates both historical key spellings — the unified ``tested_oid``
    (GitLab/Azure, and everything written through :func:`ready_evidence`)
    and GitHub's pre-consolidation ``candidate_sha`` — so resume paths
    keep reading evidence written before ADR-0027.
    """
    if not isinstance(verification, Mapping):
        return ""
    return str(verification.get("tested_oid") or verification.get("candidate_sha") or "")


def verified_verdict(verification: Mapping[str, Any] | None, candidate_sha: str) -> bool:
    """R02: a recorded verdict counts as verified only when it is a
    ``passed`` verdict bound to THIS candidate commit (ADR-0008: a verdict
    approves a specific commit, never a branch, never a neighbour sha)."""
    if not isinstance(verification, Mapping):
        return False
    return (
        str(verification.get("status") or "") == STATUS_PASSED
        and verification_bound_sha(verification) == candidate_sha
    )


def assert_ready_invariants(
    run_status: str,
    evidence: Mapping[str, Any] | None,
    candidate_sha: str,
    reviewed_sha: str | None,
    *,
    reason: str | None = None,
) -> None:
    """The iron finalization checks — call before the ready transition.

    Raises :class:`ReadyInvariantError` when any of these fails:

    1. **Superseded/terminal runs never finalize** — ``run_status`` is the
       status being finalized into (callers pass ``ready_for_human``); a
       terminal/superseded target is a resurrected dead run.
    2. **Ready requires a reviewed sha equal to the candidate sha**
       (ADR-0008) — the recorded review must approve THIS commit; a missing
       or mismatched review sha never ships.
    3. **A verdict for another commit never finalizes** — the verification
       fragment, when present, must be bound to the candidate (both
       historical key spellings tolerated via
       :func:`verification_bound_sha`).
    4. **Unverified never says "checks passed"** (R02) — when the recorded
       verdict is any honest non-``passed`` status and the reason is
       supplied, the reason must not claim checks passed.

    ``evidence`` is the run's evidence mapping; only its ``verification``
    fragment is consulted, and a missing/empty fragment is tolerated (a
    crash-resume may legitimately re-drive a run whose verification
    fragment was never recorded — checks 1/2 still apply).
    """
    if run_status in _TERMINAL_FINALIZE_STATUSES:
        raise ReadyInvariantError(
            f"superseded/terminal runs never finalize (refused status {run_status!r})"
        )
    if run_status != READY_STATUS:
        raise ReadyInvariantError(
            f"assert_ready_invariants governs {READY_STATUS!r}, got {run_status!r}"
        )
    if not candidate_sha:
        raise ReadyInvariantError("ready_for_human without a candidate sha (ADR-0008)")
    if not reviewed_sha:
        raise ReadyInvariantError("ready_for_human without a recorded review sha (ADR-0008)")
    if reviewed_sha != candidate_sha:
        raise ReadyInvariantError(
            "ready_for_human with the review bound to a different sha: reviewed "
            f"{reviewed_sha[:8]} != candidate {candidate_sha[:8]} (ADR-0008)"
        )

    verification = evidence.get("verification") if isinstance(evidence, Mapping) else None
    if isinstance(verification, Mapping) and verification:
        bound = verification_bound_sha(verification)
        if bound and bound != candidate_sha:
            raise ReadyInvariantError(
                "ready_for_human with a verification verdict for a different commit: "
                f"tested {bound[:8]} != candidate {candidate_sha[:8]} (ADR-0008)"
            )
        status = str(verification.get("status") or "")
        if status and status != STATUS_PASSED:
            if status not in _HONEST_STATUSES:
                raise ReadyInvariantError(f"unknown verification status {status!r}")
            if reason is not None and "checks passed" in reason:
                raise ReadyInvariantError(
                    "unverified ready reason must never claim checks passed (R02)"
                )
