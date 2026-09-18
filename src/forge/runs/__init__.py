"""Durable M2 run loop (ADR-0004): issue → gate → MR → CI → ready.

Public surface:

- :class:`forge.runs.service.RunService` — owns the whole advance loop, with
  constructor-injected factory agents (LLM-driven by default).
- :func:`forge.runs.reconciler.run_reconciler` — the poll-based CI/harness
  reconciler.
- :mod:`forge.runs.backends` — the ADR-0015 pluggable implementer backends
  (``builtin`` and ``ci_harness``).
- :mod:`forge.runs.candidate` — the ADR-0016 CandidateBundle and the trusted
  ``git diff`` artifact parser.
- :mod:`forge.runs.publisher` — the ADR-0016 trusted publisher: the single
  validation → publication boundary for every backend candidate.
- :mod:`forge.runs.stubs` — deterministic planner/implementer/reviewer
  stand-ins used by tests and offline runs.
- :mod:`forge.runs.ci_contract` — the ADR-0008 quality contract and CI
  failure classification.
- :mod:`forge.runs.revival` — terminal-failure classification and the
  Tier-1 auto-revive / Tier-2 ``/retry`` revival of dead runs.
- :mod:`forge.runs.admission` — the ADR-0018 §3 pre-spend admission check.
- :mod:`forge.runs.verification` — the F19 verification profile.
- :mod:`forge.runs.spec` — the executable RunSpec (R04, ADR-0018 §1): the
  typed frozen input every post-approval leg consumes, digest-verified on
  every read.
"""

from forge.runs.admission import AdmissionDecision, approvers_for, check_admission
from forge.runs.backends import (
    CITharnessBackend,
    BuiltinBackend,
    HarnessOutcome,
    ImplementerBackend,
    build_backend,
    is_harness_backend,
)
from forge.runs.candidate import (
    CandidateBundle,
    CandidateError,
    ChangeManifestEntry,
    HarnessUsage,
    attempt_base_for,
    bundle_from_changeset,
    parse_unified_diff,
)
from forge.runs.ci_contract import classify_failure, evaluate_quality_contract
from forge.runs.publisher import FenceCheck, PublishResult, publish_candidate
from forge.runs.reconciler import run_reconciler
from forge.runs.revival import classify_terminal_failure, revival_backoff_seconds
from forge.runs.service import GATE_TTL_SECONDS, RunService, execute_run_command
from forge.runs.spec import EXECUTABLE_SPEC_SCHEMA_VERSION, ExecutableRunSpec, SpecInvalid
from forge.runs.stubs import (
    StubImplementer,
    StubPlanner,
    StubReviewer,
    factory_branch,
    plan_digest_of,
    short_run_id,
)
from forge.runs.verification import VerificationProfile
from forge.runs.verification import evaluate as evaluate_verification

__all__ = [
    "AdmissionDecision",
    "BuiltinBackend",
    "CITharnessBackend",
    "CandidateBundle",
    "CandidateError",
    "ChangeManifestEntry",
    "EXECUTABLE_SPEC_SCHEMA_VERSION",
    "ExecutableRunSpec",
    "FenceCheck",
    "GATE_TTL_SECONDS",
    "HarnessOutcome",
    "HarnessUsage",
    "ImplementerBackend",
    "PublishResult",
    "RunService",
    "SpecInvalid",
    "StubImplementer",
    "StubPlanner",
    "StubReviewer",
    "VerificationProfile",
    "attempt_base_for",
    "approvers_for",
    "build_backend",
    "bundle_from_changeset",
    "check_admission",
    "classify_failure",
    "classify_terminal_failure",
    "execute_run_command",
    "evaluate_quality_contract",
    "evaluate_verification",
    "factory_branch",
    "is_harness_backend",
    "parse_unified_diff",
    "plan_digest_of",
    "publish_candidate",
    "revival_backoff_seconds",
    "run_reconciler",
    "short_run_id",
]
