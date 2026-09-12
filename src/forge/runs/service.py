"""RunService — the durable M1 advance loop (ADR-0004).

Owns the whole run lifecycle: ``@forge /implement`` on an issue creates a
durable FlowRun and walks it ``accepted → preflight → planning →
waiting_approval``; an approver's ``@forge /go <run-id>`` consumes the human
gate and advances ``proposing → validating → committing → ensuring_draft_mr →
waiting_ci``. The :mod:`forge.runs.reconciler` then drives ``waiting_ci → … →
ready_for_human`` by polling pipelines.

Durability rules (ADR-0005): every transition goes through
:class:`forge.durable.Controller` (which journals an outbox row atomically)
and commits before the next step, so a crash between any two steps leaves a
consistent, observable state. External writes (commit, MR, issue notes) are
journaled intent-first in ``action_log``. Unknown outcomes block or fail the
run — the service never blind-retries a write.
"""

from __future__ import annotations

import hashlib
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
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.repository import ChangesetWriter, WriteOutcome, validate_changeset
from forge.runs.stubs import StubImplementer, StubPlanner, factory_branch, plan_digest_of

logger = logging.getLogger(__name__)

#: How long a recorded gate approval stays consumable (ADR-0009 expiry).
GATE_TTL_SECONDS = 3600

#: Pipeline statuses that mean "keep waiting" in the reconciler tick.
_CI_ACTIVE_STATUSES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
)

#: ``/go <run-id>`` — full 32-hex run id as posted in the plan comment.
_GO_RE = re.compile(r"/go\s+([0-9a-fA-F]{32})\b")


def forge_token(settings: Settings) -> str:
    """The token forge acts with: the dedicated bot identity when configured.

    Forge must never speak with a human approver's credentials — its own
    comments (which contain /go instructions) would then come back as
    approver-authored webhooks and self-approve gates.
    """
    if settings.FORGE_BOT_TOKEN is not None:
        return settings.FORGE_BOT_TOKEN.get_secret_value()
    return settings.GITLAB_TOKEN.get_secret_value()


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
    """Coordinates stub agents, the controller and GitLab for one run at a time."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gitlab: GitLabClient,
        settings: Settings,
        config: ForgeConfig | None = None,
        writer_class: type[ChangesetWriter] = ChangesetWriter,
    ) -> None:
        self._session_factory = session_factory
        self._gitlab = gitlab
        self._settings = settings
        self._config = config or ForgeConfig()
        self._writer_class = writer_class

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

        plan = StubPlanner().plan(issue_title, issue_description)
        digest = plan_digest_of(plan)

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.PLANNING)
            run = await session.get(FlowRun, run_id)
            run.plan_digest = digest
            run.base_sha = await self._read_base_sha(project_id)
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
            await session.commit()

        logger.info("Gate for run %s consumed by @%s — advancing", run_id[:8], author_username)
        await self._advance_after_gate(project_id, run_id)

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
    # Post-gate pipeline: propose → validate → commit → draft MR → waiting_ci
    # ------------------------------------------------------------------

    async def _advance_after_gate(self, project_id: int, run_id: str) -> None:
        # proposing: stub implementer proposes the ChangeSet (no LLM in M1).
        async with self._session_factory() as session:
            controller = Controller(session)
            run = await session.get(FlowRun, run_id)
            changeset = StubImplementer().propose(
                run, await self._read_issue_title(project_id, run)
            )
            await controller.transition(run_id, FlowStatus.VALIDATING)
            await session.commit()

        # validating: trusted ADR-0001 validation; violations block the run.
        violations = validate_changeset(changeset)
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

        # ensuring_draft_mr: Draft MR before CI (ADR-0007).
        try:
            mr_iid = await self._create_draft_mr(project_id, run_id, changeset, commit_sha)
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

        logger.info("Run %s committed %s — waiting for CI", run_id[:8], commit_sha[:8])

    async def _create_draft_mr(
        self,
        project_id: int,
        run_id: str,
        changeset,  # ChangeSet
        commit_sha: str | None,
    ) -> int:
        """Create the Draft MR for the run branch, journaling intent/outcome."""
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, "create_merge_request", correlation_id=changeset.branch
            )
            await session.commit()

        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            plan_digest = run.plan_digest or ""
            issue_iid = run.issue_iid
        issue_title = await self._read_issue_title(project_id, issue_iid)
        description = (
            f"Draft implementation by forge run `{run_id[:8]}` for #{issue_iid}.\n\n"
            f"- **Plan digest:** `{plan_digest}`\n"
            f"- **Candidate commit:** `{commit_sha}`\n\n"
            "*Merging is a human decision — forge never merges (ADR-0003).*"
        )
        try:
            mr = await self._gitlab.create_merge_request(
                project_id,
                changeset.branch,
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

        if pipeline.status == "success":
            await self._transition(
                run_id, FlowStatus.EVALUATING_CI, reason=f"pipeline {pipeline.id} success"
            )
            # Stub readonly review: skipped in M1 — recorded in status_reason.
            await self._transition(
                run_id, FlowStatus.REVIEWING, reason="readonly review skipped (M1 stub)"
            )
            await self._transition(
                run_id,
                FlowStatus.READY_FOR_HUMAN,
                reason="checks passed; merge is a human decision",
            )
            mr_url = await self._read_mr_url(project_id, mr_iid)
            await self._post_journaled_note(
                project_id,
                issue_iid,
                self._evidence_comment(mr_url, candidate_sha, pipeline, plan_digest),
                run_id,
                "post_evidence_note",
            )
            logger.info("Run %s is ready_for_human at %s", run_id[:8], candidate_sha[:8])
            return

        # failed / canceled / skipped — CI verdict is negative.
        await self._transition(
            run_id, FlowStatus.EVALUATING_CI, reason=f"pipeline {pipeline.id} {pipeline.status}"
        )
        await self._to_terminal(
            run_id, FlowStatus.FAILED, f"ci_failed: pipeline {pipeline.id} status={pipeline.status}"
        )

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
    # Helpers
    # ------------------------------------------------------------------

    def _approvers(self) -> list[str]:
        """The trusted approver list (comma-separated FORGE_APPROVERS)."""
        raw = getattr(self._settings, "FORGE_APPROVERS", "") or ""
        return [name.strip() for name in raw.split(",") if name.strip()]

    def _target_branch(self) -> str:
        return getattr(self._settings, "FORGE_TARGET_BRANCH", "main") or "main"

    def _policy_digest(self) -> str:
        # ADR-0009: the gate binds the effective policy; in M1 that is the
        # approver list — the only trusted policy the slice consults.
        return hashlib.sha256(
            (getattr(self._settings, "FORGE_APPROVERS", "") or "").encode("utf-8")
        ).hexdigest()

    def _plan_comment(self, run_id: str, plan: str, digest: str) -> str:
        mention = getattr(self._settings, "FORGE_MENTION_PATTERN", "@forge")
        approvers = self._approvers()
        approver_note = (
            ", ".join(f"`@{name}`" for name in approvers)
            or "none configured — set `FORGE_APPROVERS`"
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

    def _evidence_comment(self, mr_url: str, sha: str, pipeline, plan_digest: str) -> str:
        pipeline_url = pipeline.web_url or "(pipeline url unavailable)"
        return (
            "## Forge run ready for human review\n\n"
            f"- **Merge request:** {mr_url}\n"
            f"- **Candidate commit:** `{sha}`\n"
            f"- **Pipeline:** `{pipeline.status}` — {pipeline_url}\n"
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
        await self._transition(run_id, status, reason=reason)
        logger.warning("Run %s -> %s: %s", run_id[:8], status.value, reason)
