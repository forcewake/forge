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
Infrastructure and config failures block the run instead of burning model
calls. Every model call lands in the ``llm_calls`` ledger (ADR-0013) and the
run accumulates evidence (plan, review, pipeline) in ``flow_runs.evidence``.

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
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.config import ForgeConfig, Settings
from forge.durable import (
    TRANSITION_EVENT_TYPE,
    Controller,
    FlowRun,
    FlowStatus,
    GateApproval,
    GateAlreadyConsumed,
    Outbox,
    as_aware_utc,
    build_source_event_id,
    consume_approval,
    is_valid,
    record_approval,
)
from forge.factory.implementer import LLMImplementer
from forge.factory.llm import LLMClient, LLMError, LLMResponseError
from forge.factory.planner import PLAN_SUMMARY_CHARS, LLMPlanner
from forge.factory.reviewer import LLMReviewer
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.repository import (
    ChangesetWriter,
    MaterializationError,
    WriteOutcome,
    validate_changeset,
)
from forge.runs.backends import (
    HarnessOutcome,
    build_backend,
    fetch_git_base,
    is_harness_backend,
)
from forge.runs.ci_contract import classify_failure, evaluate_quality_contract
from forge.runs.stubs import factory_branch, plan_digest_of

logger = logging.getLogger(__name__)

#: How long a recorded gate approval stays consumable (ADR-0009 expiry).
GATE_TTL_SECONDS = 3600

#: Pipeline statuses that mean "keep waiting" in the reconciler tick.
_CI_ACTIVE_STATUSES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
)

#: ``/go <run-id>`` — full 32-hex run id as posted in the plan comment.
_GO_RE = re.compile(r"/go\s+([0-9a-fA-F]{32})\b")

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
) -> tuple[LLMPlanner, LLMImplementer, LLMReviewer]:
    """Construct the real LLM-driven factory agents over one shared client."""
    llm = LLMClient(settings=settings, session_factory=session_factory)
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
    """
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
        run_id = uuid4().hex

        async with self._session_factory() as session:
            controller = Controller(session)
            session.add(FlowRun(id=run_id, project_id=project_id, issue_iid=issue_iid))
            await controller.transition(run_id, FlowStatus.PREFLIGHT)
            await session.commit()

        try:
            plan = await self._planner.plan(issue_title, issue_description, flow_run_id=run_id)
        except (LLMError, LLMResponseError) as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"planning_failed: {exc}")
            raise
        digest = plan_digest_of(plan)

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.PLANNING)
            run = await session.get(FlowRun, run_id)
            run.plan_digest = digest
            run.base_sha = await self._read_base_sha(project_id)
            # ADR-0015: the backend choice is frozen at run start so the run
            # survives restarts with the backend it was created with.
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "backend": self._backend_name(),
                    "plan": {
                        "digest": digest,
                        "summary": self._plan_summary(plan),
                        "files_hint": self._plan_files_hint(),
                    },
                },
            )
            await session.commit()

        await self._post_journaled_note(
            project_id,
            issue_iid,
            self._plan_comment(run_id, plan, digest),
            run_id,
            "post_plan_note",
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

        async with self._session_factory() as session:
            controller = Controller(session)
            run = await session.get(FlowRun, run_id)
            if run is None or run.project_id != project_id:
                logger.info("/go references unknown run %s — ignoring", run_id[:8])
                return
            if run.issue_iid != issue_iid:
                logger.info("/go for run %s posted on a different issue — ignoring", run_id[:8])
                return
            if run.status != FlowStatus.WAITING_APPROVAL.value:
                # Already advanced (or terminal) — duplicate /go delivery.
                logger.info(
                    "/go for run %s in status %s — ignoring duplicate", run_id[:8], run.status
                )
                return

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
                gate = await record_approval(
                    session,
                    flow_run_id=run.id,
                    plan_digest=run.plan_digest or "",
                    base_sha=run.base_sha or "",
                    policy_digest=self._policy_digest(),
                    approver_user_id=author_user_id,
                    # Source identity: the /go note itself (content-derived).
                    source_event_id=build_source_event_id(
                        project_id, "note", issue_iid, "/go", f"{author_username}:{note_text}"
                    ),
                    expires_at=now + timedelta(seconds=GATE_TTL_SECONDS),
                )
            if not is_valid(
                gate,
                now,
                plan_digest=run.plan_digest or "",
                base_sha=run.base_sha or "",
                policy_digest=self._policy_digest(),
            ):
                logger.info("Gate for run %s is expired/invalid — ignoring /go", run_id[:8])
                return
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
        repair re-entry) and bumped ``commit_cycle`` for repairs.
        """
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            plan_summary, files_hint = self._plan_evidence(run)

        issue_title = await self._read_issue_title(project_id, run)
        try:
            changeset = await self._implementer.propose(
                run,
                issue_title,
                plan_summary=plan_summary,
                files_hint=files_hint,
                repair_context=repair_context,
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

        await self._transition(run_id, FlowStatus.VALIDATING)

        # validating: trusted ADR-0001 validation; violations block the run.
        git_base = await self._fetch_git_base(
            project_id, [change.path for change in changeset.changes], run.base_sha
        )
        violations = validate_changeset(changeset, git_base)
        if violations:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "changeset_invalid: " + "; ".join(violations)
            )
            return

        await self._transition(run_id, FlowStatus.COMMITTING)

        # committing: journaled, reconcilable write (ADR-0005).
        writer = self._writer_class(self._gitlab, self._session_factory, project_id)
        try:
            result = await writer.apply(run_id, changeset, start_ref=self._target_branch())
        except GitLabAPIError as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"commit_failed: {exc}")
            return
        if result.outcome is WriteOutcome.UNKNOWN:
            # Unknown outcome: block the run, never blind-retry (ADR-0005).
            await self._to_terminal(run_id, FlowStatus.FAILED, "commit_unknown_outcome")
            return
        commit_sha = result.commit_sha

        await self._transition(run_id, FlowStatus.ENSURING_DRAFT_MR)

        # ensuring_draft_mr: Draft MR before CI (ADR-0007). On a repair the MR
        # already exists — update it instead of creating a second one.
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            mr_iid = run.mr_iid
            cycle = run.commit_cycle or 1
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
            run = await session.get(FlowRun, run_id)
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
    ) -> None:
        """ci_harness leg (ADR-0015): start the harness job, park the run.

        ``proposing`` = backend.start (ensures the factory branch, triggers
        the harness pipeline with the task brief as pipeline variables) →
        ``waiting_harness`` with the durable handle in the run's evidence.
        The reconciler (``evaluate_waiting_harness``) polls from here — the
        wait is worker-free, like ``waiting_ci``. A repair delegation appends
        the bounded CI-failure context to the brief so the harness fixes its
        own candidate.
        """
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            plan_summary, _ = self._plan_evidence(run)

        brief = plan_summary
        if repair_context:
            brief = (
                f"{plan_summary}\n\n## Repair context — previous candidate failed CI"
                f" ({repair_reason or 'code failure'})\n\n{repair_context}"
            )

        issue_title = await self._read_issue_title(project_id, run)
        try:
            backend = self._harness_backend(project_id)
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
            await controller.transition(
                run_id, FlowStatus.WAITING_HARNESS, reason=f"harness pipeline {pipeline_id}"
            )
            run = await session.get(FlowRun, run_id)
            # The durable handle: the reconciler restarts from exactly here.
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "harness": {
                        "handle": handle,
                        "pipeline_id": pipeline_id,
                        "job_id": handle_data.get("job_id"),
                        "branch": handle_data.get("branch"),
                    }
                },
            )
            await session.commit()

        logger.info(
            "Run %s delegated to harness backend (pipeline %d) — waiting_harness",
            run_id[:8],
            pipeline_id,
        )

    def _backend_name(self) -> str:
        """The configured implementer backend (ADR-0015), frozen per run."""
        raw = getattr(self._settings, "FORGE_IMPLEMENTER_BACKEND", "builtin") or "builtin"
        return str(raw).strip()

    def _harness_backend(self, project_id: int):
        """Construct the ci_harness backend for *project_id* (ADR-0015)."""
        writer = self._writer_class(self._gitlab, self._session_factory, project_id)
        return build_backend(
            self._settings,
            gitlab=self._gitlab,
            session_factory=self._session_factory,
            writer=writer,
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
            run = await session.get(FlowRun, run_id)
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
            run = await session.get(FlowRun, run_id)
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
            run = await session.get(FlowRun, run_id)
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
        try:
            commits = await self._gitlab.list_commits(project_id, branch)
        except GitLabAPIError:
            logger.exception("Drift check read failed for run %s — keeping it waiting", run_id[:8])
            return
        if commits and commits[0]["sha"] != candidate_sha:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, "external_change")
            return

        try:
            pipelines = await self._gitlab.list_pipelines(project_id, sha=candidate_sha)
        except GitLabAPIError:
            logger.exception("Pipeline read failed for run %s — keeping it waiting", run_id[:8])
            return

        if not pipelines:
            # Missing pipeline is silence from CI — never success (ADR-0007).
            if deadline is not None and as_aware_utc(now) > as_aware_utc(deadline):
                await self._to_terminal(run_id, FlowStatus.BLOCKED, "ci_timeout")
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
            ok, contract_reason = evaluate_quality_contract(pipeline, jobs, self._required_jobs())
            if not ok:
                # ADR-0008: a green icon without the required jobs is not done.
                # No LLM repair — this is CI configuration, not code.
                await self._to_terminal(
                    run_id, FlowStatus.BLOCKED, f"quality_contract: {contract_reason}"
                )
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
            )
            return

        # failed / canceled / skipped — negative verdict: classify BEFORE
        # deciding (ADR-0008), and never repair on infra/config.
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
                mr_url, candidate_sha, pipeline, plan_digest, review_summary=summary
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
            run = await session.get(FlowRun, run_id)
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
                commits = await self._gitlab.list_commits(project_id, branch)
            except GitLabAPIError:
                commits = []
            if commits:
                sections.append(
                    f"Previous commit on {branch}: {commits[0]['message']} "
                    f"({commits[0]['sha'][:8]})"
                )

        failed = [job for job in jobs if job.status == "failed"][:REPAIR_MAX_FAILED_JOBS]
        for job in failed:
            try:
                log = await self._gitlab.get_job_log(project_id, job.id)
            except GitLabAPIError:
                log = "(log unavailable)"
            sections.append(f"--- failed job: {job.name} ---\n{log[-REPAIR_LOG_PER_JOB_CHARS:]}")
        return "\n\n".join(sections)[-REPAIR_CONTEXT_MAX_CHARS:]

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
            run = await session.get(FlowRun, run_id)
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
            # Harness failures never enter the LLM repair loop (ADR-0015):
            # blocked, with the harness-level classification in the reason.
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"harness_{kind}: {outcome.reason}")
            return

        await self._adopt_harness_change(run_id, project_id, outcome)

    async def _adopt_harness_change(
        self,
        run_id: str,
        project_id: int,
        outcome: HarnessOutcome,
    ) -> None:
        """Verified harness head → committing → Draft MR → waiting_ci.

        The verified branch head SHA becomes the candidate (appended to
        ``candidate_shas``) — forge makes **no** commit of its own; the
        harness's commits are already on the branch (ADR-0015). From
        ``waiting_ci`` the existing quality-contract → review → evidence
        flow takes over, unchanged.
        """
        sha = outcome.commit_sha or ""
        await self._transition(
            run_id, FlowStatus.COMMITTING, reason=f"harness change verified {sha[:8]}"
        )
        await self._merge_run_evidence(
            run_id, {"harness_change": {"sha": sha, "summary": outcome.summary}}
        )
        await self._transition(run_id, FlowStatus.ENSURING_DRAFT_MR)

        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
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
            run = await session.get(FlowRun, run_id)
            run.mr_iid = mr_iid
            run.candidate_shas = list(run.candidate_shas or []) + [sha]
            await session.commit()

        logger.info(
            "Run %s adopted verified harness change %s — waiting for CI",
            run_id[:8],
            sha[:8],
        )

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

    def _policy_digest(self) -> str:
        # ADR-0009: the gate binds the effective policy; in M1 that is the
        # approver list — the only trusted policy the slice consults.
        return hashlib.sha256(
            (getattr(self._settings, "FORGE_APPROVERS", "") or "").encode("utf-8")
        ).hexdigest()

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
            run = await session.get(FlowRun, run_id)
            return self._plan_evidence(run)

    async def _merge_run_evidence(self, run_id: str, patch: dict) -> None:
        """Incrementally fold *patch* into flow_runs.evidence (ADR-0008)."""
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            run.evidence = _merge_evidence(run.evidence, patch)
            await session.commit()

    async def _read_review_evidence(self, run_id: str) -> dict | None:
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            review = (run.evidence or {}).get("review")
            return dict(review) if isinstance(review, dict) else None

    async def _read_commit_cycle(self, run_id: str) -> int:
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            return run.commit_cycle or 1

    async def _read_issue_iid(self, run_id: str) -> int | None:
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
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

    def _plan_comment(self, run_id: str, plan: str, digest: str) -> str:
        mention = getattr(self._settings, "FORGE_MENTION_PATTERN", "@forge")
        approvers = self._approvers()
        # Mentions must stay OUTSIDE code spans: GitLab never linkifies (or
        # notifies) @usernames inside backticks.
        approver_note = (
            ", ".join(f"@{name}" for name in approvers) or "none configured — set `FORGE_APPROVERS`"
        )
        return (
            f"## Forge plan — run `{run_id[:8]}`\n\n"
            f"{plan}\n"
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
    ) -> str:
        pipeline_url = pipeline.web_url or "(pipeline url unavailable)"
        review_line = ""
        if review_summary:
            review_line = f"- **Review:** {review_summary}\n"
        return (
            "## Forge run ready for human review\n\n"
            f"- **Merge request:** {mr_url}\n"
            f"- **Candidate commit:** `{sha}`\n"
            f"- **Pipeline:** `{pipeline.status}` — {pipeline_url}\n"
            f"{review_line}"
            f"- **Plan digest:** `{plan_digest}`\n\n"
            "All checks passed for this exact SHA. Merging is a human decision.\n\n"
            "*This is an automated message.*"
        )

    async def _read_base_sha(self, project_id: int) -> str:
        """Record the pinned base (head of the target branch) at planning time."""
        try:
            commits = await self._gitlab.list_commits(project_id, self._target_branch())
        except GitLabAPIError:
            logger.warning("Could not read base head for project %d", project_id, exc_info=True)
            return ""
        return commits[0]["sha"] if commits else ""

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
