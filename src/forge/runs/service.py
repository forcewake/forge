"""RunService — the durable M2 advance loop (ADR-0004).

Owns the whole run lifecycle: ``@forge /implement`` on an issue creates a
durable FlowRun and walks it ``accepted → preflight → planning →
waiting_approval``; an approver's ``@forge /go <run-id>`` consumes the human
gate and advances ``proposing → validating → committing → ensuring_draft_mr →
waiting_ci``. The :mod:`forge.runs.reconciler` then drives ``waiting_ci → … →
ready_for_human`` by polling pipelines.

Since M2-1 the planner/implementer/reviewer behind these steps are real LLM
agents (ADR-0014), constructor-injected with the LLM-driven defaults; tests
inject stubs/fakes. Since M2-2 the implementer step itself is pluggable
(ADR-0015): the ``builtin`` backend keeps the synchronous propose→validate→
commit path, while ``ci_harness`` delegates implementation to a coding
harness running as a job in the target project's CI — the run parks durably
in ``waiting_harness`` and the reconciler polls it, adopting the result only
after verifying the real branch head SHA. CI verdicts go through the
ADR-0008 quality contract with failure classification: only *code* failures
trigger the bounded repair loop (``evaluating_ci → proposing → … →
waiting_ci``, at most ``FORGE_MAX_COMMIT_CYCLES - 1`` repairs) — and never
on harness runs, which make no forge-side LLM calls after the gate.
Infrastructure, config and unknown-evidence failures block the run instead
of burning model calls. Every model call lands in the ``llm_calls`` ledger
(ADR-0013) and the run accumulates evidence (plan, review, pipeline) in
``flow_runs.evidence``.

Durability rules (ADR-0005): every transition goes through
:class:`forge.durable.Controller` (which journals an outbox row atomically)
and commits before the next step, so a crash between any two steps leaves a
consistent, observable state. External writes (commit, MR, issue notes) are
journaled intent-first in ``action_log``. Unknown outcomes block or fail the
run — the service never blind-retries a write.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.sqlite import insert as sqlite_dialect_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.adaptive import continuation
from forge.adaptive.admission import QUEUED_STATUSES
from forge.adaptive.admission import AdmissionPolicy as FairUsePolicy
from forge.adaptive.admission import check_admission as check_fair_use
from forge.adaptive.admission import (
    NativeProbe,
    NativeStatus,
    clear_native_start_intent,
    definite_start_refusal,
    execution_capacity_comment,
    lease_snapshot,
    open_lease_for_run,
    record_native_handle,
    record_native_start_intent,
    reconcile_draining,
    release_lease_with_evidence,
    try_acquire_lease,
)
from forge.adaptive.credential_broker import (
    CREDENTIAL_DELIVERY_REF_VARIABLE,
    CREDENTIAL_DELIVERY_REDEEM_VARIABLE,
    CredentialBroker,
    CredentialDeliveryPlan,
    EnvBroker,
    delivery_plan,
)
from forge.adaptive.pause_fence import pause_fence_decision
from forge.adaptive.project_credentials import (
    CredentialRefusal,
    ProjectCredentialRegistry,
    binding_subject_of_run,
    provider_route_for_driver,
    registry_from_env,
)
from forge.adaptive.revisions import (
    ACTIVE_PLAN_KEY,
    APPROVED_INPUT_KEY,
    CLARIFICATION_CLASS,
    IN_SCOPE_CORRECTION_CLASS,
    MATERIAL_CHANGE_CLASS,
    REQUEST_CLARIFICATION_OPEN,
    REQUEST_CONFLICTING,
    REQUEST_DELETED_DISCUSSION,
    REQUEST_DISPATCHED,
    REQUEST_MATERIALIZED,
    REQUEST_RECORDED,
    REQUEST_REFUSED_UNAUTHORIZED,
    REQUEST_STAGED,
    REQUEST_STALE_HEAD,
    REQUEST_WINDOW_CLOSED,
    REVISION_EXECUTOR_DIGEST_KEY,
    RevisionRebindRefused,
    ReviewFeedbackRefused,
    ReviewFeedbackRequest,
    classify_review_feedback,
    executor_digest_document,
    head_binding_guard,
    mark_review_feedback_request,
    parse_review_feedback_note,
    read_review_feedback_requests,
    record_review_feedback_request,
    referenced_paths_of,
    refused_wip_reuse,
    resolve_approved_input,
    review_feedback_requests_of,
    review_feedback_summary_section,
    stage_review_correction,
)
from forge.config import ForgeConfig, Settings, parse_budget_profiles
from forge.durable import (
    DEFAULT_SETTLE_WINDOW_SECONDS,
    TRANSITION_EVENT_TYPE,
    ActionLog,
    Controller,
    FlowRun,
    FlowStatus,
    GateApproval,
    GateAlreadyConsumed,
    LLMCall,
    MRReservation,
    Outbox,
    PublicationIntent,
    RunNotFound,
    RunSpec,
    SettleDecision,
    StepRun,
    UsageReceipt,
    as_aware_utc,
    build_source_event_id,
    classify_probe,
    commit_matches,
    complete_intent,
    consume_approval,
    due_intents,
    is_valid,
    OPEN_STATES,
    ProbeObservation,
    ProbeVerdict,
    record_approval,
    settle_negative_probe,
    settle_state_record,
)
from forge.durable.budgets import (
    BUDGET_EXHAUSTED,
    BudgetGuard,
    BudgetLimits,
    budget_block_reason,
    open_budget,
    reconcile_harness_receipt,
    resolve_budget_limits,
)
from forge.durable.controller import TERMINAL_STATUSES, InvalidTransition
from forge.factory.implementer import IMPLEMENTER_TIER, LLMImplementer
from forge.factory.llm import LLMClient, LLMError, LLMResponseError
from forge.factory.planner import PLAN_SUMMARY_CHARS, LLMPlanner
from forge.factory.reviewer import LLMReviewer
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.harnesses.brief_envelope import build_brief_envelope
from forge.orchestrator.project_config import ConfigReadResult, read_project_config
from forge.policy.evidence import EvidencePolicy
from forge.repository import (
    ChangesetWriter,
    MaterializationError,
    WriteOutcome,
    changeset_from_document,
    changeset_to_document,
    validate_changeset,
)
from forge.repository.writer import BranchDriftError
from forge.runs.admission import check_admission
from forge.runs.backends import (
    BackendStartSpec,
    HarnessOutcome,
    build_backend,
    fetch_git_base,
    is_harness_backend,
)
from forge.runs.checkpoints import load_step_output, record_step_output, step_input_digest
from forge.runs.consistency import (
    assert_ready_invariants,
    ready_closing_line,
    ready_evidence,
    ready_reason,
    verified_verdict,
)
from forge.runs.candidate import AttemptContext
from forge.runs.ci_contract import classify_failure
from forge.runs.execution_profile import derive_from_reader
from forge.runs.harness_selection import (
    BudgetCeilings,
    HarnessSelection,
    advance_harness_fallback,
    compile_harness_selection,
    current_driver,
    implementation_block,
    resolve_available_drivers,
    resolve_preference,
    selection_from_spec_document,
    SHIPPED_DRIVERS,
    validate_preference,
)
from forge.runs.publisher import publish_candidate
from forge.runs.revival import (
    RECONCILE_RE,
    STATUS_RE,
    WHY_BLOCKED_RE,
    RetryRefusalCode,
    RetryRejection,
    build_retry_context,
    begin_revival_attempt,
    claim_attempt_dispatch,
    classify_retryability,
    collect_status_snapshot,
    evaluate_attempt_recovery,
    evaluate_config_blocks,
    evaluate_revivals,
    find_revival_attempt,
    format_reconcile_reply,
    format_status_reply,
    has_active_run,
    intents_for_run,
    open_revival_attempt,
    resolve_retry_target,
    resolve_status_target,
    retry_delivery_key,
    retry_in_flight_rejection,
    retry_rejection,
    terminalize_failure,
    why_blocked_reply,
)
from forge.runs import revival
from forge.runs.revival import RevivalInFlight
from forge.runs.spec import (
    EXECUTABLE_SPEC_SCHEMA_VERSION,
    ExecutableRunSpec,
    SpecInvalid,
    canonical_json_digest,
    load_verified_spec,
    task_text_digest,
)
from forge.runs.stubs import factory_branch, plan_digest_of
from forge.runs.verification import (
    PRODUCER_GITLAB_PIPELINE,
    VerificationProfile,
    waived_conclusions_from_settings,
)
from forge.runs.verification import evaluate as evaluate_verification

logger = logging.getLogger(__name__)

#: How long a recorded gate approval stays consumable (ADR-0009 expiry).
GATE_TTL_SECONDS = 3600

#: ADR-0027: the GitLab situational detail after the shared "unverified — "
#: ready-reason prefix (forge.runs.consistency) — an empty verification
#: profile judged a green pipeline (R02).
UNVERIFIED_DETAIL = "no verification profile configured"

#: ADR-0018 §1 (F14): schema version of the RunSpec document. The GitLab lane
#: freezes the EXECUTABLE spec (v3, :mod:`forge.runs.spec`) — task text, plan
#: artifact, model route, verification contract and budgets ride beside the
#: digests, and every consumption read verifies the document's digest.
#: A02: the GitHub/Azure lanes freeze and consume the same v3 document; runs
#: created before that upgrade carry v2 rows and park
#: ``blocked(spec_legacy: re-approval required)`` on their next leg
#: (:class:`forge.runs.spec.SpecLegacy`). This historical constant stays 2.
RUN_SPEC_SCHEMA_VERSION = 2

#: ADR-0017 §3: pre-CI states a crashed worker leaves a run in after the gate
#: was consumed. A re-delivered or re-claimed ``/go`` command step does not
#: ignore these — it re-drives the advance leg (the recovery driver).
_RESUMABLE_ADVANCE_STATUSES = frozenset(
    {"proposing", "validating", "committing", "ensuring_draft_mr"}
)

#: R07: pre-gate states a crashed ``/implement`` leaves its OWN run in. A
#: re-delivered or re-claimed ``start_run`` command resumes that run from its
#: persisted plan checkpoint instead of forking a duplicate (which the
#: one-active-run invariant would refuse, stranding the mid-planning run with
#: its gate never opened).
_RESUMABLE_PLAN_STATUSES = frozenset({"preflight", "planning"})

#: Fallback when FORGE_DECISION_TTL_SECONDS is unset (ADR-0018 §2: one week).
_DECISION_TTL_FALLBACK_SECONDS = 7 * 86400

# ----------------------------------------------------------------------
# R37-07 (issue #288): the GitLab dispatch envelope — the lane-resume /
# lane-control contract the GitHub lane dispatches (R32-04) carried over
# to the ci_harness pipeline dispatch, byte-compatible with
# forge.lane_driver's env spelling.
# ----------------------------------------------------------------------

#: The WIP-continuity contract the dispatch SELECTS for the lane — the
#: same three-mode vocabulary the GitHub dispatch carries as the
#: ``lane_resume_mode`` workflow input (``forge.runs.github_service.
#: LANE_RESUME_MODES``) and ``forge.adaptive.continuation.ContinuationMode``
#: emits. The three modes are DISTINCT product actions, never three
#: combinations of accidentally absent variables:
#:
#: - ``fresh`` — the initial dispatch (and a repair cycle that re-implements
#:   from the attempt base): nothing restores accidentally; a missing
#:   checkpoint (404) is normal for it;
#: - ``required`` — retry/revival re-dispatch over a committed checkpoint:
#:   the pre-turn restore of the held checkpoint is REQUIRED (a failed
#:   download halts the lane before any vendor session exists);
#: - ``restart`` — the operator EXPLICITLY discarded the held WIP
#:   (``/retry <run-id> restart``): no download at all.
LANE_RESUME_MODES: frozenset[str] = frozenset({"fresh", "required", "restart"})
LANE_RESUME_MODE_FRESH: str = "fresh"
LANE_RESUME_MODE_REQUIRED: str = "required"
LANE_RESUME_MODE_RESTART: str = "restart"

#: The env spelling ``forge.lane_driver.resume_mode`` reads (dispatched as
#: a pipeline VARIABLE, which GitLab exports into every step's env — the
#: same mapping the GitHub template performs in its driver step):
#: required → the truthy marker, restart → the literal discard marker,
#: fresh/absent → '' (a first run; a 404 restore is normal).
LANE_RESUME_ENV_SPELLING: dict[str, str] = {
    LANE_RESUME_MODE_FRESH: "",
    LANE_RESUME_MODE_REQUIRED: "1",
    LANE_RESUME_MODE_RESTART: "restart",
}

#: The envelope's pipeline variable names (all prefixed FORGE_, exported
#: into the job env by GitLab; the observability mode word rides beside the
#: env spelling so the dispatch ledger can fingerprint the contract).
LANE_RESUME_VARIABLE: str = "FORGE_LANE_RESUME"
LANE_RESUME_MODE_VARIABLE: str = "FORGE_LANE_RESUME_MODE"
LANE_CHECKPOINT_VARIABLE: str = "FORGE_RESUME_CHECKPOINT"
LANE_ATTEMPT_VARIABLE: str = "FORGE_ATTEMPT_GENERATION"
LANE_DECISION_VARIABLE: str = "FORGE_CONTINUATION_DECISION_ID"
LANE_CONTROL_URL_VARIABLE: str = "FORGE_LANE_CONTROL_URL"
LANE_CONTROL_TOKEN_VARIABLE: str = "FORGE_LANE_CONTROL_TOKEN"
#: Q39-02 (#321): the revision-rebind variables — dispatched ONLY when an
#: approved revision is ACTIVE (a revision-bound approved input), mirroring
#: the GitHub dispatch's conditional ``plan_digest`` input. ``FORGE_PLAN_
#: DIGEST`` is the ACTIVE revision's canonical digest (the one the
#: activation CAS switched), ``FORGE_SPEC_DIGEST`` the frozen RunSpec
#: digest, ``FORGE_BRIEF_ENVELOPE_DIGEST`` the approved brief envelope
#: built FROM the resolved approved input (run id + task bytes + the
#: revision's brief TEXT + the spec digest) — the identity the runner
#: re-verifies the consumed ``FORGE_PLAN`` bytes against.
LANE_PLAN_DIGEST_VARIABLE: str = "FORGE_PLAN_DIGEST"
LANE_SPEC_DIGEST_VARIABLE: str = "FORGE_SPEC_DIGEST"
LANE_BRIEF_ENVELOPE_DIGEST_VARIABLE: str = "FORGE_BRIEF_ENVELOPE_DIGEST"

#: The process-env name the CONTROL PLANE deployment carries its own
#: externally-reachable lane-control URL under (the same source the app
#: deployment and the tests use; GitLab-parity of the GitHub lane's repo
#: VARIABLE — the dispatch repeats it into the pipeline variables so the
#: lane's dial-out never depends on a project variable being set).
LANE_CONTROL_URL_ENV: str = "FORGE_LANE_CONTROL_URL"


def task_digest_of(title: str, description: str) -> str:
    """sha256 over the issue text — the task snapshot digest (ADR-0018 §2).

    Bound into the RunSpec and the pending decision at plan time; a differing
    digest at READY time means the issue changed after approval, which is
    noted in the evidence comment instead of silently executed.
    """
    return task_text_digest(title, description)


#: Pipeline statuses that mean "keep waiting" in the reconciler tick.
_CI_ACTIVE_STATUSES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
)

#: ``/go <run-id>`` — the full 32-hex run id from the plan footer OR an
#: unambiguous prefix of at least 8 hex chars (the plan heading shows the
#: 8-char form; LIVE 2026-09-21: operators typing the short form fell
#: through the old ``{32}``-only regex and the note was silently ignored).
_GO_RE = re.compile(r"/go\s+([0-9a-fA-F]{8,32})\b")
_CANCEL_RE = re.compile(r"/cancel(?:\s+([0-9a-f]{8,32})\b)?", re.IGNORECASE)
#: ``/retry [run-id]`` — bare retries the issue's latest failed/blocked run.
_RETRY_RE = re.compile(r"/retry(?:\s+([0-9a-f]{8,32})\b)?", re.IGNORECASE)

#: The journaled kind of a /go refusal reply (one per ignored /go note).
_GO_REFUSAL_KIND = "go_refusal_note"

#: Q39-13 (#332): the journaled kind of a review-feedback reply (one per
#: note id — the same A11 discipline as the /go refusal above).
_REVIEW_FEEDBACK_REPLY_KIND = "review_feedback_note"

#: Q39-13 (#332): the journaled kind of the held-candidate summary note
#: (one per run + candidate sha).
_REVIEW_FEEDBACK_SUMMARY_KIND = "review_feedback_summary"


def _automated_footer() -> str:
    """The closing line every forge-authored note carries."""
    return "\n\n*This is an automated message.*"


def _go_unknown_run_body(requested: str) -> str:
    """The reply to a /go id that matches no run on this issue.

    Names BOTH valid id forms so the operator can self-serve: the full id
    (the plan footer's copy-paste command) and the ≥8-hex unambiguous
    prefix (the plan heading's short form).
    """
    return (
        f"`/go {requested}` matched no run on this issue. Valid forms: the full "
        "32-hex run id (`@forge /go <run-id>`, as in the plan footer) or an "
        "unambiguous prefix of at least 8 hex characters (the plan heading's "
        f"short id).\n\n*This is an automated message.*"
    )


def _go_ambiguous_body(requested: str, candidate_ids: list[str]) -> str:
    """The reply to a /go prefix matching more than one run: list them."""
    listed = "\n".join(f"- `{run_id}`" for run_id in candidate_ids)
    return (
        f"`/go {requested}` matches {len(candidate_ids)} runs on this issue — the "
        f"prefix is ambiguous. Use the full id:\n\n{listed}\n\n"
        "*This is an automated message.*"
    )


def _go_wrong_issue_body(requested: str) -> str:
    return (
        f"`/go {requested[:8]}` targets a run planned on a different issue — each run "
        "is approved on the issue it was planned on (`/status` here shows this "
        "issue's runs).\n\n*This is an automated message.*"
    )


def _go_not_approver_body(author: str) -> str:
    return (
        f"`/go` from @{author} was ignored — gate approval is restricted to the "
        "configured approvers (FORGE_APPROVERS). The run stays at its "
        "gate.\n\n*This is an automated message.*"
    )


def _go_duplicate_body(run_id: str, status: str) -> str:
    return (
        f"`/go` for run `{run_id[:8]}` is a duplicate — the gate was already "
        f"consumed and the run is `{status}`. Nothing to do.\n\n"
        "*This is an automated message.*"
    )


# NXT-01: the /go resolution outcome is TYPED, not a nullable run plus a
# side-channel refusal string. The resolver below returns exactly one of
# these; the consuming path discriminates once and then works with a plain
# ``FlowRun`` — the "logically never None" branches the typechecker could
# not see are now impossible by construction instead of by argument.
@dataclass(frozen=True)
class _ResolvedGo:
    """The identifier narrowed to exactly one run of this project+issue."""

    run: FlowRun


@dataclass(frozen=True)
class _RefusedGo:
    """A /go answered with one operator-visible refusal note.

    ``body`` is the already-composed reply (the ``_go_*_body`` builders);
    ``run_id`` ties the journaled refusal to the run when one was found.
    """

    body: str
    run_id: str | None = None


@dataclass(frozen=True)
class _AdvanceGo:
    """An accepted /go: advance this run via this backend.

    Built inside the resolving session (the evidence read must precede the
    commit that expires it) and consumed after it closes. ``resuming`` is
    the ADR-0017 §3 crash-recovery leg — the gate was already consumed, so
    no transition precedes the advance; ``status`` is the status the run
    was found in (the resume log's honest "found X after a worker crash").
    """

    run_id: str
    backend_name: str
    resuming: bool = False
    status: str = ""


async def _resolve_go_run(
    session: AsyncSession,
    requested: str,
    *,
    project_id: int,
    issue_iid: int | None,
) -> _ResolvedGo | _RefusedGo:
    """Narrow a ``/go`` identifier to exactly one run of this project+issue.

    Provider, project and issue predicates are identical for every
    identifier form (R03/A07 subject scoping): the full 32-hex id is
    fetched directly and then checked against them; an ≥8-hex prefix is
    resolved by a query that carries the same predicates in its WHERE. A
    foreign run therefore reads as unknown, and a same-project run of a
    different issue reads as wrong-issue — never silently adopted.
    """
    if len(requested) == 32:
        run = await session.get(FlowRun, requested)
        if run is None or run.provider != "gitlab" or run.project_id != project_id:
            logger.info("/go references unknown run %s — ignoring", requested[:8])
            return _RefusedGo(_go_unknown_run_body(requested))
        if run.issue_iid != issue_iid:
            logger.info("/go for run %s posted on a different issue — ignoring", run.id[:8])
            return _RefusedGo(_go_wrong_issue_body(requested), run_id=run.id)
        return _ResolvedGo(run)
    # Short id (the plan heading shows the 8-char form): resolve by prefix
    # among THIS issue's runs — provider/project/issue-scoped like every
    # subject lookup (R03/A07). Ambiguous or unmatched is answered with the
    # valid id forms, never silence.
    matches = (
        (
            await session.execute(
                select(FlowRun)
                .where(
                    FlowRun.provider == "gitlab",
                    FlowRun.project_id == project_id,
                    FlowRun.issue_iid == issue_iid,
                    FlowRun.id.like(f"{requested}%"),
                )
                .order_by(FlowRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if len(matches) == 1:
        return _ResolvedGo(matches[0])
    if not matches:
        logger.info("/go id %s matches no run on this issue — ignoring", requested[:8])
        return _RefusedGo(_go_unknown_run_body(requested))
    logger.info(
        "/go prefix %s matches %d runs on this issue — ignoring",
        requested[:8],
        len(matches),
    )
    return _RefusedGo(_go_ambiguous_body(requested, [found.id for found in matches]))


#: Repair-loop log budgets (ADR-0013: bounded repair context).
REPAIR_LOG_PER_JOB_CHARS = 4000
REPAIR_CONTEXT_MAX_CHARS = 12000
REPAIR_MAX_FAILED_JOBS = 3


def budget_enforcement_for_backend(backend: str) -> str:
    """The honest enforcement level of a lane for its frozen budget (R13 §4).

    ``full`` — every model dispatch of the lane is intercepted before the
    provider is contacted (the builtin lane: each call reserves through the
    :class:`~forge.durable.budgets.BudgetGuard`, so calls, tokens AND the
    wall clock are enforced). ``partial`` — CLI harness lanes: forge makes no
    per-call interception (the model calls happen inside the CI job), so only
    the wall clock and the episode count are enforced at dispatch/poll time;
    call/token ceilings are reconciled after the fact from the artifact's
    usage receipt and are never claimed as enforced.
    """
    return "partial" if is_harness_backend(backend) else "full"


def forge_token(settings: Settings) -> str:
    """The token forge acts with: the dedicated bot identity when configured.

    Forge must never speak with a human approver's credentials — its own
    comments (which contain /go instructions) would then come back as
    approver-authored webhooks and self-approve gates.
    """
    if settings.FORGE_BOT_TOKEN is not None:
        return settings.FORGE_BOT_TOKEN.get_secret_value()
    return settings.GITLAB_TOKEN.get_secret_value()


def build_default_agents(
    settings: Settings,
    gitlab: GitLabClient,
    session_factory: async_sessionmaker[AsyncSession],
    budget: "BudgetGuard | None" = None,
) -> tuple[LLMPlanner, LLMImplementer, LLMReviewer]:
    """Construct the real LLM-driven factory agents over one shared client.

    When a run budget is supplied, every model call through the shared
    client reserves against it before dispatch (F22).
    """
    llm = LLMClient(settings=settings, session_factory=session_factory, budget=budget)
    return (
        LLMPlanner(llm, settings=settings),
        LLMImplementer(llm, gitlab=gitlab, settings=settings),
        LLMReviewer(llm, gitlab=gitlab, settings=settings),
    )


async def execute_run_command(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    metadata: dict[str, Any],
) -> None:
    """Execute a ``run_command`` payload with a per-task GitLab client.

    Shared by the worker (queued tasks) and the gateway's no-Redis
    BackgroundTasks fallback, so both paths behave identically.

    E3a: GitHub-subject commands (``provider: github``, ingested by the
    GitHub webhook) dispatch to :mod:`forge.runs.github_service` — the
    FlowRun-backed GitHub gate path; RunService stays GitLab-bound until the
    v0.5 contracts extraction.

    v0.7: ``security_triage`` commands dispatch to
    :mod:`forge.findings.triage` BEFORE the gate machinery — findings are a
    separate subsystem wired into the same durable step runtime (they never
    touch RunService state). ``debug_pipeline`` commands dispatch to
    :mod:`forge.reactive.ci_debug` the same way — the durable pipeline
    failure debugger (factory/ branches are skipped there: the run's own
    repair loop owns those failures).
    """
    if metadata.get("command") == "security_triage":
        from forge.findings.triage import execute_security_command

        if metadata.get("provider") in ("github", "azure_devops"):
            # Provider-neutral durable triage: no GitLab client on the
            # non-GitLab paths (GitHub E3a; Azure DevOps ADR-0024).
            await execute_security_command(settings, forge_config, session_factory, metadata)
            return
        async with GitLabClient(
            base_url=settings.GITLAB_URL,
            token=forge_token(settings),
        ) as gitlab:
            await execute_security_command(
                settings, forge_config, session_factory, metadata, gitlab=gitlab
            )
        return

    if metadata.get("command") == "debug_pipeline":
        from forge.reactive.ci_debug import execute_debug_pipeline_command

        async with GitLabClient(
            base_url=settings.GITLAB_URL,
            token=forge_token(settings),
        ) as gitlab:
            await execute_debug_pipeline_command(
                settings, forge_config, session_factory, metadata, gitlab=gitlab
            )
        return

    if metadata.get("provider") == "azure_devops":
        # AZ-2 (ADR-0024): Azure DevOps-subject commands land on the
        # FlowRun-backed AzureRunService gate path, mirroring the GitHub
        # dispatch above.
        from forge.runs.azure_service import execute_azure_run_command

        await execute_azure_run_command(settings, forge_config, session_factory, metadata)
        return

    if metadata.get("provider") == "github":
        from forge.runs.github_service import execute_github_run_command

        await execute_github_run_command(settings, forge_config, session_factory, metadata)
        return

    async with GitLabClient(
        base_url=settings.GITLAB_URL,
        token=forge_token(settings),
    ) as gitlab:
        service = RunService(
            session_factory=session_factory,
            gitlab=gitlab,
            settings=settings,
            config=forge_config,
        )
        await service.run_command(metadata)


class _DispatchEnvelopeClient:
    """R37-07 (#288): a per-dispatch proxy over the GitLab client that
    appends the lane-resume/control ENVELOPE variables to every pipeline
    the ci_harness backend triggers.

    ``CITharnessBackend.start`` (forge.runs.backends — the shared,
    provider-neutral seam both lanes use) assembles the pipeline variables
    itself; the envelope is GITLAB-dispatch policy, so this proxy injects
    it AT THE PROVIDER BOUNDARY instead of forking the backend or its
    variable list. Everything but ``create_pipeline`` delegates verbatim
    to the inner client — the writer's branch calls, the poll reads and
    the artifact downloads are untouched.
    """

    def __init__(self, inner: GitLabClient, envelope: list[dict[str, str]]) -> None:
        self._inner = inner
        self._envelope = [dict(entry) for entry in envelope]

    async def create_pipeline(
        self,
        project_id: int,
        ref: str,
        variables: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        merged = [dict(entry) for entry in (variables or [])] + [
            dict(entry) for entry in self._envelope
        ]
        return await self._inner.create_pipeline(project_id, ref, variables=merged)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class RunService:
    """Coordinates the factory agents, the controller and GitLab for one run."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gitlab: GitLabClient,
        settings: Settings,
        config: ForgeConfig | None = None,
        writer_class: type[ChangesetWriter] = ChangesetWriter,
        planner: Any | None = None,
        implementer: Any | None = None,
        reviewer: Any | None = None,
        credential_registry: ProjectCredentialRegistry | None = None,
        credential_broker: CredentialBroker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gitlab = gitlab
        self._settings = settings
        self._config = config or ForgeConfig()
        self._writer_class = writer_class
        # NEXT-19 (#207): the credential binding registry + broker the
        # dispatch seam resolves under (defaults: the deployment's
        # FORGE_CREDENTIAL_BINDINGS document, the ambient EnvBroker).
        self._credential_registry = (
            credential_registry if credential_registry is not None else registry_from_env()
        )
        self._credential_broker = (
            credential_broker if credential_broker is not None else EnvBroker()
        )
        if planner is None or implementer is None or reviewer is None:
            default_planner, default_implementer, default_reviewer = build_default_agents(
                settings, gitlab, session_factory
            )
            planner = planner if planner is not None else default_planner
            implementer = implementer if implementer is not None else default_implementer
            reviewer = reviewer if reviewer is not None else default_reviewer
        self._planner = planner
        self._implementer = implementer
        self._reviewer = reviewer
        # Q39-13 (#332): once the MR discussions surface answers 404
        # (an older CE instance, a scoped token), the auxiliary
        # discussion checks degrade for this process instead of
        # hammering the endpoint every pass.
        self._discussions_surface_down = False

    # ------------------------------------------------------------------
    # Entry points (called from gateway router / worker / reconciler)
    # ------------------------------------------------------------------

    async def start_run(
        self,
        project_id: int,
        issue_iid: int,
        issue_title: str,
        issue_description: str,
        author_username: str,
    ) -> str:
        """``@forge /implement``: create the run and park it at the human gate."""
        # One active run per (project, issue): a second /implement while a run
        # is still alive would fork branches and Draft MRs for the same task.
        # Re-delivered webhooks are already collapsed by the gateway dedup —
        # this guard covers two distinct comments (observed live in M3).
        active = await self._find_active_run(project_id, issue_iid)
        if active is not None:
            if active.status in _RESUMABLE_PLAN_STATUSES:
                # R07: a crashed /implement left its own run mid-planning —
                # the re-claimed command step RESUMES it from the persisted
                # plan checkpoint (``_plan_and_publish`` replays the result
                # when the model call already completed; it plans for the
                # first time when the crash preceded any persisted output).
                # Anything else (a stranger's duplicate comment) keeps the
                # refusal below.
                admission = check_admission(
                    self._settings, self._config, project_id, author_username
                )
                if admission.allowed:
                    logger.info(
                        "Run %s found %s after a crash — resuming planning from its checkpoint",
                        active.id[:8],
                        active.status,
                    )
                    await self._plan_and_publish(
                        active.id,
                        project_id=project_id,
                        issue_iid=issue_iid,
                        issue_title=issue_title,
                        issue_description=issue_description,
                        author_username=author_username,
                    )
                    return active.id
                logger.warning(
                    "Run %s is stuck mid-planning and @%s is not admitted — left untouched",
                    active.id[:8],
                    author_username,
                )
            await self._post_journaled_note(
                project_id,
                issue_iid,
                self._active_run_comment(active),
                active.id,
                "duplicate_implement",
            )
            logger.info(
                "/implement on issue !%s ignored — run %s is already %s",
                issue_iid,
                active.id[:8],
                active.status,
            )
            return active.id

        run_id = uuid4().hex

        # R28-23: the fair-use counts are gathered BEFORE the row exists —
        # they describe the world without this candidate run.
        fair_counts = await self._fair_use_counts(project_id, issue_iid, author_username)
        fair_use = check_fair_use(FairUsePolicy.from_env(), **fair_counts)

        try:
            async with self._session_factory() as session:
                controller = Controller(session)
                session.add(
                    FlowRun(
                        id=run_id,
                        project_id=project_id,
                        issue_iid=issue_iid,
                        provider="gitlab",
                        # R28-23: the requesting actor, journaled at creation
                        # so the per-user hourly fair-use dimension has a
                        # durable source (merged, never replaced).
                        evidence={"requested_by": author_username},
                    )
                )
                await controller.transition(run_id, FlowStatus.PREFLIGHT)
                await session.commit()
        except IntegrityError:
            # F12 (ADR-0017): the partial unique index uq_active_run_per_issue
            # is the invariant of last resort — a concurrent /implement won
            # the (project, issue) slot between the active-run check and this
            # insert. Treat it as a duplicate, never surface the raw error.
            existing = await self._find_active_run(project_id, issue_iid)
            if existing is None:
                raise
            await self._post_journaled_note(
                project_id,
                issue_iid,
                self._active_run_comment(existing),
                existing.id,
                "duplicate_implement",
            )
            logger.info(
                "/implement on issue !%s lost the run-creation race — run %s is active",
                issue_iid,
                existing.id[:8],
            )
            return existing.id

        # F16 (ADR-0018 §3): admission before the first paid call. The denial
        # path never constructs a planner prompt — the run is parked as
        # blocked(admission_denied) with a journaled note instead.
        admission = check_admission(self._settings, self._config, project_id, author_username)
        if not admission.allowed:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, f"admission_denied: {admission.reason}"
            )
            await self._post_journaled_note(
                project_id,
                issue_iid,
                self._admission_denied_comment(run_id, author_username),
                run_id,
                "admission_denied",
            )
            logger.warning(
                "Run %s denied admission for @%s: %s",
                run_id[:8],
                author_username,
                admission.reason,
            )
            return run_id

        # R28-23: bounded admission and fair use — ADDITIVE to the F16
        # identity check above (which stays the authority on WHO may
        # spend). This is the capacity half: per-issue attempt cap,
        # per-user hourly rate, per-project WIP bound, queue depth. The
        # denial path parks the run exactly like F16 does — blocked, with
        # a journaled note whose reason the operator can quote — and the
        # planner is never constructed.
        if not fair_use.allowed:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, f"fair_use_denied: {fair_use.reason}"
            )
            await self._post_journaled_note(
                project_id,
                issue_iid,
                self._fair_use_denied_comment(run_id, author_username, fair_use.reason),
                run_id,
                "admission_denied",
            )
            logger.warning(
                "Run %s refused for fair use (@%s): %s — counts %s",
                run_id[:8],
                author_username,
                fair_use.reason,
                fair_use.counts,
            )
            return run_id

        await self._plan_and_publish(
            run_id,
            project_id=project_id,
            issue_iid=issue_iid,
            issue_title=issue_title,
            issue_description=issue_description,
            author_username=author_username,
        )
        return run_id

    async def _plan_and_publish(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_iid: int,
        issue_title: str,
        issue_description: str,
        author_username: str,
    ) -> None:
        """The planning leg: plan (or replay its checkpoint), freeze, publish.

        R07 bounded step ``plan``: the planner's output is persisted under
        ``(run_id, cycle=1, step="plan", input_digest)`` the moment the call
        returns — BEFORE the plan comment, the pending decision or any
        transition is scheduled — so a crashed leg replays the persisted
        plan and never calls the model a second time. A moved input (an
        edited issue, a changed path scope) changes the digest and
        legitimately re-plans.

        Shared by the fresh ``/implement`` path and the interrupted-start
        resume in :meth:`start_run`; every stage it ends with is idempotent
        (see :meth:`_finish_plan_publication`).
        """
        # F22/R13: the run's numeric budget is resolved and opened BEFORE the
        # first paid call (idempotent per run — a resumed leg finds the
        # frozen row). The budget class comes from the harness decision
        # (compiled at plan time, ADR-0023 §2), and the class names a numeric
        # profile resolved AT FREEZE TIME; nothing configured → no ceilings.
        harness_selection = self._compile_harness_selection()
        budget_limits = self._budget_limits_for_class(harness_selection.budget_class)
        if budget_limits is not None:
            async with self._session_factory() as session:
                await open_budget(
                    session,
                    run_id=run_id,
                    wallclock_s=budget_limits.wallclock_s,
                    max_calls=budget_limits.max_calls,
                    max_tokens=budget_limits.max_tokens,
                )
                await session.commit()
        await self._apply_run_budget(run_id)
        # v0.7 monorepo path scoping, A13: the project's `.forge.yml`
        # ``implement.paths`` globs are resolved BEFORE the plan — they shape
        # the plan prompt and are frozen into the RunSpec (with the config
        # read's provenance) that the publisher and the builtin validation
        # enforce. The read is TYPED (R14 pattern): only a provider-confirmed
        # absence earns the documented default profile; an unreadable or
        # invalid config parks the run blocked(config_…) — a read failure
        # must never WIDEN the run's scope, and nothing was paid or
        # committed while it waits (the reconciler retries the read).
        config_read = await read_project_config(self._gitlab, project_id, ref=self._target_branch())
        if config_read.needs_block:
            await self._park_config_blocked(
                run_id,
                project_id=project_id,
                issue_iid=issue_iid,
                config_read=config_read,
                issue_title=issue_title,
                issue_description=issue_description,
                author_username=author_username,
            )
            return
        path_scope = list(config_read.config.implement_paths) if config_read.config else []

        plan_input_digest = step_input_digest(
            {
                "title": issue_title,
                "description": issue_description,
                "path_scope": path_scope,
            }
        )
        checkpoint = await load_step_output(
            self._session_factory,
            run_id=run_id,
            step="plan",
            input_digest=plan_input_digest,
        )
        if checkpoint is not None and str(checkpoint.get("plan") or ""):
            plan = str(checkpoint["plan"])
            digest = str(checkpoint.get("plan_digest") or plan_digest_of(plan))
            task_digest = str(
                checkpoint.get("task_digest") or task_digest_of(issue_title, issue_description)
            )
            logger.info(
                "Run %s replays its persisted plan checkpoint — no second model call",
                run_id[:8],
            )
        else:
            try:
                plan = await self._planner.plan(
                    issue_title,
                    issue_description,
                    flow_run_id=run_id,
                    path_scope=path_scope or None,
                )
            except (LLMError, LLMResponseError) as exc:
                # R13: a budget refusal never contacts the provider and never
                # unblocks by retrying the same run — classify it visibly as
                # blocked(budget_exhausted), not a planning failure.
                if str(exc) == BUDGET_EXHAUSTED:
                    await self._to_terminal(
                        run_id,
                        FlowStatus.BLOCKED,
                        f"{BUDGET_EXHAUSTED}: planner refused — run budget cannot grant a call",
                    )
                else:
                    await self._to_terminal(run_id, FlowStatus.FAILED, f"planning_failed: {exc}")
                raise
            # B11: recompile the selection against the CURRENT plan (the
            # planner's ``last_plan`` is THIS plan) — a reused planner can
            # never leak a previous run's plan into this decision.
            harness_selection = self._compile_harness_selection()
            digest = plan_digest_of(plan)
            task_digest = task_digest_of(issue_title, issue_description)
            # R07 CHECKPOINT FIRST: the paid plan result becomes durable
            # before anything else is scheduled. Everything after this write
            # is replayable; the model call is not.
            await record_step_output(
                self._session_factory,
                run_id=run_id,
                step="plan",
                input_digest=plan_input_digest,
                output={
                    "plan": plan,
                    "plan_digest": digest,
                    "task_digest": task_digest,
                    "summary": self._plan_summary(plan),
                    "files_hint": self._plan_files_hint(),
                },
            )

        await self._finish_plan_publication(
            run_id,
            project_id=project_id,
            issue_iid=issue_iid,
            issue_title=issue_title,
            issue_description=issue_description,
            author_username=author_username,
            plan=plan,
            digest=digest,
            task_digest=task_digest,
            path_scope=path_scope,
            harness_selection=harness_selection,
            budget_limits=budget_limits,
            config_read=config_read,
        )

    async def _finish_plan_publication(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_iid: int,
        issue_title: str,
        issue_description: str,
        author_username: str,
        plan: str,
        digest: str,
        task_digest: str,
        path_scope: list[str],
        harness_selection: HarnessSelection,
        budget_limits: BudgetLimits | None,
        config_read: ConfigReadResult | None = None,
    ) -> None:
        """Publish the plan for approval — every stage idempotent (R07).

        A crashed leg re-entering here finds its earlier stages persisted
        (frozen RunSpec + evidence, the plan-note journal, the pending
        decision) and performs only the missing ones, so the run ends at
        ``waiting_approval`` exactly once, with one plan comment and one
        gate row.
        """
        now = datetime.now(timezone.utc)
        spec_document: dict | None = None
        spec_digest = ""
        base_sha = ""
        plan_refrozen = False
        async with self._session_factory() as session:
            controller = Controller(session)
            run = await self._get_run(session, run_id)
            if run.spec_digest and run.plan_digest == digest:
                # A crashed attempt already froze THIS plan's spec — replay
                # its decision, never a re-derivation from live settings
                # (R04). A digest that differs means the frozen spec belongs
                # to a different (pre-approval, un-executed) plan: the run
                # re-freezes below, exactly like the issue-edit replan.
                spec_digest = run.spec_digest
                base_sha = run.base_sha or ""
            else:
                plan_refrozen = True
                if run.status == FlowStatus.PREFLIGHT.value:
                    await controller.transition(run_id, FlowStatus.PLANNING)
                run.plan_digest = digest
                base_sha = run.base_sha = await self._read_base_sha(project_id)
                plan_summary = self._plan_summary(plan)
                plan_files_hint = self._plan_files_hint()
                # ADR-0015: the backend choice is frozen at run start so the
                # run survives restarts with the backend it was created with.
                # The harness selection rides beside it (ADR-0023): "backend"
                # stays the backend-name string the reconcilers dispatch on.
                run.evidence = _merge_evidence(
                    run.evidence,
                    {
                        "backend": self._backend_name(),
                        "harness_selection": harness_selection.as_document(),
                        "plan": {
                            "digest": digest,
                            "summary": plan_summary,
                            "files_hint": plan_files_hint,
                        },
                        # R13 §4: the honest enforcement record — what the
                        # frozen budget actually enforces on THIS lane, and
                        # which ceilings ride in the spec. Absent when no
                        # finite profile applies.
                        **(
                            {
                                "budget": {
                                    "budget_class": harness_selection.budget_class,
                                    "enforcement": budget_enforcement_for_backend(
                                        self._backend_name()
                                    ),
                                    "max_calls": budget_limits.max_calls,
                                    "max_tokens": budget_limits.max_tokens,
                                    "wallclock_s": budget_limits.wallclock_s,
                                }
                            }
                            if budget_limits is not None
                            else {}
                        ),
                    },
                )
                # F14/R04 (ADR-0018 §1): freeze the EXECUTABLE RunSpec at plan
                # acceptance — before the plan is published. The document
                # carries the task text, plan artifact, model route, path
                # policy, verification contract and budgets; the gate binds
                # its digest, so /go approves exactly the bytes the run will
                # execute.
                # A18: derive the execution profile from the TARGET repo at
                # freeze time — toolchain pins from its own lock, install
                # strategy, honest ci_contract — and freeze its digest into
                # the spec, so the gate approves the exact build/test
                # contract the lane must run. Typed-best-effort: an
                # unreadable lock freezes the unknown-honest record, never
                # parks the run.
                profile_digest = (
                    await derive_from_reader(
                        self._gitlab,
                        project_id=project_id,
                        ref=base_sha or self._target_branch(),
                    )
                ).profile_digest
                spec_document = self._build_run_spec_document(
                    project_id=project_id,
                    issue_iid=issue_iid,
                    base_sha=base_sha,
                    task_title=issue_title,
                    task_description=issue_description,
                    task_digest=task_digest,
                    plan_summary=plan_summary,
                    plan_files_hint=plan_files_hint,
                    plan_digest=digest,
                    allowed_paths=path_scope,
                    harness_selection=harness_selection,
                    config_read=config_read,
                    profile_digest=profile_digest,
                )
                spec_digest = canonical_json_digest(spec_document)
                session.add(
                    RunSpec(
                        run_id=run_id,
                        schema_version=EXECUTABLE_SPEC_SCHEMA_VERSION,
                        document=spec_document,
                        digest=spec_digest,
                    )
                )
                run.spec_digest = spec_digest
                await session.commit()

        if spec_document is None:
            # Reload the FROZEN document by its bound digest (``RunSpec.id``
            # is a random hex id, not an ordering key — the digest is the
            # only honest lookup for "the spec this run executes").
            async with self._session_factory() as session:
                row = (
                    (
                        await session.execute(
                            select(RunSpec)
                            .where(RunSpec.run_id == run_id, RunSpec.digest == spec_digest)
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
                spec_document = row.document if row is not None else None

        # F22: open the run's budget from the spec (idempotent) so the
        # planning + factory legs reserve against real limits.
        if spec_document is not None:
            from forge.durable import open_budget_from_spec

            async with self._session_factory() as session:
                await open_budget_from_spec(
                    session, run_id=run_id, spec_document=spec_document, spec_digest=spec_digest
                )
                await session.commit()

        # The plan note: journaled like every external write; a leg that
        # already posted it (its journal row succeeded) never re-posts —
        # unless the plan itself was re-frozen (input digest moved), in
        # which case the stale comment must not be the approval surface.
        if plan_refrozen or not await self._action_succeeded(run_id, "post_plan_note"):
            await self._post_journaled_note(
                project_id,
                issue_iid,
                self._plan_comment(run_id, plan, digest, harness_selection),
                run_id,
                "post_plan_note",
            )

        # F15 (ADR-0018 §2): the pending decision is created when the plan is
        # published — carrying the plan/task/spec digests and an absolute
        # deadline. /go consumes THIS row; it no longer creates one. A
        # crashed leg's row stands — never a second gate for one plan (a
        # re-frozen plan opens a fresh decision bound to the NEW digests).
        if plan_refrozen or not await self._gate_exists(run_id):
            await self._open_pending_decision(
                run_id,
                project_id=project_id,
                issue_iid=issue_iid,
                plan_digest=digest,
                base_sha=base_sha,
                task_digest=task_digest,
                spec_digest=spec_digest,
                now=now,
            )

        try:
            await self._transition(run_id, FlowStatus.WAITING_APPROVAL)
        except InvalidTransition:
            pass  # a crashed leg already parked the run at the gate

        logger.info(
            "Run %s started for project %d issue !%s (by @%s) — waiting for /go",
            run_id[:8],
            project_id,
            issue_iid,
            author_username,
        )

    async def _apply_run_budget(self, run_id: str) -> None:
        """Bind the run's budget guard to the factory agents (F22).

        Agents are shared across the service instance; the budget lives in
        the database and is re-loaded per execution leg. ``None`` (no budget
        row — unlimited run) clears any previous binding.
        """
        from forge.durable import load_budget_guard

        guard = await load_budget_guard(self._session_factory, run_id)
        for agent in (self._planner, self._implementer, self._reviewer):
            client = getattr(agent, "_llm", None)
            if client is not None and hasattr(client, "set_budget"):
                client.set_budget(guard)

    def _budget_profiles(self) -> dict[str, dict[str, Any]]:
        """The configured numeric budget profiles (R13): forge.yml first
        (``budget_profiles:``), else the FORGE_BUDGET_PROFILES JSON — the
        same precedence as the harness preference."""
        from_config = self._config.budget_profiles
        if from_config:
            return from_config
        return parse_budget_profiles(
            str(getattr(self._settings, "FORGE_BUDGET_PROFILES", "") or "")
        )

    def _limits_of_selection(self, selection) -> BudgetLimits | None:
        """C02: the selection's RESOLVED ceilings as the canonical limits."""
        ceilings = getattr(selection, "budget_ceilings", None)
        if ceilings is None:
            return None
        return BudgetLimits(
            max_calls=ceilings.max_calls,
            max_tokens=ceilings.max_tokens,
            wallclock_s=ceilings.wallclock_s,
        )

    def _budget_limits_for_class(self, budget_class: str) -> BudgetLimits | None:
        """The numeric ceilings of *budget_class*'s profile, or ``None``.

        Thin wrapper over :func:`forge.durable.budgets.resolve_budget_limits`
        (unknown classes degrade to ``standard``; nothing configured → no
        ceilings).
        """
        return resolve_budget_limits(self._budget_profiles(), budget_class)

    async def _find_active_run(self, project_id: int, issue_iid: int | None) -> FlowRun | None:
        """The latest non-terminal run for the issue, or None.

        R03: scoped to THIS service's provider — ``project_id`` is a
        provider-local numeric id, so a GitHub/Azure run with the same
        numbers is a different subject and must never block a GitLab
        /implement.

        *issue_iid* may be None (a note webhook without issue context); the
        query then matches issue-less runs, which never collide in practice.
        """
        terminal = {status.value for status in TERMINAL_STATUSES}
        async with self._session_factory() as session:
            run = (
                (
                    await session.execute(
                        select(FlowRun)
                        .where(
                            FlowRun.provider == "gitlab",
                            FlowRun.project_id == project_id,
                            FlowRun.issue_iid == issue_iid,
                            FlowRun.status.notin_(terminal),
                        )
                        .order_by(FlowRun.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if run is None:
                return None
            session.expunge(run)
            return run

    async def _fair_use_counts(
        self, project_id: int, issue_iid: int | None, author_username: str
    ) -> dict[str, int]:
        """The four live counts :func:`forge.adaptive.admission.check_admission`
        judges, gathered for THIS provider's project before the candidate
        run exists (R28-23).

        - ``active_count`` — non-terminal runs past their gate (proposing
          onward): executing work holding expensive capacity;
        - ``queued_count`` — non-terminal runs still waiting to execute
          (accepted/preflight/planning/waiting_approval): a waiting human
          decision holds QUEUE capacity, never ACTIVE capacity;
        - ``issue_run_count`` — every run ever for this issue, terminal
          included (the per-issue attempt cap);
        - ``user_recent_count`` — runs this actor requested in the
          trailing hour, per the ``requested_by`` evidence key journaled
          at creation.

        The user dimension is matched in Python (the evidence key is a
        JSON column and the portable cross-database query is not worth
        the coupling); hourly volume per project is small by construction
        — the fair-use bounds exist precisely because it is.
        """
        terminal = {status.value for status in TERMINAL_STATUSES}
        queued = set(QUEUED_STATUSES)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        FlowRun.status,
                        FlowRun.issue_iid,
                        FlowRun.created_at,
                        FlowRun.evidence,
                    ).where(
                        FlowRun.provider == "gitlab",
                        FlowRun.project_id == project_id,
                    )
                )
            ).all()
        active_count = queued_count = issue_run_count = user_recent_count = 0
        for status, row_issue_iid, created_at, evidence in rows:
            if status not in terminal:
                if status in queued:
                    queued_count += 1
                else:
                    active_count += 1
            if row_issue_iid == issue_iid:
                issue_run_count += 1
            if author_username and created_at is not None:
                created = (
                    created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
                )
                if created >= cutoff and (evidence or {}).get("requested_by") == author_username:
                    user_recent_count += 1
        return {
            "active_count": active_count,
            "queued_count": queued_count,
            "issue_run_count": issue_run_count,
            "user_recent_count": user_recent_count,
        }

    async def _get_run(self, session: AsyncSession, run_id: str) -> FlowRun:
        """Fetch a run row this service minted earlier, or fail loudly.

        Callers only ever dereference ids created in the same flow (start_run
        / the journaled legs), so a missing row is an invariant violation, not
        a tolerated outcome — unlike the guarded ``session.get`` sites, which
        keep their explicit ``if run is None`` branches.
        """
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise RunNotFound(f"flow run {run_id!r} not found")
        return run

    @staticmethod
    def _active_run_comment(run: FlowRun) -> str:
        return (
            "## Forge — a run is already active on this issue\n\n"
            f"Run `{run.id}` is **{run.status}** — a new `/implement` would fork the "
            "branch and Draft MR.\n\n"
            f"- Approve it: `@forge /go {run.id}`\n"
            f"- Cancel it first: `@forge /cancel {run.id}`"
        )

    async def handle_cancel_note(
        self,
        project_id: int,
        note_text: str,
        author_username: str,
        issue_iid: int | None,
    ) -> None:
        """``@forge /cancel [run-id]``: approver-authorized cancellation (M3).

        Without an explicit run id the latest ACTIVE run for the issue is
        cancelled. Authority mirrors the /go gate: FORGE_APPROVERS only.
        Terminal runs are reported in the log, never touched.
        """
        match = _CANCEL_RE.search(note_text or "")
        if match is None:
            return
        if author_username not in self._approvers():
            logger.info(
                "/cancel from @%s who is not in FORGE_APPROVERS — ignoring", author_username
            )
            return

        requested = (match.group(1) or "").lower()
        async with self._session_factory() as session:
            if not requested:
                run = await self._find_active_run(project_id, issue_iid)
                if run is None:
                    logger.info("/cancel on issue !%s — no active run", issue_iid)
                    return
            elif len(requested) == 32:
                run = await session.get(FlowRun, requested)
                if (
                    run is None
                    or run.provider != "gitlab"
                    or run.project_id != project_id
                    or run.issue_iid != issue_iid
                ):
                    logger.info("/cancel references unknown run %s — ignoring", requested[:8])
                    return
            else:
                # Short id (plan comments show the 8-char form): resolve by
                # prefix among the issue's runs; ambiguity means no action.
                # R03: provider-scoped like every subject lookup above.
                runs = (
                    (
                        await session.execute(
                            select(FlowRun)
                            .where(
                                FlowRun.provider == "gitlab",
                                FlowRun.project_id == project_id,
                                FlowRun.issue_iid == issue_iid,
                                FlowRun.id.like(f"{requested}%"),
                            )
                            .order_by(FlowRun.created_at.desc())
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(runs) != 1:
                    logger.info(
                        "/cancel prefix %s matches %d runs — ignoring",
                        requested[:8],
                        len(runs),
                    )
                    return
                run = runs[0]
            run_id = run.id
            status = run.status

        if status in {s.value for s in TERMINAL_STATUSES}:
            logger.info("/cancel for terminal run %s (%s) — ignoring", run_id[:8], status)
            return

        # F13 (ADR-0018 §4): cancel = revoke the publication grant first. The
        # durable flag is what in-flight legs re-read before publishing, and
        # scheduled steps are withdrawn so no worker picks them up later.
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            run.cancel_requested = True
            await session.execute(
                update(StepRun)
                .where(StepRun.flow_run_id == run_id, StepRun.status == "scheduled")
                .values(status="cancelled")
            )
            await session.commit()

        await self._transition(
            run_id, FlowStatus.CANCELLED, reason=f"cancelled by @{author_username}"
        )
        await self._post_journaled_note(
            project_id,
            issue_iid,
            f"Run `{run_id[:8]}` **cancelled** by @{author_username}.",
            run_id,
            "cancel_note",
        )
        logger.info("Run %s cancelled by @%s", run_id[:8], author_username)

    async def _select_continuation(
        self,
        run_id: str,
        *,
        death_reason: str,
        evidence: dict,
        candidate_shas: list | None = None,
        operator_discard_requested: bool = False,
        checkpoint: Any | None = None,
        intent_lookup: continuation.IntentLookup | None = None,
        discard_authorized_by: str | None = None,
        native_command_id: str | None = None,
        source_attempt: int | None = None,
        lineage: str | None = None,
        dispatches: bool = False,
    ) -> continuation.ContinuationDecision:
        """The run's continuation decision — reused when the EVENT says so.

        R37-07 (#288): the GitLab half of Q35-02/R37-01 — one decision per
        recovery event, computed BEFORE the ack note and persisted on the
        run's evidence (``continuation``: mode, reason, decided_at, the
        evidence digest, the decision identity and the pinned
        ``checkpoint_digest`` the dispatch later carries as the exact
        resume reference). Reuse is keyed on the decision IDENTITY (the
        same ``native_command_id`` AND ``source_attempt`` re-materializes
        the SAME frozen decision with the ORIGINAL pinned checkpoint; a
        genuinely new event never matches the old document); the same
        identity returning with MOVED objective booleans is the typed
        ``ContinuationConflictError`` — surfaced on the document, never a
        silent reuse. The shape mirrors ``GitHubRunService.
        _select_continuation`` exactly; the mode it selects is what
        :meth:`_advance_harness` dispatches as the lane-resume contract.

        R36-03: checkpoint presence comes from the CONFIGURED ASYNC
        AUTHORITY — a pre-resolved typed *checkpoint* (the caller's single
        consultation) wins; else this service's session factory selects
        the same repository the upload/resume surfaces share. The typed
        outcome also pins the exact checkpoint's digest on the decision.
        """
        if checkpoint is None:
            try:
                result = await revival.durable_checkpoint_outcome(
                    run_id, session_factory=self._session_factory
                )
            except Exception:  # noqa: BLE001 — a failed lookup is unknown, not False
                result = None
        else:
            result = checkpoint
        committed, checkpoint_digest = continuation.normalize_checkpoint_result(result)
        prior_doc = evidence.get(continuation.CONTINUATION_EVIDENCE_KEY)
        snapshot = await continuation.evidence_from_record(
            death_reason=death_reason,
            run_id=run_id,
            evidence=evidence,
            candidate_shas=candidate_shas,
            operator_discard_requested=operator_discard_requested,
            checkpoint_committed=committed,
            prior_mode_selected=(
                str(prior_doc.get("mode")) if isinstance(prior_doc, dict) else None
            ),
            intent_lookup=intent_lookup or self._native_start_verdict,
            discard_authorized_by=discard_authorized_by,
            native_command_id=native_command_id,
            source_attempt=source_attempt,
            checkpoint_digest=checkpoint_digest,
        )
        conflict: dict[str, Any] | None = None
        try:
            reused = continuation.matching_decision(prior_doc, snapshot)
        except continuation.ContinuationConflictError as exc:
            # The SAME event identity returned with moved objective
            # evidence — surfaced, never silently reused; the decision is
            # re-made below with the conflict recorded beside it.
            conflict = {
                "decision_id": exc.decision_id,
                "recorded_digest": exc.recorded_digest,
                "delivered_digest": exc.delivered_digest,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.warning(
                "continuation.conflicting_recovery_event: run %s — the recovery event "
                "behind decision %s returned with a changed objective snapshot "
                "(recorded %s, delivered %s); re-deciding (R37-01)",
                run_id[:8],
                exc.decision_id[:12],
                exc.recorded_digest[:12],
                exc.delivered_digest[:12],
            )
            reused = None
        if reused is not None:
            # An EXACT replay of the same recovery event: re-persist the
            # frozen decision as the LATEST (the dispatch leg consumes the
            # decision BY identity — the re-drive of a stranded /retry pins
            # the SAME checkpoint the event approved, not whatever is
            # newest), history carried forward untouched.
            document = continuation.persisted_document(reused, prior_doc)
            if dispatches:
                document["continuation_decision_id"] = reused.decision_id
            await self._merge_run_evidence(
                run_id, {continuation.CONTINUATION_EVIDENCE_KEY: document}
            )
            logger.info(
                "Run %s continuation decision replayed by identity %s (%s, checkpoint %s) "
                "— replay_outcome=exact (R37-01)",
                run_id[:8],
                reused.decision_id[:12],
                reused.mode_selected,
                (reused.evidence.checkpoint_digest or "none")[:12],
            )
            return reused
        if lineage and not isinstance(prior_doc, dict):
            lineage = None  # nothing to re-establish — this is the first decision
        if lineage is None and continuation.legacy_decision(prior_doc):
            lineage = "reestablished"
        decision = continuation.decide_continuation(snapshot, lineage=lineage)
        document = continuation.persisted_document(decision, prior_doc)
        if conflict is not None:
            document["conflicting_recovery_event"] = conflict
        # R36-03/R37-01: pin the EXACT checkpoint the decision approves —
        # one pin per authorized decision, keyed by its DECISION identity
        # (idempotent per (checkpoint_id, reason); the lane's strict
        # required-restore guard stays the safety line when the pin fails).
        if (
            decision.mode is continuation.ContinuationMode.EXACT_WIP
            and checkpoint_digest
            and decision.dispatchable
        ):
            try:
                from forge.adaptive.checkpoint_repository import (
                    resolve_checkpoint_lookup_authority,
                )

                authority = resolve_checkpoint_lookup_authority(
                    session_factory=self._session_factory
                )
                pin = getattr(authority, "pin", None)
                pinned = (
                    bool(await pin(run_id, checkpoint_digest, f"decision:{decision.decision_id}"))
                    if pin is not None
                    else False
                )
            except Exception as exc:  # noqa: BLE001 — the pin is protection, not authority
                pinned = False
                logger.warning(
                    "Run %s: pinning continuation checkpoint %s for decision %s failed: %s",
                    run_id[:8],
                    checkpoint_digest[:12],
                    decision.decision_id[:12],
                    exc,
                )
            if not pinned:
                logger.warning(
                    "Run %s continuation decision %s approved checkpoint %s but the GC "
                    "pin did not land — the lane's strict required-restore guard "
                    "remains the safety net (R37-01)",
                    run_id[:8],
                    decision.decision_id[:12],
                    checkpoint_digest[:12],
                )
        if dispatches:
            document["continuation_decision_id"] = decision.decision_id
        await self._merge_run_evidence(run_id, {continuation.CONTINUATION_EVIDENCE_KEY: document})
        logger.info(
            "Run %s continuation decision %s (%s, attempt %s, event %s): %s — %s (R37-07)",
            run_id[:8],
            decision.decision_id[:12],
            "reestablished" if decision.lineage else "new",
            source_attempt,
            native_command_id or "none",
            decision.mode_selected,
            decision.reason,
        )
        return decision

    async def _native_start_verdict(self, run_id: str) -> str:
        """The persisted native-start intent verdict (R36-02).

        The evidence source for vendor-start certainty: the
        ``execution_leases`` rows :func:`record_native_start_intent` wrote
        BEFORE every provider call on this run (the GitLab dispatch leg
        records them around ``backend.start``). Lease rows with NO intent
        on a dead run PROVE the provider call was never attempted
        (``never_dispatched``); an intent anywhere means the call WAS
        attempted (``dispatched``) — which still leaves the vendor start
        unknown; no lease rows at all is ``unknown``.
        """
        from forge.adaptive.admission import ExecutionLease

        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ExecutionLease.native_intent_at).where(
                            ExecutionLease.run_id == run_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return continuation.NATIVE_START_UNKNOWN
        if any(intent_at is not None for intent_at in rows):
            return continuation.NATIVE_START_DISPATCHED
        return continuation.NATIVE_START_NEVER_DISPATCHED

    async def handle_retry_note(
        self,
        project_id: int,
        note_text: str,
        author_username: str,
        issue_iid: int | None,
        *,
        delivery_id: str | None = None,
    ) -> None:
        """``@forge /retry [run-id]``: operator revival of a dead run (Tier 2).

        Bare, the latest ``failed``/``blocked`` run for the issue; authority
        mirrors ``/go`` (FORGE_APPROVERS only). The walk is the explicit
        revival graph edge (``blocked``/``failed`` → ``proposing``), the
        commit cycle gains ONE operator-granted cycle (it may exceed
        FORGE_MAX_COMMIT_CYCLES), and the SAME branch is re-dispatched with a
        repair context built from the terminal reason and the last
        verification evidence — no new run id, no re-planning.

        Cancelled runs, and runs that never committed a candidate, are
        rejected with an actionable note: there is no work to continue, so
        ``/implement`` is the honest path.

        A11: the revival is ONE durable, idempotent transition. The attempt
        record (the ``retry_requested`` action row) is written in the SAME
        transaction as the CAS walk, keyed by the webhook DELIVERY id:
        a redelivered ``/retry`` with the same delivery id is a no-op (no
        second cycle bump, no second dispatch); a different delivery id
        while an attempt is in flight is refused with the rejection-note
        machinery; the persisted ``pending`` dispatch state lets the
        recovery scan (:meth:`evaluate_revival_recovery`) re-drive a lost
        dispatch exactly once.

        R37-07 (#288): the harness lane's re-dispatch carries the
        continuation decision — selected ONCE from the recorded recoverable
        state (Q35-02/R37-01: identity-keyed reuse, pinned checkpoint
        digest), persisted BEFORE the ack note, and dispatched as the
        lane-resume envelope (``required`` over the exact checkpoint /
        ``fresh`` on a proven no-WIP death / ``restart`` on the operator's
        explicit discard). An UNCERTAIN harness recoverable state
        dispatches NOTHING — the ambiguity goes to the operator, never
        into a guessed re-execution. The retry also opens a NEW attempt
        generation (NEXT-01), so the re-dispatched lane token is
        attempt-scoped and the dead attempt's credentials retire.
        """
        match = _RETRY_RE.search(note_text or "")
        if match is None:
            return
        if author_username not in self._approvers():
            logger.info("/retry from @%s who is not in FORGE_APPROVERS — ignoring", author_username)
            return
        if issue_iid is None:
            logger.info("/retry off-issue — ignoring")
            return

        requested = (match.group(1) or "").lower()
        attempt_key = retry_delivery_key(delivery_id)
        run_id: str | None = None
        # A07: the dispatch legs below aim at the VERIFIED run's subject, read
        # back from the matched run — never the command context. The scoped
        # resolution makes the two equal; reading them from the run keeps the
        # dispatch honest even if resolution were ever widened.
        retry_project_id = 0
        retry_issue_iid: int | None = None
        rejection = ""
        status = ""
        status_reason = ""
        cycle = 1
        evidence: dict = {}
        candidates: list = []
        cancel_requested = False
        other_active = False
        source_attempt = 0
        checkpoint_outcome: Any = None
        async with self._session_factory() as session:
            run = await resolve_retry_target(
                session,
                provider="gitlab",
                project_id=project_id,
                issue_iid=issue_iid,
                requested=requested,
            )
            if run is not None:
                run_id = run.id
                retry_project_id = int(run.project_id)
                retry_issue_iid = run.issue_iid
                # A11 delivery identity first: a redelivered /retry is a
                # no-op; a different delivery while an attempt is in flight
                # is refused before any status-based wording can mislead.
                if attempt_key is not None:
                    existing = await find_revival_attempt(
                        session, run_id=run.id, kind="retry_requested", idempotency_key=attempt_key
                    )
                    if existing is not None:
                        logger.info(
                            "/retry delivery %s already delivered for run %s — no-op",
                            delivery_id,
                            run_id[:8],
                        )
                        return
                    if await open_revival_attempt(session, run_id=run.id) is not None:
                        rejection = retry_in_flight_rejection(run.id)
                if not rejection:
                    # R36-03: ONE typed consultation of the CONFIGURED async
                    # checkpoint authority feeds BOTH the refusal evaluation
                    # and the continuation decision below.
                    checkpoint_outcome = await revival.durable_checkpoint_outcome(
                        run.id, session_factory=self._session_factory
                    )
                    other_active = await has_active_run(
                        session,
                        provider=run.provider,
                        project_id=run.project_id,
                        issue_iid=run.issue_iid,
                        repo_full_name=run.github_repo_full_name,
                        exclude_run_id=run.id,
                    )
                    rejection = retry_rejection(
                        run, other_active=other_active, checkpoint=checkpoint_outcome
                    )
                # Captured regardless of the rejection so the decision below
                # always decides over the run's real record.
                status = run.status
                status_reason = run.status_reason or ""
                cycle = run.commit_cycle or 1
                evidence = dict(run.evidence or {})
                candidates = list(run.candidate_shas or [])
                cancel_requested = bool(run.cancel_requested)
                source_attempt = int(run.cancellation_generation or 0)
        if run_id is None:
            logger.info("/retry on issue !%s — no retryable run", issue_iid)
            return

        # R37-07 (#288): the typed recovery request — the ``restart`` verb is
        # granted only from its documented argument position and only when
        # the token addresses the resolved subject (R36-02).
        recovery = continuation.parse_recovery_request(note_text, run_id)
        # Q35-02/R37-01: decide ONCE, from the recorded recoverable state,
        # BEFORE the ack note — the persisted decision (its identity, its
        # pinned checkpoint digest) is what the dispatch leg later carries as
        # the exact-resume envelope. A repeated retry EVENT re-materializes
        # the SAME frozen decision; a new event never reuses the old one.
        decision = await self._select_continuation(
            run_id,
            death_reason=status_reason,
            evidence=evidence,
            candidate_shas=candidates,
            operator_discard_requested=recovery.restart,
            discard_authorized_by=(f"operator:@{author_username}" if recovery.restart else None),
            native_command_id=delivery_id,
            source_attempt=source_attempt,
            checkpoint=checkpoint_outcome,
            dispatches=True,
        )
        backend_name = str(evidence.get("backend") or "").strip() or self._backend_name()
        harness_lane = is_harness_backend(backend_name)
        # R36-02: the typed override matrix — ONLY the continuity arm
        # (``nothing_to_retry``) may be superseded, and only when the
        # decision PROVES the demand wrong (a proven no-WIP death) or the
        # operator authorized the discard. Every other code keeps refusing.
        rejection_code = rejection.code if isinstance(rejection, RetryRejection) else ""
        if (
            rejection
            and rejection_code == RetryRefusalCode.NOTHING_TO_RETRY.value
            and decision.mode
            in (
                continuation.ContinuationMode.COMMITTED_BASELINE,
                continuation.ContinuationMode.EXPLICIT_RESTART,
            )
            and not other_active
            and status in ("failed", "blocked")
            and not cancel_requested
            and not candidates
        ):
            logger.info(
                "/retry of run %s — %s overrides the no-candidate/no-checkpoint refusal (R36-02)",
                run_id[:8],
                decision.mode_selected,
            )
            rejection = ""
        if rejection:
            if rejection_code == RetryRefusalCode.NOTHING_TO_RETRY.value and harness_lane:
                # The continuity arm refused a harness lane whose
                # recoverable state is UNCERTAIN: the honest reply is the
                # decision note (the park WITH its explanation and the
                # documented ways out), never a dispatch that would guess.
                await self._post_journaled_note(
                    project_id,
                    issue_iid,
                    continuation.uncertain_retry_note(run_id, decision),
                    run_id,
                    "retry_uncertain_note",
                )
                logger.info(
                    "/retry of run %s parked for an operator decision — continuation "
                    "source unknown (%s), nothing dispatched (R37-07)",
                    run_id[:8],
                    decision.mode_selected,
                )
                return
            await self._post_journaled_note(
                project_id, issue_iid, f"🔁 {rejection}", run_id, "retry_rejected_note"
            )
            return
        if harness_lane and not decision.dispatchable:
            # Q35-02, UNCERTAIN on the harness lane: absence of a checkpoint
            # never proves there was no WIP — NOTHING is dispatched (zero
            # pipelines, zero vendor sessions, no attempt, no cycle bump).
            # The builtin backend keeps its legacy repair semantics (the
            # published candidate IS its durable continuation; there is no
            # held WIP to mis-restore) — recorded as an honest limitation of
            # this parity wave, not a silent divergence.
            await self._post_journaled_note(
                project_id,
                issue_iid,
                continuation.uncertain_retry_note(run_id, decision),
                run_id,
                "retry_uncertain_note",
            )
            logger.info(
                "/retry of run %s stood down — continuation source unknown (%s), "
                "nothing dispatched (R37-07)",
                run_id[:8],
                decision.mode_selected,
            )
            return

        # NEXT-01 (the harness lane): the retry opens a NEW attempt — the
        # durable attempt generation is bumped (aligned with the pause
        # fence's resumed epoch) BEFORE the lane launches, so the
        # re-dispatch's lane token is attempt-scoped to the retry and every
        # credential of the dead attempt retires at both control APIs.
        fence_floor = 0
        if harness_lane:
            fence = await pause_fence_decision(self._session_factory, run_id)
            fence_floor = fence.resumed_publication_epoch or 0

        # One durable, idempotent transition (A11): the attempt record and
        # the CAS revival walk commit atomically. The attempt carries the
        # delivery id (webhook-redelivery idempotency) and the operator
        # override retryability class.
        try:
            async with self._session_factory() as session:
                controller = Controller(session)
                attempt = await begin_revival_attempt(
                    session,
                    run_id=run_id,
                    kind="retry_requested",
                    idempotency_key=attempt_key,
                    retryability=classify_retryability("retry_requested"),
                )
                if not attempt.created:
                    logger.info(
                        "/retry delivery redelivered for run %s (index arbiter) — no-op",
                        run_id[:8],
                    )
                    return
                await controller.revive_transition(
                    run_id,
                    reason=f"retry requested by @{author_username}",
                    authorized_by=f"operator:@{author_username}",
                )
                run = await self._get_run(session, run_id)
                run.commit_cycle = cycle + 1
                if harness_lane:
                    run.cancellation_generation = max(
                        int(run.cancellation_generation or 0) + 1, fence_floor
                    )
                await session.commit()
        except RevivalInFlight:
            # A concurrent driver opened an attempt between the check and the
            # write — the index refused this one; carry the same rejection.
            await self._post_journaled_note(
                project_id,
                issue_iid,
                f"🔁 {retry_in_flight_rejection(run_id)}",
                run_id,
                "retry_rejected_note",
            )
            return
        action_id = attempt.action_id

        branch = factory_branch(retry_issue_iid, run_id)
        logger.info(
            "Run %s retried by @%s — re-dispatching %s (cycle %d, continuation %s)",
            run_id[:8],
            author_username,
            branch,
            cycle + 1,
            decision.mode_selected,
        )
        await self._post_journaled_note(
            project_id,
            issue_iid,
            f"## 🔁 Run `{run_id[:8]}` retried by @{author_username}\n\n"
            f"- Branch: `{branch}` — the work continues in place, no re-planning\n"
            f"- Commit cycle: {cycle + 1}\n"
            # Q35-02: the ack NAMES the selected source of continuation (and
            # never promises preservation when the decision is a discard).
            f"- Continuation: {continuation.retry_ack_line(decision)}\n"
            "\n*This is an automated message.*",
            run_id,
            "retry_ack_note",
        )

        # A11: claim the dispatch (pending → dispatched) BEFORE any leg runs —
        # the recovery scan never re-drives a claimed window, and a crash
        # after this point resolves through the leg's own journaled action.
        async with self._session_factory() as session:
            claimed = await claim_attempt_dispatch(session, action_id)
            await session.commit()
        if not claimed:
            logger.warning(
                "Run %s revival dispatch already claimed by another driver — standing down",
                run_id[:8],
            )
            return

        repair_context = build_retry_context(self._settings, status_reason, evidence)
        repair_reason = f"retry by @{author_username}: {status_reason or status}"
        try:
            if harness_lane:
                await self._advance_harness(
                    retry_project_id,
                    run_id,
                    repair_context=repair_context,
                    repair_reason=repair_reason,
                    # R37-07: the WIP-continuity contract the decision
                    # SELECTED — ``required`` only when an exact committed
                    # checkpoint is the authorized continuation (the lane's
                    # strict restore guard then enforces it); a PROVEN
                    # no-WIP death retries ``fresh`` on the frozen base; an
                    # explicit operator discard is ``restart``.
                    resume_mode=decision.resume_mode(),
                )
            else:
                await self._advance_proposal(
                    retry_project_id,
                    run_id,
                    repair_context=repair_context,
                    repair_reason=repair_reason,
                )
        except Exception as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        await self._complete_action(action_id, "succeeded", {"backend": backend_name})

    async def evaluate_revival_recovery(self, now: datetime | None = None) -> int:
        """One recovery pass over stranded revival attempts (A11).

        A worker that died between the revive commit and the dispatch (or
        inside the dispatch leg, journal unfinished) leaves the persisted
        attempt ``pending``/uncompleted — this pass re-drives each stranded
        dispatch exactly once (:func:`forge.runs.revival.evaluate_attempt_recovery`).
        """
        return await evaluate_attempt_recovery(
            self._session_factory,
            provider="gitlab",
            redispatch=self._redispatch_revival,
            now=now,
            log=logger,
        )

    # ------------------------------------------------------------------
    # R29 operator surface around dead/stuck runs
    # ------------------------------------------------------------------

    async def handle_status_note(
        self,
        project_id: int,
        note_text: str,
        author_username: str,
        issue_iid: int | None,
    ) -> None:
        """``@forge /status [run-id]``: READ-ONLY run snapshot (R29).

        Bare, the issue's latest run of any state. The reply is composed
        from durable state only — status, reason, cycle, candidates, budget
        headroom, verification evidence, publication-intent states and the
        revive/retry counters. No transitions, no model calls, no provider
        effects; the only write is the journaled reply note itself.
        """
        match = STATUS_RE.search(note_text or "")
        if match is None:
            return
        if issue_iid is None:
            logger.info("/status off-issue — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        async with self._session_factory() as session:
            run = await resolve_status_target(
                session,
                provider="gitlab",
                project_id=project_id,
                issue_iid=issue_iid,
                requested=requested,
            )
            if run is None:
                body = (
                    "## Forge — status\n\nNo forge run found on this issue yet. "
                    "Start one with `@forge /implement`.\n\n*This is an automated message.*"
                )
                logger.info("/status on issue !%s — no run", issue_iid)
            else:
                body = format_status_reply(await collect_status_snapshot(session, run))
                run_id = run.id
        await self._post_journaled_note(
            project_id,
            issue_iid,
            body,
            run_id,
            "status_note",
        )

    async def handle_why_blocked_note(
        self,
        project_id: int,
        note_text: str,
        author_username: str,
        issue_iid: int | None,
    ) -> None:
        """``@forge /why-blocked [run-id]``: READ-ONLY precise cause (R29).

        Explains the terminal/blocked cause — the parked reason, its Tier-1
        classification and the honest revive/retry eligibility (which quotes
        the ONE rejection table ``/retry`` itself uses).
        """
        match = WHY_BLOCKED_RE.search(note_text or "")
        if match is None:
            return
        if issue_iid is None:
            logger.info("/why-blocked off-issue — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        async with self._session_factory() as session:
            run = await resolve_status_target(
                session,
                provider="gitlab",
                project_id=project_id,
                issue_iid=issue_iid,
                requested=requested,
            )
            if run is None:
                body = (
                    "## Forge — why blocked\n\nNo forge run found on this issue yet. "
                    "Start one with `@forge /implement`.\n\n*This is an automated message.*"
                )
                logger.info("/why-blocked on issue !%s — no run", issue_iid)
            else:
                other_active = await has_active_run(
                    session,
                    provider=run.provider,
                    project_id=run.project_id,
                    issue_iid=run.issue_iid,
                    repo_full_name=run.github_repo_full_name,
                    exclude_run_id=run.id,
                )
                body = why_blocked_reply(
                    run,
                    other_active=other_active,
                    # R36-03: the read-only verdict reports what the
                    # configured async authority answered.
                    checkpoint=await revival.durable_checkpoint_outcome(
                        run.id, session_factory=self._session_factory
                    ),
                )
                run_id = run.id
        await self._post_journaled_note(
            project_id,
            issue_iid,
            body,
            run_id,
            "why_blocked_note",
        )

    async def handle_reconcile_note(
        self,
        project_id: int,
        note_text: str,
        author_username: str,
        issue_iid: int | None,
    ) -> None:
        """``@forge /reconcile <run-id>``: drive the R11 recovery explicitly (R29).

        THE mutating new command — approver-gated exactly like ``/retry``
        and NOT a generic revival (``/retry`` stays the revival path). It
        targets runs with an OPEN publication intent (a crash left the
        outcome unrecorded) or a superseded/unknown completion, and drives
        the SAME probe/recovery pass the reconciler runs, then reports the
        resolution: adopted / duplicated / unknown (+ manual instruction).
        Runs without any publication intent are refused — there is nothing
        to reconcile.
        """
        match = RECONCILE_RE.search(note_text or "")
        if match is None:
            return
        if author_username not in self._approvers():
            logger.info(
                "/reconcile from @%s who is not in FORGE_APPROVERS — ignoring", author_username
            )
            return
        if issue_iid is None:
            logger.info("/reconcile off-issue — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        intents = []
        async with self._session_factory() as session:
            run = await resolve_retry_target(
                session,
                provider="gitlab",
                project_id=project_id,
                issue_iid=issue_iid,
                requested=requested,
            )
            if run is not None:
                run_id = run.id
                intents = await intents_for_run(session, run.id)
        if run_id is None:
            logger.info("/reconcile references unknown run %s — ignoring", requested[:8])
            return
        if not intents:
            await self._post_journaled_note(
                project_id,
                issue_iid,
                f"Run `{run_id[:8]}` has no publication intent — nothing to reconcile. "
                "`/reconcile` drives lost publications only; revival is `/retry`'s job.",
                run_id,
                "reconcile_refused_note",
            )
            return

        now = datetime.now(timezone.utc)
        for intent in intents:
            if intent.status not in OPEN_STATES:
                continue  # terminal intents are immutable — reported, never re-driven
            try:
                await self._resolve_one_publication_intent(intent, now=now)
            except Exception:
                # One broken intent must not strand the others' resolutions.
                logger.exception("Reconcile pass failed for intent %s", intent.id[:8])
        async with self._session_factory() as session:
            resolved = [await session.get(PublicationIntent, intent.id) for intent in intents]
        await self._post_journaled_note(
            project_id,
            issue_iid,
            format_reconcile_reply(run_id, [row for row in resolved if row is not None]),
            run_id,
            "reconcile_note",
        )

    # ------------------------------------------------------------------
    # Issue-edit replan + label-off cancel (operator busywork, event-driven)
    # ------------------------------------------------------------------

    async def handle_review_feedback_note(
        self,
        project_id: int,
        note_text: str,
        author_username: str,
        mr_iid: int | None,
        issue_iid: int | None = None,
        *,
        note_id: str | None = None,
        discussion_id: str = "",
    ) -> None:
        """Q39-13 (#332): a reviewer comment on the Draft MR, classified.

        The GitLab MR-note ingress region (``run_command`` → here, the
        same dispatch every note command travels). One reviewer comment
        produces ONE durable :class:`ReviewFeedbackRequest` — the note id
        is the idempotency key, so a webhook replay records nothing new
        and earns at most one reply (the /go A11 pattern). The request
        binds to the CURRENT MR head, the originating discussion, the
        authorized actor (``approvers_for`` — the same authority as
        ``/go``, never authorship) and the approved work scope (the
        frozen spec's ``allowed_paths``), then classifies:

        - ``clarification`` — a question (``/ask``): routed to the
          approvers with an operator-visible reply; NO dispatch, NO
          staging (reviewer-only recovery consumes no implementation
          budget).
        - ``material_change`` — the named edit cannot be proven inside
          the approved write scope: the request becomes a durable
          MATERIAL PROPOSAL and the reply says so. The bounded lane never
          widens the write surface a human approved — the way forward is
          the existing material-revision approval route, not a permission
          expansion.
        - ``in-scope_correction`` — the bounded correction: staged as an
          input revision (the referenced diff context + the head binding
          + the permitted change) through the EXISTING approval route; a
          human ``/approve-revision`` makes it the active revision TEXT
          and :meth:`evaluate_review_corrections` re-dispatches the
          correction cycle (the #321 ApprovedInput machinery briefs the
          executor with it).

        The bot never merges and never resolves the discussion — the
        reviewer's resolve is the human decision that gates readiness.
        """
        parsed = parse_review_feedback_note(note_text or "")
        if parsed is None:
            return  # not review feedback — the ingress ignores it
        kind, body = parsed
        if mr_iid is None:
            logger.info("Review feedback note without an MR — ignoring")
            return
        if not note_id:
            # No delivery identity → no dedup → refusing to act is the
            # only safe posture (one comment must be one request).
            logger.info("Review feedback note %r without a note id — ignoring", body[:40])
            return
        async with self._session_factory() as session:
            run = (
                (
                    await session.execute(
                        select(FlowRun)
                        .where(
                            FlowRun.provider == "gitlab",
                            FlowRun.project_id == project_id,
                            FlowRun.mr_iid == mr_iid,
                        )
                        .order_by(FlowRun.created_at.desc(), FlowRun.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if run is None:
                logger.info("Review feedback on MR !%s matches no run — ignoring", mr_iid)
                return
            run_id = run.id
            run_status = run.status
            run_issue_iid = run.issue_iid
            existing = review_feedback_requests_of(run.evidence or {})
        issue_iid = issue_iid if issue_iid is not None else run_issue_iid

        # A redelivery of an already-recorded note: the durable request
        # answers, and the reply journal dedups the operator note.
        if author_username not in self._approvers():
            request = ReviewFeedbackRequest(
                note_id=note_id,
                run_id=run_id,
                discussion_id=discussion_id,
                mr_iid=mr_iid,
                actor=author_username,
                head_sha="",
                classification=CLARIFICATION_CLASS,
                text=body,
                created_at=datetime.now(timezone.utc).isoformat(),
                status=REQUEST_REFUSED_UNAUTHORIZED,
            )
            if note_id not in existing:
                await record_review_feedback_request(self._session_factory, run_id, request)
            logger.info(
                "Review feedback from @%s who is not in FORGE_APPROVERS — refusing",
                author_username,
            )
            await self._reply_review_feedback(
                project_id, mr_iid, run_id, note_id, self._rf_unauthorized_body(author_username)
            )
            return

        try:
            head = await self._read_mr_head(project_id, mr_iid, issue_iid, run_id)
        except GitLabAPIError:
            logger.info("MR head read failed for MR !%s — the redelivery re-enters here", mr_iid)
            return
        # The originating discussion: the typed deleted-discussion check
        # runs when the discussions surface is readable.
        resolved_discussion = discussion_id
        diff_context = ""
        discussions = await self._discussions_or_none(project_id, mr_iid)
        if discussions is not None and discussion_id:
            match = next((d for d in discussions if d.id == discussion_id), None)
            if match is None:
                request = ReviewFeedbackRequest(
                    note_id=note_id,
                    run_id=run_id,
                    discussion_id=discussion_id,
                    mr_iid=mr_iid,
                    actor=author_username,
                    head_sha=head,
                    classification=CLARIFICATION_CLASS,
                    text=body,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    status=REQUEST_DELETED_DISCUSSION,
                )
                if note_id not in existing:
                    await record_review_feedback_request(self._session_factory, run_id, request)
                logger.info(
                    "Review feedback note %s references deleted discussion %s — refusing",
                    note_id,
                    discussion_id,
                )
                await self._reply_review_feedback(
                    project_id,
                    mr_iid,
                    run_id,
                    note_id,
                    self._rf_deleted_discussion_body(discussion_id),
                )
                return
            resolved_discussion = match.id
            for entry in match.notes:
                if str(entry.id) == str(note_id) and entry.position is not None:
                    paths = [
                        path for path in (entry.position.old_path, entry.position.new_path) if path
                    ]
                    diff_context = " -> ".join(paths) if paths else ""
                    break

        try:
            spec = await self._load_executable_spec(run_id)
            allowed_paths = tuple(spec.allowed_paths)
        except SpecInvalid:
            logger.info(
                "Review feedback on run %s without a resolvable spec — ignoring", run_id[:8]
            )
            return
        classification = classify_review_feedback(kind, body, allowed_paths)
        request = ReviewFeedbackRequest(
            note_id=note_id,
            run_id=run_id,
            discussion_id=resolved_discussion,
            mr_iid=mr_iid,
            actor=author_username,
            head_sha=head,
            classification=classification,
            text=body,
            referenced_paths=referenced_paths_of(body),
            diff_context=diff_context,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            recorded = await record_review_feedback_request(self._session_factory, run_id, request)
        except ReviewFeedbackRefused as exc:
            logger.info("Review feedback note %s refused [%s]", note_id, exc.code)
            return

        if recorded.status != REQUEST_RECORDED:
            # A redelivery after the request already took its first
            # lifecycle step: the durable record answers — nothing
            # re-runs and nothing is re-staged. The reply journal dedups
            # the operator note (re-attempting it only when the first
            # posting failed), the /go refusal pattern.
            replay_body = self._rf_replay_body(recorded, allowed_paths)
            if replay_body:
                await self._reply_review_feedback(project_id, mr_iid, run_id, note_id, replay_body)
            return

        if classification == CLARIFICATION_CLASS:
            await mark_review_feedback_request(
                self._session_factory, run_id, note_id, REQUEST_CLARIFICATION_OPEN
            )
            await self._reply_review_feedback(
                project_id, mr_iid, run_id, note_id, self._rf_clarification_body(run_id, body)
            )
            return

        if classification == MATERIAL_CHANGE_CLASS:
            await mark_review_feedback_request(
                self._session_factory, run_id, note_id, REQUEST_MATERIALIZED
            )
            await self._reply_review_feedback(
                project_id,
                mr_iid,
                run_id,
                note_id,
                self._rf_material_body(run_id, body, allowed_paths),
            )
            return

        # in-scope correction: the bounded lane only re-enters the
        # delivery pipeline from the pre-ready states (ADR-0004 —
        # ready_for_human has no outgoing edge: the human decision is
        # final). Outside the window the request is recorded (the audit
        # stands) and the reviewer is told the honest path.
        if run_status not in (FlowStatus.WAITING_CI.value, FlowStatus.EVALUATING_CI.value):
            await mark_review_feedback_request(
                self._session_factory, run_id, note_id, REQUEST_WINDOW_CLOSED
            )
            await self._reply_review_feedback(
                project_id,
                mr_iid,
                run_id,
                note_id,
                self._rf_window_closed_body(run_id, run_status),
            )
            return
        pending = [
            other
            for other in review_feedback_requests_of(
                (await self._read_run_evidence(run_id)) or {}
            ).values()
            if other.status == REQUEST_STAGED and other.note_id != note_id
        ]
        if pending:
            await mark_review_feedback_request(
                self._session_factory,
                run_id,
                note_id,
                REQUEST_CONFLICTING,
                conflict_with=pending[0].decision_id,
            )
            await self._reply_review_feedback(
                project_id,
                mr_iid,
                run_id,
                note_id,
                self._rf_conflicting_body(run_id, pending[0]),
            )
            return
        try:
            decision_id = await stage_review_correction(self._session_factory, run_id, request)
        except ReviewFeedbackRefused as exc:
            logger.info(
                "Review correction for run %s refused [%s] — %s",
                run_id[:8],
                exc.code,
                exc.detail,
            )
            await self._reply_review_feedback(
                project_id, mr_iid, run_id, note_id, self._rf_refused_body(run_id, exc)
            )
            return
        await self._reply_review_feedback(
            project_id,
            mr_iid,
            run_id,
            note_id,
            self._rf_staged_body(run_id, decision_id, head, request),
        )

    async def evaluate_review_corrections(self) -> None:
        """The bounded correction re-dispatch pass (Q39-13).

        A durable scan in the reconciler's own shape: every run whose
        staged review correction a human has since APPROVED (the decision
        the activation consumed is the correction's own) and which has
        not yet re-dispatched, re-enters the delivery pipeline through
        the repair edge — with the MR head re-checked first (an
        unexpected head move is the typed ``stale_head`` conflict: human
        edits preserved, nothing dispatched).
        """
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(FlowRun.id).where(
                            FlowRun.provider == "gitlab",
                            FlowRun.status.in_(
                                [FlowStatus.WAITING_CI.value, FlowStatus.EVALUATING_CI.value]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
        for run_id in rows:
            try:
                await self._redispatch_review_correction(run_id)
            except Exception:
                # One broken run must not stall the pass (the reconciler
                # pattern every other scan here follows).
                logger.exception("Review-correction pass failed for run %s", run_id[:8])

    async def _redispatch_review_correction(self, run_id: str) -> None:
        """Re-dispatch ONE run's approved review correction, or record why not."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            evidence = dict(run.evidence or {})
            project_id = run.project_id
            issue_iid = run.issue_iid
            mr_iid = run.mr_iid
        active = evidence.get(ACTIVE_PLAN_KEY) if isinstance(evidence, dict) else None
        activated_by = (
            str(active.get("activated_by_decision") or "") if isinstance(active, dict) else ""
        )
        for request in review_feedback_requests_of(evidence).values():
            if request.status != REQUEST_STAGED or request.decision_id != activated_by:
                continue
            try:
                await self._begin_review_correction(
                    run_id,
                    project_id,
                    issue_iid,
                    mr_iid,
                    request,
                )
            except ReviewFeedbackRefused as exc:
                if exc.code == "stale_head":
                    await mark_review_feedback_request(
                        self._session_factory, run_id, request.note_id, REQUEST_STALE_HEAD
                    )
                    await self._reply_review_feedback(
                        project_id,
                        mr_iid,
                        run_id,
                        request.note_id,
                        self._rf_stale_head_body(run_id, request),
                        allow_repeat=True,
                    )
                else:
                    logger.info(
                        "Review correction re-dispatch for run %s refused [%s]",
                        run_id[:8],
                        exc.code,
                    )
            return

    async def _begin_review_correction(
        self,
        run_id: str,
        project_id: int,
        issue_iid: int | None,
        mr_iid: int | None,
        request: ReviewFeedbackRequest,
    ) -> None:
        """The correction's dispatch entry: head fence, then the repair edge."""
        head = await self._read_mr_head(project_id, request.mr_iid, issue_iid, run_id)
        head_binding_guard(request.head_sha, head)  # typed stale_head
        backend_name = ""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            backend_name = str((run.evidence or {}).get("backend") or "").strip()
            cycle = run.commit_cycle or 1
        context = self._rf_correction_context(request)
        reason = f"review correction: discussion {request.discussion_id or request.note_id}"
        async with self._session_factory() as session:
            controller = Controller(session)
            run = await self._get_run(session, run_id)
            if run.status not in (FlowStatus.WAITING_CI.value, FlowStatus.EVALUATING_CI.value):
                raise ReviewFeedbackRefused(
                    "correction_window_closed",
                    f"run {run_id!r} is {run.status!r} — the correction cycle only "
                    "re-enters the delivery pipeline before the human decision",
                )
            run.commit_cycle = cycle + 1
            if run.status == FlowStatus.WAITING_CI.value:
                await controller.transition(
                    run_id, FlowStatus.EVALUATING_CI, reason="review feedback received"
                )
            await controller.transition(run_id, FlowStatus.PROPOSING, reason=reason)
            await session.commit()
        # Durable-first: the request is marked dispatched BEFORE the
        # advance legs run, so a crash mid-dispatch resumes through the
        # proposing status (the repair leg's own crash-resume discipline)
        # instead of dispatching twice.
        await mark_review_feedback_request(
            self._session_factory, run_id, request.note_id, REQUEST_DISPATCHED
        )
        logger.info(
            "Run %s enters review correction cycle %d (head %s) — re-dispatching",
            run_id[:8],
            cycle + 1,
            request.head_sha[:8],
        )
        if is_harness_backend(backend_name or self._backend_name()):
            await self._advance_harness(
                project_id, run_id, repair_context=context, repair_reason=reason
            )
        else:
            await self._advance_proposal(
                project_id, run_id, repair_context=context, repair_reason=reason
            )

    async def _read_run_evidence(self, run_id: str) -> dict | None:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            return dict(run.evidence or {}) if run is not None else None

    def _rf_correction_context(self, request: ReviewFeedbackRequest) -> str:
        """The bounded context the correction's executor brief appends."""
        lines = [
            f"Reviewer correction (discussion {request.discussion_id or 'unknown'}, "
            f"note {request.note_id}) on head {request.head_sha}:",
            request.text.strip(),
        ]
        if request.referenced_paths:
            lines.append("Permitted paths: " + ", ".join(request.referenced_paths))
        if request.diff_context:
            lines.append(f"Referenced diff: {request.diff_context}")
        lines.append("Only the named correction is in scope — preserve every other human edit.")
        return "\n".join(lines)

    async def _reply_review_feedback(
        self,
        project_id: int,
        mr_iid: int | None,
        run_id: str,
        note_id: str,
        body: str,
        *,
        allow_repeat: bool = False,
    ) -> None:
        """Post ONE operator-visible reply on the MR — at most one per note id.

        The /go refusal A11 pattern: the journaled row carries the note's
        delivery identity in ``idempotency_key``, so a redelivered webhook
        earns exactly one reply. ``allow_repeat`` marks the follow-ups
        that belong to the SAME note but a later lifecycle step (the
        stale-head conflict is reported when it is discovered, at the
        re-dispatch — the requester already saw the staging reply).
        """
        if mr_iid is None:
            return
        key = None if allow_repeat else f"review-feedback:{project_id}:{mr_iid}:{note_id}"
        if key is not None and await self._rf_reply_delivered(key):
            return
        async with self._session_factory() as session:
            action = ActionLog(
                flow_run_id=run_id,
                action_kind=_REVIEW_FEEDBACK_REPLY_KIND,
                correlation_id=f"mr-{mr_iid}",
                idempotency_key=key,
                status="requested",
            )
            session.add(action)
            await session.commit()
            action_id = action.id
        try:
            note = await self._gitlab.create_mr_note(project_id, mr_iid, body)
        except (httpx.HTTPError, GitLabAPIError) as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        await self._complete_action(action_id, "succeeded", {"note_id": getattr(note, "id", None)})

    async def _rf_reply_delivered(
        self, key: str, *, kind: str = _REVIEW_FEEDBACK_REPLY_KIND
    ) -> bool:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ActionLog.id)
                        .where(
                            ActionLog.action_kind == kind,
                            ActionLog.idempotency_key == key,
                            ActionLog.status == "succeeded",
                        )
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        return row is not None

    async def _read_mr_head(
        self, project_id: int, mr_iid: int, issue_iid: int | None, run_id: str
    ) -> str:
        """The CURRENT MR head — the LIVE source-branch head, not the MR doc.

        The MR document's ``sha`` is frozen where the provider snapshotted
        it; the head that matters (a human push moving the branch between
        request and publication) is the branch's own head — the same read
        the drift checks use. The MR sha is the fallback when the branch
        read fails.
        """
        try:
            head = await self._gitlab.get_branch_head(project_id, factory_branch(issue_iid, run_id))
        except GitLabAPIError:
            head = ""
        if head:
            return head
        mr = await self._gitlab.get_merge_request(project_id, mr_iid)
        return str(mr.sha or "").strip()

    async def _discussions_or_none(self, project_id: int, mr_iid: int | None):
        """The MR's discussions, or ``None`` when the surface is unavailable.

        A 404 marks the auxiliary discussions surface unavailable for
        this process (older CE instances, scoped tokens): the typed
        deleted-discussion check and the resolution accounting degrade
        honestly — logged, never fatal, and never a silent guess that a
        LIVE discussion resolved.
        """
        if mr_iid is None or self._discussions_surface_down:
            return None
        try:
            return await self._gitlab.list_discussions(project_id, mr_iid)
        except GitLabAPIError as exc:
            if getattr(exc, "status_code", None) == 404:
                if not self._discussions_surface_down:
                    logger.warning(
                        "Discussions surface unavailable for MR !%s — resolution "
                        "accounting degrades (logged, not guessed)",
                        mr_iid,
                    )
                self._discussions_surface_down = True
                return None
            raise

    def _discussion_resolution_states(self, discussions) -> dict[str, bool]:
        """``{discussion_id: resolved}`` over the RESOLVABLE discussions only.

        A plain (non-resolvable) thread can never resolve, so it is never
        a required discussion — GitLab's own resolvable model decides
        which threads can gate readiness, not forge.
        """
        states: dict[str, bool] = {}
        for discussion in discussions or []:
            resolvable = [note for note in discussion.notes if note.resolvable]
            if not resolvable:
                continue
            states[discussion.id] = bool(resolvable[0].resolved)
        return states

    @staticmethod
    def _rf_unauthorized_body(actor: str) -> str:
        return (
            f"Review feedback from @{actor} was ignored — corrections and questions "
            "are restricted to the configured approvers (the same gate as `/go`: "
            f"FORGE_APPROVERS).{_automated_footer()}"
        )

    @staticmethod
    def _rf_deleted_discussion_body(discussion_id: str) -> str:
        return (
            f"The review feedback references discussion `{discussion_id}`, which no "
            "longer exists on this merge request — the request is recorded but "
            f"refused (`deleted_discussion`). Re-raise the comment on a live "
            f"discussion thread.{_automated_footer()}"
        )

    @staticmethod
    def _rf_clarification_body(run_id: str, question: str) -> str:
        return (
            f"Question recorded for run `{run_id[:8]}` — a human approver answers "
            "in this thread (forge does not answer questions with implementation "
            "budget). No code change is dispatched for a "
            f"clarification.{_automated_footer()}"
        )

    @staticmethod
    def _rf_material_body(run_id: str, text: str, allowed_paths: tuple[str, ...]) -> str:
        scope = ", ".join(f"`{path}`" for path in allowed_paths) or "(none recorded)"
        return (
            f"Review feedback on run `{run_id[:8]}` is classified **material change**: "
            "the requested edit cannot be proven inside the approved write scope "
            f"({scope}), so it is recorded as a material proposal — the approved "
            "permissions are never expanded by a comment. Raise the change through "
            "the material-revision route (a revised plan/contract a human approves) "
            f"or a new implementation request.{_automated_footer()}"
        )

    @staticmethod
    def _rf_window_closed_body(run_id: str, status: str) -> str:
        return (
            f"Review feedback on run `{run_id[:8]}` is recorded, but the run is "
            f"`{status}` — corrections re-enter only before the human decision "
            "(a `ready_for_human` run's decision is final). The audit stands; raise "
            f"the change as a new request.{_automated_footer()}"
        )

    @staticmethod
    def _rf_conflicting_body(run_id: str, pending: ReviewFeedbackRequest) -> str:
        return (
            f"Review feedback on run `{run_id[:8]}` conflicts with the correction "
            f"already staged from note `{pending.note_id}` (decision "
            f"`{pending.decision_id}`) — one live correction at a time. Approve or "
            f"resolve the staged one first.{_automated_footer()}"
        )

    @staticmethod
    def _rf_refused_body(run_id: str, exc: ReviewFeedbackRefused) -> str:
        return (
            f"Review feedback on run `{run_id[:8]}` was refused [`{exc.code}`]: "
            f"{exc.detail}.{_automated_footer()}"
        )

    @staticmethod
    def _rf_staged_body(
        run_id: str, decision_id: str, head: str, request: ReviewFeedbackRequest
    ) -> str:
        return (
            f"**Reviewer correction staged** for run `{run_id[:8]}` — bounded to the "
            f"named edit, bound to MR head `{head[:12]}`… (discussion "
            f"`{request.discussion_id or 'unknown'}`).\n\n"
            f"A human approves it with `/approve-revision {run_id} {decision_id}`; "
            "the approved correction becomes the active revision and the next "
            "dispatch carries it (the affected checks and the review rerun before "
            "any readiness claim).\n\n"
            "Forge never merges and never resolves this discussion — the reviewer's "
            f"resolve is the decision.{_automated_footer()}"
        )

    @staticmethod
    def _rf_stale_head_body(run_id: str, request: ReviewFeedbackRequest) -> str:
        return (
            f"## 🛑 Review correction for run `{run_id[:8]}` blocked: `stale_head`\n\n"
            f"The correction bound MR head `{request.head_sha[:12]}`…, but the head "
            "has moved — human edits are preserved and nothing was dispatched. "
            "Re-raise the correction against the current "
            f"head.{_automated_footer()}"
        )

    @staticmethod
    def _rf_replay_body(
        recorded: ReviewFeedbackRequest, allowed_paths: tuple[str, ...] = ()
    ) -> str:
        """The outcome body a REPLAYED delivery re-derives from the record.

        The lifecycle states that posted a substantive reply rebuild it
        byte-identically, so the reply journal's note-id dedup collapses
        the repeat (and re-attempts it only when the first posting
        failed). The terminal refusal states posted their own reply at
        the time; a replay of those stays silent.
        """
        if recorded.status == REQUEST_CLARIFICATION_OPEN:
            return RunService._rf_clarification_body(recorded.run_id, recorded.text)
        if recorded.status == REQUEST_MATERIALIZED:
            return RunService._rf_material_body(recorded.run_id, recorded.text, allowed_paths)
        if recorded.status == REQUEST_WINDOW_CLOSED:
            return RunService._rf_window_closed_body(
                recorded.run_id, "past the pre-decision window"
            )
        if recorded.status == REQUEST_STAGED:
            return RunService._rf_staged_body(
                recorded.run_id, recorded.decision_id, recorded.head_sha, recorded
            )
        return ""

    async def handle_issue_edited(
        self,
        *,
        project_id: int,
        issue_iid: int | None,
        issue_title: str,
        issue_body: str,
        author_username: str,
    ) -> str | None:
        """``issues`` update (title/description): keep the gate honest.

        The #29 GitLab mirror of ``GitHubRunService.handle_issue_edited``
        — the same three cases, decided against the issue-text snapshot
        frozen at plan time (the RunSpec's ``task_digest``):

        - the run is still ``waiting_approval`` and its gate is unconsumed:
          the waiting plan is stale. The stale run is cancelled durably
          (cancel-as-revoke, F13), a fresh run plans from the new text and
          a note says the plan was regenerated.
        - the run is beyond the gate: the agent executes the APPROVED
          snapshot — never yanked mid-flight. One informational note says
          the edit is not in the current plan.
        - the text matches the snapshot: a redelivered edit (or an edit
          back to the planned text) — nothing went stale, nothing happens.

        Returns the id of the run that owns the issue afterwards (None when
        the edit was ignored).
        """
        if issue_iid is None:
            logger.info("GitLab issue edit without an issue iid — ignoring")
            return None
        admission = check_admission(self._settings, self._config, project_id, author_username)
        if not admission.allowed:
            # ADR-0009: forge reacts to an edit only for an actor it would
            # let start a run — anyone else's edit never yanks or replans.
            logger.info(
                "GitLab issue edit by @%s on project %d !%s ignored — not admitted (%s)",
                author_username,
                project_id,
                issue_iid,
                admission.reason,
            )
            return None

        run = await self._find_active_run(project_id, issue_iid)
        stale_run_id: str | None = None
        if run is not None:
            edited_digest = task_digest_of(issue_title, issue_body)
            if await self._frozen_task_digest(run.id) == edited_digest:
                logger.info(
                    "GitLab issue edit on !%s matches run %s's snapshot — ignoring",
                    issue_iid,
                    run.id[:8],
                )
                return run.id

            if run.status == FlowStatus.WAITING_APPROVAL.value and not await self._gate_consumed(
                run.id
            ):
                stale_run_id = run.id
                await self._revoke_publication_grant(stale_run_id)
                await self._transition(
                    stale_run_id,
                    FlowStatus.CANCELLED,
                    reason=f"superseded by issue edit by @{author_username}",
                )
                logger.info(
                    "GitLab run %s superseded by an issue edit — replanning !%s",
                    stale_run_id[:8],
                    issue_iid,
                )
            else:
                # Approved / in flight: the change is NOT pulled into the
                # approved plan.
                await self._post_journaled_note(
                    project_id,
                    issue_iid,
                    f"Issue edited while run `{run.id[:8]}` is in flight — the change is "
                    "**not** in the approved plan. The run keeps executing its approved "
                    "snapshot; run `@forge /cancel` and `@forge /implement` if it should "
                    "pick the change up.\n\n*This is an automated message.*",
                    run.id,
                    "issue_edited_note",
                )
                return run.id
        elif (rework_source := await self._replan_interrupted(project_id, issue_iid)) is None:
            logger.info("GitLab issue edit on !%s — no active run", issue_iid)
            return None

        new_run_id = await self.start_run(
            project_id, issue_iid, issue_title, issue_body, author_username
        )
        # R24 acceptance honesty: the replacement run records WHICH prior
        # attempt it reworks, and a cancelled-superseded run records what
        # replaced it — evidence-only linkage for the delivery metrics; the
        # failed sibling stays in the attempts denominator either way.
        prior_run_id = stale_run_id or rework_source
        if prior_run_id is not None and prior_run_id != new_run_id:
            await self._merge_run_evidence(new_run_id, {"rework_of": prior_run_id})
        if stale_run_id is not None:
            await self._merge_run_evidence(stale_run_id, {"superseded_by": new_run_id})
        # A retried replan (the first attempt died mid-step) has no stale run
        # of its own to name — the note then just says where the plan came from.
        if stale_run_id is not None:
            origin = (
                f"The plan of run `{stale_run_id[:8]}` was **stale** — the issue was edited "
                "while its plan waited for approval. It was cancelled and the plan "
            )
        else:
            origin = (
                "The issue was edited while its plan waited for approval — that plan was "
                "stale, so the plan "
            )
        await self._post_journaled_note(
            project_id,
            issue_iid,
            f"{origin}"
            f"regenerated from the current issue body as run `{new_run_id[:8]}`. "
            f"Approve with `@forge /go {new_run_id}`.\n\n*This is an automated message.*",
            new_run_id,
            "replan_note",
        )
        return new_run_id

    async def handle_label_removed(
        self, *, project_id: int, issue_iid: int | None, author_username: str
    ) -> int:
        """Trigger-label removal: label-off = cancel at the gate (#29).

        The GitLab mirror of ``GitHubRunService.handle_label_removed`` —
        symmetry with label-on = plan (ADR-0020 §4): removing the trigger
        label cancels runs still parked in ``waiting_approval`` — the plan
        was never approved, so nothing executed is lost. Runs past the gate
        are untouched: the approval consumed that plan, the label no longer
        owns it. Returns the number of cancelled runs.
        """
        if issue_iid is None:
            return 0
        if author_username not in self._approvers():
            logger.info(
                "GitLab label removal by @%s on !%s ignored — not an approver",
                author_username,
                issue_iid,
            )
            return 0
        async with self._session_factory() as session:
            runs = (
                (
                    await session.execute(
                        select(FlowRun).where(
                            FlowRun.provider == "gitlab",
                            FlowRun.project_id == project_id,
                            FlowRun.issue_iid == issue_iid,
                            FlowRun.status == FlowStatus.WAITING_APPROVAL.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            run_ids = [run.id for run in runs]
        for run_id in run_ids:
            await self._revoke_publication_grant(run_id)
            await self._transition(
                run_id,
                FlowStatus.CANCELLED,
                reason=f"trigger label removed by @{author_username}",
            )
            await self._post_journaled_note(
                project_id,
                issue_iid,
                f"Run `{run_id[:8]}` **cancelled** — the `{self._trigger_label()}` label was "
                f"removed by @{author_username} while its plan waited for approval. Re-add "
                "the label (or run `@forge /implement`) to plan again."
                "\n\n*This is an automated message.*",
                run_id,
                "cancel_note",
            )
            logger.info(
                "GitLab run %s cancelled — trigger label removed by @%s",
                run_id[:8],
                author_username,
            )
        return len(run_ids)

    def _trigger_label(self) -> str:
        """The configured plan-trigger label (ADR-0020 §4)."""
        return str(getattr(self._settings, "FORGE_TRIGGER_LABEL", "forge") or "forge")

    async def _frozen_task_digest(self, run_id: str) -> str | None:
        """The issue-text snapshot digest frozen into the RunSpec at plan time."""
        async with self._session_factory() as session:
            spec = (
                (
                    await session.execute(
                        select(RunSpec).where(RunSpec.run_id == run_id).order_by(RunSpec.id)
                    )
                )
                .scalars()
                .first()
            )
        if spec is None:
            return None
        document = spec.document if isinstance(spec.document, dict) else {}
        digest = document.get("task_digest")
        return str(digest) if digest else None

    async def _gate_consumed(self, run_id: str) -> bool:
        """Whether the run's latest gate decision is already consumed.

        Status alone is not proof: between ``consume_approval`` and the
        PROPOSING transition commit the run still reads ``waiting_approval`` —
        an issue edit in exactly that window must not cancel an approved run
        (the "gate already consumed" guard).
        """
        async with self._session_factory() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval)
                        .where(GateApproval.flow_run_id == run_id)
                        .order_by(GateApproval.id.desc())
                    )
                )
                .scalars()
                .first()
            )
        return gate is not None and gate.consumed_at is not None

    async def _action_succeeded(self, run_id: str, action_kind: str) -> bool:
        """Whether a journaled external write of *kind* already succeeded (R07).

        The replay predicate for the notify-shaped steps: a crashed leg's
        re-entry finds the succeeded journal row and skips the remote call
        instead of posting the same note twice.
        """
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ActionLog.id)
                        .where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == action_kind,
                            ActionLog.status == "succeeded",
                        )
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        return row is not None

    async def _gate_exists(self, run_id: str) -> bool:
        """Whether a pending decision row already exists for the run (R07)."""
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(GateApproval.id).where(GateApproval.flow_run_id == run_id).limit(1)
                    )
                )
                .scalars()
                .first()
            )
        return row is not None

    async def _revoke_publication_grant(self, run_id: str) -> None:
        """Cancel-as-revoke's durable core (F13, ADR-0018 §4).

        Sets ``cancel_requested`` — the flag an in-flight publication leg
        re-reads before writing — and withdraws the run's scheduled steps so
        no worker picks them up later. The terminal transition stays the
        caller's.
        """
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            run.cancel_requested = True
            await session.execute(
                update(StepRun)
                .where(StepRun.flow_run_id == run_id, StepRun.status == "scheduled")
                .values(status="cancelled")
            )
            await session.commit()

    async def _replan_interrupted(self, project_id: int, issue_iid: int) -> str | None:
        """The interrupted replan's run id, when an edit-triggered replan died mid-step.

        The command step retries with backoff, and the retry must be able to
        finish what the ``202`` promised: the stale run is already cancelled,
        so a plain ``no active run`` would leave the issue run-less. Two
        shapes are retried — the superseded cancellation itself (``start_run``
        never created the fresh run), and a ``planning_failed`` run it did
        create (the fresh attempt plans again, exactly like a retried
        ``/implement``).

        R24: returns the interrupted run's ID (not a bool) so the fresh run
        records its ``rework_of`` linkage even when the superseding replan
        is the RETRIED one and has no stale run of its own to name.
        """
        async with self._session_factory() as session:
            run = (
                (
                    await session.execute(
                        select(FlowRun)
                        .where(
                            FlowRun.provider == "gitlab",
                            FlowRun.project_id == project_id,
                            FlowRun.issue_iid == issue_iid,
                        )
                        .order_by(FlowRun.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if run is None:
            return None
        interrupted = (
            run.status == FlowStatus.CANCELLED.value
            and (run.status_reason or "").startswith("superseded by issue edit")
        ) or (
            run.status == FlowStatus.FAILED.value
            and (run.status_reason or "").startswith("planning_failed")
        )
        return run.id if interrupted else None

    async def handle_command_note(
        self,
        project_id: int,
        note_text: str,
        author_username: str,
        issue_iid: int | None,
        author_user_id: int = 0,
        now: datetime | None = None,
        delivery_id: str | None = None,
    ) -> None:
        """``@forge /go <run-id>``: validate + consume the gate, then advance.

        The run id is the full 32-hex id from the plan footer OR an
        unambiguous ≥8-hex prefix (the plan heading's short form — LIVE
        2026-09-21: the short form used to miss the regex and the note was
        ignored silently, with the step even recording "succeeded"). Every
        ignore path now answers the operator with one journaled note,
        rate-limited to ONE reply per note id (a bot reply storm would be
        worse than silence).

        Idempotent: a re-delivered note finds the run already out of
        ``waiting_approval`` or the gate already consumed — both ignore (with
        a one-line duplicate reply); the gate consumption logic itself is
        unchanged.
        """
        match = _GO_RE.search(note_text or "")
        if match is None:
            return
        requested = match.group(1).lower()
        now = now or datetime.now(timezone.utc)
        # An ignored /go no longer returns silently: the refusal body is
        # composed inside the session but POSTED after it closes (the note
        # journal opens its own session — never nested under this one).
        # NXT-01: the pass below ends in exactly one typed outcome — the
        # refusal to journal, or the advance instruction — so no nullable
        # run ever flows past the discrimination.
        outcome: _RefusedGo | _AdvanceGo
        async with self._session_factory() as session:
            controller = Controller(session)
            resolved = await _resolve_go_run(
                session, requested, project_id=project_id, issue_iid=issue_iid
            )
            if isinstance(resolved, _RefusedGo):
                outcome = resolved
            else:
                run = resolved.run
                if run.status in _RESUMABLE_ADVANCE_STATUSES:
                    # ADR-0017 §3: the gate is consumed and a crashed worker left
                    # the run mid-advance; the re-claimed command step is the
                    # recovery driver. The leg below looks for the
                    # already-existing effects before creating new ones.
                    outcome = _AdvanceGo(
                        run_id=run.id,
                        backend_name=(
                            str((run.evidence or {}).get("backend") or "").strip()
                            or self._backend_name()
                        ),
                        resuming=True,
                        status=run.status,
                    )
                elif run.status != FlowStatus.WAITING_APPROVAL.value:
                    # Already advanced (or terminal) — duplicate /go delivery.
                    logger.info(
                        "/go for run %s in status %s — ignoring duplicate", run.id[:8], run.status
                    )
                    outcome = _RefusedGo(_go_duplicate_body(run.id, run.status), run_id=run.id)
                elif author_username not in self._approvers():
                    # ADR-0009: authority comes from trusted configuration, not authorship.
                    logger.info(
                        "/go from @%s who is not in FORGE_APPROVERS — ignoring", author_username
                    )
                    outcome = _RefusedGo(_go_not_approver_body(author_username), run_id=run.id)
                else:
                    gate = (
                        (
                            await session.execute(
                                select(GateApproval)
                                .where(GateApproval.flow_run_id == run.id)
                                .order_by(GateApproval.id.desc())
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if gate is None:
                        # F15 (ADR-0018 §2): decisions are created at plan
                        # publication — a /go without a pending decision has
                        # nothing to consume.
                        logger.info("No pending decision for run %s — ignoring /go", run.id[:8])
                        return
                    if not is_valid(
                        gate,
                        now,
                        plan_digest=run.plan_digest or "",
                        base_sha=run.base_sha or "",
                        policy_digest=self._policy_digest(),
                        spec_digest=run.spec_digest,
                    ):
                        logger.info(
                            "Decision for run %s is expired/invalid — ignoring /go", run.id[:8]
                        )
                        return
                    # The decision was opened anonymously at plan publication;
                    # record who consumed it.
                    gate.approver_user_id = author_user_id
                    try:
                        await consume_approval(session, gate.id, now)
                    except GateAlreadyConsumed:
                        logger.info("Gate for run %s already consumed — ignoring", run.id[:8])
                        outcome = _RefusedGo(_go_duplicate_body(run.id, run.status), run_id=run.id)
                    else:
                        # R04 (ADR-0018 §1): consuming the gate binds the run
                        # to the executable RunSpec the pending decision froze
                        # — the advance legs execute ONLY its digest-verified
                        # content (frozen task text, plan, model route,
                        # verification contract, budgets). A spec that no
                        # longer matches this digest is blocked(spec_invalid),
                        # never re-read from live settings.
                        await controller.transition(
                            run.id, FlowStatus.PROPOSING, reason=f"approved by @{author_username}"
                        )
                        # ADR-0015: the backend frozen at run start decides the
                        # advance leg. Read BEFORE the commit below expires the
                        # ORM attributes.
                        outcome = _AdvanceGo(
                            run_id=run.id,
                            backend_name=(
                                str((run.evidence or {}).get("backend") or "").strip()
                                or self._backend_name()
                            ),
                            status=run.status,
                        )
                        # Commit ONLY the accepted leg — a duplicate keeps the
                        # original no-write semantics (the approver_user_id
                        # touch above must not survive a refused consumption).
                        await session.commit()

        if isinstance(outcome, _RefusedGo):
            await self._post_go_refusal(
                project_id, issue_iid, outcome.body, delivery_id, outcome.run_id
            )
            return

        if outcome.resuming:
            logger.info(
                "Run %s found %s after a worker crash — resuming the advance leg",
                outcome.run_id[:8],
                outcome.status,
            )
            if is_harness_backend(outcome.backend_name):
                await self._advance_harness(project_id, outcome.run_id)
            else:
                await self._advance_proposal(project_id, outcome.run_id)
            return

        logger.info(
            "Gate for run %s consumed by @%s — advancing", outcome.run_id[:8], author_username
        )
        if is_harness_backend(outcome.backend_name):
            await self._advance_harness(project_id, outcome.run_id)
        else:
            await self._advance_proposal(project_id, outcome.run_id)

    async def _post_go_refusal(
        self,
        project_id: int,
        issue_iid: int | None,
        body: str,
        delivery_id: str | None,
        run_id: str | None,
    ) -> None:
        """Post one operator-visible /go refusal note — at most one per note id.

        The reply is journaled like every external write (intent first,
        outcome second). Rate limit: the refusal row carries the note's
        delivery identity in ``idempotency_key`` (the /retry A11 pattern),
        so a redelivered webhook earns exactly ONE reply — the gateway
        inbox dedup already collapses most redeliveries; this is the
        belt-and-braces that keeps a reply storm impossible. Direct
        invocations without a delivery id keep the pre-dedup behavior (a
        reply per call), mirroring ``retry_delivery_key``'s contract.
        """
        if issue_iid is None:
            return
        key = f"go-note:{delivery_id}" if delivery_id else None
        if key is not None and await self._go_refusal_delivered(key):
            return
        async with self._session_factory() as session:
            action = ActionLog(
                flow_run_id=run_id,
                action_kind=_GO_REFUSAL_KIND,
                correlation_id=f"issue-{issue_iid}",
                idempotency_key=key,
                status="requested",
            )
            session.add(action)
            await session.commit()
            action_id = action.id
        try:
            note = await self._gitlab.create_issue_note(project_id, issue_iid, body)
        except httpx.HTTPError as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        await self._complete_action(action_id, "succeeded", {"note_id": note.get("id")})

    async def _go_refusal_delivered(self, key: str) -> bool:
        """Whether this note id already earned its one refusal reply."""
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ActionLog.id)
                        .where(
                            ActionLog.action_kind == _GO_REFUSAL_KIND,
                            ActionLog.idempotency_key == key,
                            ActionLog.status == "succeeded",
                        )
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        return row is not None

    async def run_command(self, metadata: dict[str, Any]) -> None:
        """Dispatch a ``run_command`` task produced by the gateway router."""
        command = metadata.get("command")
        if command == "start_run":
            project_id = metadata["project_id"]
            issue_iid = metadata["issue_iid"]
            issue = await self._gitlab.get_issue(project_id, issue_iid)
            await self.start_run(
                project_id,
                issue_iid,
                issue.title,
                issue.description or "",
                metadata.get("author_username", ""),
            )
        elif command == "go":
            await self.handle_command_note(
                metadata["project_id"],
                metadata.get("note_text", ""),
                metadata.get("author_username", ""),
                metadata.get("issue_iid"),
                author_user_id=int(metadata.get("author_user_id") or 0),
                # The note id is the delivery identity — it rate-limits the
                # refusal reply to one per note (A11 pattern, like /retry).
                delivery_id=str(metadata.get("note_id") or "") or None,
            )
        elif command == "cancel":
            await self.handle_cancel_note(
                metadata["project_id"],
                metadata.get("note_text", ""),
                metadata.get("author_username", ""),
                metadata.get("issue_iid"),
            )
        elif command == "retry":
            await self.handle_retry_note(
                metadata["project_id"],
                metadata.get("note_text", ""),
                metadata.get("author_username", ""),
                metadata.get("issue_iid"),
                # A11: the note id is the delivery identity — the same
                # redelivered webhook dedupes to a no-op at the attempt.
                delivery_id=str(metadata.get("note_id") or "") or None,
            )
        elif command == "status":
            await self.handle_status_note(
                metadata["project_id"],
                metadata.get("note_text", ""),
                metadata.get("author_username", ""),
                metadata.get("issue_iid"),
            )
        elif command == "why_blocked":
            await self.handle_why_blocked_note(
                metadata["project_id"],
                metadata.get("note_text", ""),
                metadata.get("author_username", ""),
                metadata.get("issue_iid"),
            )
        elif command == "reconcile":
            await self.handle_reconcile_note(
                metadata["project_id"],
                metadata.get("note_text", ""),
                metadata.get("author_username", ""),
                metadata.get("issue_iid"),
            )
        elif command == "review_feedback":
            # Q39-13 (#332): a reviewer comment on the Draft MR — the
            # GitLab MR-note ingress (the same metadata shape the note
            # dispatch already carries for MR-bound commands like
            # security triage: the note id, the mr_iid, the discussion).
            await self.handle_review_feedback_note(
                metadata["project_id"],
                metadata.get("note_text", ""),
                metadata.get("author_username", ""),
                metadata.get("mr_iid"),
                metadata.get("issue_iid"),
                note_id=str(metadata.get("note_id") or "") or None,
                discussion_id=str(metadata.get("discussion_id") or ""),
            )
        elif command == "issue_edited":
            # The edited text travels in the command metadata (the webhook
            # payload's issue object) — no extra API read on the hot path.
            await self.handle_issue_edited(
                project_id=metadata["project_id"],
                issue_iid=metadata.get("issue_iid"),
                issue_title=str(metadata.get("issue_title") or ""),
                issue_body=str(metadata.get("issue_body") or ""),
                author_username=str(metadata.get("author_username") or ""),
            )
        elif command == "unlabeled":
            await self.handle_label_removed(
                project_id=metadata["project_id"],
                issue_iid=metadata.get("issue_iid"),
                author_username=str(metadata.get("author_username") or ""),
            )
        else:
            logger.warning("Unknown run command %r — ignoring", command)

    # ------------------------------------------------------------------
    # Proposal pipeline: propose → validate → commit → draft MR → waiting_ci
    # ------------------------------------------------------------------

    async def _advance_proposal(
        self,
        project_id: int,
        run_id: str,
        *,
        repair_context: str = "",
        repair_reason: str | None = None,
    ) -> None:
        """Run one propose → validate → commit → MR cycle (initial or repair).

        The caller has already moved the run to ``proposing`` (post-gate or
        repair re-entry) and bumped ``commit_cycle`` for repairs. A crash
        resume may enter mid-leg instead (the run already sat in
        ``validating``/``committing``/``ensuring_draft_mr``): stages already
        left behind are not re-entered — the walk continues from where the
        durable state says it is (ADR-0017 §3).
        """
        # NEXT-11/R32-05: this is the dispatch boundary — the execution
        # lease is reserved here (idempotently per run), so no path that
        # reaches the paid proposal (``/go``, ``/retry``, a repair
        # re-dispatch, the recovery scan) can bypass the project's slot
        # limit — the same choke point the Azure and GitHub paths cross.
        if not await self._reserve_execution_capacity(project_id, run_id):
            return
        # F22/R13: (re)bind this run's budget guard before any paid call —
        # the guard lives per run in the database, and the service instance
        # handling this leg may be fresh (the worker builds one per command),
        # so the start_run binding cannot be relied on here.
        await self._apply_run_budget(run_id)
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            # F02/R06: ONE context owns this attempt's bases — the implementer
            # reads and materializes at ``attempt_base``, update/delete
            # existence is validated against the SAME snapshot, and the writer
            # pins the branch to it. Repairs build on the last VERIFIED
            # candidate, not the original approved base — otherwise cycle 2
            # cannot see cycle 1's files and its update would roll work back.
            # The ``source_base`` stays frozen for the final cumulative
            # review/acceptance only; it never decides repair existence.
            attempt = AttemptContext.of(run)
            entry_status = run.status
            recorded_attempt = (run.evidence or {}).get("attempt")

        # R04 (ADR-0018 §1): the executable spec is THE approved input — the
        # frozen task text, plan artifact, model route and path policy come
        # from it, never from live Settings or a live issue re-read. A
        # missing or tampered spec blocks the run (never a silent fallback).
        try:
            spec = await self._load_executable_spec(run_id)
        except SpecInvalid as exc:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"spec_invalid: {exc}")
            return
        plan_summary, files_hint = spec.plan_summary, list(spec.plan_files_hint)
        await self._record_spec_drift(project_id, run_id, spec)
        mid_leg = entry_status in {"validating", "committing", "ensuring_draft_mr"}
        # ADR-0017 §3 (R06): mid-leg, the persisted record — not a fresh
        # re-derivation — says what this attempt actually started from.
        # Durable state may have drifted under a crashed attempt
        # (``candidate_shas``, ``commit_cycle``); re-deriving would aim the
        # remaining legs at a snapshot the proposal never read and re-propose
        # (a second paid call) changes that are already materialized. A
        # same-cycle record is never stale, so its base wins.
        if (
            mid_leg
            and isinstance(recorded_attempt, dict)
            and recorded_attempt.get("cycle") == attempt.cycle
            and isinstance(recorded_attempt.get("attempt_base"), str)
            and recorded_attempt.get("attempt_base")
        ):
            previous = recorded_attempt.get("previous_candidate")
            attempt = AttemptContext(
                cycle=attempt.cycle,
                attempt_base=str(recorded_attempt["attempt_base"]),
                source_base=attempt.source_base,
                previous_candidate=previous if isinstance(previous, str) else None,
            )

        # ADR-0017 §3 (R06): a walk that re-enters its own attempt adopts the
        # manifest it already materialized for THIS cycle at THIS base — same
        # changes, no second paid proposal. Anything else (first proposal, a
        # new repair cycle, a stale or absent record) proposes at the attempt
        # base as before.
        # R04: no live issue re-read — the implementer executes the FROZEN
        # task text the approver saw, whatever the issue shows now (drift is
        # recorded as ``spec_drift`` evidence above, never re-read into work).
        changeset = (
            changeset_from_document(recorded_attempt.get("manifest"))
            if isinstance(recorded_attempt, dict)
            and recorded_attempt.get("cycle") == attempt.cycle
            and recorded_attempt.get("attempt_base") == attempt.attempt_base
            else None
        )
        if changeset is not None:
            logger.info(
                "Run %s cycle %d resumes on its recorded manifest (%d changes)",
                run_id[:8],
                attempt.cycle,
                len(changeset.changes),
            )
        else:
            try:
                changeset = await self._implementer.propose(
                    run,
                    spec.task_title,
                    plan_summary=plan_summary,
                    files_hint=files_hint,
                    repair_context=repair_context,
                    attempt_base=attempt.attempt_base,
                    task_text=spec.task_text,
                    model_route=spec.model_route,
                )
            except MaterializationError as exc:
                # No fuzzy matching, ever (ADR-0001): an inapplicable proposal is
                # a blocked run, not a guess.
                await self._to_terminal(
                    run_id, FlowStatus.BLOCKED, f"changeset_invalid: materialization: {exc}"
                )
                return
            except (LLMError, LLMResponseError) as exc:
                # R13: a budget refusal means the provider was never
                # contacted and no in-run retry can succeed — classify it
                # blocked(budget_exhausted), not a proposal failure.
                if str(exc) == BUDGET_EXHAUSTED:
                    await self._to_terminal(
                        run_id,
                        FlowStatus.BLOCKED,
                        f"{BUDGET_EXHAUSTED}: proposer refused — run budget cannot grant a call",
                    )
                else:
                    await self._to_terminal(run_id, FlowStatus.FAILED, f"proposal_failed: {exc}")
                return
            # R06: persist the attempt (number, manifest, previous candidate)
            # the moment it materializes — the durable record a resumed walk
            # continues from.
            await self._merge_run_evidence(
                run_id,
                {
                    "attempt": {
                        **attempt.document(),
                        "manifest": changeset_to_document(changeset),
                    }
                },
            )

        if not mid_leg:
            await self._transition(run_id, FlowStatus.VALIDATING)

        # validating: trusted ADR-0001 validation; violations block the run.
        # The run's frozen RunSpec ``allowed_paths`` scope (v0.7 monorepo
        # scoping) is enforced here too — the builtin path is the second of
        # the two write boundaries (the trusted publisher is the other).
        # R04: the scope comes from the digest-verified spec, not a raw read.
        allowed_paths = list(spec.allowed_paths)
        # R06: existence is checked at the ATTEMPT base — the snapshot the
        # proposal was materialized against. The frozen source base would
        # report a cycle-1 created file as missing and block a legitimate
        # cycle-2 update of it.
        git_base = await self._fetch_git_base(
            project_id,
            [change.path for change in changeset.changes],
            attempt.attempt_base,
        )
        violations = validate_changeset(changeset, git_base, allowed_paths=allowed_paths)
        if violations:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "changeset_invalid: " + "; ".join(violations)
            )
            return

        # A resumed ``validating`` entry still owes the graph the committing
        # move — validating -> ensuring_draft_mr is not a legal edge (ADR-0004).
        if not mid_leg or entry_status == FlowStatus.VALIDATING.value:
            await self._transition(run_id, FlowStatus.COMMITTING)

        # committing: journaled, reconcilable write (ADR-0005). The factory
        # branch is cut from the FROZEN attempt base (review F03) — never
        # from the live target branch — and the expected head is checked
        # before the commit (BranchDriftError on drift).
        writer = self._writer_class(
            self._gitlab,
            self._session_factory,
            project_id,
            settle_seconds=self._publish_settle_seconds(),
        )
        # F13 (ADR-0018 §4): re-read the publication grant right before
        # applying — a cancel that landed while this proposal was in flight
        # revokes it, so this leg stands down instead of racing the cancel.
        if await self._publication_revoked(run_id):
            logger.info(
                "Run %s cancelled before publication — dropping in-flight proposal",
                run_id[:8],
            )
            return
        # ADR-0017 §3: look for the already-existing effect before creating a
        # new one — a crashed attempt may have landed the commit after the
        # journal recorded it but before the run state caught up.
        commit_sha = await self._committed_candidate(run_id, project_id, changeset.branch)
        if commit_sha is not None:
            logger.info(
                "Run %s adopting committed candidate %s — no second commit",
                run_id[:8],
                commit_sha[:8],
            )
        else:
            try:
                result = await writer.apply(
                    run_id,
                    changeset,
                    start_ref=attempt.attempt_base,
                    expected_head=attempt.attempt_base,
                    # R11 intent identity: the writer journals the durable
                    # publication intent (stable operation key + expected
                    # parent) BEFORE the HTTP effect; an open intent from a
                    # crashed attempt is probed-and-adopted, never
                    # duplicated and never misread as branch drift.
                    provider="gitlab",
                    repo=str(project_id),
                    commit_cycle=attempt.cycle,
                )
            except GitLabAPIError as exc:
                await self._to_terminal(run_id, FlowStatus.FAILED, f"commit_failed: {exc}")
                return
            except BranchDriftError as exc:
                # A human push on the factory branch is never force-fixed
                # (review F03): the guarded apply refused, so the run stops
                # here with the reason — the same contract as the harness
                # publisher and the GitHub/Azure lanes, and the branch keeps
                # the human's commit.
                await self._to_terminal(run_id, FlowStatus.BLOCKED, f"branch_drift: {exc}")
                return
            if result.outcome is WriteOutcome.SETTLING:
                # A12: the recovery probe was negative — the intent parked in
                # the effect-certainty window. NOT a terminal outcome: the
                # run stays in ``committing`` so the window-end re-probe
                # (evaluate_publication_intents) can adopt a late-landing
                # commit or resolve the honest unknown. The step's lease
                # expiry re-drives the leg, which adopts via the journal.
                logger.info(
                    "Run %s publication negative-probed — settling in the A12 "
                    "certainty window (no re-dispatch)",
                    run_id[:8],
                )
                return
            if result.outcome is WriteOutcome.UNKNOWN:
                # Unknown outcome: block the run, never blind-retry (ADR-0005).
                await self._to_terminal(run_id, FlowStatus.FAILED, "commit_unknown_outcome")
                return
            commit_sha = result.commit_sha
            if commit_sha is None:
                # A known (committed) outcome always carries the sha — treat a
                # missing one as unknown rather than crash downstream.
                await self._to_terminal(run_id, FlowStatus.FAILED, "commit_unknown_outcome")
                return

        if entry_status != FlowStatus.ENSURING_DRAFT_MR.value:
            await self._transition(run_id, FlowStatus.ENSURING_DRAFT_MR)

        # ensuring_draft_mr: Draft MR before CI (ADR-0007). On a repair the MR
        # already exists — update it instead of creating a second one.
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            mr_iid = run.mr_iid
            cycle = run.commit_cycle or 1
        if mr_iid is None:
            # ADR-0017 §3: a crashed attempt may have created the Draft MR
            # already (its intent/outcome is journaled) — adopt it, never
            # create a second MR for the run.
            mr_iid = await self._journaled_draft_mr(run_id, project_id, changeset.branch)
        try:
            if mr_iid is not None:
                await self._update_draft_mr(
                    project_id,
                    run_id,
                    mr_iid,
                    changeset.branch,
                    commit_sha,
                    cycle,
                    repair_reason=repair_reason,
                )
            else:
                mr_iid = await self._create_draft_mr(
                    project_id, run_id, changeset.branch, commit_sha
                )
        except GitLabAPIError as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"mr_failed: {exc}")
            return
        except httpx.HTTPError:
            # Lost MR response that survived retries — unknown, stop the run.
            await self._to_terminal(run_id, FlowStatus.FAILED, "mr_unknown_outcome")
            return

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(
                run_id, FlowStatus.WAITING_CI, reason=f"pipeline for {commit_sha[:8]}"
            )
            run = await self._get_run(session, run_id)
            run.mr_iid = mr_iid
            run.candidate_shas = list(run.candidate_shas or []) + [commit_sha]
            await session.commit()

        logger.info(
            "Run %s committed %s (cycle %d) — waiting for CI", run_id[:8], commit_sha[:8], cycle
        )

    async def _advance_harness(
        self,
        project_id: int,
        run_id: str,
        *,
        repair_context: str = "",
        repair_reason: str | None = None,
        driver: str | None = None,
        resume_mode: str = LANE_RESUME_MODE_FRESH,
    ) -> None:
        """ci_harness leg (ADR-0015): start the harness job, park the run.

        ``proposing`` = backend.start (ensures the factory branch, triggers
        the harness pipeline with the task brief as pipeline variables) →
        ``waiting_harness`` with the durable handle in the run's evidence.
        The reconciler (``evaluate_waiting_harness``) polls from here — the
        wait is worker-free, like ``waiting_ci``. A repair delegation appends
        the bounded CI-failure context to the brief so the harness fixes its
        own candidate.

        The dispatched driver is the one frozen in the RunSpec (ADR-0023
        §6); *driver* overrides it for a fallback advance. Called for a
        fallback the run is already ``waiting_harness`` — it stays parked,
        only the handle moves (ADR-0004 has no waiting_harness self-loop).

        R37-07 (#288): *resume_mode* is the WIP-continuity contract THIS
        dispatch selects for the lane — the GitLab half of R32-04, riding
        the pipeline variables the dispatched job exports into its env
        (``FORGE_LANE_RESUME`` carries ``forge.lane_driver.resume_mode``'s
        exact spelling; ``FORGE_LANE_RESUME_MODE`` carries the mode word for
        the dispatch ledger). ``fresh`` (the default — the initial dispatch
        and a repair cycle) restores nothing accidentally; ``required``
        (the ``/retry``/revival legs, selected by the persisted continuation
        decision) makes the held checkpoint's restore a precondition of the
        turn and the dispatch carries the decision's EXACT pinned
        ``continuation.checkpoint_digest``; ``restart`` is the operator's
        explicit discard. The same envelope carries the attempt-scoped
        lane-control credentials (URL + generation-scoped HMAC token —
        never a control-plane root secret) so the lane's steering channel
        dials out with exactly the authority this attempt owns. A
        ``required`` mode whose persisted decision pins NO checkpoint
        digest is a corrupt dispatch contract: the run parks BEFORE any
        provider I/O (no branch, no intent, no pipeline — zero model
        turns).

        Q39-02 (#321): the brief this leg dispatches is generated from ONE
        immutable :class:`~forge.adaptive.revisions.ApprovedInput`
        resolved from durable state at EVERY entry (start, retry, revival,
        repair). An ACTIVE approved revision wins: its durable content is
        digest-verified against the pointer the activation CAS switched
        and its rendered TEXT becomes the ``FORGE_PLAN`` brief (rebind
        variables ``FORGE_PLAN_DIGEST`` / ``FORGE_SPEC_DIGEST`` /
        ``FORGE_BRIEF_ENVELOPE_DIGEST`` ride the envelope, and the
        resolved record + ``revision.executor_input_digest`` persist
        beside the native-start intent). No active revision keeps the
        spec-frozen brief under an explicit ``source: spec`` label; a
        prior-version pointer resolves through the labeled legacy adapter;
        an unresolvable record refuses ``rebind_refused`` — never a quiet
        fallback to superseded bytes. The same boundary refuses a
        ``required`` resume whose checkpoint the activation's WIP reuse
        decision routed to a fresh attempt (``checkpoint_reuse_refused``).
        """
        if resume_mode not in LANE_RESUME_MODES:
            raise ValueError(
                f"resume_mode must be one of {sorted(LANE_RESUME_MODES)}, got {resume_mode!r}"
            )
        # NEXT-11/R32-05: the harness dispatch is a dispatch boundary too —
        # the ``/retry`` revival, the recovery scan's re-drive and a repair
        # re-dispatch all reach this leg directly, so the execution lease is
        # (idempotently) reserved here as well; none of them can bypass the
        # slot limit.
        if not await self._reserve_execution_capacity(project_id, run_id):
            return
        # R13: an exhausted budget (or a spent wall clock) starts no new
        # harness episode — the dispatch is the only enforcement point a
        # non-intercepted lane has, so it is checked before any I/O.
        block = await self._budget_episode_block(run_id)
        if block is not None:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, block)
            return
        # R04: the brief's TASK half, the task title and the dispatched
        # driver come from the digest-verified executable spec — never live
        # settings and never a live issue re-read. A missing/tampered spec
        # blocks the run. (Q39-02: the brief's PLAN half may since have
        # been REVISED by an approved activation — the ApprovedInput
        # resolution below decides which text wins, and the spec stays
        # history either way.)
        try:
            spec = await self._load_executable_spec(run_id)
        except SpecInvalid as exc:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"spec_invalid: {exc}")
            return
        await self._record_spec_drift(project_id, run_id, spec)

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            already_waiting = run.status == FlowStatus.WAITING_HARNESS.value
            # R37-07: the attempt generation the dispatch mints the lane
            # credential FOR — FlowRun.cancellation_generation, the one
            # durable attempt counter the lane-control and checkpoint APIs
            # verify against (NEXT-01: the retry/revival legs bump it, so a
            # re-dispatched token is scoped to the NEW attempt and the dead
            # attempt's credential retires at both APIs).
            generation = int(run.cancellation_generation or 0)
            # The pinned continuation identity — the EXACT checkpoint
            # content address the persisted continuation decision approved
            # (``continuation.checkpoint_digest``), read from durable state
            # when this dispatch resumes WIP (``required``); empty for
            # fresh/restart, which resume nothing. The decision id rides
            # beside it (``control.applied_attempt`` observability: which
            # authorized decision this dispatch executes).
            continuation_document = (run.evidence or {}).get("continuation")
            continuation_ref_digest = (
                str(continuation_document.get("checkpoint_digest") or "")
                if isinstance(continuation_document, dict)
                else ""
            )
            continuation_decision_id = (
                str(continuation_document.get("decision_id") or "")
                if isinstance(continuation_document, dict)
                else ""
            )
            # NEXT-19 (#207): the credential generation THIS attempt
            # already dispatched under (a repair re-dispatch presents it
            # — a rotation in between is a typed refusal, never a silent
            # substitution; a NEW attempt generation re-resolves fresh).
            prior_credential = dict(
                (run.evidence or {}).get("harness", {}).get("dispatch_credential") or {}
            )
            prior_credential_generation = prior_credential.get("attempt_generation")
            prior_credential_ref = (
                str(prior_credential.get("credential_ref") or "")
                if prior_credential_generation is not None
                and int(prior_credential_generation) == generation
                else ""
            )
            # Q39-02 (#321): the frozen spec digest — the identity half of
            # the approved-input envelope below (the gate-approved spec is
            # the write authority; the ACTIVE revision is the brief text).
            spec_digest = str(run.spec_digest or "")
            issue_iid_for_notes = run.issue_iid

        if resume_mode == LANE_RESUME_MODE_REQUIRED and not continuation_ref_digest:
            # The dispatch-side half of the zero-model-turns contract: a
            # required resume MUST name the exact checkpoint it authorizes
            # — a decision that pinned nothing is a corrupt dispatch
            # contract, refused BEFORE any provider I/O (the lane-side
            # strict restore guard is the second half).
            logger.warning(
                "Run %s required-resume dispatch refused — the persisted continuation "
                "decision pins no checkpoint digest (continuation_ref_missing)",
                run_id[:8],
            )
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                "continuation_ref_missing: a required-resume dispatch must carry the "
                "persisted decision's exact checkpoint digest",
            )
            return

        # Q39-02 (#321), the GitLab half of the R36-13 (#272) fence: the
        # WIP reuse decision a material revision's activation persisted is
        # a DISPATCH-BOUNDARY fence here too — a checkpoint that revision
        # routed to an explicit ``fresh_attempt`` must never ride a
        # ``required`` resume silently. Refused BEFORE any provider I/O
        # (nothing is ensured, dispatched or journaled below), naming the
        # recorded reason (the rejected reuse, explained) and the
        # operator's way out: the ``restart`` discard, or a fresh plan.
        reuse_refusal = await refused_wip_reuse(
            self._session_factory, run_id, resume_mode=resume_mode
        )
        if reuse_refusal is not None:
            route_reason = str(reuse_refusal.get("route_reason") or "")
            logger.warning(
                "Run %s dispatch refused [checkpoint_reuse_refused] — the activation "
                "routed the held checkpoint to a fresh attempt",
                run_id[:8],
            )
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                f"checkpoint_reuse_refused: {route_reason[:200]}",
            )
            await self._post_journaled_note(
                project_id,
                issue_iid_for_notes,
                self._wip_reuse_refusal_body(run_id, reuse_refusal),
                run_id,
                "wip_reuse_refusal_note",
            )
            return

        # Q39-02 (#321) — the rebind: the executor's brief is generated
        # from ONE immutable ApprovedInput resolved from durable state at
        # EVERY dispatch entry (start, retry, revival, repair). The ACTIVE
        # revision wins — its durable content is digest-verified against
        # the pointer the activation CAS switched, and the rendered
        # revision TEXT becomes the brief (the live counterexample's fix:
        # the resumed lane obeys the APPROVED direction, not the
        # spec-frozen one, and no rescue steer is needed). No active
        # revision keeps today's behavior under an explicit ``source:
        # spec`` label; a prior-version pointer without content resolves
        # through the labeled legacy adapter; anything that cannot be
        # verified refuses ``revision.rebind_refused{reason}`` — never a
        # quiet fallback to the superseded bytes.
        try:
            approved_input = await resolve_approved_input(
                self._session_factory,
                run_id,
                task_title=spec.task_title,
                task_description=spec.task_description,
                spec_plan_text=spec.plan_summary,
                spec_plan_digest=spec.plan_digest,
                allowed_writes=spec.allowed_paths,
            )
        except RevisionRebindRefused as exc:
            logger.warning(
                "revision.rebind_refused: run %s dispatch refused [%s] — %s",
                run_id[:8],
                exc.code,
                exc.detail,
            )
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                f"rebind_refused: {exc.code}: {exc.detail[:200]}",
            )
            await self._post_journaled_note(
                project_id,
                issue_iid_for_notes,
                self._rebind_refusal_body(run_id, exc.code, exc.detail),
                run_id,
                "rebind_refusal_note",
            )
            return
        if approved_input.revision_bound:
            logger.info(
                "revision.rebind: run %s dispatch briefs under active revision %d "
                "(digest %s, decision %s) — source %s",
                run_id[:8],
                approved_input.active_revision,
                approved_input.plan_digest[:12],
                approved_input.activated_by_decision[:12] or "unknown",
                approved_input.source,
            )

        plan_summary = approved_input.brief()
        brief = plan_summary
        if repair_context:
            brief = (
                f"{plan_summary}\n\n## Repair context — previous candidate failed CI"
                f" ({repair_reason or 'code failure'})\n\n{repair_context}"
            )

        issue_title = spec.task_title
        if driver is None:
            driver = spec.harness_driver
        # NEXT-19 (#207) / R38-02 (#303): the credential DELIVERY plan —
        # BEFORE the provider call, beside the reserved capacity this leg
        # opened at the top. A subject the deployment never bound plans
        # nothing (today's ambient behavior, attribution ambient-legacy);
        # a bound subject resolves through the registry's fail-closed
        # checks into a provider-safe transport (a protected+masked
        # project CI/CD variable — trigger variables are documented as
        # visible on job pages AND precedence-trumping, so the VALUE
        # never rides the trigger payload — or runner-time redemption).
        # Any typed refusal parks the run with ZERO provider dispatches.
        credential_subject = binding_subject_of_run(run)
        credential_delivery: CredentialDeliveryPlan | None = None
        if credential_subject is not None:
            try:
                credential_delivery = await delivery_plan(
                    self._credential_registry,
                    self._credential_broker,
                    subject=credential_subject,
                    provider_route=provider_route_for_driver(driver),
                    profile="gitlab",
                    presented_ref=prior_credential_ref,
                    work_id=run_id,
                    attempt_generation=generation,
                    environ=os.environ,
                )
            except CredentialRefusal as exc:
                logger.warning(
                    "credential.%s: run %s dispatch refused at the credential seam — %s",
                    "rotation_refusal" if exc.reason == "rotated" else "refusal",
                    run_id[:8],
                    exc,
                )
                await self._to_terminal(run_id, FlowStatus.BLOCKED, f"credential_refused: {exc}")
                return
            if credential_delivery is not None:
                logger.info(
                    "credential.binding_subject: run %s subject %s provider %s revision %d — "
                    "credential.delivery mode %s transport %s (attribution bound-delivery)",
                    run_id[:8],
                    credential_delivery.subject,
                    credential_delivery.provider,
                    credential_delivery.binding_revision,
                    credential_delivery.mode,
                    credential_delivery.transport_ref,
                )
                # Q39-01 (#320): a redemption-mode dispatch PERSISTS its
                # operation grant BEFORE the provider call — the lane that
                # boots can then only redeem the exact ref+route+window THIS
                # dispatch authorized. Idempotent per attempt+route+ref: a
                # re-dispatch of the same attempt keeps the existing grant
                # (the deadline never re-anchors). A grant that cannot be
                # persisted parks the run — never a lane redeeming against
                # an authorization nobody wrote.
                if credential_delivery.operation_grant is not None:
                    from forge.api_lane_control import (
                        LaneAuthorityUnavailable,
                        persist_operation_grant,
                    )

                    try:
                        effective = await persist_operation_grant(
                            self._session_factory, grant=credential_delivery.operation_grant
                        )
                    except LaneAuthorityUnavailable as exc:
                        logger.warning(
                            "credential.operation_grant: run %s grant persistence failed — %s",
                            run_id[:8],
                            exc,
                        )
                        await self._to_terminal(
                            run_id,
                            FlowStatus.BLOCKED,
                            f"credential_refused: the operation grant could not be "
                            f"persisted ({exc})",
                        )
                        return
                    logger.info(
                        "credential.operation_grant: run %s grant %s route %s attempt %d "
                        "deadline %s (idempotent per attempt+route+ref)",
                        run_id[:8],
                        effective.grant_id[:8],
                        effective.provider,
                        effective.attempt_generation,
                        effective.redemption_deadline.isoformat(),
                    )
        # R37-07 (#288): the dispatch ENVELOPE — the lane-resume/continuity
        # contract plus the attempt-scoped lane-control credentials, riding
        # the pipeline variables the job exports into every step's env. The
        # token is COMPUTED here (never stored): HMAC of the run id AND its
        # CURRENT attempt generation under FORGE_LANE_CONTROL_SECRET — the
        # same derivation both lane APIs verify; empty when no secret is
        # configured (the steering channel stays off, fail-closed). The
        # control URL repeats the control plane's own externally reachable
        # address (the same env the deployment carries); NO control-plane
        # root secret and NO publication token ever enters this set.
        control_url = (os.environ.get(LANE_CONTROL_URL_ENV) or "").strip()
        lane_token = self._lane_control_token(run_id, generation=generation)
        envelope_variables: list[dict[str, str]] = [
            # The WIP-continuity contract: the env spelling lane_driver's
            # resume_mode() reads, plus the mode word for the ledger.
            {"key": LANE_RESUME_MODE_VARIABLE, "value": resume_mode},
            {"key": LANE_RESUME_VARIABLE, "value": LANE_RESUME_ENV_SPELLING[resume_mode]},
            # The exact checkpoint content address the persisted decision
            # approved (empty on fresh/restart — they restore nothing).
            {"key": LANE_CHECKPOINT_VARIABLE, "value": continuation_ref_digest},
            # The durable attempt identity the credentials are scoped to.
            {"key": LANE_ATTEMPT_VARIABLE, "value": str(generation)},
            # The decision identity this dispatch executes.
            {"key": LANE_DECISION_VARIABLE, "value": continuation_decision_id},
            # NXT-10 outbound leg (the lane dials OUT): the control-plane
            # URL + the work-scoped, attempt-scoped token.
            {"key": LANE_CONTROL_URL_VARIABLE, "value": control_url},
            {"key": LANE_CONTROL_TOKEN_VARIABLE, "value": lane_token},
        ]
        # Q39-02 (#321): the revision-rebind variables — ONLY on a
        # revision-bound dispatch (no-revision dispatches stay
        # byte-identical to the pre-rebind envelope, the same conditional
        # the GitHub dispatch applies to its ``plan_digest`` input). The
        # brief envelope is built FROM the resolved approved input: run id
        # + the frozen task bytes + the ACTIVE revision's brief TEXT +
        # the frozen spec digest, so the runner can re-verify the
        # ``FORGE_PLAN`` bytes it consumes against the dispatched digest
        # (the A03 discipline, ported to the GitLab lane).
        brief_envelope = (
            build_brief_envelope(
                run_id=run_id,
                task_title=approved_input.task_title,
                task_description=approved_input.task_description,
                plan_text=brief,
                spec_digest=spec_digest,
            )
            if approved_input.revision_bound
            else None
        )
        if brief_envelope is not None:
            envelope_variables.extend(
                (
                    {"key": LANE_PLAN_DIGEST_VARIABLE, "value": approved_input.plan_digest},
                    {"key": LANE_SPEC_DIGEST_VARIABLE, "value": spec_digest},
                    {
                        "key": LANE_BRIEF_ENVELOPE_DIGEST_VARIABLE,
                        "value": str(brief_envelope.get("envelope_digest") or ""),
                    },
                )
            )
        if credential_delivery is not None:
            # R38-02 (#303): the lane envelope gains ONLY the credential
            # delivery REFERENCE — the non-secret ref variable (the
            # protected+masked project variable FORGE_MODEL_<ref> is
            # consumed runner-side by the lane template; the VALUE never
            # rides a trigger variable — trigger variables display on
            # job pages and OUTRANK project variables) plus the
            # non-secret redemption flag for the SDK lanes' startup hook.
            envelope_variables.append(
                {"key": CREDENTIAL_DELIVERY_REF_VARIABLE, "value": credential_delivery.dispatch_ref}
            )
            envelope_variables.append(
                {
                    "key": CREDENTIAL_DELIVERY_REDEEM_VARIABLE,
                    "value": "1" if credential_delivery.redemption else "",
                }
            )
        # `gitlab.dispatch_envelope_digest` — a stable fingerprint over the
        # identity fields (the token only enters as a boolean: digests are
        # journaled, credentials are not).
        dispatch_envelope_digest = hashlib.sha256(
            json.dumps(
                {
                    "run_id": run_id,
                    "resume_mode": resume_mode,
                    "checkpoint": continuation_ref_digest,
                    "attempt_generation": generation,
                    "decision_id": continuation_decision_id,
                    "driver": driver or "",
                    "control_url": control_url,
                    "token_dispatched": bool(lane_token),
                    # R38-02: the delivery mode only — the credential rides
                    # by REFERENCE, and digests are journaled.
                    "credential_delivery": (
                        credential_delivery.mode if credential_delivery is not None else ""
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            backend = self._harness_backend(
                project_id,
                driver=driver,
                gitlab=_DispatchEnvelopeClient(self._gitlab, envelope_variables),
            )
        except ValueError as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"backend_config: {exc}")
            return

        # Intent-first journal for the harness start (ADR-0005). The pipeline
        # id only exists once start returns, so it lands in the action's
        # correlation and outcome below.
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(run_id, "harness_start")
            await session.commit()

        try:
            # C05: the backend executes the APPROVED shape — model, target
            # and driver from the frozen RunSpec, never live settings (a
            # post-approval settings change or worker restart cannot move
            # what the gate approved).
            start_spec = BackendStartSpec(
                model=spec.harness_model,
                target_branch=spec.target_branch or self._target_branch(),
                driver=driver or spec.harness_driver,
                attempt_base=run.base_sha or "",
                timeout_seconds=spec.harness_timeout,
            )
            # Q35-04: the native-start intent, persisted BEFORE the
            # provider call. ``backend.start`` cuts the factory branch and
            # triggers the pipeline in one seam — a GitLabAPIError from
            # either step may have reached the provider, so the intent
            # stays unless the provider PROVED it refused the start; the
            # reconciler's probe (pipelines on this run-owned branch)
            # decides the remaining uncertainty. Without this write a
            # lost start response would free the slot on local status
            # while the pipeline runs.
            intent_ref = f"gitlab:pipeline:{project_id}@{factory_branch(run.issue_iid, run.id)}"
            await record_native_start_intent(self._session_factory, run_id, intent_ref)
            # Q39-02 (#321): the resolved ApprovedInput and its
            # executor-input digest persist BESIDE the native-start intent
            # — BEFORE the provider call, so a lost start response or a
            # worker death still leaves the exact identity this dispatch
            # briefed under (the same window the intent marker occupies).
            # ``revision.executor_input_digest`` is recomputable from the
            # native ledger's recorded variables (run id + the ACTIVE plan
            # digest + the brief envelope + the spec digest + the resume
            # mode): the three-way digest equality — evidence == ledger ==
            # the runner's consumed ``FORGE_PLAN`` bytes — is the rebind's
            # artifact-level proof, not a status string.
            await self._merge_run_evidence(
                run_id,
                {
                    APPROVED_INPUT_KEY: approved_input.document(),
                    **(
                        {
                            REVISION_EXECUTOR_DIGEST_KEY: executor_digest_document(
                                run_id=run_id,
                                plan_digest=approved_input.plan_digest,
                                active_revision=approved_input.active_revision,
                                envelope_digest=str(brief_envelope.get("envelope_digest") or ""),
                                spec_digest=spec_digest,
                                lane_resume_mode=resume_mode,
                                revised_from_digest=approved_input.revised_from_digest,
                                activated_by_decision=approved_input.activated_by_decision,
                            )
                        }
                        if brief_envelope is not None
                        else {}
                    ),
                },
            )
            handle = await backend.start(run, issue_title, "", brief, spec=start_spec)
        except GitLabAPIError as exc:
            if definite_start_refusal(exc.status_code):
                # The provider PROVED it refused the start — nothing
                # native began; clear the intent so the terminal release
                # sees proven never-dispatched and capacity returns now.
                await clear_native_start_intent(self._session_factory, run_id, intent_ref)
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            await self._to_terminal(run_id, FlowStatus.FAILED, f"harness_start_failed: {exc}")
            return
        except httpx.HTTPError as exc:
            # Transport failure — the start's outcome is undecidable:
            # the intent stays and the reconciler probes the branch.
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            await self._to_terminal(run_id, FlowStatus.FAILED, f"harness_start_failed: {exc}")
            return

        handle_data = json.loads(handle)
        pipeline_id = int(handle_data.get("pipeline_id") or 0)
        # Q35-04: the provider answered — attach the native correlation.
        # The lease moves dispatched_unknown → native_running; the
        # reconciler's probe key is provider-shaped (project + pipeline
        # id) so a fresh process can resolve the occupancy alone.
        if pipeline_id:
            lease = await open_lease_for_run(self._session_factory, run_id)
            if lease is not None:
                await record_native_handle(
                    lease.lease_id,
                    f"gitlab:pipeline:{project_id}:{pipeline_id}",
                    self._session_factory,
                )

        async with self._session_factory() as session:
            controller = Controller(session)
            action = await controller.complete_action(
                action_id,
                "succeeded",
                {
                    "pipeline_id": pipeline_id,
                    "job_id": handle_data.get("job_id"),
                    "branch": handle_data.get("branch"),
                },
            )
            action.correlation_id = f"pipeline-{pipeline_id}"
            if already_waiting:
                # ADR-0023 §6 fallback advance: the run stays parked in
                # waiting_harness; the fresh handle below is the only change.
                run = await self._get_run(session, run_id)
            else:
                await controller.transition(
                    run_id, FlowStatus.WAITING_HARNESS, reason=f"harness pipeline {pipeline_id}"
                )
                run = await self._get_run(session, run_id)
            # The durable handle: the reconciler restarts from exactly here.
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "harness": {
                        "handle": handle,
                        "pipeline_id": pipeline_id,
                        "job_id": handle_data.get("job_id"),
                        "branch": handle_data.get("branch"),
                        # ADR-0023: the driver this leg actually dispatched.
                        "driver": str(handle_data.get("harness") or driver or ""),
                        # R37-07: the dispatch envelope this leg carried —
                        # the journal records the CONTRACT (mode, pinned
                        # checkpoint, attempt generation, decision id, the
                        # digest) and NEVER the token value; the variable
                        # KEYS name what the pipeline received so the
                        # native ledger's recorded variables reconcile
                        # key-for-key against this journal entry.
                        "dispatch_envelope": {
                            "resume_mode": resume_mode,
                            "checkpoint_digest": continuation_ref_digest,
                            "decision_id": continuation_decision_id,
                            "attempt_generation": generation,
                            "control_url": control_url,
                            "token_dispatched": bool(lane_token),
                            # R38-02 (#303): the delivery plan riding the
                            # envelope proof — WHICH ref/mode/transport/
                            # revision the lane's credential arrives
                            # through, never the value.
                            "credential_ref": (
                                credential_delivery.credential_ref
                                if credential_delivery is not None
                                else ""
                            ),
                            "credential_delivery_mode": (
                                credential_delivery.mode if credential_delivery is not None else ""
                            ),
                            "credential_binding_revision": (
                                credential_delivery.binding_revision
                                if credential_delivery is not None
                                else 0
                            ),
                            "variable_keys": [entry["key"] for entry in envelope_variables],
                            "digest": dispatch_envelope_digest,
                        },
                        # R38-02 (#303): the credential delivery plan
                        # (/1) beside the envelope — binding subject, ref,
                        # revision, delivery mode, transport reference and
                        # expected identity. Refs and metadata only, never
                        # the value.
                        **(
                            {
                                "dispatch_credential": {
                                    **credential_delivery.as_document(),
                                    "attempt_generation": generation,
                                    "grant": {
                                        "run_id": run_id,
                                        "attempt_generation": str(generation),
                                    },
                                }
                            }
                            if credential_delivery is not None
                            else {}
                        ),
                    },
                },
            )
            await session.commit()

        # Taken-in-work ack: the issue never goes quiet between /go and the
        # evidence comment — name the agent and link the live pipeline.
        pipeline_url = ""
        get_pipeline = getattr(self._gitlab, "get_pipeline", None)
        if get_pipeline is not None and pipeline_id:
            try:
                pipeline = await get_pipeline(project_id, pipeline_id)
                pipeline_url = pipeline.web_url or ""
            except GitLabAPIError:
                pass  # the ack is best-effort; the reconciler still runs
        if pipeline_url:
            driver_doc = {
                "claude-code": "Claude Code",
                "grok-build": "Grok Build",
                "opencode": "opencode",
                "copilot": "GitHub Copilot CLI",
            }.get(str(handle_data.get("harness") or driver or ""), driver or "harness")
            await self._post_journaled_note(
                project_id,
                issue_iid=run.issue_iid or 0,
                body=(
                    f"## 🔨 Run `{run_id[:8]}` taken into work\n\n"
                    f"- Agent: **{driver_doc}** in project CI\n"
                    f"- Branch: `{handle_data.get('branch')}`\n"
                    f"- [▶ watch the pipeline live]({pipeline_url})\n\n"
                    "*This is an automated message.*"
                ),
                run_id=run_id,
                kind="taken_in_work_note",
            )

        logger.info(
            "gitlab.dispatch_envelope_digest: run %s attempt %d dispatched a %s resume "
            "(checkpoint %s, decision %s) — envelope %s",
            run_id[:8],
            generation,
            resume_mode,
            continuation_ref_digest[:12] or "none",
            continuation_decision_id[:12] or "none",
            dispatch_envelope_digest[:12],
        )
        logger.info(
            "Run %s delegated to harness backend (pipeline %d, driver %s) — waiting_harness",
            run_id[:8],
            pipeline_id,
            driver or "configured",
        )

    def _backend_name(self) -> str:
        """The configured implementer backend (ADR-0015), frozen per run."""
        raw = getattr(self._settings, "FORGE_IMPLEMENTER_BACKEND", "builtin") or "builtin"
        return str(raw).strip()

    async def _budget_episode_block(
        self, run_id: str, *, now: datetime | None = None
    ) -> str | None:
        """R13: why no new work may start against this run's budget, or None.

        The dispatch-time gate for the harness lanes: their model calls
        happen inside a CI job forge cannot intercept, so the wall clock and
        the start of new episodes are enforced HERE — at the dispatch/poll
        boundary (on the builtin lane the per-call reservations make the same
        refusal happen inside the LLM client). An exhausted budget, or one
        whose wall clock has run out, starts no further episode and parks the
        run ``blocked(budget_exhausted)``.
        """
        async with self._session_factory() as session:
            block = await budget_block_reason(session, run_id, now=now)
            if block is not None:
                # A wall-clock expiry flips the budget exhausted inside this
                # session — commit so the stop is durable and visible.
                await session.commit()
            return block

    def _harness_backend(
        self,
        project_id: int,
        *,
        driver: str | None = None,
        gitlab: Any = None,
    ):
        """Construct the ci_harness backend for *project_id* (ADR-0015).

        *driver* (ADR-0023) pins the leg to the RunSpec's frozen selection.
        *gitlab* (R37-07) lets the dispatch leg hand the backend an
        envelope-injecting client proxy (a ``create_pipeline``-intercepting
        delegation over the real client — duck-typed like the test fakes);
        the default is the service's own client, byte-compatible with the
        pre-envelope dispatch.
        """
        client = gitlab if gitlab is not None else self._gitlab
        writer = self._writer_class(
            client,
            self._session_factory,
            project_id,
            settle_seconds=self._publish_settle_seconds(),
        )
        return build_backend(
            self._settings,
            gitlab=client,
            session_factory=self._session_factory,
            writer=writer,
            driver=driver,
        )

    def _lane_control_token(self, run_id: str, *, generation: int | None = None) -> str:
        """The per-work HMAC token for the lane control channel (or "").

        R37-07 (the GitLab half of NXT-10/NEXT-01): with *generation* the
        token is ATTEMPT-SCOPED — the dispatch passes the run's CURRENT
        ``cancellation_generation`` so the credential dies with the attempt
        it was minted for (both the lane-control API and the checkpoint
        channel verify exactly this derivation). Empty when no
        FORGE_LANE_CONTROL_SECRET is configured — the lane's steering
        channel stays off, fail-closed.
        """
        secret = getattr(self._settings, "FORGE_LANE_CONTROL_SECRET", None)
        if not secret:
            return ""
        from forge.api_lane_control import lane_control_token

        # SecretStr: str() is "**********" — the masked repr, NEVER the
        # value. get_secret_value() is the value (the GitHub lane's
        # LIVE-found lesson, kept identical here).
        raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
        return lane_control_token(raw, run_id, generation=generation)

    def _mr_io_timeout_s(self) -> float:
        """C08: the bounded provider-I/O window under the reservation lock."""
        raw = getattr(self._settings, "FORGE_MR_IO_TIMEOUT_SECONDS", None)
        return float(raw) if raw else 30.0

    async def _reserve_mr(self, run_id: str, branch: str) -> None:
        """Durable MR reservation for run+branch, BEFORE any provider I/O.

        B03 (migration 019): the logical ONE-MR intent commits first — a
        concurrent creator (or a crash observer) sees the reservation in
        its own connection while the winner is still mid-I/O; ``FOR
        UPDATE`` on the row then serializes the create/adopt decisions.
        """
        async with self._session_factory() as session:
            dialect_insert = (
                postgresql.insert
                if session.bind.dialect.name == "postgresql"
                else sqlite_dialect_insert
            )
            await session.execute(
                dialect_insert(MRReservation)
                .values(flow_run_id=run_id, branch=branch, status="open")
                .on_conflict_do_nothing()
            )
            await session.commit()

    async def _draft_target_branch(self, run_id: str) -> str:
        """The MR target from the FROZEN spec (C05) — never live settings.

        A post-approval FORGE_TARGET_BRANCH change must not move the MR
        target of a run the gate already approved; legacy runs without a
        readable spec keep the live default (recorded in the run's spec
        provenance when it exists).
        """
        try:
            spec = await self._load_executable_spec(run_id)
        except Exception:
            return self._target_branch()
        if spec is None:
            return self._target_branch()
        return spec.target_branch or self._target_branch()

    async def _create_draft_mr(
        self,
        project_id: int,
        run_id: str,
        branch: str,
        commit_sha: str | None,
    ) -> int:
        """Create the Draft MR for the run branch — ONE per run+branch, ever.

        B03 separates the logical intent from the immutable attempt
        history (ADR-0005's journal contract):

        1. **Reserve** (own committed transaction, before any I/O): the
           ``mr_reservations`` row is the durable one-MR intent — visible
           to every other connection immediately.
        2. **Serialize** under the reservation row lock (``FOR UPDATE``):
           a concurrent leg or the recovery scanner blocks behind the
           winner, then sees ``confirmed``.
        3. **Adopt before create**: a journaled succeeded row, then the
           provider's own MR list (a lost response leaves the MR on the
           provider with a terminal ``unknown_outcome`` row in the
           journal — reconciliation journals its OWN observation row, it
           never rewrites the terminal one).
        4. **Fail closed on provider read errors**: a 5xx/403 on the MR
           list proves nothing about the MR's existence — no create.
        """
        await self._reserve_mr(run_id, branch)
        # C05: resolve the FROZEN target BEFORE the locked transaction (the
        # spec read opens its own session; under the row lock it would
        # interleave with this transaction's uncommitted state).
        target_branch = await self._draft_target_branch(run_id)
        async with self._session_factory() as session:
            reservation = (
                (
                    await session.execute(
                        select(MRReservation)
                        .where(
                            MRReservation.flow_run_id == run_id,
                            MRReservation.branch == branch,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .one()
            )
            if reservation.status == "confirmed" and reservation.mr_iid is not None:
                try:
                    await self._gitlab.get_merge_request(project_id, int(reservation.mr_iid))
                    return int(reservation.mr_iid)
                except GitLabAPIError:
                    # the confirmed MR is gone (closed/merged) — reopen the
                    # reservation and create a fresh Draft MR.
                    reservation.status = "open"
                    reservation.mr_iid = None

            controller = Controller(session)

            # 3a. adopt the journal first — a previous attempt succeeded.
            journal_iid = await self._journaled_draft_mr(run_id, project_id, branch)
            if journal_iid is not None:
                reservation.status = "confirmed"
                reservation.mr_iid = journal_iid
                await session.commit()
                return journal_iid

            # 3b. the provider is the arbiter a lost response needs: the MR
            # exists upstream with no succeeded journal row. Adoption
            # journals its OWN observation row (the prior unknown/failed
            # rows stay immutable history).
            try:
                # C08: provider I/O under the reservation lock is BOUNDED —
                # a hung provider must not pin the row lock forever; the
                # failure is fail-closed (the reservation stays open, the
                # next pass retries).
                provider_mrs = await asyncio.wait_for(
                    self._gitlab.list_merge_requests(project_id, state="opened", per_page=50),
                    timeout=self._mr_io_timeout_s(),
                )
            except (GitLabAPIError, TimeoutError):
                # 4. fail CLOSED: an unreadable list proves nothing — do
                # not create on top of an unknown surface.
                await session.rollback()
                raise
            for existing_mr in provider_mrs:
                if existing_mr.source_branch == branch:
                    action_id = await controller.record_action(
                        run_id, "create_merge_request", correlation_id=branch
                    )
                    await controller.complete_action(
                        action_id,
                        "succeeded",
                        {"mr_iid": int(existing_mr.iid), "adopted": True},
                    )
                    reservation.status = "confirmed"
                    reservation.mr_iid = int(existing_mr.iid)
                    await session.commit()
                    return int(existing_mr.iid)

            # 5. verified miss on both surfaces — create, under the same
            # locked transaction (the reservation is the serializer).
            action_id = await controller.record_action(
                run_id, "create_merge_request", correlation_id=branch
            )
            run = await self._get_run(session, run_id)
            plan_digest = run.plan_digest or ""
            issue_iid = run.issue_iid
            issue_title = await self._read_issue_title(project_id, issue_iid)
            description = self._mr_description(run_id, plan_digest, issue_iid, commit_sha)
            try:
                mr = await asyncio.wait_for(
                    self._gitlab.create_merge_request(
                        project_id,
                        branch,
                        target_branch,
                        f"Draft: {issue_title}",  # Draft: prefix marks it draft
                        description,
                    ),
                    timeout=self._mr_io_timeout_s(),
                )
            except (httpx.HTTPError, TimeoutError):
                # a timeout after possibly executing = unknown outcome
                await controller.complete_action(action_id, "unknown_outcome")
                await session.commit()
                raise
            except GitLabAPIError as exc:
                await controller.complete_action(action_id, "failed", {"error": str(exc)})
                await session.commit()
                raise
            await controller.complete_action(
                action_id,
                "succeeded",
                {"mr_iid": mr.get("iid"), "web_url": mr.get("web_url")},
            )
            reservation.status = "confirmed"
            reservation.mr_iid = int(mr["iid"])
            await session.commit()
            return int(mr["iid"])

    async def _committed_candidate(self, run_id: str, project_id: int, branch: str) -> str | None:
        """The candidate a crashed attempt already committed on *branch* (ADR-0017 §3).

        The journaled ``commit`` action is the durable record of the write: a
        succeeded row carries the sha even when the process died before the
        run state caught up. The sha is adopted only when it is NOT yet
        accounted for in ``candidate_shas`` (otherwise this is a repair cycle,
        which must write a NEW candidate) and is still the live branch head
        (otherwise the branch drifted and the guarded apply must decide).
        """
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ActionLog)
                        .where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "commit",
                            ActionLog.correlation_id == branch,
                            ActionLog.status == "succeeded",
                        )
                        .order_by(ActionLog.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            sha = str(((row.remote_result or {}).get("sha") if row is not None else "") or "")
            if not sha:
                return None
            run = await self._get_run(session, run_id)
            if run is not None and sha in list(run.candidate_shas or []):
                return None  # repair cycle — this commit is accounted for
        try:
            head = await self._gitlab.get_branch_head(project_id, branch)
        except GitLabAPIError:
            return None  # cannot verify — let the drift-guarded apply decide
        return sha if head == sha else None

    async def _journaled_draft_mr(self, run_id: str, project_id: int, branch: str) -> int | None:
        """The Draft MR a crashed attempt already created, if it still exists (ADR-0017 §3)."""
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ActionLog)
                        .where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "create_merge_request",
                            ActionLog.correlation_id == branch,
                            ActionLog.status == "succeeded",
                        )
                        .order_by(ActionLog.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        raw = (row.remote_result or {}).get("mr_iid") if row is not None else None
        if raw is None:
            return None
        try:
            await self._gitlab.get_merge_request(project_id, int(raw))
        except GitLabAPIError:
            return None  # the journaled MR is gone — create a fresh one
        return int(raw)

    async def _update_draft_mr(
        self,
        project_id: int,
        run_id: str,
        mr_iid: int,
        branch: str,
        commit_sha: str | None,
        cycle: int,
        *,
        repair_reason: str | None = None,
    ) -> None:
        """Point the existing Draft MR at the repair commit, journaling writes."""
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, "update_merge_request", correlation_id=branch
            )
            await session.commit()

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            plan_digest = run.plan_digest or ""
            issue_iid = run.issue_iid
        description = self._mr_description(run_id, plan_digest, issue_iid, commit_sha, cycle)
        try:
            await self._gitlab.update_merge_request(project_id, mr_iid, description=description)
        except httpx.HTTPError:
            await self._complete_action(action_id, "unknown_outcome")
            raise
        except GitLabAPIError as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        await self._complete_action(action_id, "succeeded", {"mr_iid": mr_iid})

        if repair_reason:
            await self._post_journaled_mr_note(
                project_id,
                mr_iid,
                f"**Repair cycle {cycle}:** {repair_reason}\n\n"
                f"New candidate commit: `{commit_sha}`.\n\n"
                "*This is an automated message.*",
                run_id,
            )

    @staticmethod
    def _mr_description(
        run_id: str, plan_digest: str, issue_iid: int | None, commit_sha: str | None, cycle: int = 1
    ) -> str:
        cycle_note = "" if cycle <= 1 else f"\n- **Commit cycle:** {cycle} (repair)\n"
        return (
            f"Draft implementation by forge run `{run_id[:8]}` for #{issue_iid}.\n\n"
            f"- **Plan digest:** `{plan_digest}`\n"
            f"- **Candidate commit:** `{commit_sha}`{cycle_note}\n"
            "*Merging is a human decision — forge never merges (ADR-0003).*"
        )

    # ------------------------------------------------------------------
    # Reconciler tick: waiting_ci → evaluating_ci → … (poll-based, ADR-0005)
    # ------------------------------------------------------------------

    async def evaluate_waiting_ci(self, now: datetime | None = None) -> None:
        """One reconciler pass over every run parked in ``waiting_ci`` — plus
        the runs a crashed pass stranded in ``evaluating_ci`` or ``reviewing``
        (R07: their verdict and review are replayed from the persisted
        evidence, so the resume never re-derives — and never re-pays for —
        an already-recorded result).

        R03: the scan is provider-scoped — GitHub/Azure runs are driven by
        their own reconcilers and would 404 against the GitLab reads here.
        """
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(FlowRun.id, FlowRun.status).where(
                        FlowRun.provider == "gitlab",
                        FlowRun.status.in_(
                            [
                                FlowStatus.WAITING_CI.value,
                                FlowStatus.EVALUATING_CI.value,
                                FlowStatus.REVIEWING.value,
                            ]
                        ),
                    )
                )
            ).all()
        for run_id, status in rows:
            try:
                if status == FlowStatus.REVIEWING.value:
                    await self._resume_review(run_id)
                else:
                    await self._evaluate_one(run_id, now)
            except Exception:
                # One broken run must not stall the reconciler loop.
                logger.exception("Reconcile pass failed for run %s", run_id[:8])

    async def _resume_review(self, run_id: str) -> None:
        """Re-drive a run stranded in ``reviewing`` by a crashed pass (R07).

        A crash between the REVIEWING move and the ready transition used to
        strand the run: no scanner picked ``reviewing`` up. The resume feeds
        ``_review_and_ready`` from the run's persisted evidence — a review
        already recorded for the candidate is replayed there without a
        second model call; a crash before the review simply lets the
        reviewer run its first (and only) pass.
        """
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            if run.status != FlowStatus.REVIEWING.value:
                return  # moved on or cancelled elsewhere — superseded
            project_id = run.project_id
            issue_iid = run.issue_iid
            mr_iid = run.mr_iid
            candidate_shas = list(run.candidate_shas or [])
            base_sha = run.base_sha or ""
            plan_digest = run.plan_digest or ""
            cancel_requested = bool(run.cancel_requested)
            verification = dict((run.evidence or {}).get("verification") or {})
            pipeline_evidence = dict((run.evidence or {}).get("pipeline") or {})
        if cancel_requested:
            logger.info("Run %s cancelled — stranded review pass stood down", run_id[:8])
            return
        candidate_sha = candidate_shas[-1] if candidate_shas else ""
        if not candidate_sha:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, "reviewing without candidate sha")
            return
        pipeline = SimpleNamespace(
            id=pipeline_evidence.get("id"),
            status=str(pipeline_evidence.get("status") or "unknown"),
            web_url=pipeline_evidence.get("url"),
        )
        # R02: the recorded verdict is trusted only when it is still bound to
        # THIS candidate; anything else re-enters the review leg honestly.
        verified = verified_verdict(verification, candidate_sha)
        warnings = (
            [] if verified else ["No verification profile configured — pipeline success only."]
        )
        await self._review_and_ready(
            run_id,
            project_id=project_id,
            issue_iid=issue_iid,
            mr_iid=mr_iid,
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            pipeline=pipeline,
            plan_digest=plan_digest,
            verified=verified,
            verification_warnings=warnings,
            verification_evidence=verification,
        )

    async def _evaluate_one(self, run_id: str, now: datetime) -> None:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            project_id = run.project_id
            issue_iid = run.issue_iid
            # R03 slice: non-GitLab runs are driven by their own reconcilers
            # (github_service / azure_service) — the GitLab drift check would
            # 404 against a foreign provider.
            if getattr(run, "provider", "gitlab") != "gitlab":
                return
            entry_status = run.status
            candidate_shas = list(run.candidate_shas or [])
            mr_iid = run.mr_iid
            plan_digest = run.plan_digest or ""
            base_sha = run.base_sha or ""
            backend_name = str((run.evidence or {}).get("backend") or "").strip()
            cancel_requested = bool(run.cancel_requested)
            deadline = await self._waiting_ci_deadline(session, run_id)

        # R04: the verification contract (required jobs) and the commit-cycle
        # budget are frozen in the executable spec — post-approval evaluation
        # never consults live settings for them. A missing/tampered spec
        # blocks the run instead of guessing what "verified" means.
        try:
            spec = await self._load_executable_spec(run_id)
        except SpecInvalid as exc:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"spec_invalid: {exc}")
            return
        profile = VerificationProfile(required_jobs=spec.required_jobs)
        # ADR-0027 slice 2: this lane deliberately does NOT call the shared
        # ObserveVerification use case (forge.runs.usecases) — a finished
        # pipeline with a missing required job blocks here (quality
        # contract), while the A01 lanes keep waiting on unproven checks;
        # and an empty profile is honestly unverified here, whereas the A01
        # empty-contract rule makes every observed check required. The
        # structural reasons live in the forge.runs.usecases docstring; the
        # shared verdict vocabulary (VerificationResult / ready_evidence /
        # ready_reason) is what this lane joins at.

        candidate_sha = candidate_shas[-1] if candidate_shas else None
        if not candidate_sha:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, "waiting_ci without candidate sha")
            return

        # F13: the grant was revoked mid-wait — no provider call, no publish.
        if cancel_requested:
            logger.info("Run %s cancelled — late verification pass ignored", run_id[:8])
            return

        # R17 (deadline-before-I/O): the durable CI deadline is a LOCAL check —
        # whatever CI reports (silence, an API failure, a pipeline stuck
        # forever in an active state), a run past its deadline parks
        # blocked(ci_timeout) without a single provider call, so a
        # permanently erroring GitLab API can never hold a run past its
        # FORGE_CI_WAIT_SECONDS budget. A run a crashed pass already moved to
        # ``evaluating_ci`` has CONCLUDED its wait — the deadline governs the
        # wait, not the verdict, so it is not re-applied on the resume.
        if (
            entry_status == FlowStatus.WAITING_CI.value
            and deadline is not None
            and as_aware_utc(now) > as_aware_utc(deadline)
        ):
            await self._to_terminal(run_id, FlowStatus.BLOCKED, "ci_timeout")
            return

        branch = factory_branch(issue_iid, run_id)

        # Verdict invalidation (ADR-0006/0008): a human push on the bot branch
        # invalidates any pipeline verdict — block, never overwrite.
        # F28: a single branch-head read, not a paginated commit history.
        try:
            head = await self._gitlab.get_branch_head(project_id, branch)
        except GitLabAPIError:
            logger.exception("Drift check read failed for run %s — keeping it waiting", run_id[:8])
            return
        if head != candidate_sha:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, "external_change")
            return

        try:
            pipelines = await self._gitlab.list_pipelines(project_id, sha=candidate_sha)
        except GitLabAPIError:
            logger.exception("Pipeline read failed for run %s — keeping it waiting", run_id[:8])
            return

        if not pipelines:
            # Missing pipeline is silence from CI — never success (ADR-0007).
            return

        pipeline = pipelines[0]
        if pipeline.status in _CI_ACTIVE_STATUSES:
            return  # keep waiting — the next tick re-checks

        try:
            await self._transition(
                run_id,
                FlowStatus.EVALUATING_CI,
                reason=f"pipeline {pipeline.id} {pipeline.status}",
            )
        except InvalidTransition:
            # R07: a crashed pass already made this move (the run was
            # scanned in ``evaluating_ci``) — resume on the evidence below.
            pass
        await self._merge_run_evidence(
            run_id,
            {
                "pipeline": {
                    "id": pipeline.id,
                    "url": pipeline.web_url,
                    "status": pipeline.status,
                    "sha": candidate_sha,
                }
            },
        )

        try:
            jobs = await self._gitlab.list_pipeline_jobs(project_id, pipeline.id)
        except GitLabAPIError:
            logger.warning(
                "Job read failed for pipeline %s (run %s) — contract on empty job list",
                pipeline.id,
                run_id[:8],
                exc_info=True,
            )
            jobs = []

        if pipeline.status == "success":
            # F19 (ADR-0018 §5): the verification profile decides what
            # "verified" means — R04: the profile frozen in the spec, not the
            # live settings. R02 honesty: an empty profile never presents
            # pipeline success as verified — the run still reaches review,
            # but the evidence records status="unverified" and the ready
            # reason says so.
            ok, contract_reason = evaluate_verification(pipeline, jobs, profile)
            if not ok:
                # ADR-0008: a green icon without the required jobs is not done.
                # No LLM repair — this is CI configuration, not code.
                await self._to_terminal(
                    run_id, FlowStatus.BLOCKED, f"quality_contract: {contract_reason}"
                )
                return
            verification_warnings: list[str] = []
            if profile.required_jobs:
                verified = True
                verification_fragment = ready_evidence(
                    True,
                    candidate_sha,
                    PRODUCER_GITLAB_PIPELINE,
                    summary=contract_reason,
                    surface=(
                        {"name": job.name, "status": job.status}
                        for job in jobs
                        if job.name in set(profile.required_jobs)
                    ),
                )
            else:
                verified = False
                verification_warnings.append(
                    "No verification profile configured — pipeline success only."
                )
                verification_fragment = ready_evidence(
                    False,
                    candidate_sha,
                    PRODUCER_GITLAB_PIPELINE,
                    summary="no verification profile configured — pipeline success only",
                )
            await self._merge_run_evidence(run_id, {"verification": verification_fragment})
            # Q39-13 (#332): the required-discussion gate — a candidate
            # whose review-feedback discussions are still unresolved is
            # NOT ready, however green its pipeline is. The gate parks
            # the run in ``evaluating_ci`` (the reconciler re-checks each
            # pass); the reviewer's resolve is the human decision.
            if await self._review_feedback_unresolved(run_id, project_id, mr_iid, candidate_sha):
                return
            await self._review_and_ready(
                run_id,
                project_id=project_id,
                issue_iid=issue_iid,
                mr_iid=mr_iid,
                candidate_sha=candidate_sha,
                base_sha=base_sha,
                pipeline=pipeline,
                plan_digest=plan_digest,
                verified=verified,
                verification_warnings=verification_warnings,
                verification_evidence=verification_fragment,
            )
            return

        # failed / canceled / skipped — negative verdict: classify BEFORE
        # deciding (ADR-0008), and never repair on infra/config/unknown
        # ("unknown" = empty evidence: no failed job blames the code).
        failure_class = classify_failure(jobs)
        if failure_class != "code":
            failed = _failed_job_names(jobs)
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                f"{failure_class}_failure: pipeline {pipeline.id} {pipeline.status}"
                + (f"; failed jobs: {failed}" if failed else ""),
            )
            return

        cycle = await self._read_commit_cycle(run_id)
        # R04: the commit-cycle budget is frozen in the spec at plan time.
        max_cycles = spec.commit_cycles
        if cycle >= max_cycles:
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                f"commit_cycles_exhausted: {cycle} of {max_cycles} commit cycles used"
                + (f"; failed jobs: {_failed_job_names(jobs)}" if jobs else ""),
            )
            return

        await self._begin_repair(
            run_id, project_id, cycle, jobs, harness=is_harness_backend(backend_name)
        )

    # ------------------------------------------------------------------
    # Q39-06 (#325): the closing budget — protect the promised review
    # ------------------------------------------------------------------

    async def _review_budget_block(self, run_id: str) -> dict | None:
        """The standing reviewer-leg budget decision, or ``None``.

        Recorded by the reviewer leg's BUDGET_EXHAUSTED arm
        (:meth:`_record_review_budget_block`), released only by the
        explicit operator continuation (:meth:`continue_review_only`).
        """
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            block = (run.evidence or {}).get("review_budget_block")
            return dict(block) if isinstance(block, dict) else None

    async def _record_review_budget_block(
        self, run_id: str, *, candidate_sha: str, tested_identity: str
    ) -> dict:
        """Record the reviewer-leg budget decision + its closing budget.

        Consults the closing policy (``FORGE_CLOSING_RESERVE_USD`` /
        ``FORGE_CLOSING_RESERVE_FRACTION`` / ``FORGE_SPEND_CAP_USD``)
        over the run's durable usage receipts. The recorded block binds
        the decision to the candidate sha + tested identity — the
        review-only continuation's binding — and carries the five-field
        budget report (exact / known subtotal / lower bound / reserved
        liability / unknown intervals) with the reserve visible.
        """
        from forge.adaptive.closing_budget import (
            CandidateBinding,
            ClosingReservePolicy,
            closing_budget_report,
            rows_from_durable_receipts,
        )

        async with self._session_factory() as session:
            receipts = (
                (await session.execute(select(UsageReceipt).where(UsageReceipt.run_id == run_id)))
                .scalars()
                .all()
            )
            run = await self._get_run(session, run_id)
            standing = (run.evidence or {}).get("review_budget_block")
            standing = dict(standing) if isinstance(standing, dict) else {}
        policy = ClosingReservePolicy.from_env()
        report = closing_budget_report(
            rows_from_durable_receipts(receipts), cap_usd=policy.cap_usd, policy=policy
        )
        budget = report.to_json()
        if budget["closing_reserve_usd"] is None:
            short_reason = (
                "no closing reserve policy is configured — set"
                " FORGE_CLOSING_RESERVE_USD (or FORGE_CLOSING_RESERVE_FRACTION"
                " with FORGE_SPEND_CAP_USD) to protect the closing review"
            )
        elif budget["closing_review_fits"]:
            short_reason = (
                f"closing reserve of {budget['closing_reserve_usd']} usd is held"
                " for the review — the review-only continuation (an explicit"
                " operator action, zero coder dispatches) completes it"
            )
        else:
            short_reason = (
                f"closing reserve of {budget['closing_reserve_usd']} usd does not"
                f" cover the review (known {budget['known_subtotal_usd']} usd +"
                f" reserved liability {budget['reserved_liability_usd']} usd vs the"
                f" {budget['coder_ceiling_usd']} usd coder ceiling) — an explicit,"
                " auditable top-up is required"
            )
        block = {
            "budget_decision": BUDGET_EXHAUSTED,
            "stage": "reviewer",
            "candidate": CandidateBinding(
                candidate_sha=candidate_sha, tested_identity=tested_identity
            ).to_json(),
            "released": False,
            # the applied top-up ledger SURVIVES a re-recorded refusal —
            # a retried operator command stays a replay across refusal
            # cycles (the idempotency key is deterministic)
            "top_ups": list(standing.get("top_ups") or []),
            "top_up_total_usd": standing.get("top_up_total_usd") or 0.0,
            "short_reason": short_reason,
            "budget": budget,
        }
        await self._merge_run_evidence(run_id, {"review_budget_block": block})
        return block

    async def continue_review_only(
        self,
        run_id: str,
        *,
        operator: str,
        top_up_usd: float = 0.0,
        top_up_reason: str = "",
    ) -> dict[str, Any]:
        """The explicit review-only continuation (Q39-06/#325 item 4).

        After an explicit reviewer-leg budget decision, repeat ONLY the
        review of the SAME candidate/tested identity: no coder dispatch,
        no new commits — the re-drive never touches the implementer or
        the writer. The candidate binding is re-checked first: a moved
        head (or tested identity) invalidates the shortcut with the
        typed ``review_shortcut_stale`` and the run parks for the
        required fresh verification. A top-up (amount + reason, both
        required) is recorded BEFORE the re-drive and is
        replay-idempotent — the same command retried adds its amount
        exactly once.

        Returns the outcome document (``allowed`` / typed ``reason``).
        """
        from forge.adaptive.closing_budget import (
            OBSERVABLE_REVIEW_ONLY_RECOVERY,
            REVIEW_SHORTCUT_STALE,
            BudgetTopUp,
            CandidateBinding,
            TopUpLedger,
            review_only_continuation,
        )

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            status = run.status
            project_id = run.project_id
            issue_iid = run.issue_iid
            mr_iid = run.mr_iid
            base_sha = run.base_sha or ""
            candidate_shas = list(run.candidate_shas or [])
            plan_digest = run.plan_digest or ""
            evidence = dict(run.evidence or {})
        block = evidence.get("review_budget_block")
        block = dict(block) if isinstance(block, dict) else None
        if status != FlowStatus.REVIEWING.value or block is None:
            return {
                "allowed": False,
                "reason": "no_review_budget_block",
                "detail": (
                    "the review-only continuation requires a run parked in"
                    " reviewing with a recorded reviewer-leg budget decision"
                ),
                "observable": OBSERVABLE_REVIEW_ONLY_RECOVERY,
            }
        recorded = block.get("candidate") or {}
        recorded_binding = CandidateBinding(
            candidate_sha=str(recorded.get("candidate_sha") or ""),
            tested_identity=str(recorded.get("tested_identity") or ""),
        )
        candidate_sha = candidate_shas[-1] if candidate_shas else ""
        verification = dict(evidence.get("verification") or {})
        # Candidate FRESHNESS: the shortcut binds to the LIVE branch head,
        # re-read now — a push that landed since the recorded decision
        # invalidates the shortcut (the required verification reruns).
        head = await self._gitlab.get_branch_head(project_id, factory_branch(issue_iid, run_id))
        current_binding = CandidateBinding(
            candidate_sha=str(head or candidate_sha),
            tested_identity=str(verification.get("tested_oid") or candidate_sha),
        )
        decision = review_only_continuation(
            budget_decision=str(block.get("budget_decision") or ""),
            recorded=recorded_binding,
            current=current_binding,
        )
        if not decision.allowed:
            await self._merge_run_evidence(
                run_id, {"review_budget_block": {**block, "shortcut": decision.to_json()}}
            )
            if decision.reason == REVIEW_SHORTCUT_STALE:
                await self._to_terminal(
                    run_id, FlowStatus.BLOCKED, f"{REVIEW_SHORTCUT_STALE}: {decision.detail}"
                )
            return decision.to_json()

        # The explicit, auditable top-up — recorded BEFORE the re-drive,
        # replay-idempotent by its derived key.
        ledger = TopUpLedger(block.get("top_ups") or [])
        if top_up_usd:
            applied = ledger.apply(
                BudgetTopUp(
                    run_id=run_id,
                    amount_usd=float(top_up_usd),
                    reason=top_up_reason,
                    operator=operator,
                )
            )
            logger.info(
                "Run %s budget top-up of %s usd by %s (%s) — applied=%s",
                run_id[:8],
                applied.top_up.amount_usd,
                operator,
                applied.top_up.reason,
                applied.applied,
            )
        block = {
            **block,
            "top_ups": list(ledger.records()),
            "top_up_total_usd": ledger.total_added_usd(),
            "released": {"operator": operator, "top_up_usd": ledger.total_added_usd()},
        }
        await self._merge_run_evidence(run_id, {"review_budget_block": block})

        # Repeat ONLY the review of the SAME candidate/tested identity:
        # zero coder dispatches, zero commits (this path never touches
        # the implementer or the writer).
        pipeline_evidence = dict(evidence.get("pipeline") or {})
        pipeline = SimpleNamespace(
            id=pipeline_evidence.get("id"),
            status=str(pipeline_evidence.get("status") or "unknown"),
            web_url=pipeline_evidence.get("url"),
        )
        verified = verified_verdict(verification, candidate_sha)
        warnings = (
            [] if verified else ["No verification profile configured — pipeline success only."]
        )
        await self._review_and_ready(
            run_id,
            project_id=project_id,
            issue_iid=issue_iid,
            mr_iid=mr_iid,
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            pipeline=pipeline,
            plan_digest=plan_digest,
            verified=verified,
            verification_warnings=warnings,
            verification_evidence=verification,
        )
        return decision.to_json()

    async def _review_and_ready(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_iid: int | None,
        mr_iid: int | None,
        candidate_sha: str,
        base_sha: str,
        pipeline,
        plan_digest: str,
        verified: bool = True,
        verification_warnings: list[str] | None = None,
        verification_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        """checks passed → reviewing → ready_for_human (ADR-0008 review leg).

        ``verified=False`` (an empty verification profile) is honest: the
        run still reaches the human, but the ready reason and the evidence
        comment say ``unverified`` instead of implying checks passed (R02).
        The reason and the iron finalization checks come from
        :mod:`forge.runs.consistency` (R27) — this leg does not own their
        wording anymore.
        """
        try:
            await self._transition(
                run_id, FlowStatus.REVIEWING, reason="readonly review of candidate"
            )
        except InvalidTransition:
            # R07: a crashed pass already made the move — resume this leg
            # from the persisted state instead of dying on the re-entry.
            pass

        # F22/R13: the reviewer's model calls reserve against the run budget
        # too — the leg rebinds the guard itself (the serving instance may be
        # fresh; see _advance_proposal).
        await self._apply_run_budget(run_id)

        # Q39-06 (#325): a STANDING reviewer-leg budget decision guards the
        # leg — without the explicit operator release (the recorded top-up in
        # review_budget_block.released), a scanner re-drive stands down
        # instead of re-attempting the paid review call: never a hidden
        # retry. The block itself is only written by the budget arm below.
        standing = await self._review_budget_block(run_id)
        if (
            isinstance(standing, dict)
            and not standing.get("released")
            and str((standing.get("candidate") or {}).get("candidate_sha") or "") == candidate_sha
        ):
            logger.info(
                "Run %s stands in reviewing under its recorded reviewer-leg"
                " budget decision (closing reserve %s usd) — the explicit"
                " review-only continuation releases it",
                run_id[:8],
                (standing.get("budget") or {}).get("closing_reserve_usd"),
            )
            return

        # R07 bounded step ``review``: a review already persisted for THIS
        # candidate sha is replayed — the reviewer (a paid model call) runs
        # exactly once per (run, cycle, candidate). A different sha (a new
        # candidate after a repair) legitimately re-reviews.
        plan_summary, _ = await self._read_plan_evidence(run_id)
        stored = await self._read_review_evidence(run_id)
        if (
            isinstance(stored, dict)
            and stored.get("sha") == candidate_sha
            and str(stored.get("verdict") or "")
        ):
            verdict = str(stored.get("verdict") or "")
            summary = str(stored.get("summary") or "")
            findings = _findings_from_evidence(stored.get("findings"))
            logger.info(
                "Run %s replays its persisted review of %s — no second model call",
                run_id[:8],
                candidate_sha[:8],
            )
        else:
            try:
                review = await self._reviewer.review(
                    project_id=project_id,
                    issue_title=await self._read_issue_title(project_id, issue_iid),
                    plan_summary=plan_summary,
                    base_sha=base_sha,
                    candidate_sha=candidate_sha,
                    flow_run_id=run_id,
                )
            except (LLMError, LLMResponseError, GitLabAPIError) as exc:
                # R13: a budget refusal is not a review failure — the reviewer
                # never ran. Q39-06 (#325): the refusal now consults the
                # CLOSING RESERVE before parking anything: when the reserve
                # still covers the closing review, the run stays in the
                # precise NON-READY ``reviewing`` state with the reserve
                # visible (the explicit review-only continuation completes
                # it); only a reserve that cannot cover the review blocks —
                # with the shortage named. Never a hidden retry: the standing
                # block above guards every later re-drive.
                if str(exc) == BUDGET_EXHAUSTED:
                    block = await self._record_review_budget_block(
                        run_id,
                        candidate_sha=candidate_sha,
                        tested_identity=str(
                            (verification_evidence or {}).get("tested_oid") or candidate_sha
                        ),
                    )
                    if block["budget"].get("closing_review_fits"):
                        logger.info(
                            "Run %s keeps reviewing within its closing reserve"
                            " (%s usd) — the review-only continuation completes it",
                            run_id[:8],
                            block["budget"].get("closing_reserve_usd"),
                        )
                        return
                    await self._to_terminal(
                        run_id,
                        FlowStatus.BLOCKED,
                        f"{BUDGET_EXHAUSTED}: reviewer refused — {block['short_reason']}",
                    )
                else:
                    await self._to_terminal(run_id, FlowStatus.BLOCKED, f"review_failed: {exc}")
                return

            verdict = str(getattr(review, "verdict", ""))
            summary = str(getattr(review, "summary", ""))
            findings = [_finding_dict(raw) for raw in (getattr(review, "findings", ()) or ())]
            review_evidence = {
                "review": {
                    "verdict": verdict,
                    "sha": candidate_sha,  # ADR-0008: the review approves THIS sha
                    "summary": summary,
                    "findings": findings,
                }
            }
            await self._merge_run_evidence(run_id, review_evidence)
            stored = await self._read_review_evidence(run_id)

        # Self-check: the recorded review must be bound to the candidate sha.
        if stored is None or stored.get("sha") != candidate_sha:
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                "review_sha_mismatch: review is not bound to the candidate sha (ADR-0008)",
            )
            return

        # F19 (ADR-0018 §5): post-review freshness — the branch head must
        # still BE the reviewed candidate the moment the run goes ready. The
        # pre-review external_change check cannot cover a push that lands
        # while the review is in flight.
        branch = factory_branch(issue_iid, run_id)
        try:
            head = await self._gitlab.get_branch_head(project_id, branch)
        except GitLabAPIError as exc:
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                f"candidate_drift_after_review: branch head read failed: {exc}",
            )
            return
        if head != candidate_sha:
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                "candidate_drift_after_review: branch head moved past the reviewed candidate",
            )
            return

        warnings = list(verification_warnings or [])
        drift = await self._task_drift_warning(run_id, project_id, issue_iid)
        if drift is not None:
            warnings.append(drift)

        # Q39-13 (#332): the required-discussion gate, re-checked at the
        # review boundary too — a note can land while the review runs.
        # The run stays in ``reviewing``; the reconciler re-drives this
        # leg and the review above replays from the persisted evidence
        # (no second model call).
        if await self._review_feedback_unresolved(run_id, project_id, mr_iid, candidate_sha):
            return

        # Q39-13 (#332): the final summary names the resolved and the
        # still-open discussions and the tested candidate (the section
        # renders empty — byte-identical legacy comment — for runs that
        # never recorded review feedback).
        feedback_section = ""
        requests = await read_review_feedback_requests(self._session_factory, run_id)
        if requests:
            states = self._discussion_resolution_states(
                await self._discussions_or_none(project_id, mr_iid)
            )
            feedback_section = review_feedback_summary_section(requests, states, candidate_sha)

        # ADR-0027: the reason and the finalization iron checks have ONE
        # shared source (forge.runs.consistency) across GitLab/GitHub/Azure.
        reason = ready_reason(verified, verdict, UNVERIFIED_DETAIL)
        assert_ready_invariants(
            FlowStatus.READY_FOR_HUMAN.value,
            {"verification": dict(verification_evidence or {})},
            candidate_sha,
            reviewed_sha=str((stored or {}).get("sha") or ""),
            reason=reason,
        )
        await self._transition(run_id, FlowStatus.READY_FOR_HUMAN, reason=reason)

        if findings or verdict == "concerns":
            await self._post_journaled_mr_note(
                project_id, mr_iid, _review_mr_comment(verdict, summary, findings), run_id
            )

        mr_url = await self._read_mr_url(project_id, mr_iid)
        await self._post_journaled_note(
            project_id,
            issue_iid,
            self._evidence_comment(
                mr_url,
                candidate_sha,
                pipeline,
                plan_digest,
                review_summary=summary,
                warnings=warnings,
                verified=verified,
                review_feedback=feedback_section,
            ),
            run_id,
            "post_evidence_note",
        )
        logger.info("Run %s is ready_for_human at %s", run_id[:8], candidate_sha[:8])

    async def _review_feedback_unresolved(
        self, run_id: str, project_id: int, mr_iid: int | None, candidate_sha: str
    ) -> bool:
        """Whether a REQUIRED review-feedback discussion still blocks readiness.

        Q39-13: only the code-requesting corrections whose staged cycle
        entered the delivery pipeline gate the tested candidate — a
        repair that passes its tests while a required discussion stays
        unresolved is NOT ready. The resolution signal is GitLab's own
        resolvable-discussion model (the REVIEWER resolves the thread;
        forge never resolves, never merges, never marks the human
        decision complete). A plain non-resolvable thread can never
        resolve and therefore never gates; an unreadable discussions
        surface degrades with a logged warning rather than deadlocking
        the run. When discussions are still open, ONE summary note (per
        candidate — the A11 dedup) names the resolved and still-open
        threads so the reviewer knows exactly what blocks readiness.
        """
        requests = await read_review_feedback_requests(self._session_factory, run_id)
        required = [
            request
            for request in requests.values()
            if request.classification == IN_SCOPE_CORRECTION_CLASS
            and request.status in (REQUEST_STAGED, REQUEST_DISPATCHED)
        ]
        if not required or mr_iid is None:
            return False
        discussions = await self._discussions_or_none(project_id, mr_iid)
        if discussions is None:
            return False  # degraded honestly — logged at the read
        states = self._discussion_resolution_states(discussions)
        unresolved = [
            request
            for request in required
            if request.discussion_id in states and not states[request.discussion_id]
        ]
        if not unresolved:
            return False
        logger.info(
            "Run %s holds a green candidate at %s but %d required review "
            "discussion(s) stay unresolved — not ready",
            run_id[:8],
            candidate_sha[:8],
            len(unresolved),
        )
        section = review_feedback_summary_section(requests, states, candidate_sha)
        await self._post_deduped_mr_note(
            project_id,
            mr_iid,
            run_id,
            f"review-feedback-summary:{run_id}:{candidate_sha}",
            "## Candidate held for human review feedback\n\n"
            f"{section}\n\n"
            "The pipeline is green, but the correction discussions above are "
            "still open — resolve them in this merge request and the run "
            f"continues to readiness on its next pass.{_automated_footer()}",
        )
        return True

    async def _post_deduped_mr_note(
        self, project_id: int, mr_iid: int, run_id: str, key: str, body: str
    ) -> None:
        """Post ONE MR note per *key* — intent/outcome journaled (ADR-0005)."""
        if await self._rf_reply_delivered(key, kind=_REVIEW_FEEDBACK_SUMMARY_KIND):
            return
        async with self._session_factory() as session:
            action = ActionLog(
                flow_run_id=run_id,
                action_kind=_REVIEW_FEEDBACK_SUMMARY_KIND,
                correlation_id=f"mr-{mr_iid}",
                idempotency_key=key,
                status="requested",
            )
            session.add(action)
            await session.commit()
            action_id = action.id
        try:
            note = await self._gitlab.create_mr_note(project_id, mr_iid, body)
        except (httpx.HTTPError, GitLabAPIError) as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            logger.warning("Review-feedback summary note failed for run %s", run_id[:8])
            return
        await self._complete_action(action_id, "succeeded", {"note_id": getattr(note, "id", None)})

    async def _begin_repair(
        self,
        run_id: str,
        project_id: int,
        cycle: int,
        jobs,
        *,
        harness: bool = False,
    ) -> None:
        """evaluating_ci → proposing (repair): bump the cycle, re-propose.

        Builtin runs re-enter the LLM implementer; harness runs re-trigger
        the harness pipeline with the same bounded repair context appended
        to the brief (ADR-0015). The reason carries the failed job names
        (ADR-0004/0008).
        """
        repair_context = await self._build_repair_context(project_id, run_id, jobs)
        failed = _failed_job_names(jobs)
        next_cycle = cycle + 1
        repair_reason = "code failure" + (f" in jobs: {failed}" if failed else "")

        async with self._session_factory() as session:
            controller = Controller(session)
            run = await self._get_run(session, run_id)
            run.commit_cycle = next_cycle
            await controller.transition(
                run_id,
                FlowStatus.PROPOSING,
                reason=f"repair cycle {next_cycle}: {repair_reason}",
            )
            await session.commit()

        logger.info(
            "Run %s enters repair cycle %d — re-proposing with CI logs", run_id[:8], next_cycle
        )
        if harness:
            await self._advance_harness(
                project_id, run_id, repair_context=repair_context, repair_reason=repair_reason
            )
        else:
            await self._advance_proposal(
                project_id, run_id, repair_context=repair_context, repair_reason=repair_reason
            )

    async def _build_repair_context(
        self,
        project_id: int,
        run_id: str,
        jobs,
    ) -> str:
        """Bounded repair context: previous commit summary + failed-job logs.

        Per failed job the log is tail-truncated to ``REPAIR_LOG_PER_JOB_CHARS``
        and the whole context to ``REPAIR_CONTEXT_MAX_CHARS`` (ADR-0013).
        """
        sections: list[str] = []
        issue_iid = await self._read_issue_iid(run_id)
        if issue_iid is not None:
            branch = factory_branch(issue_iid, run_id)
            try:
                # F28: the branch object carries the head commit (incl. the
                # message) — no paginated commit-history read here either.
                branch_data = await self._gitlab.get_branch(project_id, branch)
            except GitLabAPIError:
                branch_data = {}
            head = branch_data.get("commit") or {}
            if head.get("id"):
                sections.append(
                    f"Previous commit on {branch}: {head.get('message', '')} "
                    f"({str(head['id'])[:8]})"
                )

        failed = [job for job in jobs if job.status == "failed"][:REPAIR_MAX_FAILED_JOBS]
        for job in failed:
            try:
                log = await self._gitlab.get_job_log(project_id, job.id)
            except GitLabAPIError:
                log = "(log unavailable)"
            sections.append(f"--- failed job: {job.name} ---\n{log[-REPAIR_LOG_PER_JOB_CHARS:]}")
        # F23: CI logs are untrusted — redact deny-pattern values before the
        # context enters a brief or an MR note (then apply the ADR-0013 cap).
        redacted, _ = EvidencePolicy.from_settings(self._settings).apply_policy(
            "\n\n".join(sections)
        )
        return redacted[-REPAIR_CONTEXT_MAX_CHARS:]

    async def _waiting_ci_deadline(self, session: AsyncSession, run_id: str) -> datetime | None:
        """Durable CI deadline: waiting_ci outbox timestamp + FORGE_CI_WAIT_SECONDS.

        The outbox row written atomically with the transition *is* the durable
        timer (ADR-0005) — no schema change needed in M1.
        """
        rows = (
            (
                await session.execute(
                    select(Outbox)
                    .where(Outbox.flow_run_id == run_id, Outbox.event_type == TRANSITION_EVENT_TYPE)
                    .order_by(Outbox.id)
                )
            )
            .scalars()
            .all()
        )
        entered: datetime | None = None
        for row in rows:
            if (row.payload or {}).get("to") == FlowStatus.WAITING_CI.value:
                entered = row.created_at
        wait_seconds = int(getattr(self._settings, "FORGE_CI_WAIT_SECONDS", 3600) or 3600)
        if entered is None:
            return None
        return as_aware_utc(entered) + timedelta(seconds=wait_seconds)

    # ------------------------------------------------------------------
    # Reconciler tick: Tier-1 auto-revive of transiently dead runs
    # ------------------------------------------------------------------

    async def evaluate_revival(self, now: datetime | None = None) -> None:
        """One reconciler pass over every run waiting for its auto-revive.

        A transiently dead run parks ``blocked`` with a revival stamp
        (:mod:`forge.runs.revival`); this pass re-dispatches the due ones on
        the SAME branch — the wait is worker-free, like ``waiting_ci``.
        """
        await evaluate_revivals(
            self._session_factory,
            self._settings,
            provider="gitlab",
            redispatch=self._redispatch_revival,
            now=now,
            log=logger,
        )

    async def _redispatch_revival(self, run_id: str) -> None:
        """Re-dispatch a revived run — same branch, attempt base = last candidate.

        R37-07 (#288): a HARNESS revival carries a continuation decision
        selected from the recorded recoverable state (Q35-02), dispatched
        as the lane-resume envelope; an UNCERTAIN decision dispatches
        NOTHING — zero pipelines, zero vendor sessions; the ambiguity goes
        to the operator via /retry's note. The revival also opens a NEW
        attempt generation (NEXT-01 — the dispatched lane token is
        attempt-scoped, the stalled attempt's credentials retire), bumped
        BEFORE the lane launches and aligned with the pause fence's
        resumed epoch.

        R37-01 (issue #282): the re-drive resolves the recovery EVENT it
        stands for from the ``retry_delivery_key`` history — the OPEN
        revival attempt's idempotency key names the delivery that
        stranded. With the identity established, the re-drive
        RECONSTRUCTS the identical committed decision BY ID (the frozen
        checkpoint loads — a newer checkpoint landing meanwhile changes
        nothing); with the identity unestablishable (an auto-revive
        window, an unkeyed direct drive) the decision is re-made and
        marked ``lineage: reestablished``. The builtin backend keeps its
        legacy repair re-proposal (recorded as this wave's honest
        limitation: no lane, no held WIP, no envelope).
        """
        fence_floor = 0
        fence = await pause_fence_decision(self._session_factory, run_id)
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            project_id = run.project_id
            evidence = dict(run.evidence or {})
            candidates = list(run.candidate_shas or [])
            backend_name = str(evidence.get("backend") or "").strip() or self._backend_name()
            # The death reason the walk to ``proposing`` overwrote: the
            # auto-revive stamp keeps the ORIGINAL terminal reason.
            revival_reason = str(
                (evidence.get("revival") or {}).get("reason")
                if isinstance(evidence.get("revival"), dict)
                else ""
            )
            death_reason = revival_reason or str(run.status_reason or "")
            # R36-02 lineage: the dead attempt's durable generation the
            # decision continues FROM — captured BEFORE the bump below opens
            # the revival's NEW attempt.
            source_attempt = int(run.cancellation_generation or 0)
            # R37-01: the stranded delivery this re-drive stands for — the
            # OPEN revival attempt's idempotency key (the
            # ``retry_delivery_key`` history). A ``delivery:<id>`` key names
            # the /retry event; anything else has no event identity.
            native_command_id: str | None = None
            open_attempt = await open_revival_attempt(session, run_id=run_id)
            if open_attempt is not None:
                open_key = str(open_attempt.idempotency_key or "")
                if open_key.startswith("delivery:"):
                    native_command_id = open_key[len("delivery:") :] or None
            if native_command_id is not None:
                # The committed decision the stranded event owns — its own
                # recorded event block is the authoritative source attempt
                # (the run row has since moved past it).
                stranded_entry = continuation.find_decision_by_event(
                    evidence.get(continuation.CONTINUATION_EVIDENCE_KEY), native_command_id
                )
                if stranded_entry is not None:
                    recorded_attempt = (stranded_entry.get("event") or {}).get("source_attempt")
                    if isinstance(recorded_attempt, int):
                        source_attempt = recorded_attempt
            if is_harness_backend(backend_name):
                fence_floor = fence.resumed_publication_epoch or 0
                run.cancellation_generation = max(
                    int(run.cancellation_generation or 0) + 1, fence_floor
                )
                await session.commit()
        if is_harness_backend(backend_name):
            decision = await self._select_continuation(
                run_id,
                death_reason=death_reason,
                evidence=evidence,
                candidate_shas=candidates,
                source_attempt=source_attempt,
                native_command_id=native_command_id,
                # No durable lineage to the stranded event's decision could be
                # established — whatever is minted now RE-ESTABLISHES it.
                lineage="reestablished",
                dispatches=True,
            )
            if not decision.dispatchable:
                logger.warning(
                    "Run %s revival stood down — continuation source unknown (%s); "
                    "an operator must resolve it via /retry (R37-07)",
                    run_id[:8],
                    decision.mode_selected,
                )
                return
            await self._advance_harness(
                project_id,
                run_id,
                # Q35-02: the WIP-continuity contract the decision selected
                # from the recoverable state — the same envelope /retry
                # dispatches.
                resume_mode=decision.resume_mode(),
            )
        else:
            await self._advance_proposal(project_id, run_id)

    # ------------------------------------------------------------------
    # Reconciler tick: A13 config-block recovery
    # ------------------------------------------------------------------

    async def _read_start_config(self, project_id: int) -> ConfigReadResult:
        """The typed `.forge.yml` read a run's start path scopes from (A13)."""
        return await read_project_config(self._gitlab, project_id, ref=self._target_branch())

    async def _park_config_blocked(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_iid: int | None,
        config_read: ConfigReadResult,
        issue_title: str,
        issue_description: str,
        author_username: str,
    ) -> None:
        """Park a run whose `.forge.yml` read failed — BEFORE any paid call.

        A13 policy: permissions may narrow, never widen. An unreadable or
        invalid config leaves the project's restrictions UNKNOWN, so the
        run never starts on the (wider) default profile: it parks
        ``blocked(config_unreadable|config_invalid: detail)`` with zero
        model calls and zero commits. The start context is stashed in the
        evidence so the reconciler's recovery pass can re-enter planning
        with the exact input the run was created with.
        """
        reason = config_read.blocked_reason or f"config_unreadable: {config_read.detail}"
        await self._to_terminal(run_id, FlowStatus.BLOCKED, reason)
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "config_block": {
                        "reason": reason[:200],
                        "issue_title": issue_title,
                        "issue_description": issue_description,
                        "author_username": author_username,
                    }
                },
            )
            await session.commit()
        await self._post_journaled_note(
            project_id,
            issue_iid,
            self._config_blocked_comment(run_id, reason),
            run_id,
            "config_blocked",
        )
        logger.warning(
            "Run %s parked %s — no planning call was made; the reconciler retries the read",
            run_id[:8],
            reason,
        )

    @staticmethod
    def _config_blocked_comment(run_id: str, reason: str) -> str:
        return (
            f"Run `{run_id[:8]}` is **blocked**: {reason}\n\n"
            f"The project's `.forge.yml` could not be read. Forge never widens a run's "
            "path scope because a config read failed — the run stays parked with no "
            "model calls and no commits until the config is readable. The reconciler "
            f"retries the read automatically; `/retry {run_id[:8]}` forces it sooner."
            "\n\n*This is an automated message.*"
        )

    async def evaluate_config_recovery(self, now: datetime | None = None) -> None:
        """One reconciler pass over runs parked ``blocked(config_…)`` (A13).

        Retries the typed config read for each; a run whose config is
        readable again (or provider-confirmed absent) walks back to
        ``preflight`` through the fenced plan-restart edge and re-plans —
        with zero paid calls while it waited.
        """
        await evaluate_config_blocks(
            self._session_factory,
            provider="gitlab",
            reread=self._read_start_config,
            replan=self._resume_config_blocked,
            log=logger,
        )

    async def _resume_config_blocked(self, run_id: str, stash: dict) -> None:
        """Re-enter planning for a recovered config-blocked run."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            project_id = run.project_id
            issue_iid = run.issue_iid
        await self._plan_and_publish(
            run_id,
            project_id=project_id,
            issue_iid=issue_iid or 0,
            issue_title=str(stash.get("issue_title") or ""),
            issue_description=str(stash.get("issue_description") or ""),
            author_username=str(stash.get("author_username") or ""),
        )

    # ------------------------------------------------------------------
    # Publication-intent recovery scanner (R11)
    # ------------------------------------------------------------------

    #: Run statuses where a publish leg is still in progress — the only
    #: states the recovery scanner may act on the run from.
    _PUBLISHING_STATUSES = frozenset(
        {
            FlowStatus.PROPOSING.value,
            FlowStatus.VALIDATING.value,
            FlowStatus.COMMITTING.value,
            FlowStatus.ENSURING_DRAFT_MR.value,
        }
    )

    #: Backoff for an intent the probe says is safe to re-dispatch — the
    #: run's own leg owns the re-dispatch (it holds the candidate); this
    #: scanner only resolves outcomes and stops the polling loop.
    _INTENT_PROBE_BACKOFF_SECONDS = 60

    def _publish_settle_seconds(self) -> int:
        """The A12 effect-certainty window (``FORGE_PUBLISH_SETTLE_SECONDS``).

        How long a negative probe parks an intent in ``probing`` before its
        window-end re-probe; a broken/absent setting degrades to the
        intents-module default, never to zero (a zero window would make one
        negative read decisive again).
        """
        raw = getattr(self._settings, "FORGE_PUBLISH_SETTLE_SECONDS", DEFAULT_SETTLE_WINDOW_SECONDS)
        try:
            seconds = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return DEFAULT_SETTLE_WINDOW_SECONDS
        return seconds if seconds > 0 else DEFAULT_SETTLE_WINDOW_SECONDS

    async def evaluate_publication_intents(self, now: datetime | None = None) -> None:
        """One recovery pass over every GitLab publication intent (R11).

        The post-restart half of the R11 fix on the GitLab lane: a worker
        that died between the remote commit and the journal completion
        leaves the intent open — this pass probes by identity (the
        ``(forge-op:<key>)`` marker + the intent-time expected parent) and:

        - ADOPT: the found commit is journaled as the run's committed
          candidate (a succeeded ``commit`` action) so the crashed leg's
          re-drive adopts it via ``_committed_candidate`` — never a second
          commit; when the Draft MR is already journaled the run also walks
          on to ``waiting_ci`` right here;
        - DUPLICATED / UNKNOWN: the intent resolves and a mid-publication
          run parks ``blocked`` (branch_drift / unknown_outcome contract);
        - REDISPATCH (nothing landed, head intact): A12 — a negative probe
          is not proof of absence, so the intent parks in the effect-
          certainty window (``probing``); a window-end re-probe that is
          STILL negative parks ``blocked(unknown_outcome)`` with operator
          instructions (GitLab has no branch-wide CAS that could refuse a
          duplicate), while a late-landing commit is adopted by the ADOPT
          branch above. This pass never POSTs.

        Superseded runs (R10: cancelled / terminal) resolve ``duplicated``
        — never adopted into a READY state.
        """
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            intents = await due_intents(session, provider="gitlab", now=now)
        for intent in intents:
            try:
                await self._resolve_one_publication_intent(intent, now=now)
            except Exception:
                # One broken intent must not stall the recovery pass.
                logger.exception("Publication-intent resolution failed for %s", intent.id[:8])

    async def _resolve_one_publication_intent(
        self, intent: PublicationIntent, *, now: datetime
    ) -> None:
        async with self._session_factory() as session:
            run = await session.get(FlowRun, intent.run_id)
            if run is None:
                await complete_intent(
                    session, intent.id, "duplicated", remote_result={"reason": "run_vanished"}
                )
                await session.commit()
                return
            run_status = run.status
            cancel_requested = bool(run.cancel_requested)
            project_id = int(run.project_id)
            issue_iid = int(run.issue_iid or 0)
        if cancel_requested or run_status in {s.value for s in TERMINAL_STATUSES}:
            async with self._session_factory() as session:
                await complete_intent(
                    session,
                    intent.id,
                    "duplicated",
                    remote_result={"reason": "run_superseded", "run_status": run_status},
                )
                await session.commit()
            await self._merge_run_evidence(
                intent.run_id,
                {
                    "superseded": {
                        "reason": "cancelled_during_publication"
                        if cancel_requested
                        else f"run already {run_status}",
                        "attempt_base": intent.expected_parent_oid,
                    }
                },
            )
            return
        if intent.status == "requested":
            return  # never dispatched — the run's own probe-first leg owns it

        # Probe by identity: marker + expected parent over the branch commits.
        try:
            head = await self._gitlab.get_branch_head(project_id, intent.target_ref)
            commits = await self._gitlab.list_commits(project_id, intent.target_ref)
        except GitLabAPIError:
            logger.exception(
                "Publication-intent probe read failed for branch %r — leaving open",
                intent.target_ref,
            )
            return
        hits = commit_matches(
            commits,
            operation_key=intent.operation_key,
            expected_parent_oid=intent.expected_parent_oid,
        )
        verdict = classify_probe(
            ProbeObservation(
                marker_hits=tuple(hits),
                head_oid=head,
                expected_parent_oid=intent.expected_parent_oid,
            )
        )
        if verdict is ProbeVerdict.ADOPT:
            sha = hits[0]
            async with self._session_factory() as session:
                # Journal the found commit as THIS run's succeeded commit —
                # the durable record the resumed leg's ``_committed_candidate``
                # adoption reads (sha must still be the live branch head).
                controller = Controller(session)
                action_id = await controller.record_action(
                    intent.run_id, "commit", correlation_id=intent.target_ref
                )
                await controller.complete_action(
                    action_id,
                    "succeeded",
                    {"sha": sha, "reconciled": True, "adopted": True},
                )
                await complete_intent(
                    session,
                    intent.id,
                    "adopted",
                    provider_object_id=sha,
                    remote_result={"sha": sha, "reconciled": True},
                )
                await session.commit()
            logger.warning(
                "Recovered publication intent %s for run %s — adopted commit %s",
                intent.id[:8],
                intent.run_id[:8],
                sha[:8],
            )
            mr_iid = await self._journaled_draft_mr(intent.run_id, project_id, intent.target_ref)
            if mr_iid is None:
                # The journal lags the effect: the crashed leg may have
                # created the MR without journaling it yet (LIVE-found via
                # the FI suite 2026-09-20: two create_merge_request calls
                # for one intent). The PROVIDER is the arbiter — adopt an
                # already-open MR for this branch before creating one.
                try:
                    for mr in await self._gitlab.list_merge_requests(
                        project_id, state="opened", per_page=50
                    ):
                        if mr.source_branch == intent.target_ref:
                            mr_iid = int(mr.iid)
                            break
                except GitLabAPIError:
                    mr_iid = None
            if mr_iid is None:
                # A12 convergence: the publish leg may have stood down inside
                # the effect-certainty window (its step completed, the run
                # stayed mid-publish) — this scanner finishes the walk itself:
                # open the Draft MR on the adopted sha, then advance the run.
                try:
                    mr_iid = await self._create_draft_mr(
                        project_id, intent.run_id, intent.target_ref, sha
                    )
                except (GitLabAPIError, httpx.HTTPError):
                    logger.warning(
                        "Adopted commit %s for run %s but the Draft MR could not be "
                        "created — leaving the run mid-publish for a re-drive",
                        sha[:8],
                        intent.run_id[:8],
                        exc_info=True,
                    )
            if mr_iid is not None:
                # The crashed leg already opened the Draft MR — finish the
                # walk to waiting_ci on the adopted sha right here.
                async with self._session_factory() as session:
                    controller = Controller(session)
                    for status in (
                        FlowStatus.VALIDATING,
                        FlowStatus.COMMITTING,
                        FlowStatus.ENSURING_DRAFT_MR,
                        FlowStatus.WAITING_CI,
                    ):
                        try:
                            await controller.transition(
                                intent.run_id,
                                status,
                                reason=f"publication intent: adopted commit {sha[:8]}",
                            )
                        except InvalidTransition:
                            pass  # already past this stage — resume the walk
                    run = await self._get_run(session, intent.run_id)
                    run.mr_iid = mr_iid
                    if sha not in list(run.candidate_shas or []):
                        run.candidate_shas = list(run.candidate_shas or []) + [sha]
                    run.evidence = _merge_evidence(
                        run.evidence,
                        {
                            "published_candidate": {
                                "sha": sha,
                                "base": intent.expected_parent_oid,
                                "branch": intent.target_ref,
                                "mr_iid": mr_iid,
                                "reconciled": True,
                            }
                        },
                    )
                    await session.commit()
            return
        if verdict is ProbeVerdict.DUPLICATED:
            async with self._session_factory() as session:
                await complete_intent(
                    session,
                    intent.id,
                    "duplicated",
                    remote_result={"branch": intent.target_ref},
                )
                await session.commit()
            if run_status in self._PUBLISHING_STATUSES:
                await self._to_terminal(
                    intent.run_id,
                    FlowStatus.BLOCKED,
                    f"branch_drift: {intent.target_ref} moved away from the intent "
                    "(reconciled by the publication-intent scanner)",
                )
            return
        if verdict is ProbeVerdict.UNKNOWN:
            async with self._session_factory() as session:
                await complete_intent(
                    session, intent.id, "unknown", remote_result={"matches": hits}
                )
                await session.commit()
            if run_status in self._PUBLISHING_STATUSES:
                await self._to_terminal(intent.run_id, FlowStatus.FAILED, "commit_unknown_outcome")
            return
        # REDISPATCH — A12: a negative probe proves nothing landed YET; it
        # cannot prove that no first effect is in flight, and GitLab's
        # Commits API has no branch-wide CAS that could refuse a duplicate.
        # The intent parks in the effect-certainty window (``probing``) and
        # only a window-end re-probe that is STILL negative parks the honest
        # unknown — this pass never derives a dispatch from one read.
        async with self._session_factory() as session:
            decision = await settle_negative_probe(
                session,
                intent,
                now=now,
                window_seconds=self._publish_settle_seconds(),
            )
            exhausted = decision is SettleDecision.PARK_UNKNOWN
            if exhausted:
                await complete_intent(
                    session,
                    intent.id,
                    "unknown",
                    remote_result={
                        "reason": "settle_window_exhausted",
                        "operator_instruction": (
                            "Publication outcome unresolved after the effect-certainty "
                            "window: an operator must inspect branch "
                            f"`{intent.target_ref}` and reconcile manually — forge will "
                            "not re-publish over an unknown outcome."
                        ),
                        "settle": settle_state_record(intent),
                    },
                )
            await session.commit()
        if exhausted:
            if run_status in self._PUBLISHING_STATUSES:
                await self._to_terminal(intent.run_id, FlowStatus.BLOCKED, "commit_unknown_outcome")
                await self._post_journaled_note(
                    project_id,
                    issue_iid,
                    f"Run `{intent.run_id[:8]}` publication outcome is **unresolved** — the "
                    "certainty window expired with consistently-negative probes and GitLab "
                    "offers no write precondition that could prove no effect is in flight. "
                    f"An operator must inspect branch `{intent.target_ref}` and reconcile "
                    "manually; forge will not re-publish over an unknown outcome.\n\n"
                    "*This is an automated message.*",
                    intent.run_id,
                    "publish_unknown_outcome",
                )
            return
        # WAIT: the certainty window was (re)opened — the re-probe happens at
        # its end, never hot, and nothing is dispatched from a read alone.

    # ------------------------------------------------------------------
    # Reconciler tick: waiting_harness → … (ADR-0015)
    # ------------------------------------------------------------------

    async def evaluate_waiting_harness(self, now: datetime | None = None) -> None:
        """One reconciler pass over every run parked in ``waiting_harness``.

        R03: provider-scoped like ``evaluate_waiting_ci`` — the GitLab CI
        backend cannot poll an Actions/Pipelines handle.
        """
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            run_ids = (
                (
                    await session.execute(
                        select(FlowRun.id).where(
                            FlowRun.provider == "gitlab",
                            FlowRun.status == FlowStatus.WAITING_HARNESS.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
        for run_id in run_ids:
            try:
                await self._evaluate_harness_one(run_id, now)
            except Exception:
                # One broken run must not stall the reconciler loop.
                logger.exception("Harness reconcile pass failed for run %s", run_id[:8])
        # Q35-04: the occupancy pass — leases whose runs landed terminal
        # above parked draining; probe their native jobs and release the
        # ones OBSERVED terminal (the same tick that observed them).
        try:
            released = await self._reconcile_draining_leases()
            if released:
                logger.info("Occupancy pass released %d draining execution lease(s)", released)
        except Exception:
            logger.exception("Draining-lease occupancy pass failed")

    async def _evaluate_harness_one(self, run_id: str, now: datetime) -> None:
        """Poll one waiting_harness run through its journaled backend handle.

        R17 (deadline-before-I/O): the FIRST operations of every evaluation
        are local deadline/cancel checks over the journaled handle — no
        provider call is made once the harness budget is spent, so a
        permanently erroring GitLab API can never hold a run past its
        ``harness_timeout``. A poll failure therefore cannot extend the
        deadline either: the deadline derives only from the journaled
        ``started_at``, never from poll outcomes.
        """
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            evidence = dict(run.evidence or {})
            project_id = run.project_id
            cancel_requested = bool(run.cancel_requested)
            entry_status = run.status

        backend_name = str(evidence.get("backend") or "").strip()
        if backend_name and not is_harness_backend(backend_name):
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "waiting_harness on a non-harness backend"
            )
            return

        handle = ((evidence.get("harness") or {}).get("handle")) or ""
        if not handle:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "waiting_harness without harness handle"
            )
            return

        # R37-07 (#288): the callback is bound to the CURRENT attempt — the
        # run's live state names which dispatch this reconciler drives. A
        # poll arriving for anything but ``waiting_harness`` (a resurrected
        # worker holding the PREVIOUS dispatch's journaled handle after a
        # pause/resume, a late pipeline completion) reconciles to a
        # superseded record with ZERO provider writes — a delayed older
        # pipeline can never become the current WIP/candidate source. The
        # durable handle the reconciler restarts from is the LAST
        # dispatch's (the retry leg replaced it), so the binding key is the
        # run's own state, checked before any provider I/O. (A CANCELLED
        # run falls through to the grant-revocation branch below, whose
        # wording is its own.)
        if entry_status != FlowStatus.WAITING_HARNESS.value and not cancel_requested:
            try:
                stale_attempt_base = str((json.loads(handle) or {}).get("attempt_base") or "")
            except (TypeError, ValueError):
                stale_attempt_base = ""
            await self._merge_run_evidence(
                run_id,
                {
                    "superseded": {
                        "reason": f"run already {entry_status}",
                        "attempt_base": stale_attempt_base,
                    }
                },
            )
            logger.info(
                "Run %s is %s — harness callback for a previous dispatch recorded as "
                "superseded, zero writes (R37-07)",
                run_id[:8],
                entry_status,
            )
            return

        # GitHub/Azure-subject runs are polled by their own harness
        # reconcilers (runs/github_service.py / runs/azure_service.py) —
        # the GitLab CI backend cannot read an Actions/Pipelines handle.
        if getattr(run, "provider", "gitlab") in ("github", "azure_devops") or (
            '"provider": "github"' in (handle or "") or "azure_pipelines" in (handle or "")
        ):
            return

        # --- R17: local deadline / grant check BEFORE any provider I/O ----
        try:
            handle_data = json.loads(handle)
        except (TypeError, ValueError):
            handle_data = {}
        if cancel_requested:
            # F13: the publication grant is revoked — stand down without
            # touching the provider. The late candidate is recorded as
            # superseded either way.
            await self._merge_run_evidence(
                run_id,
                {
                    "superseded": {
                        "reason": "cancelled",
                        "attempt_base": str(handle_data.get("attempt_base") or ""),
                    }
                },
            )
            logger.info("Run %s cancelled — harness evaluation stood down pre-poll", run_id[:8])
            return
        timeout = int(getattr(self._settings, "FORGE_HARNESS_TIMEOUT_SECONDS", 1800) or 1800)
        started = _parse_journaled_time(handle_data.get("started_at"))
        if started is not None and as_aware_utc(now) > as_aware_utc(started) + timedelta(
            seconds=timeout
        ):
            await self._handle_harness_failure(
                run_id,
                project_id,
                HarnessOutcome.failed("infrastructure", "harness_timeout"),
            )
            return

        # R13: the run budget's wall clock is the second LOCAL deadline —
        # past it the run parks blocked(budget_exhausted) without a single
        # provider call (the same deadline-before-I/O posture as R17), so a
        # budgeted run can never outlive its frozen wall clock on polls.
        block = await self._budget_episode_block(run_id, now=now)
        if block is not None:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, block)
            return

        try:
            backend = self._harness_backend(project_id)
        except ValueError as exc:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"backend_config: {exc}")
            return

        try:
            outcome = await backend.poll(run, handle, now=now)
        except (GitLabAPIError, httpx.HTTPError):
            logger.exception("Harness poll read failed for run %s — keeping it waiting", run_id[:8])
            return

        if outcome.status == "running":
            return  # keep waiting — the durable deadline decides the rest

        if outcome.status == "failed":
            await self._handle_harness_failure(run_id, project_id, outcome)
            return

        await self._adopt_harness_change(run_id, project_id, outcome)

    async def _handle_harness_failure(
        self, run_id: str, project_id: int, outcome: HarnessOutcome
    ) -> None:
        """One terminal harness failure → optional fallback advance, else blocked.

        Shared by the poll outcome and the R17 local deadline path (which
        feeds a synthetic ``harness_timeout`` outcome without any provider
        call). With FORGE_HARNESS_FALLBACK off — the default — this is a
        straight local transition to ``blocked``.
        """
        kind = outcome.failure_kind or "code"
        # ADR-0023 §6: an opt-in, journaled advance down the frozen
        # chain — only infrastructure, only pre-candidate, OFF by
        # default. Everything else keeps the ADR-0015 semantics:
        # harness failures never enter the LLM repair loop.
        if await self._advance_harness_fallback(
            run_id, project_id, failure_kind=kind, failure_reason=outcome.reason
        ):
            return
        await self._to_terminal(run_id, FlowStatus.BLOCKED, f"harness_{kind}: {outcome.reason}")

    async def _current_harness_selection(self, run_id: str) -> tuple[HarnessSelection | None, bool]:
        """The run's current position in the frozen chain (ADR-0023 §6).

        Returns (selection, candidate_exists). The RunSpec is immutable
        (ADR-0018), so the runtime position lives in the run's
        ``harness_selection`` evidence; pre-v2 runs (no frozen chain) have
        none and never advance.
        """
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            candidate_exists = bool(run.candidate_shas)
            fragment = dict((run.evidence or {}).get("harness_selection") or {})
        selection: HarnessSelection | None
        if fragment:
            selection = selection_from_spec_document({"backend_config": fragment})
            return selection, candidate_exists
        async with self._session_factory() as session:
            spec = (
                (
                    await session.execute(
                        select(RunSpec).where(RunSpec.run_id == run_id).order_by(RunSpec.id)
                    )
                )
                .scalars()
                .first()
            )
        selection = selection_from_spec_document(spec.document if spec is not None else None)
        return selection, candidate_exists

    async def _advance_harness_fallback(
        self,
        run_id: str,
        project_id: int,
        *,
        failure_kind: str,
        failure_reason: str,
    ) -> bool:
        """One dispatch-time fallback advance; True when the leg re-fired.

        OFF by default (FORGE_HARNESS_FALLBACK); infrastructure-kind only;
        only before any candidate exists (ADR-0016 single producer); only
        down the chain frozen in the RunSpec. Every advance is journaled in
        ``action_log`` and the run stays ``waiting_harness`` on the next
        leg's handle.

        R13: the re-dispatch below is budget-gated — ``_advance_harness``
        refuses to start the next leg against an exhausted/spent wall clock
        and parks the run ``blocked(budget_exhausted)`` instead.
        """
        if not bool(getattr(self._settings, "FORGE_HARNESS_FALLBACK", False)):
            return False
        if failure_kind != "infrastructure":
            return False
        selection, candidate_exists = await self._current_harness_selection(run_id)
        if selection is None:
            return False
        nxt = advance_harness_fallback(
            selection,
            failed_driver=selection.harness,
            failure_kind=failure_kind,
            fallback_enabled=True,
            candidate_exists=candidate_exists,
        )
        if nxt is None:
            return False

        # Intent-first journal, then the outcome record the audit trail
        # reads: {"event","from","to","reason"} (ADR-0023 §6).
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(run_id, "harness_fallback")
            await session.commit()
        await self._complete_action(
            action_id,
            "succeeded",
            {
                "event": "harness_fallback",
                "from": selection.harness,
                "to": nxt.harness,
                "reason": failure_reason,
            },
        )
        await self._merge_run_evidence(run_id, {"harness_selection": nxt.as_document()})
        logger.warning(
            "Run %s harness fallback: %s -> %s (%s)",
            run_id[:8],
            selection.harness,
            nxt.harness,
            failure_reason,
        )
        await self._advance_harness(project_id, run_id, driver=nxt.harness)
        return True

    async def _adopt_harness_change(
        self,
        run_id: str,
        project_id: int,
        outcome: HarnessOutcome,
    ) -> None:
        """Well-formed candidate bundle → publish → Draft MR → waiting_ci.

        The bundle goes through the trusted publisher (ADR-0016 §2): grant,
        spec digest and fence checks, strict materialization against the
        authoritative attempt-base blobs, policy validation, then ONE
        journaled commit via the ChangesetWriter pinned to the attempt base
        — forge's write is the only write, ever. From ``waiting_ci`` the
        existing quality-contract → review → evidence flow takes over,
        unchanged.
        """
        bundle = outcome.bundle
        if bundle is None:
            # A non-running, non-failed outcome must carry a candidate bundle
            # (HarnessOutcome.change_candidate) — a malformed one parks the
            # run instead of crashing the reconciler tick.
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "harness outcome without candidate bundle"
            )
            return
        # F13 (ADR-0018 §4) + R17 liveness: a late candidate for a run that
        # already reached ANY terminal state (cancelled, failed, blocked,
        # ready) is superseded — recorded as evidence only, never published,
        # and a terminal run is never revived by the callback. The
        # publication grant is gone the moment the run left the active set.
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            status = run.status
            revoked = bool(run.cancel_requested or status in {s.value for s in TERMINAL_STATUSES})
        if revoked:
            reason = (
                "cancelled"
                if run.cancel_requested or status == FlowStatus.CANCELLED.value
                else f"run already {status}"
            )
            await self._merge_run_evidence(
                run_id,
                {
                    "superseded": {
                        "reason": reason,
                        "attempt_base": bundle.attempt_base_oid,
                    }
                },
            )
            logger.info(
                "Run %s is %s — harness candidate on %s recorded as superseded",
                run_id[:8],
                reason,
                bundle.attempt_base_oid[:8],
            )
            return

        await self._transition(
            run_id,
            FlowStatus.COMMITTING,
            reason=f"publishing harness candidate on {bundle.attempt_base_oid[:8]}",
        )

        # Stage-B fence reused via a callable (ADR-0017): the publication is
        # abandoned unless the run is still the COMMITTING run we just made
        # it — a cancel or a state move between transition and write fences
        # the publisher out.
        async def _fence_valid() -> bool:
            async with self._session_factory() as session:
                run = await session.get(FlowRun, run_id)
                if run is None:
                    return False
                if run.cancel_requested or run.status == FlowStatus.CANCELLED.value:
                    return False
                return run.status == FlowStatus.COMMITTING.value

        writer = self._writer_class(
            self._gitlab,
            self._session_factory,
            project_id,
            settle_seconds=self._publish_settle_seconds(),
        )
        result = await publish_candidate(
            gitlab=self._gitlab,
            session_factory=self._session_factory,
            writer=writer,
            run=run,
            bundle=bundle,
            fence_check=_fence_valid,
        )
        if not result.ok:
            if result.reason.startswith("claim_superseded"):
                # A04: the executing claim lost its step (lease expired and a
                # new owner reclaimed, fence moved, run binding changed) —
                # the publisher stood down BEFORE the native call and already
                # recorded the superseded evidence. The run's fate belongs to
                # the live owner's re-driven leg; parking it from this stale
                # one would fight the reclaim.
                logger.warning(
                    "Run %s harness publish stood down — execution claim stale (%s)",
                    run_id[:8],
                    result.reason,
                )
                return
            if result.settling:
                # A12: the recovery probe was negative — the intent parked in
                # the effect-certainty window. Leave the run in its
                # non-terminal publishing state; the window-end re-probe by
                # evaluate_publication_intents resolves it (adopt or park).
                logger.info(
                    "Run %s harness publish negative-probed — settling in the A12 "
                    "certainty window (no re-dispatch)",
                    run_id[:8],
                )
                return
            if result.unknown_outcome:
                # The commit MAY exist: block as failed, never blind-retry.
                await self._to_terminal(run_id, FlowStatus.FAILED, result.reason)
            else:
                await self._to_terminal(
                    run_id, FlowStatus.BLOCKED, f"candidate_rejected: {result.reason}"
                )
            return
        sha = result.commit_sha or ""
        # F23: the artifact meta summary is harness-controlled text — apply
        # the evidence policy before it is stored on the run row.
        summary, _ = EvidencePolicy.from_settings(self._settings).apply_policy(outcome.summary)
        await self._merge_run_evidence(
            run_id,
            {
                "harness_change": {"sha": sha, "summary": summary},
                "published_candidate": {
                    "sha": sha,
                    "attempt_base": bundle.attempt_base_oid,
                    "entries": len(bundle.entries),
                },
            },
        )
        await self._record_harness_usage(run_id, bundle)
        await self._transition(run_id, FlowStatus.ENSURING_DRAFT_MR)

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            mr_iid = run.mr_iid
            cycle = run.commit_cycle or 1
        branch = factory_branch(run.issue_iid, run_id)
        try:
            if mr_iid is not None:
                await self._update_draft_mr(project_id, run_id, mr_iid, branch, sha, cycle)
            else:
                mr_iid = await self._create_draft_mr(project_id, run_id, branch, sha)
        except GitLabAPIError as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"mr_failed: {exc}")
            return
        except httpx.HTTPError:
            await self._to_terminal(run_id, FlowStatus.FAILED, "mr_unknown_outcome")
            return

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(
                run_id, FlowStatus.WAITING_CI, reason=f"pipeline for {sha[:8]}"
            )
            run = await self._get_run(session, run_id)
            run.mr_iid = mr_iid
            run.candidate_shas = list(run.candidate_shas or []) + [sha]
            await session.commit()

        logger.info(
            "Run %s published harness candidate %s — waiting for CI",
            run_id[:8],
            sha[:8],
        )

    async def _record_harness_usage(self, run_id: str, bundle) -> None:
        """F22 lite + R13: record one harness episode's usage receipt.

        The receipt comes from the parsed event stream (candidate.meta.json);
        unknown counts stay NULL — never zero, never fabricated. The same
        receipt also reconciles the run budget as exactly one opaque call,
        keyed by the episode (the dispatched harness pipeline) so a repeated
        artifact poll or a crash between adopt and record cannot
        double-consume.
        """
        usage = bundle.usage
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            pipeline_id = int(((run.evidence or {}).get("harness") or {}).get("pipeline_id") or 0)
            session.add(
                LLMCall(
                    flow_run_id=run_id,
                    role="implementer",
                    provider="ci_harness",
                    model=(
                        usage.model
                        if usage is not None and usage.model
                        else str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or "unknown")
                    ),
                    status="ok",
                    input_tokens=usage.input_tokens if usage is not None else None,
                    output_tokens=usage.output_tokens if usage is not None else None,
                    cached_tokens=usage.cached_input_tokens if usage is not None else None,
                    driver=usage.driver if usage is not None else None,
                    completeness=usage.completeness if usage is not None else "unknown",
                )
            )
            await reconcile_harness_receipt(
                session,
                run_id,
                usage,
                dedupe_key=(f"{run_id}:{pipeline_id}" if pipeline_id else None),
            )
            await session.commit()

    async def evaluate_ready_evidence(self) -> None:
        """Recover runs already READY whose evidence note never got posted.

        Crash window (ADR-0017 §5): the ``ready_for_human`` transition
        committed but the process died before the journaled evidence note even
        started — no ``post_evidence_note`` action row exists. A note whose
        posting DID begin has a journal row and is left alone: its outcome is
        the journal's to answer, never a blind re-post (ADR-0005).

        R03: provider-scoped — the recovery note is posted through the GitLab
        client, so only GitLab runs belong in this scan.
        """
        async with self._session_factory() as session:
            runs = (
                (
                    await session.execute(
                        select(FlowRun).where(
                            FlowRun.provider == "gitlab",
                            FlowRun.status == FlowStatus.READY_FOR_HUMAN.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            journaled = set(
                (
                    await session.execute(
                        select(ActionLog.flow_run_id).where(
                            ActionLog.action_kind == "post_evidence_note"
                        )
                    )
                )
                .scalars()
                .all()
            )
        for run in runs:
            if run.id in journaled:
                continue
            try:
                await self._post_missing_evidence_note(run.id)
            except Exception:
                # One broken run must not stall the recovery pass.
                logger.exception("Evidence-note recovery failed for run %s", run.id[:8])

    async def _post_missing_evidence_note(self, run_id: str) -> None:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            sha = (run.candidate_shas or [""])[-1]
            plan_digest = run.plan_digest or ""
            mr_iid = run.mr_iid
            project_id = run.project_id
            issue_iid = run.issue_iid
            pipeline_evidence = dict((run.evidence or {}).get("pipeline") or {})
            review = dict((run.evidence or {}).get("review") or {})
        if not sha:
            logger.warning(
                "Ready run %s has no candidate sha — cannot recover evidence", run_id[:8]
            )
            return
        pipeline = SimpleNamespace(
            id=pipeline_evidence.get("id"),
            status=str(pipeline_evidence.get("status") or "unknown"),
            web_url=pipeline_evidence.get("url"),
        )
        mr_url = await self._read_mr_url(project_id, mr_iid)
        await self._post_journaled_note(
            project_id,
            issue_iid,
            self._evidence_comment(
                mr_url,
                sha,
                pipeline,
                plan_digest,
                review_summary=str(review.get("summary") or "") or None,
            ),
            run_id,
            "post_evidence_note",
        )
        logger.warning("Run %s: recovered the evidence note after a crash", run_id[:8])

    async def evaluate_accepted(self) -> None:
        """R24 acceptance reconciliation: record a READY run's merge outcome.

        Forge never merges (ADR-0003) — the human's merge IS the acceptance
        signal, and it is observable provider-side. This lightweight pass
        (no webhooks) reads each ready run's native MR state once and records
        the decision as ``acceptance`` evidence: ``merged`` feeds the
        ``accepted`` ladder rung and ``forge_runs_accepted``; ``closed``
        records an honest rejection. A run with a recorded decision is never
        re-read — acceptance is counted exactly once — and an API failure
        just waits for the next tick.

        R03: GitLab-scoped like ``evaluate_ready_evidence`` — the GitHub and
        Azure lanes get their acceptance reads from their own reconcilers.
        """
        async with self._session_factory() as session:
            runs = (
                (
                    await session.execute(
                        select(FlowRun).where(
                            FlowRun.provider == "gitlab",
                            FlowRun.status == FlowStatus.READY_FOR_HUMAN.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
        for run in runs:
            evidence = dict(run.evidence or {})
            recorded = evidence.get("acceptance")
            if isinstance(recorded, dict) and recorded.get("state"):
                continue  # already counted — never re-read, never re-recorded
            if run.mr_iid is None:
                continue
            try:
                mr = await self._gitlab.get_merge_request(run.project_id, run.mr_iid)
            except GitLabAPIError:
                logger.debug(
                    "Acceptance read failed for run %s — keeping it for the next tick",
                    run.id[:8],
                )
                continue
            state = (mr.state or "").strip().lower()
            if state not in ("merged", "closed"):
                continue  # still open — the human has not decided
            await self._merge_run_evidence(
                run.id,
                {
                    "acceptance": {
                        "state": state,
                        "sha": mr.sha or "",
                        "merged_at": mr.merged_at or "",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )
            logger.info("Run %s recorded acceptance %s — MR !%s", run.id[:8], state, run.mr_iid)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _approvers(self) -> list[str]:
        """The trusted approver list (comma-separated FORGE_APPROVERS)."""
        raw = getattr(self._settings, "FORGE_APPROVERS", "") or ""
        return [name.strip() for name in raw.split(",") if name.strip()]

    def _required_jobs(self) -> list[str]:
        """The ADR-0008 quality contract (comma-separated FORGE_REQUIRED_JOBS)."""
        raw = getattr(self._settings, "FORGE_REQUIRED_JOBS", "") or ""
        return [name.strip() for name in raw.split(",") if name.strip()]

    def _target_branch(self) -> str:
        return getattr(self._settings, "FORGE_TARGET_BRANCH", "main") or "main"

    def _compile_harness_selection(self) -> HarnessSelection:
        """ADR-0023 §2: preference ∩ lanes → the frozen harness decision.

        The configured backend driver is always part of the list (tighten-
        only, ADR-0015) — a contradictory preference is refused, never
        silently repaired. R31: the compilable lanes are the project's
        available-driver manifest (:func:`resolve_available_drivers`;
        unset — the shipped driver set, exactly what the compiler was
        handed before the manifest existed), so a driver the project did
        not onboard is never selected — not by the preference, not by the
        planner's proposal. Credential presence stays declared by the
        preference and doctor-verified (ADR-0011); a lane without creds
        fails infrastructure at dispatch, which with the fallback switch
        OFF (the default) blocks the run visibly.

        R31 §5: the planner's structured proposal ({"harness",
        "budget_class", "reason"}) is honored when the planner output
        carries one (``last_plan``) — policy-constrained ranking: the
        compiler accepts it only inside preference ∩ available. The stub
        planner carries no proposal, so the stub path falls back to the
        preference order verbatim.
        """
        preference = resolve_preference(self._config, self._settings)
        backend = self._backend_name()
        validate_preference(
            preference, current_driver(backend) if is_harness_backend(backend) else None
        )
        # D06: None (no manifest anywhere) widens to the shipped legacy set;
        # a DECLARED set — even empty — is the boundary (C06 raises on a
        # disjoint configured driver below).
        _resolved = resolve_available_drivers(self._config, self._settings)
        available = _resolved if _resolved is not None else set(SHIPPED_DRIVERS)
        selection = compile_harness_selection(
            preference,
            backend,
            available,
            self._planner_harness_proposal(),
        )
        # R31: the budget class's numeric profile (R13 FORGE_BUDGET_PROFILES)
        # resolves AT FREEZE TIME and rides ON the selection — the gate
        # approves exactly these ceilings. ``None`` (no finite profile)
        # freezes no ceiling block at all (byte-compatible).
        limits = self._budget_limits_for_class(selection.budget_class)
        if limits is not None:
            selection = replace(
                selection,
                budget_ceilings=BudgetCeilings(
                    max_calls=limits.max_calls,
                    max_tokens=limits.max_tokens,
                    wallclock_s=limits.wallclock_s,
                ),
            )
        return selection

    def _planner_harness_proposal(self) -> dict | None:
        """R31: the planner's optional harness proposal, leniently read.

        Mirrors :meth:`_plan_files_hint` discipline: whatever the planner
        agent exposes as ``last_plan`` may carry ``harness`` /
        ``budget_class`` / ``reason``; anything missing, non-string or
        empty yields no proposal (the compiler keeps its defaults). The
        planner is never the authority — every field is re-validated by
        :func:`compile_harness_selection` against the frozen policy.
        """
        last_plan = getattr(self._planner, "last_plan", None)
        if not isinstance(last_plan, dict):
            return None
        proposal: dict[str, str] = {}
        for key in ("harness", "budget_class", "reason"):
            value = str(last_plan.get(key) or "").strip()
            if value:
                proposal[key] = value
        return proposal or None

    def _policy_digest(self) -> str:
        # ADR-0009 + ADR-0018 §1: the gate binds the effective execution
        # policy — canonical-JSON sha256 of the approvers, the target branch,
        # the required jobs, the implementer backend and the harness model.
        # A settings drift between approval and execution is detectable, not
        # silent. ADR-0023 §3: the harness preference list and the fallback
        # switch join the digest — changing either invalidates pending gates
        # exactly like plan drift (the per-run budget class and selection
        # reason are bound via the RunSpec digest instead).
        document = {
            "approvers": self._approvers(),
            "target_branch": self._target_branch(),
            "required_jobs": self._required_jobs(),
            "implementer_backend": self._backend_name(),
            "harness_model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
            "harness_preference": resolve_preference(self._config, self._settings),
            "harness_fallback": bool(getattr(self._settings, "FORGE_HARNESS_FALLBACK", False)),
        }
        return canonical_json_digest(document)

    def _build_run_spec_document(
        self,
        *,
        project_id: int,
        issue_iid: int | None,
        base_sha: str,
        task_title: str,
        task_description: str,
        task_digest: str,
        plan_summary: str,
        plan_files_hint: list[str],
        plan_digest: str,
        allowed_paths: list[str] | None = None,
        harness_selection: HarnessSelection | None = None,
        config_read: ConfigReadResult | None = None,
        profile_digest: str = "",
    ) -> dict:
        """The immutable, EXECUTABLE RunSpec document (R04, ADR-0018 §1).

        Frozen at plan acceptance — before the plan is published — so the
        gate approves exactly what the run will execute: the task text, the
        plan artifact, the model route, the tool/path policy, the
        verification contract, the budgets and the backend/driver. Post-
        approval legs read this document through
        :meth:`_load_executable_spec` (digest-verified on every read), never
        live Settings.

        ``allowed_paths`` (v0.7 monorepo scoping) is present only for scoped
        runs. ADR-0023 §3: ``backend_config`` also freezes the harness
        decision (selected driver, fallback tail, budget class, reason).
        A13: ``config_read`` freezes the path scope's provenance — the
        config read status, its ref and the content digest — so a restart
        validates against the approved snapshot instead of the live file.
        A18: ``profile_digest`` freezes the execution profile — the
        toolchain pins, install strategy and honest ci_contract derived
        from the target repo (forge.runs.execution_profile) — so the gate
        approves the exact build/test contract the lane must run.
        """
        selection = harness_selection or self._compile_harness_selection()
        backend = self._backend_name()
        # R13: the budget class's numeric profile is resolved AT FREEZE TIME
        # and stored IN the spec — the gate approves exactly these ceilings
        # and the honest enforcement level of this lane. ``None`` (no finite
        # profile) freezes no ceiling fields at all (byte-compatible). C02:
        # the CANONICAL numbers are the selection's RESOLVED ceilings —
        # never re-resolved from the class NAME here.
        limits = self._limits_of_selection(selection)
        enforcement = budget_enforcement_for_backend(backend) if limits is not None else ""
        spec = ExecutableRunSpec.freeze(
            provider="gitlab",
            project_id=project_id,
            issue_iid=issue_iid,
            source_base_oid=base_sha or "",
            task_title=task_title,
            task_description=task_description,
            plan_summary=plan_summary,
            plan_files_hint=plan_files_hint,
            plan_digest=plan_digest,
            model_route=IMPLEMENTER_TIER,
            policy_digest=self._policy_digest(),
            required_jobs=self._required_jobs(),
            waived_conclusions=sorted(waived_conclusions_from_settings(self._settings)),
            allowed_paths=allowed_paths or [],
            backend=backend,
            harness_model=str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
            target_branch=self._target_branch(),
            harness_driver=selection.harness,
            harness_fallbacks=selection.fallbacks,
            budget_class=selection.budget_class,
            selection_reason=selection.reason,
            commit_cycles=int(getattr(self._settings, "FORGE_MAX_COMMIT_CYCLES", 3) or 3),
            harness_timeout=int(
                getattr(self._settings, "FORGE_HARNESS_TIMEOUT_SECONDS", 1800) or 1800
            ),
            budget_max_calls=limits.max_calls if limits is not None else None,
            budget_max_tokens=limits.max_tokens if limits is not None else None,
            budget_wallclock_s=limits.wallclock_s if limits is not None else None,
            budget_enforcement=enforcement,
            config_status=str(config_read.provenance_status) if config_read else "",
            config_ref=config_read.ref if config_read else "",
            config_sha256=config_read.content_sha256 if config_read else "",
            profile_digest=profile_digest,
        )
        return spec.to_document()

    async def _load_executable_spec(self, run_id: str) -> ExecutableRunSpec:
        """The digest-verified executable spec for *run* (R04, ADR-0018 §1).

        The one consumption read every post-approval leg shares: it
        re-computes the canonical digest of the stored document and checks it
        against both the row and the digest the gate froze into
        ``run.spec_digest``. A missing, tampered, corrupt or legacy spec
        raises :class:`SpecInvalid` — callers park the run
        ``blocked(spec_invalid)``; there is no fallback to live settings.
        """
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            spec_digest = run.spec_digest
            row = (
                (
                    await session.execute(
                        select(RunSpec)
                        .where(RunSpec.run_id == run_id)
                        .order_by(RunSpec.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            document = row.document if row is not None else None
            row_digest = str(row.digest) if row is not None else None
            row_schema_version = int(row.schema_version) if row is not None else None
        return load_verified_spec(
            document=document,
            digest=row_digest,
            run_spec_digest=spec_digest,
            schema_version=row_schema_version,
        )

    async def _record_spec_drift(
        self, project_id: int, run_id: str, spec: ExecutableRunSpec
    ) -> None:
        """Record ``spec_drift`` evidence when the issue text moved on.

        R04 reapproval semantics: the run executes the FROZEN task text
        either way — the drift is recorded, never a blocking path (#29's
        auto-replan owns keeping issues fresh). Unavailable evidence (no
        issue, read failure) records nothing rather than guessing.
        """
        if spec.issue_iid is None:
            return
        try:
            issue = await self._gitlab.get_issue(project_id, spec.issue_iid)
        except GitLabAPIError:
            return
        live_digest = task_text_digest(issue.title, issue.description or "")
        if live_digest == spec.task_digest:
            return
        await self._merge_run_evidence(
            run_id,
            {
                "spec_drift": {
                    "frozen_task_digest": spec.task_digest,
                    "live_task_digest": live_digest,
                    "issue_iid": spec.issue_iid,
                }
            },
        )
        logger.warning(
            "Run %s: issue text drifted after approval — executing the frozen task",
            run_id[:8],
        )

    async def _open_pending_decision(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_iid: int | None,
        plan_digest: str,
        base_sha: str,
        task_digest: str,
        spec_digest: str,
        now: datetime,
    ) -> None:
        """Create the pending gate decision the moment the plan is published.

        Generation 0, no approver yet (recorded at consumption), an absolute
        deadline of ``FORGE_DECISION_TTL_SECONDS`` and the plan/task/spec
        digests frozen at plan time (F15, ADR-0018 §2).
        """
        ttl = int(
            getattr(self._settings, "FORGE_DECISION_TTL_SECONDS", 0)
            or _DECISION_TTL_FALLBACK_SECONDS
        )
        async with self._session_factory() as session:
            gate = await record_approval(
                session,
                flow_run_id=run_id,
                plan_digest=plan_digest,
                base_sha=base_sha or "",
                policy_digest=self._policy_digest(),
                approver_user_id=0,
                # Source identity: the plan publication itself (content-derived).
                source_event_id=build_source_event_id(
                    project_id, "run", issue_iid, "plan_publication", run_id
                ),
                expires_at=now + timedelta(seconds=ttl),
            )
            gate.spec_digest = spec_digest
            gate.task_digest = task_digest
            await session.commit()

    async def _publication_revoked(self, run_id: str) -> bool:
        """Whether the run's publication grant was revoked (F13, ADR-0018 §4)."""
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return True
            return bool(run.cancel_requested or run.status == FlowStatus.CANCELLED.value)

    async def _task_drift_warning(
        self, run_id: str, project_id: int, issue_iid: int | None
    ) -> str | None:
        """Evidence-comment warning when the issue text changed after approval.

        Compares the gate's task snapshot digest (plan time) with the current
        issue text; unavailable evidence (no gate, no digest, read failure)
        warns nothing rather than guessing (ADR-0018 §2).
        """
        async with self._session_factory() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval)
                        .where(GateApproval.flow_run_id == run_id)
                        .order_by(GateApproval.id.desc())
                    )
                )
                .scalars()
                .first()
            )
        approved_digest = gate.task_digest if gate is not None else None
        if not approved_digest or issue_iid is None:
            return None
        try:
            issue = await self._gitlab.get_issue(project_id, issue_iid)
        except GitLabAPIError:
            return None
        current = task_digest_of(issue.title, issue.description or "")
        if current == approved_digest:
            return None
        return "issue text changed since approval; the run executed the approved task snapshot"

    @staticmethod
    def _admission_denied_comment(run_id: str, actor: str) -> str:
        return (
            "## Forge — run not started\n\n"
            f"Run `{run_id[:8]}` was **not started**: admission denied — "
            f"@{actor} is not in the approver list (`FORGE_APPROVERS`).\n\n"
            "*This is an automated message.*"
        )

    @staticmethod
    def _fair_use_denied_comment(run_id: str, actor: str, reason: str) -> str:
        """The R28-23 refusal note — the typed fair-use reason, quoted
        verbatim so the operator (and the issue thread) can explain why
        the task was refused instead of queued."""
        return (
            "## Forge — run not started\n\n"
            f"Run `{run_id[:8]}` was **not started**: fair-use admission "
            f"refused for @{actor} — {reason}.\n\n"
            "Bounds are operator-configurable via the "
            "`FORGE_ADMISSION_*` variables.\n\n"
            "*This is an automated message.*"
        )

    def _plan_summary(self, plan: str) -> str:
        """The evidence plan summary (delegate when the planner provides one)."""
        summarizer = getattr(self._planner, "plan_summary", None)
        if callable(summarizer):
            try:
                return str(summarizer(plan))
            except Exception:  # pragma: no cover — defensive
                pass
        return plan[:PLAN_SUMMARY_CHARS]

    def _plan_files_hint(self) -> list[str]:
        getter = getattr(self._planner, "files_hint", None)
        if callable(getter):
            try:
                return [str(hint) for hint in (getter() or [])]
            except Exception:  # pragma: no cover — defensive
                return []
        return []

    @staticmethod
    def _plan_evidence(run: FlowRun) -> tuple[str, list[str]]:
        """Read plan summary + files_hint back out of the run's evidence."""
        plan = (run.evidence or {}).get("plan") or {}
        summary = str(plan.get("summary") or "")
        hints = [str(hint) for hint in (plan.get("files_hint") or [])]
        return summary, hints

    async def _read_plan_evidence(self, run_id: str) -> tuple[str, list[str]]:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            return self._plan_evidence(run)

    async def _merge_run_evidence(self, run_id: str, patch: dict) -> None:
        """Incrementally fold *patch* into flow_runs.evidence (ADR-0008)."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            run.evidence = _merge_evidence(run.evidence, patch)
            await session.commit()

    async def _read_review_evidence(self, run_id: str) -> dict | None:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            review = (run.evidence or {}).get("review")
            return dict(review) if isinstance(review, dict) else None

    async def _read_commit_cycle(self, run_id: str) -> int:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            return run.commit_cycle or 1

    async def _read_issue_iid(self, run_id: str) -> int | None:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            return run.issue_iid

    async def _fetch_git_base(
        self,
        project_id: int,
        paths: list[str],
        base_sha: str | None,
    ) -> dict[str, str]:
        """Fetch base content for the update/delete paths at the base snapshot.

        Delegates to :func:`forge.runs.backends.fetch_git_base` — the trusted
        layer's own read (ADR-0001/0006), shared with the builtin backend.
        """
        return await fetch_git_base(self._gitlab, project_id, paths, base_sha)

    def _plan_comment(
        self, run_id: str, plan: str, digest: str, harness_selection: HarnessSelection
    ) -> str:
        mention = getattr(self._settings, "FORGE_MENTION_PATTERN", "@forge")
        approvers = self._approvers()
        # Mentions must stay OUTSIDE code spans: GitLab never linkifies (or
        # notifies) @usernames inside backticks.
        approver_note = (
            ", ".join(f"@{name}" for name in approvers) or "none configured — set `FORGE_APPROVERS`"
        )
        # ADR-0023 §4: the execution shape sits between the plan body and
        # the command footer — /go authorizes it with the plan.
        implementation = implementation_block(
            harness_selection,
            model=str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
            commit_cycles=int(getattr(self._settings, "FORGE_MAX_COMMIT_CYCLES", 3) or 3),
        )
        return (
            f"## Forge plan — run `{run_id[:8]}`\n\n"
            f"{plan}\n"
            f"{implementation}\n"
            "---\n\n"
            f"**Plan digest:** `{digest}`\n\n"
            f"Approve this exact plan by commenting `{mention} /go {run_id}`.\n\n"
            f"Approvers: {approver_note}.\n\n"
            "*This is an automated message.*"
        )

    def _evidence_comment(
        self,
        mr_url: str,
        sha: str,
        pipeline,
        plan_digest: str,
        review_summary: str | None = None,
        warnings: list[str] | None = None,
        verified: bool = True,
        review_feedback: str = "",
    ) -> str:
        pipeline_url = pipeline.web_url or "(pipeline url unavailable)"
        review_line = ""
        if review_summary:
            review_line = f"- **Review:** {review_summary}\n"
        warning_lines = "".join(f"⚠️ {warning}\n" for warning in (warnings or []))
        # R02 honesty: an unverified run is labeled as such, never implied
        # green — the closing pair has one shared source (ADR-0027).
        closing = ready_closing_line(verified)
        # Q39-13 (#332): the review-feedback section names the resolved
        # and still-open discussions and the tested candidate — empty for
        # runs that never recorded feedback (the legacy bytes unchanged).
        feedback_block = f"{review_feedback}\n\n" if review_feedback else ""
        return (
            "## Forge run ready for human review\n\n"
            f"- **Merge request:** {mr_url}\n"
            f"- **Candidate commit:** `{sha}`\n"
            f"- **Pipeline:** `{pipeline.status}` — {pipeline_url}\n"
            f"{review_line}"
            f"- **Plan digest:** `{plan_digest}`\n\n"
            f"{warning_lines}"
            f"{feedback_block}"
            f"{closing}\n\n"
            "*This is an automated message.*"
        )

    async def _read_base_sha(self, project_id: int) -> str:
        """Record the pinned base (head of the target branch) at planning time.

        F28: reads the branch object (single GET) — head checks must not
        paginate the commit history.
        """
        try:
            return await self._gitlab.get_branch_head(project_id, self._target_branch())
        except GitLabAPIError:
            logger.warning("Could not read base head for project %d", project_id, exc_info=True)
            return ""

    async def _read_issue_title(self, project_id: int, run_or_iid) -> str:
        """Fetch the issue title; fall back to a neutral label on read failure."""
        issue_iid = (
            run_or_iid if isinstance(run_or_iid, int) else getattr(run_or_iid, "issue_iid", None)
        )
        if issue_iid is None:
            return "unknown issue"
        try:
            issue = await self._gitlab.get_issue(project_id, issue_iid)
            return issue.title
        except GitLabAPIError:
            logger.warning(
                "Could not read title of issue #%s — using fallback", issue_iid, exc_info=True
            )
            return f"issue {issue_iid}"

    async def _read_mr_url(self, project_id: int, mr_iid: int | None) -> str:
        if mr_iid is None:
            return "(mr unknown)"
        try:
            mr = await self._gitlab.get_merge_request(project_id, mr_iid)
            return mr.web_url or f"!{mr_iid}"
        except GitLabAPIError:
            return f"!{mr_iid}"

    @staticmethod
    def _wip_reuse_refusal_body(run_id: str, document: Mapping[str, Any]) -> str:
        """The actionable refusal for a required resume the activation routed away.

        Names the recorded reason verbatim (the rejected reuse, explained)
        and the operator's two ways out — the explicit ``restart`` discard
        or a fresh plan. Nothing was dispatched.
        """
        reason = str(
            document.get("route_reason")
            or "the activation routed the held checkpoint to a fresh attempt"
        )
        revision = str(document.get("activated_revision") or "the activated revision")
        return (
            f"## 🛑 Run `{run_id[:8]}` blocked: `checkpoint_reuse_refused`\n\n"
            f"Revision {revision} invalidated the held checkpoint's WIP:\n\n"
            f"> {reason}\n\n"
            "A `required` resume cannot silently restore WIP the approved revision "
            "rejected. Re-issue with an explicit discard (`/retry <run-id> restart`) "
            "or plan the remaining work fresh.\n\n"
            "*This is an automated message.*"
        )

    @staticmethod
    def _rebind_refusal_body(run_id: str, code: str, detail: str) -> str:
        """The actionable refusal for an approved input that failed to verify.

        The dispatch never fell back to the superseded brief — the run
        parks until the durable revision record and its digest agree
        again (a re-approval re-stages clean content).
        """
        return (
            f"## 🛑 Run `{run_id[:8]}` blocked: `rebind_refused` ({code})\n\n"
            f"> {detail}\n\n"
            "The dispatch briefs ONLY from a digest-verified active revision — "
            "it never falls back to the superseded plan. Resolve the revision "
            "record (re-approve the revision) and re-dispatch.\n\n"
            "*This is an automated message.*"
        )

    async def _post_journaled_note(
        self, project_id: int, issue_iid: int | None, body: str, run_id: str | None, kind: str
    ) -> None:
        """Post an issue note with intent/outcome journaling (ADR-0005)."""
        if issue_iid is None:
            return
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, kind, correlation_id=f"issue-{issue_iid}"
            )
            await session.commit()
        try:
            note = await self._gitlab.create_issue_note(project_id, issue_iid, body)
        except httpx.HTTPError as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        await self._complete_action(action_id, "succeeded", {"note_id": note.get("id")})

    async def _post_journaled_mr_note(
        self, project_id: int, mr_iid: int | None, body: str, run_id: str
    ) -> None:
        """Post a Draft-MR note with intent/outcome journaling (ADR-0005)."""
        if mr_iid is None:
            return
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, "post_mr_note", correlation_id=f"mr-{mr_iid}"
            )
            await session.commit()
        try:
            note = await self._gitlab.create_mr_note(project_id, mr_iid, body)
        except httpx.HTTPError as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        except GitLabAPIError as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        await self._complete_action(action_id, "succeeded", {"note_id": getattr(note, "id", None)})

    async def _complete_action(self, action_id: int, status: str, remote_result=None) -> None:
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.complete_action(action_id, status, remote_result)  # type: ignore[arg-type]
            await session.commit()

    def _native_occupancy_probe(self) -> NativeProbe:
        """The GitLab occupancy probe for :func:`reconcile_draining` (Q35-04).

        Two key shapes, both provider-prefixed so a foreign lease reads
        UNKNOWN (each provider's reconciler owns its own keys):

        - ``gitlab:pipeline:<project>:<pipeline_id>`` — the handle the
          dispatch recorded: one ``get_pipeline`` read, terminal when the
          status left the active set;
        - ``gitlab:pipeline:<project>@<branch>`` — the intent marker of a
          start whose handle never landed (lost response): pipelines on
          the run-OWNED factory branch decide it — none means the start
          never created a job (capacity returns), any pipeline terminal
          means the job finished, otherwise running. A provider error
          raises and reads UNKNOWN: uncertain occupancy holds.
        """

        async def probe(key: str) -> NativeStatus:
            if not key.startswith("gitlab:"):
                return NativeStatus.UNKNOWN  # another provider's lease
            payload = key.split(":", 1)[1]
            if not payload.startswith("pipeline:"):
                return NativeStatus.UNKNOWN
            body = payload.split(":", 1)[1]
            if "@" in body:  # the intent shape: <project>@<branch>
                project_raw, _, branch = body.partition("@")
                pipelines = await self._gitlab.list_pipelines(int(project_raw), ref=branch)
                if not pipelines:
                    return NativeStatus.TERMINAL  # nothing ever started
                statuses = {(p.status or "").lower() for p in pipelines}
                if statuses - _CI_ACTIVE_STATUSES:
                    return NativeStatus.TERMINAL
                return NativeStatus.RUNNING
            project_raw, _, pipeline_raw = body.partition(":")
            pipeline = await self._gitlab.get_pipeline(int(project_raw), int(pipeline_raw))
            return (
                NativeStatus.RUNNING
                if (pipeline.status or "").lower() in _CI_ACTIVE_STATUSES
                else NativeStatus.TERMINAL
            )

        return probe

    async def _reconcile_draining_leases(self) -> int:
        """One occupancy pass over DRAINING leases (the reconciler tick).

        The same evidence discipline as the release: a slot frees only
        when its native job is OBSERVED terminal through the provider
        probe. Undecidable keys stay draining with their age visible
        (:func:`forge.adaptive.admission.occupancy_report`).
        """
        return await reconcile_draining(self._session_factory, self._native_occupancy_probe())

    async def _release_execution_lease(self, run_id: str, reason: str) -> None:
        """Free the run's execution slot — from EVIDENCE, never local
        status alone (Q35-04; idempotent; no lease = no-op).

        A lease with NO native-start intent (the builtin lane, a
        pre-call abort) is proven never-dispatched and releases now; a
        lease carrying an intent parks DRAINING for the reconciler's
        native probe — the provider may have accepted a start whose
        response was lost, and a local terminal verdict is not evidence
        the runner stopped.
        """
        outcome = await release_lease_with_evidence(self._session_factory, run_id, reason=reason)
        if outcome.released:
            logger.info(
                "Run %s released %d execution lease(s): %s", run_id[:8], outcome.released, reason
            )
        if outcome.drained:
            logger.info(
                "Run %s parked %d execution lease(s) draining (native occupancy unobserved): %s",
                run_id[:8],
                outcome.drained,
                reason,
            )

    async def _reserve_execution_capacity(self, project_id: int, run_id: str) -> bool:
        """NEXT-11/R32-05: take the execution lease at DISPATCH — or park honestly.

        The single choke point every GitLab dispatch leg crosses (``/go``,
        the ``/retry`` revival, a repair re-dispatch, the recovery scan's
        re-drive): a durable CAS insert reserves one of the project's
        execution slots, idempotent per run, held until the terminal
        transition releases it — the same contract the Azure and GitHub
        paths enforce. Queue admission counted ACTIVE runs at
        ``/implement`` time; four approved tasks could otherwise all
        activate together. A dispatch that cannot reserve parks the run
        ``blocked(execution_capacity)`` with the capacity snapshot in its
        evidence and an issue note carrying the precise next action — a
        queued state with a reason, never work an observer must later
        stop, and never a repair-budget consumer. Returns whether the
        dispatch may run.
        """
        policy = FairUsePolicy.from_env()
        lease = await try_acquire_lease(
            policy, project_id, self._session_factory, run_id=run_id, provider="gitlab"
        )
        if lease is not None:
            await self._merge_run_evidence(
                run_id,
                {
                    "execution_lease": {
                        "check": "execution_lease",
                        "acquired": True,
                        "lease_id": lease.lease_id,
                        "slot": lease.slot,
                    }
                },
            )
            return True
        snapshot = await lease_snapshot(
            policy, project_id, self._session_factory, provider="gitlab"
        )
        await self._merge_run_evidence(
            run_id,
            {
                "execution_lease": {
                    "check": "execution_lease",
                    "acquired": False,
                    "capacity": snapshot,
                }
            },
        )
        await self._to_terminal(
            run_id,
            FlowStatus.BLOCKED,
            "execution_capacity: no execution slot free in this project — "
            "retry once the running work drains",
        )
        issue_iid = await self._read_issue_iid(run_id)
        if issue_iid is not None:
            await self._post_journaled_note(
                project_id,
                issue_iid,
                execution_capacity_comment(run_id, snapshot),
                run_id,
                "execution_capacity",
            )
        logger.warning(
            "Run %s parked blocked(execution_capacity) — %s of %s slots held",
            run_id[:8],
            snapshot.get("held"),
            snapshot.get("limit"),
        )
        return False

    async def _transition(self, run_id: str, status: FlowStatus, reason: str | None = None) -> None:
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, status, reason=reason)
            await session.commit()
        if status in TERMINAL_STATUSES:
            # NEXT-11/R32-05: the slot is held from dispatch until the
            # terminal landing — the single moment it must return.
            await self._release_execution_lease(run_id, f"terminal:{status.value}")

    async def _to_terminal(self, run_id: str, status: FlowStatus, reason: str) -> None:
        """Park the run in ``blocked``/``failed`` with an operator-facing reason.

        A ``failed`` terminalization is classified first (Tier-1 revival):
        a transient cause schedules a bounded auto-revive on the same branch,
        a fatal one parks ``blocked`` with the precise reason — a run never
        dies ``failed`` for an operator to notice; ``/retry`` walks the
        genuinely dead ones forward. The execution lease frees either way
        (NEXT-11): directly here, or through the terminal state the
        revival machinery parks the run in.
        """
        if status is FlowStatus.FAILED:
            await self._release_execution_lease(run_id, f"terminal:{status.value}")
            await terminalize_failure(
                self._session_factory, self._settings, run_id, reason=reason, log=logger
            )
            return
        await self._transition(run_id, status, reason=reason[:200])
        logger.warning("Run %s -> %s: %s", run_id[:8], status.value, reason)


def _merge_evidence(evidence: dict | None, patch: dict) -> dict:
    """Shallow-merge *patch* into the run's evidence blob."""
    merged = dict(evidence or {})
    merged.update(patch)
    return merged


def _parse_journaled_time(raw: Any) -> datetime | None:
    """Parse a journaled ISO timestamp (handle ``started_at``); None if broken."""
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _failed_job_names(jobs) -> str:
    return ", ".join(sorted({job.name for job in jobs if job.status == "failed"}))


def _finding_dict(raw: Any) -> dict:
    """Normalize one review finding (attribute or mapping) to a plain dict."""
    if isinstance(raw, dict):
        return {
            "severity": str(raw.get("severity", "info")),
            "file": str(raw.get("file", "")),
            "note": str(raw.get("note", "")),
        }
    return {
        "severity": str(getattr(raw, "severity", "info")),
        "file": str(getattr(raw, "file", "")),
        "note": str(getattr(raw, "note", "")),
    }


def _findings_from_evidence(raw: Any) -> list[dict]:
    """The findings list of a PERSISTED review (R07 replay shape)."""
    if not isinstance(raw, list):
        return []
    return [_finding_dict(entry) for entry in raw if isinstance(entry, dict)]


def _review_mr_comment(verdict: str, summary: str, findings: list[dict]) -> str:
    """The bot comment the reviewer's result earns on the Draft MR."""
    lines = [
        "## Forge readonly review",
        "",
        f"**Verdict:** {verdict}",
        "",
        summary,
    ]
    if findings:
        lines.append("")
        lines.append("**Findings:**")
        lines.extend(
            f"- `{f['file']}` ({f['severity']}): {f['note']}" for f in findings if f.get("note")
        )
    lines.append("")
    lines.append("*This is an automated message.*")
    return "\n".join(lines)
