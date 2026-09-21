"""The explicit discovery stage before planning (DSC epic).

A plan built without repository evidence is a guess with steps. This
module makes discovery a first-class stage — run BEFORE planning, on
the immutable snapshot set, through the sandboxed CI execution profile
— and makes its outputs machine-checkable:

- :class:`DiscoveryRun` — the stage's own durable record: evidence
  bundle, open questions, spend allowance, and loud blocking when a
  CRITICAL question would otherwise be defaulted away.
- :func:`fast_path_plan_marked` — the fast path, VISIBLY marked as not
  repository-researched (no evidence-backed label without evidence).
- :func:`dispatch_target` — discovery dispatches through the existing
  CI execution profile, never the privileged API process.
- :func:`validate_plan_citations` — every plan citation must resolve
  to repository + OID + path.
- :func:`plan_digest` / :func:`structured_plan` — the evidence-backed
  plan as a canonical, digestible object whose steps are never
  truncated for a human summary.
- :class:`ProbeRequest` / :func:`validate_probe` — bounded, NAMED
  verification probes from the trusted test executor; no arbitrary
  shell, budget enforced.
- :func:`classify_baseline_failure` — baseline failures recorded
  separately from candidate regressions, and environment breakage
  never blamed on the code.

Pure stdlib and frozen throughout.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

__all__ = [
    "DiscoveryRun",
    "ProbeRequest",
    "classify_baseline_failure",
    "dispatch_target",
    "fast_path_plan_marked",
    "plan_digest",
    "structured_plan",
    "validate_plan_citations",
    "validate_probe",
]


@dataclass(frozen=True)
class DiscoveryRun:
    """One explicit discovery stage, run BEFORE planning.

    Discovery has its own identity and durable evidence bundle — it is
    not something planning does "on the way". Every transition returns
    a NEW instance (the record is frozen; history is never rewritten).

    ``waiting_question`` releases the runner: an open question is
    paid-for silence, and holding an execution slot while a human
    thinks is pure waste — the CALLER releases the runner, and
    re-dispatches when the question resolves. A COMPLETED discovery's
    evidence bundle is DURABLE: a restart resumes the existing result
    (:meth:`resume`) instead of paying the probes again. Unresolved
    CRITICAL questions BLOCK planning (:meth:`block`) rather than
    letting the planner invent defaults.
    """

    discovery_id: str
    work_id: str
    snapshot_set_digest: str
    status: str = "pending"
    evidence_bundle: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    spend_allowance_calls: int = 20
    block_reason: str = ""

    def _with(self, **changes: object) -> DiscoveryRun:
        return replace(self, **changes)  # type: ignore[arg-type]

    def start(self) -> DiscoveryRun:
        """pending → running; the stage begins spending its allowance."""
        if self.status != "pending":
            raise ValueError(f"start() requires status 'pending', got {self.status!r}")
        return self._with(status="running")

    def record_evidence(self, evidence_id: str) -> DiscoveryRun:
        """Append one evidence record id to the durable bundle.

        Only a RUNNING discovery records evidence — a waiting or
        blocked one has no runner observing anything. Recording the
        same id twice keeps one entry: the bundle is the SET of
        evidence behind the plan, not a log of attempts.
        """

        if self.status != "running":
            raise ValueError(f"record_evidence() requires status 'running', got {self.status!r}")
        if evidence_id in self.evidence_bundle:
            return self
        return self._with(evidence_bundle=(*self.evidence_bundle, evidence_id))

    def raise_question(self, question_id: str) -> DiscoveryRun:
        """→ waiting_question; the caller RELEASES the runner.

        A question the model cannot answer is exactly the moment to
        stop spending: the status flips so the caller can free the
        runner, and the durable record keeps the question open.
        Further questions may stack while waiting.
        """

        if self.status not in ("running", "waiting_question"):
            raise ValueError(
                f"raise_question() requires status 'running' or 'waiting_question', "
                f"got {self.status!r}"
            )
        return self._with(
            status="waiting_question",
            open_questions=(*self.open_questions, question_id),
        )

    def resolve_question(self, question_id: str) -> DiscoveryRun:
        """Drop an answered question; back to running when none remain.

        While other questions are still open the run keeps waiting —
        one answer does not unblock the others. The last resolution
        returns the stage to running (the caller re-dispatches the
        runner it released).
        """

        if self.status != "waiting_question":
            raise ValueError(
                f"resolve_question() requires status 'waiting_question', got {self.status!r}"
            )
        if question_id not in self.open_questions:
            raise ValueError(f"resolve_question() got unknown question {question_id!r}")
        remaining = tuple(q for q in self.open_questions if q != question_id)
        status = "running" if not remaining else "waiting_question"
        return self._with(status=status, open_questions=remaining)

    def complete(self) -> DiscoveryRun:
        """running → complete; the evidence bundle becomes durable."""
        if self.status != "running":
            raise ValueError(f"complete() requires status 'running', got {self.status!r}")
        return self._with(status="complete")

    def block(self, reason: str) -> DiscoveryRun:
        """→ blocked, recording WHY; planning stops here.

        Blocking is louder and cheaper than inventing a default: an
        unresolved CRITICAL question halts the road to planning until
        a human resolves it, instead of the planner guessing an answer
        the evidence never supported.
        """

        if self.status in ("complete", "blocked"):
            raise ValueError(f"block() cannot run from status {self.status!r}")
        if not reason.strip():
            raise ValueError("block() requires a reason; a silent block helps nobody")
        return self._with(status="blocked", block_reason=reason)

    @classmethod
    def resume(cls, completed: DiscoveryRun) -> DiscoveryRun:
        """Restart of a COMPLETED discovery: the existing result, free.

        Discovery is paid for once. A restart resumes the durable
        evidence bundle with status complete instead of re-running
        probes against the allowance. Resuming an UNFINISHED discovery
        is refused — that is a re-RUN, not a resume, and it must pay
        again.
        """

        if completed.status != "complete":
            raise ValueError(f"resume() requires a completed discovery, got {completed.status!r}")
        return cls(
            discovery_id=completed.discovery_id,
            work_id=completed.work_id,
            snapshot_set_digest=completed.snapshot_set_digest,
            status="complete",
            evidence_bundle=completed.evidence_bundle,
            open_questions=completed.open_questions,
            spend_allowance_calls=completed.spend_allowance_calls,
        )


def fast_path_plan_marked(summary: str) -> dict:
    """The fast-path plan, VISIBLY marked as un-researched.

    Some tasks legitimately skip discovery — a one-line rename does
    not need repository research. What is never legitimate is
    presenting that shortcut as equivalent: ``repository_researched``
    is False on the face of the plan, so a normal customer task cannot
    get an evidence-backed label without discovery evidence.
    """

    return {
        "schema": "forge.plan.fast-path/1",
        "summary": summary,
        "repository_researched": False,
    }


def dispatch_target() -> str:
    """Where discovery work executes: the existing CI execution profile.

    Discovery runs model-driven probes over untrusted repository
    content; the privileged API process is the one place they must
    never run. Dispatching through the CI execution profile reuses the
    existing sandboxed, budgeted, observable execution path instead of
    growing a second one.
    """

    return "ci_execution_profile"


def validate_plan_citations(plan_steps: list[dict], evidence: dict[str, dict]) -> list[str]:
    """Machine-check every citation in a plan; return the violations.

    A citation must resolve to repository + OID + path: an unknown
    evidence id, an empty path, or a missing ``source_oid`` is a
    violation naming the step and the reference. Paths are validated
    OUTSIDE the model (existence, read-scope) — this check only pins
    that the citation RESOLVES at all.
    """

    violations: list[str] = []
    for step in plan_steps:
        step_id = step.get("step_id", "<unnamed>")
        for ref in step.get("evidence_refs", []):
            record = evidence.get(ref)
            if record is None:
                violations.append(f"step {step_id}: unknown evidence id {ref!r}")
                continue
            if not record.get("path"):
                violations.append(f"step {step_id}: evidence {ref!r} has an empty path")
            if not record.get("source_oid"):
                violations.append(f"step {step_id}: evidence {ref!r} is missing source_oid")
    return violations


def plan_digest(plan: dict) -> str:
    """sha256 over the plan's canonical JSON.

    Canonical form (sorted keys, tight separators) means equal plans
    hash equal regardless of dict ordering — the digest is what a run
    record pins and what any change to the plan invalidates.
    """

    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def structured_plan(
    summary: str,
    steps: list[dict],
    assumptions: list[str],
    unknowns: list[str],
    decision_requests: list[str],
) -> dict:
    """The evidence-backed plan: full steps, summary kept SEPARATE.

    The executable structured plan is never truncated to fit a human
    summary — ``summary`` is one field, the complete ``steps`` (with
    their citations) another. Assumptions and unknowns are stated
    rather than smoothed over, and decision requests travel WITH the
    plan instead of being lost in chat.
    """

    return {
        "schema": "forge.plan.evidence-backed/1",
        "summary": summary,
        "steps": steps,
        "assumptions": assumptions,
        "unknowns": unknowns,
        "decision_requests": decision_requests,
    }


@dataclass(frozen=True)
class ProbeRequest:
    """One NAMED probe from the trusted test executor.

    ``command`` names a whitelisted probe command, never an arbitrary
    shell line — the model requests ``run_migration_check``, not
    ``curl ... | sh``. ``budget_calls`` is what this probe claims from
    the approved discovery budget; ``reason`` records WHY the probe is
    needed (auditable spend, not curiosity).
    """

    probe_id: str
    command: str
    reason: str
    budget_calls: int = 1


def validate_probe(
    request: ProbeRequest, allowed_commands: set[str], approved_budget_calls: int
) -> tuple[bool, str]:
    """Whether a probe may run; ``(ok, reason)``, fail closed.

    The command must be one of the NAMED probes the trusted test
    executor ships (no arbitrary shell), and the probe's claimed calls
    must fit the approved budget — a probe claiming more than
    everything approved is refused outright, not clipped. A probe
    claiming nothing (``budget_calls < 1``) is refused too: budget
    accounting cannot divide by a nonsense claim.
    """

    if request.command not in allowed_commands:
        return False, f"command {request.command!r} is not an allowed named probe"
    if request.budget_calls < 1:
        return False, "budget_calls must be at least 1"
    if request.budget_calls > approved_budget_calls:
        return False, (
            f"probe claims {request.budget_calls} calls, approved budget is {approved_budget_calls}"
        )
    return True, "ok"


#: stderr markers that mean the ENVIRONMENT broke — the dependency
#: never came up, the network was down — and the code under test is
#: not to blame.
_ENVIRONMENT_MARKERS = (
    "connection refused",
    "connection reset",
    "timeout",
    "timed out",
    "network unreachable",
    "no such host",
)


def classify_baseline_failure(exit_code: int, stderr: str) -> dict:
    """Baseline failure vs environment failure — never blame the code.

    The BASELINE run failing is not a candidate regression, and is
    recorded separately from one. But first it is classified: stderr
    carrying network/dependency markers (``connection refused``,
    ``timeout``, ...) means the ENVIRONMENT broke, and environment
    failures are not attributed to the code under test at all.
    """

    lowered = (stderr or "").lower()
    for marker in _ENVIRONMENT_MARKERS:
        if marker in lowered:
            return {
                "class": "environment_failure",
                "detail": f"environment marker {marker!r} in stderr",
            }
    return {"class": "baseline_failure", "detail": f"exit code {exit_code}"}
