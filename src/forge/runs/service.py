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

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.config import ForgeConfig, Settings
from forge.durable import (
    TRANSITION_EVENT_TYPE,
    ActionLog,
    Controller,
    FlowRun,
    FlowStatus,
    GateApproval,
    GateAlreadyConsumed,
    LLMCall,
    Outbox,
    RunNotFound,
    RunSpec,
    StepRun,
    as_aware_utc,
    build_source_event_id,
    consume_approval,
    is_valid,
    record_approval,
)
from forge.durable.budgets import BudgetGuard
from forge.durable.controller import TERMINAL_STATUSES
from forge.factory.implementer import LLMImplementer
from forge.factory.llm import LLMClient, LLMError, LLMResponseError
from forge.factory.planner import PLAN_SUMMARY_CHARS, LLMPlanner
from forge.factory.reviewer import LLMReviewer
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.orchestrator.project_config import ProjectConfig, load_project_config
from forge.policy.evidence import EvidencePolicy
from forge.repository import (
    ChangesetWriter,
    MaterializationError,
    WriteOutcome,
    validate_changeset,
)
from forge.runs.admission import check_admission
from forge.runs.backends import (
    HarnessOutcome,
    build_backend,
    fetch_git_base,
    is_harness_backend,
)
from forge.runs.ci_contract import classify_failure
from forge.runs.harness_selection import (
    SHIPPED_DRIVERS,
    HarnessSelection,
    advance_harness_fallback,
    compile_harness_selection,
    current_driver,
    implementation_block,
    resolve_preference,
    selection_from_spec_document,
    validate_preference,
)
from forge.runs.publisher import publish_candidate, spec_allowed_paths
from forge.runs.stubs import factory_branch, plan_digest_of
from forge.runs.verification import VerificationProfile
from forge.runs.verification import evaluate as evaluate_verification

logger = logging.getLogger(__name__)

#: How long a recorded gate approval stays consumable (ADR-0009 expiry).
GATE_TTL_SECONDS = 3600

#: ADR-0018 §1 (F14): schema version of the RunSpec document written here.
#: v2 (ADR-0023): backend_config carries the frozen harness selection —
#: ``harness``, ``harness_fallbacks``, ``budget_class``, ``selection_reason``.
RUN_SPEC_SCHEMA_VERSION = 2

#: ADR-0017 §3: pre-CI states a crashed worker leaves a run in after the gate
#: was consumed. A re-delivered or re-claimed ``/go`` command step does not
#: ignore these — it re-drives the advance leg (the recovery driver).
_RESUMABLE_ADVANCE_STATUSES = frozenset(
    {"proposing", "validating", "committing", "ensuring_draft_mr"}
)

#: Fallback when FORGE_DECISION_TTL_SECONDS is unset (ADR-0018 §2: one week).
_DECISION_TTL_FALLBACK_SECONDS = 7 * 86400


def task_digest_of(title: str, description: str) -> str:
    """sha256 over the issue text — the task snapshot digest (ADR-0018 §2).

    Bound into the RunSpec and the pending decision at plan time; a differing
    digest at READY time means the issue changed after approval, which is
    noted in the evidence comment instead of silently executed.
    """
    return hashlib.sha256(f"{title or ''}\n{description or ''}".encode("utf-8")).hexdigest()


def canonical_json_digest(document: dict) -> str:
    """sha256 over the canonical (sorted-key) JSON of *document*."""
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


#: Pipeline statuses that mean "keep waiting" in the reconciler tick.
_CI_ACTIVE_STATUSES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
)

#: ``/go <run-id>`` — full 32-hex run id as posted in the plan comment.
_GO_RE = re.compile(r"/go\s+([0-9a-fA-F]{32})\b")
_CANCEL_RE = re.compile(r"/cancel(?:\s+([0-9a-f]{8,32})\b)?", re.IGNORECASE)

#: Repair-loop log budgets (ADR-0013: bounded repair context).
REPAIR_LOG_PER_JOB_CHARS = 4000
REPAIR_CONTEXT_MAX_CHARS = 12000
REPAIR_MAX_FAILED_JOBS = 3


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
                session.add(FlowRun(id=run_id, project_id=project_id, issue_iid=issue_iid))
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

        # F22: bind the run's budget guard before the first paid call.
        await self._apply_run_budget(run_id)
        # v0.7 monorepo path scoping: the project's `.forge.yml`
        # ``implement.paths`` globs are resolved BEFORE the plan — they shape
        # the plan prompt and are frozen into the RunSpec the publisher and
        # the builtin validation enforce. A config read failure degrades to
        # unscoped (whole repo), never aborts the run.
        try:
            project_config = await load_project_config(
                self._gitlab, project_id, ref=self._target_branch()
            )
        except Exception:
            logger.warning(
                "Project config read failed for project %d — run is unscoped",
                project_id,
                exc_info=True,
            )
            project_config = ProjectConfig()
        path_scope = list(project_config.implement_paths)
        try:
            plan = await self._planner.plan(
                issue_title,
                issue_description,
                flow_run_id=run_id,
                path_scope=path_scope or None,
            )
        except (LLMError, LLMResponseError) as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"planning_failed: {exc}")
            raise
        digest = plan_digest_of(plan)
        task_digest = task_digest_of(issue_title, issue_description)
        now = datetime.now(timezone.utc)
        # ADR-0023 §2: the harness decision is compiled at plan time and
        # frozen into the RunSpec — part of what the gate approves.
        harness_selection = self._compile_harness_selection()

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.PLANNING)
            run = await self._get_run(session, run_id)
            run.plan_digest = digest
            base_sha = run.base_sha = await self._read_base_sha(project_id)
            # ADR-0015: the backend choice is frozen at run start so the run
            # survives restarts with the backend it was created with. The
            # harness selection rides beside it (ADR-0023): "backend" stays
            # the backend-name string the reconcilers dispatch on.
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "backend": self._backend_name(),
                    "harness_selection": harness_selection.as_document(),
                    "plan": {
                        "digest": digest,
                        "summary": self._plan_summary(plan),
                        "files_hint": self._plan_files_hint(),
                    },
                },
            )
            # F14 (ADR-0018 §1): freeze the immutable RunSpec at plan
            # acceptance — before the plan is published for approval.
            spec_document = self._build_run_spec_document(
                project_id=project_id,
                issue_iid=issue_iid,
                base_sha=base_sha,
                plan_digest=digest,
                task_digest=task_digest,
                allowed_paths=path_scope,
                harness_selection=harness_selection,
            )
            spec_digest = canonical_json_digest(spec_document)
            session.add(
                RunSpec(
                    run_id=run_id,
                    schema_version=RUN_SPEC_SCHEMA_VERSION,
                    document=spec_document,
                    digest=spec_digest,
                )
            )
            run.spec_digest = spec_digest
            await session.commit()

        # F22: open the run's budget from the spec (idempotent) so the
        # planning + factory legs reserve against real limits.
        from forge.durable import open_budget_from_spec

        async with self._session_factory() as session:
            await open_budget_from_spec(
                session, run_id=run_id, spec_document=spec_document, spec_digest=spec_digest
            )
            await session.commit()

        await self._post_journaled_note(
            project_id,
            issue_iid,
            self._plan_comment(run_id, plan, digest, harness_selection),
            run_id,
            "post_plan_note",
        )

        # F15 (ADR-0018 §2): the pending decision is created when the plan is
        # published — carrying the plan/task/spec digests and an absolute
        # deadline. /go consumes THIS row; it no longer creates one.
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

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.WAITING_APPROVAL)
            await session.commit()

        logger.info(
            "Run %s started for project %d issue !%s (by @%s) — waiting for /go",
            run_id[:8],
            project_id,
            issue_iid,
            author_username,
        )
        return run_id

    async def _apply_run_budget(self, run_id: str) -> None:
        """Bind the run's budget guard to the factory agents (F22).

        Agents are shared across the service instance; the budget lives in
        the database and is re-loaded per execution leg.
        """
        from forge.durable import load_budget_guard

        guard = await load_budget_guard(self._session_factory, run_id)
        for agent in (self._planner, self._implementer, self._reviewer):
            client = getattr(agent, "_llm", None)
            if client is not None and hasattr(client, "set_budget"):
                client.set_budget(guard)

    async def _find_active_run(self, project_id: int, issue_iid: int | None) -> FlowRun | None:
        """The latest non-terminal run for the issue, or None.

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
                if run is None or run.project_id != project_id or run.issue_iid != issue_iid:
                    logger.info("/cancel references unknown run %s — ignoring", requested[:8])
                    return
            else:
                # Short id (plan comments show the 8-char form): resolve by
                # prefix among the issue's runs; ambiguity means no action.
                runs = (
                    (
                        await session.execute(
                            select(FlowRun)
                            .where(
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
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            plan_summary, files_hint = self._plan_evidence(run)
            cycle = run.commit_cycle or 1
            # F02 (review): repairs build on the last VERIFIED candidate, not
            # the original approved base — otherwise cycle 2 cannot see
            # cycle 1's files and its update would roll work back. The
            # source base stays frozen for full-result review.
            # ``or ""`` mirrors _read_base_sha's failure fallback: base_sha is
            # schema-nullable, and writer.apply needs a concrete ref.
            attempt_base = (
                (run.candidate_shas or [run.base_sha])[-1] if cycle > 1 else run.base_sha
            ) or ""
            entry_status = run.status
        mid_leg = entry_status in {"validating", "committing", "ensuring_draft_mr"}

        issue_title = await self._read_issue_title(project_id, run)
        try:
            changeset = await self._implementer.propose(
                run,
                issue_title,
                plan_summary=plan_summary,
                files_hint=files_hint,
                repair_context=repair_context,
                attempt_base=attempt_base,
            )
        except MaterializationError as exc:
            # No fuzzy matching, ever (ADR-0001): an inapplicable proposal is
            # a blocked run, not a guess.
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, f"changeset_invalid: materialization: {exc}"
            )
            return
        except (LLMError, LLMResponseError) as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"proposal_failed: {exc}")
            return

        if not mid_leg:
            await self._transition(run_id, FlowStatus.VALIDATING)

        # validating: trusted ADR-0001 validation; violations block the run.
        # The run's frozen RunSpec ``allowed_paths`` scope (v0.7 monorepo
        # scoping) is enforced here too — the builtin path is the second of
        # the two write boundaries (the trusted publisher is the other).
        allowed_paths = await self._read_spec_allowed_paths(run_id)
        git_base = await self._fetch_git_base(
            project_id, [change.path for change in changeset.changes], run.base_sha
        )
        violations = validate_changeset(changeset, git_base, allowed_paths=allowed_paths)
        if violations:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "changeset_invalid: " + "; ".join(violations)
            )
            return

        if not mid_leg:
            await self._transition(run_id, FlowStatus.COMMITTING)

        # committing: journaled, reconcilable write (ADR-0005). The factory
        # branch is cut from the FROZEN attempt base (review F03) — never
        # from the live target branch — and the expected head is checked
        # before the commit (BranchDriftError on drift).
        writer = self._writer_class(self._gitlab, self._session_factory, project_id)
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
                    start_ref=attempt_base,
                    expected_head=attempt_base,
                )
            except GitLabAPIError as exc:
                await self._to_terminal(run_id, FlowStatus.FAILED, f"commit_failed: {exc}")
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
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            plan_summary, _ = self._plan_evidence(run)
            already_waiting = run.status == FlowStatus.WAITING_HARNESS.value

        brief = plan_summary
        if repair_context:
            brief = (
                f"{plan_summary}\n\n## Repair context — previous candidate failed CI"
                f" ({repair_reason or 'code failure'})\n\n{repair_context}"
            )

        issue_title = await self._read_issue_title(project_id, run)
        if driver is None:
            driver = await self._frozen_harness_driver(run_id)
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

        logger.info(
            "Run %s delegated to harness backend (pipeline %d, driver %s) — waiting_harness",
            run_id[:8],
            pipeline_id,
            driver or "configured",
        )

    async def _frozen_harness_driver(self, run_id: str) -> str | None:
        """The driver frozen at plan time (ADR-0023 §6), or None for a
        pre-v2 RunSpec — None keeps the configured-backend default."""
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
        return selection.harness if selection is not None else None

    def _backend_name(self) -> str:
        """The configured implementer backend (ADR-0015), frozen per run."""
        raw = getattr(self._settings, "FORGE_IMPLEMENTER_BACKEND", "builtin") or "builtin"
        return str(raw).strip()

    def _harness_backend(self, project_id: int, *, driver: str | None = None):
        """Construct the ci_harness backend for *project_id* (ADR-0015).

        *driver* (ADR-0023) pins the leg to the RunSpec's frozen selection.
        """
        writer = self._writer_class(self._gitlab, self._session_factory, project_id)
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
        """One reconciler pass over every run parked in ``waiting_ci``."""
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            run_ids = (
                (
                    await session.execute(
                        select(FlowRun.id).where(FlowRun.status == FlowStatus.WAITING_CI.value)
                    )
                )
                .scalars()
                .all()
            )
        for run_id in run_ids:
            try:
                await self._evaluate_one(run_id, now)
            except Exception:
                # One broken run must not stall the reconciler loop.
                logger.exception("Reconcile pass failed for run %s", run_id[:8])

    async def _evaluate_one(self, run_id: str, now: datetime) -> None:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            project_id = run.project_id
            issue_iid = run.issue_iid
            candidate_shas = list(run.candidate_shas or [])
            mr_iid = run.mr_iid
            plan_digest = run.plan_digest or ""
            base_sha = run.base_sha or ""
            backend_name = str((run.evidence or {}).get("backend") or "").strip()
            deadline = await self._waiting_ci_deadline(session, run_id)

        candidate_sha = candidate_shas[-1] if candidate_shas else None
        if not candidate_sha:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, "waiting_ci without candidate sha")
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

        # Durable CI deadline (ADR-0005): whatever CI reports — silence, an
        # API failure, or a pipeline stuck forever in an active state — a run
        # past its deadline is parked as blocked(ci_timeout).
        if deadline is not None and as_aware_utc(now) > as_aware_utc(deadline):
            await self._to_terminal(run_id, FlowStatus.BLOCKED, "ci_timeout")
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

        await self._transition(
            run_id, FlowStatus.EVALUATING_CI, reason=f"pipeline {pipeline.id} {pipeline.status}"
        )
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
            # "verified" means. An empty profile lets the run proceed, but
            # the evidence comment must carry the warning.
            profile = VerificationProfile.from_settings(self._settings)
            ok, contract_reason = evaluate_verification(pipeline, jobs, profile)
            if not ok:
                # ADR-0008: a green icon without the required jobs is not done.
                # No LLM repair — this is CI configuration, not code.
                await self._to_terminal(
                    run_id, FlowStatus.BLOCKED, f"quality_contract: {contract_reason}"
                )
                return
            verification_warnings: list[str] = []
            if not profile.required_jobs:
                verification_warnings.append(
                    "No verification profile configured — pipeline success only."
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
                verification_warnings=verification_warnings,
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
        max_cycles = int(getattr(self._settings, "FORGE_MAX_COMMIT_CYCLES", 3) or 3)
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
        verification_warnings: list[str] | None = None,
    ) -> None:
        """checks passed → reviewing → ready_for_human (ADR-0008 review leg)."""
        await self._transition(run_id, FlowStatus.REVIEWING, reason="readonly review of candidate")

        plan_summary, _ = await self._read_plan_evidence(run_id)
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

        # Self-check: the recorded review must be bound to the candidate sha.
        stored = await self._read_review_evidence(run_id)
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

        reason = (
            "checks passed; review raised concerns — merge is a human decision"
            if verdict == "concerns"
            else "checks passed; merge is a human decision"
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
    # Reconciler tick: waiting_harness → … (ADR-0015)
    # ------------------------------------------------------------------

    async def evaluate_waiting_harness(self, now: datetime | None = None) -> None:
        """One reconciler pass over every run parked in ``waiting_harness``."""
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            run_ids = (
                (
                    await session.execute(
                        select(FlowRun.id).where(FlowRun.status == FlowStatus.WAITING_HARNESS.value)
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
        """Poll one waiting_harness run through its journaled backend handle."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            evidence = dict(run.evidence or {})
            project_id = run.project_id

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

        # GitHub-subject runs are polled by the GitHub harness reconciler
        # (runs/github_service.py) — the GitLab CI backend cannot read an
        # Actions handle (its pipeline_id contract does not apply).
        if getattr(run, "provider", "gitlab") == "github" or '"provider": "github"' in (
            handle or ""
        ):
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
            return

        await self._adopt_harness_change(run_id, project_id, outcome)

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

        TODO(F22): cancel + re-reserve the run budget per switch once
        harness legs reserve at dispatch — today harness runs make no
        forge-side model calls and only reconcile receipts
        (``_record_harness_usage``), so there is no reservation to move.
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
        # F13 (ADR-0018 §4): a candidate for a cancelled run is superseded —
        # recorded as evidence only; it can never become a commit/MR because
        # the publication grant is gone. The run stays cancelled even if the
        # harness could not be stopped.
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            revoked = bool(run.cancel_requested or run.status == FlowStatus.CANCELLED.value)
        if revoked:
            await self._merge_run_evidence(
                run_id,
                {
                    "superseded": {
                        "reason": "cancelled",
                        "attempt_base": bundle.attempt_base_oid,
                    }
                },
            )
            logger.info(
                "Run %s cancelled — harness candidate on %s recorded as superseded",
                run_id[:8],
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

        writer = self._writer_class(self._gitlab, self._session_factory, project_id)
        result = await publish_candidate(
            gitlab=self._gitlab,
            session_factory=self._session_factory,
            writer=writer,
            run=run,
            bundle=bundle,
            fence_check=_fence_valid,
        )
        if not result.ok:
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
        """F22 lite: one ``llm_calls`` row per published harness candidate.

        The receipt comes from the parsed event stream (candidate.meta.json);
        unknown counts stay NULL — never zero, never fabricated.
        """
        usage = bundle.usage
        async with self._session_factory() as session:
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
            await session.commit()

    async def evaluate_ready_evidence(self) -> None:
        """Recover runs already READY whose evidence note never got posted.

        Crash window (ADR-0017 §5): the ``ready_for_human`` transition
        committed but the process died before the journaled evidence note even
        started — no ``post_evidence_note`` action row exists. A note whose
        posting DID begin has a journal row and is left alone: its outcome is
        the journal's to answer, never a blind re-post (ADR-0005).
        """
        async with self._session_factory() as session:
            runs = (
                (
                    await session.execute(
                        select(FlowRun).where(FlowRun.status == FlowStatus.READY_FOR_HUMAN.value)
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
        silently repaired. v0.9: the compilable lanes are the shipped driver
        set — credential presence is declared by the preference and
        doctor-verified (ADR-0011); a lane without creds fails
        infrastructure at dispatch, which with the fallback switch OFF (the
        default) blocks the run visibly.

        TODO(ADR-0023 §5): pass the planner's structured proposal
        ({"harness", "budget_class", "reason"}) from LLMPlanner.plan's
        output once that surface exists — the integration point is the
        ``plan`` call in start_run (factory/planner.py returns plain
        markdown today and is outside this change's scope). None keeps the
        compiler defaults.
        """
        preference = resolve_preference(self._config, self._settings)
        backend = self._backend_name()
        validate_preference(
            preference, current_driver(backend) if is_harness_backend(backend) else None
        )
        return compile_harness_selection(
            preference,
            backend,
            set(SHIPPED_DRIVERS),
            None,
        )

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
        plan_digest: str,
        task_digest: str,
        allowed_paths: list[str] | None = None,
        harness_selection: HarnessSelection | None = None,
    ) -> dict:
        """The immutable RunSpec document frozen at plan acceptance (F14).

        ``allowed_paths`` (v0.7 monorepo scoping) is present only for scoped
        runs — an unscoped project's document is byte-identical to the
        pre-v0.7 shape, so its digest is unchanged.

        ADR-0023 §3: ``backend_config`` also freezes the harness decision
        (selected driver, fallback tail, budget class, selection reason).
        """
        selection = harness_selection or self._compile_harness_selection()
        document: dict = {
            "subject": {"project_id": project_id, "issue_iid": issue_iid},
            "source_base_oid": base_sha or "",
            "plan_digest": plan_digest,
            "task_digest": task_digest,
            "policy_digest": self._policy_digest(),
            # ADR-0018: the resolved backend config travels with the spec —
            # live Settings may not silently change an approved run's
            # execution. ADR-0023: the frozen harness chain rides beside it.
            "backend_config": {
                "backend": self._backend_name(),
                "model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
                "target_branch": self._target_branch(),
                **selection.as_document(),
            },
            "budgets": {
                "commit_cycles": int(getattr(self._settings, "FORGE_MAX_COMMIT_CYCLES", 3) or 3),
                "harness_timeout": int(
                    getattr(self._settings, "FORGE_HARNESS_TIMEOUT_SECONDS", 1800) or 1800
                ),
            },
        }
        if allowed_paths:
            document["allowed_paths"] = [str(glob) for glob in allowed_paths]
        return document

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

    async def _read_spec_allowed_paths(self, run_id: str) -> list[str]:
        """The RunSpec's frozen ``allowed_paths`` globs ([] when unscoped)."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            return await spec_allowed_paths(session, run)

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
    ) -> str:
        pipeline_url = pipeline.web_url or "(pipeline url unavailable)"
        review_line = ""
        if review_summary:
            review_line = f"- **Review:** {review_summary}\n"
        warning_lines = "".join(f"⚠️ {warning}\n" for warning in (warnings or []))
        return (
            "## Forge run ready for human review\n\n"
            f"- **Merge request:** {mr_url}\n"
            f"- **Candidate commit:** `{sha}`\n"
            f"- **Pipeline:** `{pipeline.status}` — {pipeline_url}\n"
            f"{review_line}"
            f"- **Plan digest:** `{plan_digest}`\n\n"
            f"{warning_lines}"
            "All checks passed for this exact SHA. Merging is a human decision.\n\n"
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
        self, project_id: int, issue_iid: int | None, body: str, run_id: str, kind: str
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
        """Park the run in ``blocked``/``failed`` with an operator-facing reason."""
        await self._transition(run_id, status, reason=reason[:200])
        logger.warning("Run %s -> %s: %s", run_id[:8], status.value, reason)


def _merge_evidence(evidence: dict | None, patch: dict) -> dict:
    """Shallow-merge *patch* into the run's evidence blob."""
    merged = dict(evidence or {})
    merged.update(patch)
    return merged


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
