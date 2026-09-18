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

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    Outbox,
    PublicationIntent,
    RunNotFound,
    RunSpec,
    SettleDecision,
    StepRun,
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
    build_retry_context,
    collect_status_snapshot,
    evaluate_config_blocks,
    evaluate_revivals,
    format_reconcile_reply,
    format_status_reply,
    has_active_run,
    intents_for_run,
    resolve_retry_target,
    resolve_status_target,
    retry_rejection,
    terminalize_failure,
    why_blocked_reply,
)
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

#: ``/go <run-id>`` — full 32-hex run id as posted in the plan comment.
_GO_RE = re.compile(r"/go\s+([0-9a-fA-F]{32})\b")
_CANCEL_RE = re.compile(r"/cancel(?:\s+([0-9a-f]{8,32})\b)?", re.IGNORECASE)
#: ``/retry [run-id]`` — bare retries the issue's latest failed/blocked run.
_RETRY_RE = re.compile(r"/retry(?:\s+([0-9a-f]{8,32})\b)?", re.IGNORECASE)

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
    ) -> None:
        self._session_factory = session_factory
        self._gitlab = gitlab
        self._settings = settings
        self._config = config or ForgeConfig()
        self._writer_class = writer_class
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

        try:
            async with self._session_factory() as session:
                controller = Controller(session)
                session.add(
                    FlowRun(
                        id=run_id,
                        project_id=project_id,
                        issue_iid=issue_iid,
                        provider="gitlab",
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

    async def handle_retry_note(
        self,
        project_id: int,
        note_text: str,
        author_username: str,
        issue_iid: int | None,
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
        run_id: str | None = None
        # A07: the dispatch legs below aim at the VERIFIED run's subject, read
        # back from the matched run — never the command context. The scoped
        # resolution makes the two equal; reading them from the run keeps the
        # dispatch honest even if resolution were ever widened.
        retry_project_id = 0
        retry_issue_iid: int | None = None
        rejection = ""
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
                rejection = retry_rejection(
                    run,
                    other_active=await has_active_run(
                        session,
                        provider=run.provider,
                        project_id=run.project_id,
                        issue_iid=run.issue_iid,
                        repo_full_name=run.github_repo_full_name,
                        exclude_run_id=run.id,
                    ),
                )
                if not rejection:
                    status = run.status
                    status_reason = run.status_reason or ""
                    cycle = run.commit_cycle or 1
                    evidence = dict(run.evidence or {})
        if run_id is None:
            logger.info("/retry on issue !%s — no retryable run", issue_iid)
            return
        if rejection:
            await self._post_journaled_note(
                project_id, issue_iid, f"🔁 {rejection}", run_id, "retry_rejected_note"
            )
            return

        # Intent-first journal (ADR-0005), then the durable walk. The revived
        # run re-enters ``proposing`` with one more cycle; the backend frozen
        # in the evidence decides the advance leg (ADR-0015).
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, "retry_requested", correlation_id=f"issue-{issue_iid}"
            )
            await controller.revive_transition(
                run_id,
                reason=f"retry requested by @{author_username}",
                authorized_by=f"operator:@{author_username}",
            )
            run = await self._get_run(session, run_id)
            run.commit_cycle = cycle + 1
            await session.commit()

        branch = factory_branch(retry_issue_iid, run_id)
        logger.info(
            "Run %s retried by @%s — re-dispatching %s (cycle %d)",
            run_id[:8],
            author_username,
            branch,
            cycle + 1,
        )
        await self._post_journaled_note(
            project_id,
            issue_iid,
            f"## 🔁 Run `{run_id[:8]}` retried by @{author_username}\n\n"
            f"- Branch: `{branch}` — the work continues in place, no re-planning\n"
            f"- Commit cycle: {cycle + 1}\n\n*This is an automated message.*",
            run_id,
            "retry_ack_note",
        )

        repair_context = build_retry_context(self._settings, status_reason, evidence)
        repair_reason = f"retry by @{author_username}: {status_reason or status}"
        backend_name = str(evidence.get("backend") or "").strip() or self._backend_name()
        try:
            if is_harness_backend(backend_name):
                await self._advance_harness(
                    retry_project_id,
                    run_id,
                    repair_context=repair_context,
                    repair_reason=repair_reason,
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
                body = why_blocked_reply(run, other_active=other_active)
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
    ) -> None:
        """``@forge /go <run-id>``: validate + consume the gate, then advance.

        Idempotent: a re-delivered note finds the run already out of
        ``waiting_approval`` or the gate already consumed — both ignore.
        """
        match = _GO_RE.search(note_text or "")
        if match is None:
            return
        run_id = match.group(1).lower()
        now = now or datetime.now(timezone.utc)
        resuming = False

        async with self._session_factory() as session:
            controller = Controller(session)
            run = await session.get(FlowRun, run_id)
            if run is None or run.project_id != project_id:
                logger.info("/go references unknown run %s — ignoring", run_id[:8])
                return
            if run.issue_iid != issue_iid:
                logger.info("/go for run %s posted on a different issue — ignoring", run_id[:8])
                return
            if run.status in _RESUMABLE_ADVANCE_STATUSES:
                # ADR-0017 §3: the gate is consumed and a crashed worker left
                # the run mid-advance; the re-claimed command step is the
                # recovery driver. The leg below looks for the
                # already-existing effects before creating new ones.
                resuming = True
            elif run.status != FlowStatus.WAITING_APPROVAL.value:
                # Already advanced (or terminal) — duplicate /go delivery.
                logger.info(
                    "/go for run %s in status %s — ignoring duplicate", run_id[:8], run.status
                )
                return

            if not resuming:
                # ADR-0009: authority comes from trusted configuration, not authorship.
                if author_username not in self._approvers():
                    logger.info(
                        "/go from @%s who is not in FORGE_APPROVERS — ignoring", author_username
                    )
                    return

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
                    # F15 (ADR-0018 §2): decisions are created at plan publication —
                    # a /go without a pending decision has nothing to consume.
                    logger.info("No pending decision for run %s — ignoring /go", run_id[:8])
                    return
                if not is_valid(
                    gate,
                    now,
                    plan_digest=run.plan_digest or "",
                    base_sha=run.base_sha or "",
                    policy_digest=self._policy_digest(),
                    spec_digest=run.spec_digest,
                ):
                    logger.info("Decision for run %s is expired/invalid — ignoring /go", run_id[:8])
                    return
                # The decision was opened anonymously at plan publication; record
                # who consumed it.
                gate.approver_user_id = author_user_id
                try:
                    await consume_approval(session, gate.id, now)
                except GateAlreadyConsumed:
                    logger.info("Gate for run %s already consumed — ignoring", run_id[:8])
                    return

                # R04 (ADR-0018 §1): consuming the gate binds the run to the
                # executable RunSpec the pending decision froze — the advance
                # legs below execute ONLY its digest-verified content (frozen
                # task text, plan, model route, verification contract,
                # budgets). A spec that no longer matches this digest is
                # blocked(spec_invalid), never re-read from live settings.
                await controller.transition(
                    run.id, FlowStatus.PROPOSING, reason=f"approved by @{author_username}"
                )
                # ADR-0015: the backend frozen at run start decides the advance leg.
                backend_name = (
                    str((run.evidence or {}).get("backend") or "").strip() or self._backend_name()
                )
                await session.commit()

        if resuming:
            backend_name = str((run.evidence or {}).get("backend") or "").strip() or (
                self._backend_name()
            )
            logger.info(
                "Run %s found %s after a worker crash — resuming the advance leg",
                run_id[:8],
                run.status,
            )
            if is_harness_backend(backend_name):
                await self._advance_harness(project_id, run_id)
            else:
                await self._advance_proposal(project_id, run_id)
            return

        logger.info("Gate for run %s consumed by @%s — advancing", run_id[:8], author_username)
        if is_harness_backend(backend_name):
            await self._advance_harness(project_id, run_id)
        else:
            await self._advance_proposal(project_id, run_id)

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
        """
        # R13: an exhausted budget (or a spent wall clock) starts no new
        # harness episode — the dispatch is the only enforcement point a
        # non-intercepted lane has, so it is checked before any I/O.
        block = await self._budget_episode_block(run_id)
        if block is not None:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, block)
            return
        # R04: the brief, the task title and the dispatched driver come from
        # the digest-verified executable spec — never live settings and never
        # a live issue re-read. A missing/tampered spec blocks the run.
        try:
            spec = await self._load_executable_spec(run_id)
        except SpecInvalid as exc:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"spec_invalid: {exc}")
            return
        await self._record_spec_drift(project_id, run_id, spec)

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            already_waiting = run.status == FlowStatus.WAITING_HARNESS.value

        plan_summary = spec.plan_summary
        brief = plan_summary
        if repair_context:
            brief = (
                f"{plan_summary}\n\n## Repair context — previous candidate failed CI"
                f" ({repair_reason or 'code failure'})\n\n{repair_context}"
            )

        issue_title = spec.task_title
        if driver is None:
            driver = spec.harness_driver
        try:
            backend = self._harness_backend(project_id, driver=driver)
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
            handle = await backend.start(run, issue_title, "", brief)
        except (GitLabAPIError, httpx.HTTPError) as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            await self._to_terminal(run_id, FlowStatus.FAILED, f"harness_start_failed: {exc}")
            return

        handle_data = json.loads(handle)
        pipeline_id = int(handle_data.get("pipeline_id") or 0)

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
                    }
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

    def _harness_backend(self, project_id: int, *, driver: str | None = None):
        """Construct the ci_harness backend for *project_id* (ADR-0015).

        *driver* (ADR-0023) pins the leg to the RunSpec's frozen selection.
        """
        writer = self._writer_class(
            self._gitlab,
            self._session_factory,
            project_id,
            settle_seconds=self._publish_settle_seconds(),
        )
        return build_backend(
            self._settings,
            gitlab=self._gitlab,
            session_factory=self._session_factory,
            writer=writer,
            driver=driver,
        )

    async def _create_draft_mr(
        self,
        project_id: int,
        run_id: str,
        branch: str,
        commit_sha: str | None,
    ) -> int:
        """Create the Draft MR for the run branch, journaling intent/outcome."""
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, "create_merge_request", correlation_id=branch
            )
            await session.commit()

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            plan_digest = run.plan_digest or ""
            issue_iid = run.issue_iid
        issue_title = await self._read_issue_title(project_id, issue_iid)
        description = self._mr_description(run_id, plan_digest, issue_iid, commit_sha)
        try:
            mr = await self._gitlab.create_merge_request(
                project_id,
                branch,
                self._target_branch(),
                f"Draft: {issue_title}",  # Draft: prefix marks it draft (GitLab convention)
                description,
            )
        except httpx.HTTPError:
            await self._complete_action(action_id, "unknown_outcome")
            raise
        except GitLabAPIError as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise

        await self._complete_action(
            action_id,
            "succeeded",
            {"mr_iid": mr.get("iid"), "web_url": mr.get("web_url")},
        )
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
                # never ran, and the run parks visibly blocked(budget_exhausted).
                if str(exc) == BUDGET_EXHAUSTED:
                    await self._to_terminal(
                        run_id,
                        FlowStatus.BLOCKED,
                        f"{BUDGET_EXHAUSTED}: reviewer refused — run budget cannot grant a call",
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
            ),
            run_id,
            "post_evidence_note",
        )
        logger.info("Run %s is ready_for_human at %s", run_id[:8], candidate_sha[:8])

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
        """Re-dispatch a revived run — same branch, attempt base = last candidate."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            project_id = run.project_id
            backend_name = (
                str((run.evidence or {}).get("backend") or "").strip() or self._backend_name()
            )
        if is_harness_backend(backend_name):
            await self._advance_harness(project_id, run_id)
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
        available = resolve_available_drivers(self._config, self._settings) or set(SHIPPED_DRIVERS)
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
        """
        selection = harness_selection or self._compile_harness_selection()
        backend = self._backend_name()
        # R13: the budget class's numeric profile is resolved AT FREEZE TIME
        # and stored IN the spec — the gate approves exactly these ceilings
        # and the honest enforcement level of this lane. ``None`` (no finite
        # profile) freezes no ceiling fields at all (byte-compatible).
        limits = self._budget_limits_for_class(selection.budget_class)
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
    ) -> str:
        pipeline_url = pipeline.web_url or "(pipeline url unavailable)"
        review_line = ""
        if review_summary:
            review_line = f"- **Review:** {review_summary}\n"
        warning_lines = "".join(f"⚠️ {warning}\n" for warning in (warnings or []))
        # R02 honesty: an unverified run is labeled as such, never implied
        # green — the closing pair has one shared source (ADR-0027).
        closing = ready_closing_line(verified)
        return (
            "## Forge run ready for human review\n\n"
            f"- **Merge request:** {mr_url}\n"
            f"- **Candidate commit:** `{sha}`\n"
            f"- **Pipeline:** `{pipeline.status}` — {pipeline_url}\n"
            f"{review_line}"
            f"- **Plan digest:** `{plan_digest}`\n\n"
            f"{warning_lines}"
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

    async def _transition(self, run_id: str, status: FlowStatus, reason: str | None = None) -> None:
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, status, reason=reason)
            await session.commit()

    async def _to_terminal(self, run_id: str, status: FlowStatus, reason: str) -> None:
        """Park the run in ``blocked``/``failed`` with an operator-facing reason.

        A ``failed`` terminalization is classified first (Tier-1 revival):
        a transient cause schedules a bounded auto-revive on the same branch,
        a fatal one parks ``blocked`` with the precise reason — a run never
        dies ``failed`` for an operator to notice; ``/retry`` walks the
        genuinely dead ones forward.
        """
        if status is FlowStatus.FAILED:
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
