"""GitHubRunService — the E3a GitHub path to gate parity (ADR-0019).

Mirrors the GitLab gate machinery (:mod:`forge.runs.service`) onto FlowRun
rows with ``provider='github'``: an ``/implement`` on a GitHub issue creates
a durable run, plans, posts the PLAN as an issue comment, freezes the
RunSpec, opens the pending decision (plan/base/spec/task digests +
``FORGE_DECISION_TTL_SECONDS``) and parks the run in ``waiting_approval``.
An approver's ``/go <run-id>`` consumes the decision exactly once and drives
the publish leg: CAS commit + Draft PR via the existing
:class:`~forge.integrations.github_flow.GitHubPublishFlow`, evidence
comment, then the readonly PR-diff review → ``ready_for_human``. ``/cancel``
mirrors GitLab cancel-as-revoke semantics (F13).

GitHub-specific deviations, all deliberate:

- **Admission/approvers are GitHub logins** — ``FORGE_GITHUB_APPROVERS``
  when set, else the shared ``FORGE_APPROVERS`` fallback (never merged):
  a GitLab username in the shared list can never approve a GitHub run.
- **One active run per (repo, issue)** reuses the existing partial unique
  index ``uq_active_run_per_issue`` over ``flow_runs.(project_id,
  issue_iid)`` — for GitHub those columns carry the webhook's numeric
  repository id and the issue number.
- **Actions harness lane (E3b, ADR-0020)**: when
  ``FORGE_GITHUB_HARNESS_WORKFLOW`` names the harness workflow human-applied
  to the target repo, an approved /go dispatches it (run_id / attempt_base /
  driver / model inputs on the factory branch) and parks the run in
  ``waiting_harness`` with a journaled :class:`ActionsHandle`; the
  reconciler (``evaluate_waiting_harness``) polls the Actions run, adopts
  the candidate artifact through the SAME trusted publisher (branch CAS
  commit + Draft PR) and walks on to ``waiting_ci`` → review →
  ``ready_for_human``. Empty/unset → builtin in-worker execution as before.
  Actions checks on the head are the verification surface.
- **Expired or drifted decisions block** the run with a friendly comment
  (``decision_expired`` / ``decision_drift``) instead of silently ignoring
  the /go: on GitHub the comment thread is the only operator surface.

Durability rules are unchanged (ADR-0005): every transition goes through
:class:`forge.durable.Controller`; external writes (comments, commits, PRs)
are journaled intent-first in ``action_log``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from forge.config import ForgeConfig, Settings
from forge.durable import (
    Controller,
    FlowRun,
    FlowStatus,
    GateAlreadyConsumed,
    GateApproval,
    RunNotFound,
    RunSpec,
    StepRun,
    as_aware_utc,
    build_source_event_id,
    consume_approval,
    is_valid,
    record_approval,
    short_run_id,
)
from forge.durable.controller import TERMINAL_STATUSES
from forge.execution.github_actions import ActionsHandle, GitHubActionsExecutor
from forge.factory.llm import LLMError, LLMResponseError
from forge.factory.planner import PLAN_SUMMARY_CHARS
from forge.integrations.github_flow import (
    GitHubAgents,
    build_github_agents,
    github_factory_branch,
)
from forge.orchestrator.project_config import ProjectConfig, load_project_config
from forge.repository import Change, ChangeSet, Operation, validate_changeset
from forge.runs.admission import approvers_for, check_admission
from forge.runs.backends import HARNESS_NAME, HarnessOutcome, is_harness_backend
from forge.runs.candidate import attempt_base_for
from forge.runs.harness_selection import (
    SHIPPED_DRIVERS,
    HarnessSelection,
    advance_harness_fallback,
    compile_harness_selection,
    implementation_block,
    resolve_preference,
    selection_from_spec_document,
    validate_preference,
)
from forge.runs.failure import (
    FailureClass,
    RESUMABLE_ADVANCE_STATUSES,
    arm_revive,
    classify_terminal_failure,
    revive_at,
    revive_count,
    revive_evidence,
)
from forge.runs.publisher import spec_allowed_paths
from forge.runs.service import (
    RUN_SPEC_SCHEMA_VERSION,
    _CANCEL_RE,
    _DECISION_TTL_FALLBACK_SECONDS,
    _GO_RE,
    _RETRY_RE,
    _TERMINAL_STATUS_VALUES,
    canonical_json_digest,
    plan_digest_of,
    task_digest_of,
)

logger = logging.getLogger(__name__)

#: The evidence-comment note while E3b (Actions executor) is not wired.
_VERIFICATION_NOTE = (
    "Actions checks on the head are the verification surface — no required checks are enforced yet."
)


class GitHubRunService:
    """Coordinates the GitHub agents, the controller and one run's lifecycle."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        config: ForgeConfig | None = None,
        *,
        stack: GitHubAgents,
        repo_full_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._config = config or ForgeConfig()
        self._stack = stack
        self._repo_full_name = repo_full_name

    @property
    def _owner(self) -> str:
        return self._repo_full_name.split("/", 1)[0]

    @property
    def _repo(self) -> str:
        return self._repo_full_name.split("/", 1)[1]

    # ------------------------------------------------------------------
    # Commands (dispatched from execute_github_run_command)
    # ------------------------------------------------------------------

    async def start_run(
        self,
        *,
        project_id: int,
        issue_number: int,
        issue_title: str,
        issue_description: str,
        author_username: str,
    ) -> str:
        """``/implement``: create the run, plan, park it at the human gate."""
        # One active run per (repo, issue): the partial unique index is the
        # invariant of last resort; this pre-check produces the friendly
        # refusal comment instead of a raw IntegrityError.
        active = await self._find_active_run(project_id, issue_number)
        if active is not None:
            await self._post_journaled_note(
                project_id,
                issue_number,
                self._active_run_comment(active),
                active.id,
                "duplicate_implement",
            )
            logger.info(
                "GitHub /implement on %s#%s ignored — run %s is already %s",
                self._repo_full_name,
                issue_number,
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
                        issue_iid=issue_number,
                        provider="github",
                        github_repo_full_name=self._repo_full_name,
                        github_issue_number=issue_number,
                    )
                )
                await controller.transition(run_id, FlowStatus.PREFLIGHT)
                await session.commit()
        except IntegrityError:
            # F12 (ADR-0017): a concurrent /implement won the (repo, issue)
            # slot between the active-run check and this insert — adopt it.
            existing = await self._find_active_run(project_id, issue_number)
            if existing is None:
                raise
            await self._post_journaled_note(
                project_id,
                issue_number,
                self._active_run_comment(existing),
                existing.id,
                "duplicate_implement",
            )
            logger.info(
                "GitHub /implement on %s#%s lost the creation race — run %s is active",
                self._repo_full_name,
                issue_number,
                existing.id[:8],
            )
            return existing.id

        # ADR-0018 §3: admission before the first paid call. The GitHub
        # connection's own approver list (FORGE_GITHUB_APPROVERS, falling
        # back to FORGE_APPROVERS) carries the GitHub logins on this path.
        admission = check_admission(
            self._settings, self._config, project_id, author_username, provider="github"
        )
        if not admission.allowed:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, f"admission_denied: {admission.reason}"
            )
            await self._post_journaled_note(
                project_id,
                issue_number,
                _admission_denied_comment(run_id, author_username),
                run_id,
                "admission_denied",
            )
            logger.warning(
                "GitHub run %s denied admission for @%s: %s",
                run_id[:8],
                author_username,
                admission.reason,
            )
            return run_id

        # v0.7 monorepo path scoping: the repo's `.forge.yml`
        # ``implement.paths`` globs shape the plan prompt and are frozen into
        # the RunSpec the candidate validation enforces. The repository
        # reader duck-types the config loader's ``get_file`` surface (its
        # ``project_id`` argument is accepted and ignored). A read failure
        # degrades to unscoped, never aborts the run.
        try:
            project_config = await load_project_config(
                self._stack.reader, project_id, ref=self._target_branch()
            )
        except Exception:
            logger.warning(
                "Project config read failed for %s — run is unscoped",
                self._repo_full_name,
                exc_info=True,
            )
            project_config = ProjectConfig()
        path_scope = list(project_config.implement_paths)
        try:
            plan = await self._stack.planner.plan(
                issue_title,
                issue_description,
                flow_run_id=run_id,
                path_scope=path_scope or None,
            )
        except (LLMError, LLMResponseError) as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"planning_failed: {exc}")
            await self._post_journaled_note(
                project_id,
                issue_number,
                f"Run `{run_id[:8]}` **failed** at planning: {exc}\n\n"
                "*This is an automated message.*",
                run_id,
                "planning_failed",
            )
            raise

        digest = plan_digest_of(plan)
        task_digest = task_digest_of(issue_title, issue_description)
        now = datetime.now(timezone.utc)
        base_sha = await self._read_base_sha()
        # ADR-0023 §2: the harness decision is compiled at plan time and
        # frozen into the RunSpec — part of what the gate approves.
        harness_selection = self._compile_harness_selection()

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.PLANNING)
            run = await self._get_run(session, run_id)
            run.plan_digest = digest
            run.base_sha = base_sha
            # The backend string flips to ci_harness at dispatch (ADR-0020);
            # the frozen harness selection rides beside it (ADR-0023).
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "backend": "builtin",
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
                issue_number=issue_number,
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

        await self._post_journaled_note(
            project_id,
            issue_number,
            self._plan_comment(run_id, plan, digest, harness_selection),
            run_id,
            "post_plan_note",
        )

        # F15 (ADR-0018 §2): the pending decision is created when the plan is
        # published — /go consumes THIS row; it never creates one.
        await self._open_pending_decision(
            run_id,
            project_id=project_id,
            issue_number=issue_number,
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
            "GitHub run %s started for %s#%d (by @%s) — waiting for /go",
            run_id[:8],
            self._repo_full_name,
            issue_number,
            author_username,
        )
        return run_id

    async def handle_go(
        self,
        *,
        project_id: int,
        issue_number: int,
        note_text: str,
        author_username: str,
        now: datetime | None = None,
    ) -> None:
        """``/go <run-id>``: validate + consume the gate, then publish.

        Idempotent: a re-delivered note finds the run already out of
        ``waiting_approval`` or the gate already consumed — both ignore.
        """
        match = _GO_RE.search(note_text or "")
        if match is None:
            return
        run_id = match.group(1).lower()
        now = now or datetime.now(timezone.utc)

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            if (
                run is None
                or run.provider != "github"
                or run.github_repo_full_name != self._repo_full_name
            ):
                logger.info("GitHub /go references unknown run %s — ignoring", run_id[:8])
                return
            if run.issue_iid != issue_number:
                logger.info(
                    "GitHub /go for run %s posted on a different issue — ignoring", run_id[:8]
                )
                return
            if run.status != FlowStatus.WAITING_APPROVAL.value:
                # Already advanced (or terminal) — duplicate /go delivery.
                logger.info(
                    "GitHub /go for run %s in status %s — ignoring duplicate",
                    run_id[:8],
                    run.status,
                )
                return

            # ADR-0009: authority comes from trusted configuration.
            if author_username not in self._approvers():
                logger.info(
                    "GitHub /go from @%s who is not in the GitHub approver list — ignoring",
                    author_username,
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
                logger.info("No pending decision for GitHub run %s — ignoring /go", run_id[:8])
                return
            if not is_valid(
                gate,
                now,
                plan_digest=run.plan_digest or "",
                base_sha=run.base_sha or "",
                policy_digest=self._policy_digest(),
                spec_digest=run.spec_digest,
            ):
                # The comment thread is the only operator surface on GitHub:
                # an expired or drifted decision blocks the run visibly.
                expired = as_aware_utc(gate.expires_at) <= as_aware_utc(now)
                reason = (
                    "decision_expired: the approval window closed — run /implement again"
                    if expired
                    else "decision_drift: the approved plan/policy changed — run /implement again"
                )
                await self._transition_in_session(session, run.id, FlowStatus.BLOCKED, reason)
                await self._post_journaled_note(
                    project_id,
                    issue_number,
                    f"Run `{run_id[:8]}` was **blocked**: {reason}.\n\n"
                    "*This is an automated message.*",
                    run_id,
                    "decision_expired" if expired else "decision_drift",
                )
                logger.info("GitHub decision for run %s expired/drifted — blocked", run_id[:8])
                return

            # The decision was opened anonymously at plan publication; the
            # consuming approver's login is recorded on the transition reason.
            try:
                await consume_approval(session, gate.id, now)
            except GateAlreadyConsumed:
                logger.info("GitHub gate for run %s already consumed — ignoring", run_id[:8])
                return
            await self._transition_in_session(
                session, run.id, FlowStatus.PROPOSING, f"approved by @{author_username}"
            )

        logger.info(
            "GitHub gate for run %s consumed by @%s — publishing", run_id[:8], author_username
        )
        await self._advance_publish(run_id, project_id=project_id, issue_number=issue_number)

    async def handle_cancel(
        self,
        *,
        project_id: int,
        issue_number: int,
        note_text: str,
        author_username: str,
    ) -> None:
        """``/cancel [run-id]``: cancel-as-revoke (F13) on the GitHub path.

        Without an explicit run id the latest ACTIVE run for the issue is
        cancelled. The durable ``cancel_requested`` flag revokes the
        publication grant (in-flight publish legs re-read it and stand down)
        and scheduled steps are withdrawn, so late results are superseded.
        """
        match = _CANCEL_RE.search(note_text or "")
        if match is None:
            return
        if author_username not in self._approvers():
            logger.info(
                "GitHub /cancel from @%s who is not in the GitHub approver list — ignoring",
                author_username,
            )
            return

        requested = (match.group(1) or "").lower()
        async with self._session_factory() as session:
            if not requested:
                run = await self._find_active_run(project_id, issue_number)
                if run is None:
                    logger.info(
                        "GitHub /cancel on %s#%s — no active run",
                        self._repo_full_name,
                        issue_number,
                    )
                    return
            elif len(requested) == 32:
                run = await session.get(FlowRun, requested)
                if (
                    run is None
                    or run.provider != "github"
                    or run.github_repo_full_name != self._repo_full_name
                    or run.issue_iid != issue_number
                ):
                    logger.info(
                        "GitHub /cancel references unknown run %s — ignoring", requested[:8]
                    )
                    return
            else:
                # Short id (plan comments show the 8-char form): resolve by
                # prefix among the issue's runs; ambiguity means no action.
                runs = (
                    (
                        await session.execute(
                            select(FlowRun)
                            .where(
                                FlowRun.provider == "github",
                                FlowRun.github_repo_full_name == self._repo_full_name,
                                FlowRun.issue_iid == issue_number,
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
                        "GitHub /cancel prefix %s matches %d runs — ignoring",
                        requested[:8],
                        len(runs),
                    )
                    return
                run = runs[0]
            run_id = run.id
            status = run.status

        if status in {s.value for s in TERMINAL_STATUSES}:
            logger.info("GitHub /cancel for terminal run %s (%s) — ignoring", run_id[:8], status)
            return

        # F13 (ADR-0018 §4): revoke the publication grant first — the flag is
        # what an in-flight publish leg re-reads before writing — then
        # withdraw scheduled steps so no worker picks them up later.
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            evidence = dict(run.evidence or {})
            run.cancel_requested = True
            await session.execute(
                update(StepRun)
                .where(StepRun.flow_run_id == run_id, StepRun.status == "scheduled")
                .values(status="cancelled")
            )
            await session.commit()

        # Actions lane: also stop the harness run itself (best-effort — the
        # grant revocation above is the safety property; a late candidate is
        # superseded, never published).
        raw_handle = str((evidence.get("harness") or {}).get("handle") or "")
        if raw_handle:
            try:
                await GitHubActionsExecutor(self._stack.client, self._settings).cancel(
                    ActionsHandle.from_json(raw_handle)
                )
            except Exception:
                logger.warning(
                    "Actions run cancel failed for run %s — grant already revoked",
                    run_id[:8],
                    exc_info=True,
                )

        await self._transition(
            run_id, FlowStatus.CANCELLED, reason=f"cancelled by @{author_username}"
        )
        await self._post_journaled_note(
            project_id,
            issue_number,
            f"Run `{run_id[:8]}` **cancelled** by @{author_username}. "
            "Any in-flight publication was stood down.\n\n*This is an automated message.*",
            run_id,
            "cancel_note",
        )
        logger.info("GitHub run %s cancelled by @%s", run_id[:8], author_username)

    async def handle_retry(
        self,
        *,
        project_id: int,
        issue_number: int,
        note_text: str,
        author_username: str,
    ) -> None:
        """``/retry [run-id]``: revive a dead run in place (Tier 2).

        The operator override for everything the auto-revive refuses to
        touch: the latest terminal ``failed``/``blocked`` run for the issue
        (or an explicit id/prefix) walks back to ``proposing`` on the SAME
        branch and the frozen harness lane re-fires with the terminal reason
        riding as repair context. Authority mirrors /go: FORGE_APPROVERS
        only. A builtin-lane run has no dispatch to re-fire (it would
        re-derive, not fix forward) — the rejection points at /implement.
        """
        match = _RETRY_RE.search(note_text or "")
        if match is None:
            return
        if author_username not in self._approvers():
            logger.info(
                "/retry from @%s who is not in the GitHub approver list — ignoring",
                author_username,
            )
            return

        requested = (match.group(1) or "").lower()
        async with self._session_factory() as session:
            query = select(FlowRun).where(
                FlowRun.provider == "github",
                FlowRun.github_repo_full_name == self._repo_full_name,
                FlowRun.issue_iid == issue_number,
            )
            if requested:
                query = query.where(FlowRun.id.like(f"{requested}%"))
            else:
                query = query.where(
                    FlowRun.status.in_([FlowStatus.FAILED.value, FlowStatus.BLOCKED.value])
                )
            runs = (
                (await session.execute(query.order_by(FlowRun.created_at.desc())))
                .scalars()
                .all()
            )
            if requested and len(runs) != 1:
                logger.info("/retry %s matches %d runs — ignoring", requested[:8], len(runs))
                return
            run = runs[0] if runs else None
            if run is None:
                logger.info("/retry on issue #%d — no failed/blocked run", issue_number)
                return
            run_id = run.id
            status = run.status
            status_reason = str(run.status_reason or "")
            cancelled = bool(run.cancel_requested)
            candidate_shas = list(run.candidate_shas or [])
            commit_cycle = run.commit_cycle or 1
            revivable = is_harness_backend(str((run.evidence or {}).get("backend") or "").strip())

        def _reject_reason(text: str) -> str:
            return (
                f"Run `{run_id[:8]}` cannot be retried: {text}.\n\n*This is an automated message.*"
            )

        rejection: str | None = None
        if cancelled or status not in (FlowStatus.FAILED.value, FlowStatus.BLOCKED.value):
            rejection = _reject_reason(
                f"it is {status}" + (" and cancelled" if cancelled else "")
                + ". `/retry` revives a dead (`failed`/`blocked`) run — use `/implement` "
                "to start fresh work"
            )
        elif not candidate_shas:
            rejection = _reject_reason(
                "it has no candidate to retry — it died before producing work. "
                "Run `/implement` to start fresh"
            )
        elif not revivable:
            rejection = _reject_reason(
                "it ran on the builtin lane, which has no dispatch to re-fire — "
                "run `/implement` to start fresh"
            )
        if rejection is not None:
            await self._post_journaled_note(
                project_id, issue_number, rejection, run_id, "retry_rejected"
            )
            return

        next_cycle = commit_cycle + 1
        bounded = f"operator retry after terminal {status}: {status_reason}"[:2000]

        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, "retry_requested", correlation_id=f"issue-{issue_number}"
            )
            await session.commit()

        async with self._session_factory() as session:
            controller = Controller(session)
            run = await self._get_run(session, run_id)
            # One operator-granted cycle: /retry may exceed
            # FORGE_MAX_COMMIT_CYCLES by one — a human decided, not the loop.
            run.commit_cycle = next_cycle
            run.evidence = _merge_evidence(
                run.evidence, {"revive": {"count": revive_count(run.evidence)}}
            )
            await controller.transition(
                run_id, FlowStatus.PROPOSING, reason=f"retried by @{author_username}"
            )
            await session.commit()
        await self._complete_action(
            action_id, "succeeded", {"revived_from": status, "commit_cycle": next_cycle}
        )
        await self._post_journaled_note(
            project_id,
            issue_number,
            f"Run `{run_id[:8]}` **retried** by @{author_username} — resuming on the "
            f"same branch, cycle {next_cycle}.",
            run_id,
            "retry_note",
        )
        logger.info(
            "GitHub run %s retried by @%s from %s — re-dispatching the lane",
            run_id[:8],
            author_username,
            status,
        )
        await self._advance_harness(
            run_id,
            project_id=project_id,
            issue_number=issue_number,
            repair_context=bounded,
            repair_reason=f"retry: {status_reason}",
        )

    # ------------------------------------------------------------------
    # Publish leg: propose → CAS commit → Draft PR → evidence → review
    # ------------------------------------------------------------------

    async def _advance_publish(self, run_id: str, *, project_id: int, issue_number: int) -> None:
        """One gate-approved publish cycle, ending at ``ready_for_human``.

        Harness lane (ADR-0020): a repo onboarded for Actions execution
        (``FORGE_GITHUB_HARNESS_WORKFLOW``) dispatches the harness and parks
        in ``waiting_harness`` — the reconciler drives the rest. Builtin
        (default): propose + publish synchronously as before.
        """
        if self._harness_workflow():
            await self._advance_harness(run_id, project_id=project_id, issue_number=issue_number)
            return

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            plan_summary, _ = _plan_evidence(run)
            base_sha = run.base_sha or ""
            plan_digest = run.plan_digest or ""

        # F13: a cancel that landed while this leg was starting revokes the
        # grant — stand down instead of racing the cancel.
        if await self._publication_revoked(run_id):
            logger.info(
                "GitHub run %s cancelled before publication — dropping in-flight leg",
                run_id[:8],
            )
            return

        issue_title = await self._read_issue_title(issue_number)
        outcome = await self._stack.flow.publish_proposal(
            owner=self._owner,
            repo=self._repo,
            issue_number=issue_number,
            run_id=run_id,
            issue_title=issue_title,
            plan_summary=plan_summary,
            expected_head=base_sha or None,
        )

        if not outcome.ok:
            reason = outcome.reason or "publish_failed"
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED if outcome.drift else FlowStatus.FAILED, reason
            )
            await self._post_journaled_note(
                project_id,
                issue_number,
                f"Run `{run_id[:8]}` could not publish: {reason}\n\n"
                "*This is an automated message.*",
                run_id,
                "publish_failed",
            )
            return

        commit_oid = outcome.commit_oid or ""
        async with self._session_factory() as session:
            controller = Controller(session)
            # Walk the intermediate states the publish leg covered in one
            # synchronous pass: propose (done pre-call) → validate → commit
            # → ensure Draft PR. Each journals its outbox row atomically.
            await controller.transition(
                run_id,
                FlowStatus.VALIDATING,
                reason=f"CAS commit pinned to {(outcome.expected_head_oid or '')[:8]}",
            )
            await controller.transition(
                run_id, FlowStatus.COMMITTING, reason=f"candidate {commit_oid[:8]}"
            )
            await controller.transition(
                run_id, FlowStatus.ENSURING_DRAFT_MR, reason=f"Draft PR #{outcome.pr_number}"
            )
            run = await self._get_run(session, run_id)
            run.mr_iid = outcome.pr_number
            run.candidate_shas = list(run.candidate_shas or []) + [commit_oid]
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "published_candidate": {
                        "sha": commit_oid,
                        "base": outcome.expected_head_oid,
                        "branch": outcome.branch,
                        "pr_number": outcome.pr_number,
                        "pr_url": outcome.pr_url,
                    }
                },
            )
            await session.commit()

        # The evidence comment lands as the run walks to waiting_ci; the
        # verification surface is the head's Actions checks (E3b wires the
        # executor — nothing is enforced yet).
        await self._post_journaled_note(
            project_id,
            issue_number,
            self._evidence_comment(outcome, plan_digest),
            run_id,
            "post_evidence_note",
        )

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(
                run_id,
                FlowStatus.WAITING_CI,
                reason=f"Draft PR #{outcome.pr_number} for {commit_oid[:8]}",
            )
            await session.commit()
        # R02: STOP at waiting_ci. PR checks are an independent verification
        # gate — the GitHub harness reconciler polls this run and only a
        # verified (or honestly unverified) run continues to review.

    # ------------------------------------------------------------------
    # Harness leg (E3b, ADR-0020): dispatch → waiting_harness → reconcile
    # ------------------------------------------------------------------

    async def _advance_harness(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_number: int,
        driver: str | None = None,
        repair_context: str = "",
        repair_reason: str | None = None,
    ) -> None:
        """Dispatch the harness workflow and park the run in ``waiting_harness``.

        ``proposing`` = ensure the factory branch at the frozen attempt base
        (idempotent 422) → workflow_dispatch with run_id / attempt_base_oid /
        driver / model → ``waiting_harness`` with the journaled
        :class:`ActionsHandle` in the run's evidence. The reconciler polls
        from here — the wait is worker-free, like ``waiting_ci``.

        The dispatched driver is the one frozen in the RunSpec (ADR-0023
        §6); *driver* overrides it for a fallback advance. Called for a
        fallback the run is already ``waiting_harness`` — it stays parked,
        only the handle moves (ADR-0004 has no waiting_harness self-loop).
        """
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            attempt_base = attempt_base_for(run)
            already_waiting = run.status == FlowStatus.WAITING_HARNESS.value
        if driver is None:
            driver = await self._frozen_harness_driver(run_id) or self._harness_driver()
        branch = github_factory_branch(issue_number, run_id)
        executor = GitHubActionsExecutor(self._stack.client, self._settings)
        handle = ActionsHandle(
            provider="github",
            owner=self._owner,
            repo=self._repo,
            workflow=self._harness_workflow(),
            run_id=0,
            branch=branch,
            attempt_base=attempt_base,
            run_spec_digest=run.spec_digest or "",
            driver=driver,
            forge_run_id=run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        # Intent-first journal for the harness dispatch (ADR-0005).
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(run_id, "harness_start")
            await session.commit()

        try:
            await self._ensure_harness_branch(branch, attempt_base)
            correlated = await executor.launch(
                handle,
                inputs={
                    "run_id": run_id,
                    "model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
                    # The lane renders its brief from the issue's forge plan
                    # comment (fetched read-only) — it needs the issue
                    # number, never the plan TEXT (no input size limits).
                    "issue_number": str(issue_number),
                    # Bounded verification-failure context (check names +
                    # reason) on a repair re-dispatch; cycle 1 dispatches
                    # the same shape as always.
                    **({"repair_context": repair_context[:2000]} if repair_context else {}),
                },
            )
        except Exception as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            await self._to_terminal(run_id, FlowStatus.FAILED, f"harness_start_failed: {exc}")
            return

        await self._complete_action(
            action_id,
            "succeeded",
            {
                "workflow": correlated.workflow,
                "actions_run_id": correlated.run_id or None,
                "branch": branch,
                "attempt_base": attempt_base,
                "driver": correlated.driver,
                "correlated": bool(correlated.run_id),
            },
        )

        async with self._session_factory() as session:
            controller = Controller(session)
            if already_waiting:
                # ADR-0023 §6 fallback advance: the run stays parked in
                # waiting_harness; the fresh handle below is the only change.
                run = await self._get_run(session, run_id)
            else:
                await controller.transition(
                    run_id,
                    FlowStatus.WAITING_HARNESS,
                    reason=(
                        f"harness workflow {correlated.workflow}"
                        + (f" run {correlated.run_id}" if correlated.run_id else " (run pending)")
                    ),
                )
                run = await self._get_run(session, run_id)
            # The durable handle: the reconciler restarts from exactly here
            # (workflow filename, Actions run id, attempt base, started_at).
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "backend": "ci_harness",
                    "harness": {
                        "handle": correlated.to_json(),
                        "workflow": correlated.workflow,
                        "run_id": correlated.run_id or None,
                        "branch": branch,
                        "attempt_base": attempt_base,
                        "driver": correlated.driver,
                        "started_at": correlated.started_at,
                    },
                },
            )
            await session.commit()

        logger.info(
            "Run %s delegated to the Actions harness (workflow %s, run %s, driver %s)"
            " — waiting_harness",
            run_id[:8],
            correlated.workflow,
            correlated.run_id or "pending",
            correlated.driver,
        )

        # Taken-in-work ack (dogfood feedback): the issue should never go
        # quiet between /go and the evidence comment — name the agent, the
        # branch, and the live Actions run so a human can watch it stream.
        actions_url = (
            f"https://github.com/{self._repo_full_name}/actions/runs/{correlated.run_id}"
            if correlated.run_id
            else None
        )
        driver_doc = {
            "claude-code": "Claude Code",
            "grok-build": "Grok Build",
            "opencode": "opencode",
            "copilot": "GitHub Copilot CLI",
        }.get(correlated.driver, correlated.driver)
        watching = f"[▶ watch the run live]({actions_url})" if actions_url else "run id pending"
        ack_body = (
            f"## 🔨 Run `{run_id[:8]}` taken into work\n\n"
            f"- Agent: **{driver_doc}** in GitHub Actions\n"
            f"- Branch: `{branch}`\n"
            f"- {watching}\n\n"
            "The full-fidelity log stays in the job; this issue gets the "
            "evidence comment when the run reaches a verdict.\n\n"
            "*This is an automated message.*"
        )
        ack_note_id = await self._post_journaled_note(
            project_id, issue_number, ack_body, run_id, "taken_in_work_note"
        )
        if ack_note_id is not None and not actions_url:
            # Dispatch still uncorrelated: remember the note so the
            # reconciler can upgrade it with the watch link after discovery
            # (deep-merge INTO the existing harness fragment — a shallow
            # patch would wipe workflow/run_id/branch from the evidence).
            harness_fragment = dict((run.evidence or {}).get("harness") or {})
            harness_fragment.update(
                ack_note_id=ack_note_id, ack_url_pending=True, ack_body=ack_body
            )
            await self._merge_run_evidence(run_id, {"harness": harness_fragment})

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

    async def _ensure_harness_branch(self, branch: str, attempt_base: str) -> None:
        """Cut the factory branch at the frozen attempt base; 422 = already cut."""
        try:
            await self._stack.client.create_branch(self._owner, self._repo, branch, attempt_base)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status == 422:
                return  # idempotent re-entry — the ref is there (ADR-0016)
            raise

    async def _begin_repair(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_number: int,
        failure_kind: str,
        failure_reason: str,
    ) -> bool:
        """Red-dispatch the harness lane as a bounded repair cycle (ADR-0008).

        Only from `waiting_ci`, only for `code`-classified independent-check
        failures, only while commit cycles remain. The walk is
        waiting_ci → evaluating_ci → proposing (graph-legal edges), the
        cycle counter bumps durably, and `_advance_harness` re-dispatches
        with the bounded failure context riding as a dispatch input. The
        lane appends it to the brief, so the agent fixes its own candidate
        — the exact GitLab `_begin_repair` semantics on the Actions lane.
        """
        async with self._session_factory() as session:
            controller = Controller(session)
            run = await self._get_run(session, run_id)
            if run.cancel_requested or run.status in (
                FlowStatus.CANCELLED,
                FlowStatus.FAILED,
                FlowStatus.BLOCKED,
            ):
                return False
            next_cycle = (run.commit_cycle or 1) + 1
            max_cycles = int(getattr(self._settings, "FORGE_MAX_COMMIT_CYCLES", 3) or 3)
            if next_cycle > max_cycles:
                await self._to_terminal(
                    run_id,
                    FlowStatus.BLOCKED,
                    f"quality_contract: {failure_reason} — commit cycles exhausted",
                )
                return False
            run.commit_cycle = next_cycle
            await controller.transition(
                run_id, FlowStatus.EVALUATING_CI, reason=f"repair cycle {next_cycle}"
            )
            await controller.transition(
                run_id,
                FlowStatus.PROPOSING,
                reason=f"repair cycle {next_cycle}: {failure_reason}",
            )
            await session.commit()

        bounded = f"{failure_kind}: {failure_reason}"[:2000]
        logger.info(
            "Run %s enters repair cycle %d — re-dispatching the lane",
            run_id[:8],
            next_cycle,
        )
        await self._advance_harness(
            run_id,
            project_id=project_id,
            issue_number=issue_number,
            repair_context=bounded,
            repair_reason=f"{failure_kind}: {failure_reason}",
        )
        return True

    async def evaluate_waiting_harness(self, now: datetime | None = None) -> None:
        """One reconciler pass over every GitHub run parked in ``waiting_harness``."""
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            run_ids = (
                (
                    await session.execute(
                        select(FlowRun.id).where(
                            FlowRun.provider == "github",
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
                logger.exception("Actions harness reconcile failed for run %s", run_id[:8])

    async def _evaluate_harness_one(self, run_id: str, now: datetime) -> None:
        """Poll one waiting_harness run through its journaled Actions handle."""
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return
            evidence = dict(run.evidence or {})
            project_id = run.project_id
            issue_number = run.issue_iid or 0

        if evidence.get("backend") and not is_harness_backend(str(evidence["backend"])):
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "waiting_harness on a non-harness backend"
            )
            return
        raw_handle = str((evidence.get("harness") or {}).get("handle") or "")
        if not raw_handle:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "waiting_harness without harness handle"
            )
            return
        handle = ActionsHandle.from_json(raw_handle)
        executor = GitHubActionsExecutor(self._stack.client, self._settings)

        try:
            # A handle left uncorrelated by a lost dispatch response is
            # re-discovered first (ADR-0005: one launch per intent).
            if not handle.run_id:
                handle = await executor.reconcile_launch(handle)
                if handle.run_id:
                    await self._merge_run_evidence(
                        run_id,
                        {
                            "harness": {
                                **(evidence.get("harness") or {}),
                                "handle": handle.to_json(),
                                "run_id": handle.run_id,
                            }
                        },
                    )
                    # Upgrade the taken-in-work ack with the watch link once
                    # the dispatch response is correlated (R02 UX slice).
                    harness = dict((evidence.get("harness") or {}))
                    if harness.get("ack_url_pending") and harness.get("ack_note_id"):
                        try:
                            await self._stack.client.update_issue_comment(
                                self._owner,
                                self._repo,
                                int(harness["ack_note_id"]),
                                (evidence.get("harness") or {})
                                .get("ack_body", "")
                                .replace(
                                    "run id pending",
                                    f"[▶ watch the run live](https://github.com/{self._repo_full_name}/actions/runs/{handle.run_id})",
                                ),
                            )
                            # run_id is re-pinned: `harness` is the stale
                            # pre-discovery snapshot, and merging it back
                            # would resurrect run_id=None over the fresh id.
                            await self._merge_run_evidence(
                                run_id,
                                {
                                    "harness": {
                                        **harness,
                                        "run_id": handle.run_id,
                                        "ack_url_pending": False,
                                    }
                                },
                            )
                        except Exception:
                            logger.warning(
                                "Ack comment upgrade failed for %s — non-fatal",
                                run_id[:8],
                                exc_info=True,
                            )
                else:
                    return  # discovery retries next tick; the deadline decides
            outcome = await executor.poll(handle, now=now)
        except Exception:
            logger.exception(
                "Actions harness poll failed for run %s — keeping it waiting", run_id[:8]
            )
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
                run_id,
                project_id=project_id,
                issue_number=issue_number,
                failure_kind=kind,
                failure_reason=outcome.reason,
            ):
                return
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"harness_{kind}: {outcome.reason}")
            return

        await self._publish_harness_candidate(run_id, project_id, issue_number, outcome, handle)

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
        else:
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
        *,
        project_id: int,
        issue_number: int,
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
        forge-side model calls and only reconcile candidate receipts, so
        there is no reservation to move.
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
        await self._advance_harness(
            run_id,
            project_id=project_id,
            issue_number=issue_number,
            driver=nxt.harness,
        )
        return True

    async def _publish_harness_candidate(
        self,
        run_id: str,
        project_id: int,
        issue_number: int,
        outcome: HarnessOutcome,
        handle: ActionsHandle,
    ) -> None:
        """Well-formed candidate → trusted publisher → Draft PR → waiting_ci.

        The bundle crosses the SAME trusted boundary as the builtin path
        (ADR-0016 §2): publication grant, spec digest, strict
        materialization against the authoritative attempt-base contents
        (via the repository reader), policy validation, then ONE branch-CAS
        commit via :class:`GitHubPublishFlow`. From ``waiting_ci`` the
        existing review → ready_for_human flow takes over, unchanged —
        Actions checks on the head are the verification surface.
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
        # recorded as evidence only; the publication grant is gone. The run
        # stays cancelled even if the harness could not be stopped.
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            revoked = bool(run.cancel_requested or run.status == FlowStatus.CANCELLED.value)
            plan_digest = run.plan_digest or ""
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
                "Run %s cancelled — Actions candidate on %s recorded as superseded",
                run_id[:8],
                bundle.attempt_base_oid[:8],
            )
            return

        await self._transition(
            run_id,
            FlowStatus.COMMITTING,
            reason=f"publishing Actions candidate on {bundle.attempt_base_oid[:8]}",
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

        # Authoritative full-content reads at the attempt base for the
        # modify entries (no truncation); missing files are absent —
        # validation reports create/update/delete existence against it.
        base_contents: dict[str, str] = {}
        try:
            for path in dict.fromkeys(bundle.paths):
                try:
                    base_contents[path] = await self._stack.reader.read_text(
                        path, ref=bundle.attempt_base_oid
                    )
                except Exception:
                    continue
            entries = bundle.materialize(base_contents)
        except Exception as exc:
            reason = getattr(exc, "reason", None) or "materialize_failed"
            # R02/ADR-0008: a broken candidate blames the change — bounded
            # repair re-dispatches the lane in the SAME branch with the
            # failure context (LIVE-found: patch_does_not_apply on a
            # transient authoritative read). Cycles exhausted → honest
            # blocked, the preserved artifact keeps the work.
            if await self._begin_repair(
                run_id,
                project_id=run.project_id,
                issue_number=run.issue_iid or 0,
                failure_kind="code",
                failure_reason=f"candidate invalid: {reason}",
            ):
                return
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, f"harness_candidate_invalid: {reason}: {exc}"
            )
            return

        changeset = ChangeSet(
            branch=github_factory_branch(issue_number, run_id),
            commit_message=(f"forge: implement {issue_number or 0} (run {short_run_id(run_id)})"),
            changes=[
                Change(
                    path=entry.path,
                    operation=Operation.UPDATE
                    if entry.operation == "modify"
                    else Operation.CREATE
                    if entry.operation == "create"
                    else Operation.DELETE,
                    content=entry.new_content,
                )
                for entry in entries
            ],
            attempt_base_oid=bundle.attempt_base_oid,
        )
        violations = validate_changeset(
            changeset,
            base_contents,
            allowed_paths=await self._read_spec_allowed_paths(run_id),
        )
        if violations:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "changeset_invalid: " + "; ".join(violations)
            )
            return

        if not await _fence_valid():
            logger.info("Run %s fenced out of the Actions publish — standing down", run_id[:8])
            return

        publish_outcome = await self._stack.flow.publish_changeset(
            self._owner,
            self._repo,
            issue_number=issue_number,
            run_id=run_id,
            changeset=changeset,
            base_branch=self._target_branch(),
            expected_head=bundle.attempt_base_oid,
        )
        if not publish_outcome.ok:
            reason = publish_outcome.reason or "publish_failed"
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED if publish_outcome.drift else FlowStatus.FAILED,
                reason,
            )
            await self._post_journaled_note(
                project_id,
                issue_number,
                f"Run `{run_id[:8]}` could not publish: {reason}\n\n"
                "*This is an automated message.*",
                run_id,
                "publish_failed",
            )
            return

        commit_oid = publish_outcome.commit_oid or ""
        async with self._session_factory() as session:
            controller = Controller(session)
            # The publish leg walked COMMITTING (validation + CAS write
            # happened inside the trusted boundary above) → ENSURING_DRAFT_MR.
            await controller.transition(
                run_id,
                FlowStatus.ENSURING_DRAFT_MR,
                reason=f"Draft PR #{publish_outcome.pr_number} on "
                f"{(publish_outcome.expected_head_oid or '')[:8]}",
            )
            run = await self._get_run(session, run_id)
            run.mr_iid = publish_outcome.pr_number
            run.candidate_shas = list(run.candidate_shas or []) + [commit_oid]
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "published_candidate": {
                        "sha": commit_oid,
                        "base": publish_outcome.expected_head_oid,
                        "branch": publish_outcome.branch,
                        "pr_number": publish_outcome.pr_number,
                        "pr_url": publish_outcome.pr_url,
                        "harness_workflow": handle.workflow,
                        "actions_run_id": handle.run_id or None,
                    }
                },
            )
            await session.commit()

        await self._post_journaled_note(
            project_id,
            issue_number,
            self._evidence_comment(publish_outcome, plan_digest),
            run_id,
            "post_evidence_note",
        )

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(
                run_id,
                FlowStatus.WAITING_CI,
                reason=f"Draft PR #{publish_outcome.pr_number} for {commit_oid[:8]}",
            )
            await session.commit()
        # R02: STOP at waiting_ci. PR checks are an independent verification
        # gate — the GitHub harness reconciler polls this run and only a
        # verified (or honestly unverified) run continues to review.

    async def evaluate_waiting_ci_one(self, run_id: str, now=None) -> None:
        """One verification pass over a waiting_ci run (R02).

        The candidate's PR checks are an independent gate: pending → keep
        waiting (bounded); any failure → ADR-0008 classification (repair if
        budget remains, else blocked); all green → review; NO checks at all
        → review as honestly **unverified** (evidence records it; the ready
        reason says so — never presented as verified).
        """
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return
            if run.status != FlowStatus.WAITING_CI.value:
                return  # cancelled/advanced elsewhere — superseded, never revived
            candidate_shas = list(run.candidate_shas or [])
            candidate_sha = candidate_shas[-1] if candidate_shas else (run.base_sha or "")
            issue_number = run.issue_iid or 0

        if not candidate_sha:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "verification without a candidate sha"
            )
            return

        try:
            runs = await self._stack.client.list_workflow_runs_for_sha(
                self._owner, self._repo, candidate_sha
            )
        except Exception:
            logger.exception("Checks read failed for %s — keeping it waiting", run_id[:8])
            return
        # The harness lane itself is execution, not verification — exclude it.
        harness_workflow = str(
            getattr(self._settings, "FORGE_GITHUB_HARNESS_WORKFLOW", "") or ""
        ).strip()
        checks = [r for r in runs if not (harness_workflow and r.get("name") == harness_workflow)]

        if not checks:
            # RACE GUARD: a just-opened PR's checks take a few seconds to
            # register (LIVE-found on the dogfood cycle: the verifier polled
            # before ci.yml had started and wrongly concluded no-CI). Hold
            # the run for a grace window before declaring not_configured.
            started = run.updated_at
            # `or 120` would turn a deliberate 0 into the default — check None only.
            _raw = getattr(self._settings, "FORGE_VERIFICATION_GRACE_SECONDS", None)
            grace = 120 if _raw is None else int(_raw)

            started = as_aware_utc(started) if started is not None else None
            now = as_aware_utc(now)
            print(
                f"DEBUG grace: elapsed={(now - started).total_seconds() if started else None} grace={grace}"
            )
            if started is not None and (now - started).total_seconds() < grace:
                return  # keep waiting — checks may still register
            # No independent CI configured on this repo — proceed to review
            # as honestly unverified (R02: never presented as verified).
            await self._merge_run_evidence(
                run_id,
                {"verification": {"status": "not_configured", "candidate_sha": candidate_sha}},
            )
            logger.info("No CI checks configured for %s — review as unverified", run_id[:8])
            async with self._session_factory() as session:
                controller = Controller(session)
                await controller.transition(
                    run_id,
                    FlowStatus.EVALUATING_CI,
                    reason="no CI configured — unverified",
                )
                await session.commit()
            await self._review_and_ready(
                run_id,
                project_id=run.project_id,
                issue_number=issue_number,
                pr_number=run.mr_iid or 0,
                candidate_sha=candidate_sha,
                base_sha=run.base_sha or "",
                verified=False,
            )
            return

        pending = [c for c in checks if (c.get("status") or "") != "completed"]
        failed = [
            c
            for c in checks
            if (c.get("conclusion") or "")
            in ("failure", "timed_out", "action_required", "cancelled")
        ]
        if pending:
            # Deadline: verification must converge (R17 — bounded waiting).
            deadline = int(
                getattr(self._settings, "FORGE_VERIFICATION_TIMEOUT_SECONDS", 1800) or 1800
            )
            started = run.updated_at  # the WAITING_CI transition moment
            if started is not None and (now - started).total_seconds() > deadline:
                await self._to_terminal(
                    run_id, FlowStatus.BLOCKED, "verification_timeout: checks did not conclude"
                )
            return

        if failed:
            # ADR-0008: an independent check failure blames the change —
            # bounded repair while cycles remain, else an honest blocked
            # state with the failing check names.
            names = ", ".join(sorted({c.get("name") or "check" for c in failed}))
            await self._begin_repair(
                run_id,
                project_id=run.project_id,
                issue_number=issue_number,
                failure_kind="code",
                failure_reason=f"checks failed ({names})",
            )
            return

        await self._merge_run_evidence(
            run_id,
            {
                "verification": {
                    "status": "passed",
                    "candidate_sha": candidate_sha,
                    "checks": [
                        {"name": c.get("name"), "conclusion": c.get("conclusion")} for c in checks
                    ],
                }
            },
        )
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(
                run_id,
                FlowStatus.EVALUATING_CI,
                reason="PR checks passed",
            )
            await session.commit()
        await self._review_and_ready(
            run_id,
            project_id=run.project_id,
            issue_number=issue_number,
            pr_number=run.mr_iid or 0,
            candidate_sha=candidate_sha,
            base_sha=run.base_sha or "",
            verified=True,
        )

    async def _review_and_ready(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_number: int,
        pr_number: int | None,
        candidate_sha: str,
        base_sha: str,
        verified: bool = True,
    ) -> None:
        """Reviewing → ready_for_human.

        `verified=False` (no independent CI configured) is honest: the run
        still reaches the human, but the reason and evidence say
        `unverified` instead of implying checks passed (R02)."""
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(
                run_id, FlowStatus.REVIEWING, reason="readonly review of the PR diff"
            )
            await session.commit()

        plan_summary, _ = await self._read_plan_evidence(run_id)
        issue_title = await self._read_issue_title(issue_number)
        try:
            review = await self._stack.reviewer.review(
                owner=self._owner,
                repo=self._repo,
                pr_number=pr_number or 0,
                issue_title=issue_title,
                plan_summary=plan_summary,
                base_sha=base_sha,
                candidate_sha=candidate_sha,
                flow_run_id=run_id,
            )
        except (LLMError, LLMResponseError) as exc:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"review_failed: {exc}")
            return

        verdict = str(getattr(review, "verdict", ""))
        summary = str(getattr(review, "summary", ""))
        findings = [
            {"severity": str(f.severity), "file": str(f.file), "note": str(f.note)}
            for f in (getattr(review, "findings", ()) or ())
        ]
        # ADR-0008: the review approves THIS sha.
        await self._merge_run_evidence(
            run_id,
            {
                "review": {
                    "verdict": verdict,
                    "sha": candidate_sha,
                    "summary": summary,
                    "findings": findings,
                }
            },
        )

        # Self-check: the recorded review must be bound to the candidate sha.
        stored = await self._read_review_evidence(run_id)
        if stored is None or stored.get("sha") != candidate_sha:
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                "review_sha_mismatch: review is not bound to the candidate sha (ADR-0008)",
            )
            return

        verified = bool(verified)
        reason = " · ".join(
            part
            for part in (
                "unverified — no CI configured" if not verified else None,
                "review raised concerns — merge is a human decision"
                if verdict == "concerns"
                else "merge is a human decision",
            )
            if part
        )
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.READY_FOR_HUMAN, reason=reason)
            await session.commit()
        logger.info("GitHub run %s is ready_for_human at %s", run_id[:8], candidate_sha[:8])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _find_active_run(self, project_id: int, issue_number: int) -> FlowRun | None:
        """The latest non-terminal GitHub run for the (repo, issue), or None."""
        terminal = {status.value for status in TERMINAL_STATUSES}
        async with self._session_factory() as session:
            run = (
                (
                    await session.execute(
                        select(FlowRun)
                        .where(
                            FlowRun.provider == "github",
                            FlowRun.project_id == project_id,
                            FlowRun.issue_iid == issue_number,
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

        Callers only ever dereference ids created in the same flow, so a
        missing row is an invariant violation, not a tolerated outcome —
        unlike the guarded ``session.get`` sites, which keep their explicit
        ``if run is None`` branches.
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
            "branch and Draft PR.\n\n"
            f"- Approve it: `/go {run.id}`\n"
            f"- Cancel it first: `/cancel {run.id}`"
        )

    def _approvers(self) -> list[str]:
        """The trusted approver list (GitHub logins, connection-scoped).

        ``FORGE_GITHUB_APPROVERS`` when set, else the shared
        ``FORGE_APPROVERS`` fallback; sorted so the policy digest is
        insensitive to the setting's order.
        """
        return sorted(approvers_for("github", self._settings))

    def _required_jobs(self) -> list[str]:
        raw = getattr(self._settings, "FORGE_REQUIRED_JOBS", "") or ""
        return [name.strip() for name in raw.split(",") if name.strip()]

    def _target_branch(self) -> str:
        return str(getattr(self._settings, "FORGE_TARGET_BRANCH", "main") or "main")

    def _harness_workflow(self) -> str:
        """The target repo's harness workflow filename, or "" for builtin.

        ``FORGE_GITHUB_HARNESS_WORKFLOW`` (ADR-0020): set at onboarding to
        the human-applied ``ci/templates/forge-harness.github.yml`` filename;
        empty (default) keeps the builtin in-worker lane.
        """
        return str(getattr(self._settings, "FORGE_GITHUB_HARNESS_WORKFLOW", "") or "").strip()

    def _harness_driver(self) -> str:
        """The harness driver id frozen into the RunSpec (multi-harness).

        Follows the ``ci_harness[:<driver>]`` convention of
        ``FORGE_IMPLEMENTER_BACKEND`` (ADR-0015); the bare value or empty
        default selects ``claude-code``.
        """
        raw = str(getattr(self._settings, "FORGE_IMPLEMENTER_BACKEND", "") or "").strip()
        if raw.startswith("ci_harness:") and raw.partition(":")[2].strip():
            return raw.partition(":")[2].strip()
        return HARNESS_NAME

    def _compile_harness_selection(self) -> HarnessSelection:
        """ADR-0023 §2: preference ∩ lanes → the frozen harness decision.

        The harness driver that will dispatch (``_harness_driver``) must stay
        in the list (tighten-only, ADR-0015) when the Actions lane is
        onboarded; the builtin lane dispatches no harness, so only the id
        set is validated there. v0.9: the compilable lanes are the shipped
        driver set — credential presence is declared by the preference and
        doctor-verified (ADR-0011); a lane without creds fails
        infrastructure at dispatch, which with the fallback switch OFF (the
        default) blocks the run visibly.

        TODO(ADR-0023 §5): pass the planner's structured proposal
        ({"harness", "budget_class", "reason"}) from LLMPlanner.plan's
        output once that surface exists — the integration point is the
        ``planner.plan`` call in start_run (factory/planner.py returns plain
        markdown today and is outside this change's scope). None keeps the
        compiler defaults.
        """
        preference = resolve_preference(self._config, self._settings)
        workflow = self._harness_workflow()
        validate_preference(preference, self._harness_driver() if workflow else None)
        return compile_harness_selection(
            preference,
            f"ci_harness:{self._harness_driver()}" if workflow else "builtin",
            set(SHIPPED_DRIVERS),
            None,
        )

    def _policy_digest(self) -> str:
        """ADR-0009 + ADR-0018 §1: bind the effective execution policy.

        The harness workflow filename is part of the policy (ADR-0020 §3): a
        changed workflow invalidates approval like any other execution
        profile change. ADR-0023 §3: the preference list and the fallback
        switch join too — changing either invalidates pending gates (the
        per-run budget class and selection reason are bound via the RunSpec
        digest instead).
        """
        document = {
            "approvers": self._approvers(),
            "target_branch": self._target_branch(),
            "required_jobs": self._required_jobs(),
            "implementer_backend": "ci_harness" if self._harness_workflow() else "builtin",
            "harness_model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
            "harness_preference": resolve_preference(self._config, self._settings),
            "harness_fallback": bool(getattr(self._settings, "FORGE_HARNESS_FALLBACK", False)),
        }
        workflow = self._harness_workflow()
        if workflow:
            document["harness_workflow"] = workflow
            document["harness_driver"] = self._harness_driver()
        return canonical_json_digest(document)

    def _build_run_spec_document(
        self,
        *,
        project_id: int,
        issue_number: int,
        base_sha: str,
        plan_digest: str,
        task_digest: str,
        allowed_paths: list[str] | None = None,
        harness_selection: HarnessSelection | None = None,
    ) -> dict:
        """The immutable RunSpec document frozen at plan acceptance (F14).

        With the harness lane configured, backend_config carries the frozen
        execution profile: backend ``ci_harness``, the harness workflow
        filename and the driver — the dispatch inputs later come FROM this
        document, so a spec change means a different harness run.
        ``allowed_paths`` (v0.7 monorepo scoping) is present only for scoped
        runs; unscoped documents keep the pre-v0.7 shape and digest.

        ADR-0023 §3: the harness decision (selected driver, fallback tail,
        budget class, selection reason) freezes into backend_config too —
        the ``driver`` key now names the compiled selection.
        """
        selection = harness_selection or self._compile_harness_selection()
        workflow = self._harness_workflow()
        backend_config: dict = {
            "backend": "ci_harness" if workflow else "builtin",
            "model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
            "target_branch": self._target_branch(),
            **selection.as_document(),
        }
        if workflow:
            backend_config["harness_workflow"] = workflow
            backend_config["driver"] = selection.harness
        document: dict = {
            "subject": {
                "provider": "github",
                "repo_full_name": self._repo_full_name,
                "project_id": project_id,
                "issue_iid": issue_number,
            },
            "source_base_oid": base_sha or "",
            "plan_digest": plan_digest,
            "task_digest": task_digest,
            "policy_digest": self._policy_digest(),
            "backend_config": backend_config,
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
        issue_number: int,
        plan_digest: str,
        base_sha: str,
        task_digest: str,
        spec_digest: str,
        now: datetime,
    ) -> None:
        """Create the pending gate decision the moment the plan is published.

        Generation 0, no approver yet, an absolute deadline of
        ``FORGE_DECISION_TTL_SECONDS`` and the plan/task/spec digests frozen
        at plan time (F15, ADR-0018 §2).
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
                    project_id, "run", issue_number, "plan_publication", run_id
                ),
                expires_at=now + timedelta(seconds=ttl),
            )
            gate.spec_digest = spec_digest
            gate.task_digest = task_digest
            await session.commit()

    async def _publication_revoked(self, run_id: str) -> bool:
        """Whether the run's publication grant was revoked (F13)."""
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return True
            return bool(run.cancel_requested or run.status == FlowStatus.CANCELLED.value)

    async def _read_base_sha(self) -> str:
        """Record the pinned base (head of the target branch) at planning time."""
        try:
            return await self._stack.client.get_branch_head(
                self._owner, self._repo, self._target_branch()
            )
        except Exception:
            logger.warning("Could not read base head of %s", self._repo_full_name, exc_info=True)
            return ""

    def _plan_summary(self, plan: str) -> str:
        summarizer = getattr(self._stack.planner, "plan_summary", None)
        if callable(summarizer):
            try:
                return str(summarizer(plan))
            except Exception:  # pragma: no cover — defensive
                pass
        return plan[:PLAN_SUMMARY_CHARS]

    def _plan_files_hint(self) -> list[str]:
        getter = getattr(self._stack.planner, "files_hint", None)
        if callable(getter):
            try:
                return [str(hint) for hint in (getter() or [])]
            except Exception:  # pragma: no cover — defensive
                return []
        return []

    async def _read_plan_evidence(self, run_id: str) -> tuple[str, list[str]]:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            return _plan_evidence(run)

    async def _read_review_evidence(self, run_id: str) -> dict | None:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            review = (run.evidence or {}).get("review")
            return dict(review) if isinstance(review, dict) else None

    async def _merge_run_evidence(self, run_id: str, patch: dict) -> None:
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            run.evidence = _merge_evidence(run.evidence, patch)
            await session.commit()

    async def _read_spec_allowed_paths(self, run_id: str) -> list[str]:
        """The RunSpec's frozen ``allowed_paths`` globs ([] when unscoped)."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            return await spec_allowed_paths(session, run)

    async def _read_issue_title(self, issue_number: int) -> str:
        """Fetch the issue title; fall back to a neutral label on read failure."""
        try:
            issue = await self._stack.client.get_issue(self._owner, self._repo, issue_number)
            return issue.title
        except Exception:
            logger.warning(
                "Could not read title of %s#%s — using fallback",
                self._repo,
                issue_number,
                exc_info=True,
            )
            return f"issue {issue_number}"

    def _plan_comment(
        self, run_id: str, plan: str, digest: str, harness_selection: HarnessSelection
    ) -> str:
        # On GitHub the App's bot login is forcewake-forge[bot]; the literal
        # "@forge" mention links to an unrelated org. Bare commands suffice.
        mention = ""
        approvers = self._approvers()
        # Mentions stay OUTSIDE code spans: GitHub does not linkify (or
        # notify) @usernames inside backticks either.
        approver_note = (
            ", ".join(f"@{name}" for name in approvers)
            or "none configured — set `FORGE_GITHUB_APPROVERS`"
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

    @staticmethod
    def _evidence_comment(outcome, plan_digest: str) -> str:
        pr_url = outcome.pr_url or "(PR url unavailable)"
        return (
            "## Forge run ready for human review\n\n"
            f"- **Pull request:** {pr_url}\n"
            f"- **Candidate commit:** `{outcome.commit_oid}`\n"
            f"- **Plan digest:** `{plan_digest}`\n"
            f"- **Verification:** {_VERIFICATION_NOTE}\n\n"
            "Merging is a human decision — forge never merges.\n\n"
            "*This is an automated message.*"
        )

    async def _post_journaled_note(
        self, project_id: int, issue_number: int, body: str, run_id: str, kind: str
    ) -> int | None:
        """Post an issue comment with intent/outcome journaling (ADR-0005).

        Returns the created note id (the taken-in-work ack stores it so the
        reconciler can upgrade the comment with the watch link)."""
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, kind, correlation_id=f"issue-{issue_number}"
            )
            await session.commit()
        try:
            note = await self._stack.client.create_issue_comment(
                self._owner, self._repo, issue_number, body
            )
        except Exception as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        note_id = note.get("id") if isinstance(note, dict) else getattr(note, "id", None)
        await self._complete_action(action_id, "succeeded", {"note_id": note_id})
        return note_id

    async def _complete_action(self, action_id: int, status: str, remote_result=None) -> None:
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.complete_action(action_id, status, remote_result)  # type: ignore[arg-type]
            await session.commit()

    async def _transition_in_session(
        self, session: AsyncSession, run_id: str, status: FlowStatus, reason: str | None = None
    ) -> None:
        """One transition inside the caller's open session (committed here)."""
        controller = Controller(session)
        await controller.transition(run_id, status, reason=reason)
        await session.commit()

    async def _transition(self, run_id: str, status: FlowStatus, reason: str | None = None) -> None:
        async with self._session_factory() as session:
            await self._transition_in_session(session, run_id, status, reason)

    async def _to_terminal(self, run_id: str, status: FlowStatus, reason: str) -> None:
        """Park the run in ``blocked``/``failed`` with an operator-facing reason.

        A ``failed`` terminalization is classified first (see
        :mod:`forge.runs.failure`): a transient reason arms the bounded
        auto-revive instead of dying, a fatal one parks ``blocked``.
        """
        if status is FlowStatus.FAILED:
            await self._terminalize_failure(run_id, reason)
            return
        await self._transition(run_id, status, reason=reason[:200])
        logger.warning("GitHub run %s -> %s: %s", run_id[:8], status.value, reason)

    async def _terminalize_failure(self, run_id: str, reason: str) -> None:
        """Classify a ``failed`` terminalization and act on it (Tier 1).

        Same contract as the GitLab service. Only the harness lane arms a
        revival: its dispatch can be re-fired on the SAME branch (attempt
        base = last candidate, ADR-0016 §4). The builtin lane re-derives the
        work from the approved base on a CAS commit, so a revival there
        would re-derive instead of fixing forward — it parks ``blocked``.
        """
        revivable = await self._revivable(run_id)
        if revivable is None or classify_terminal_failure(reason) is FailureClass.FATAL:
            await self._transition(run_id, FlowStatus.BLOCKED, reason=reason[:200])
            logger.warning("GitHub run %s -> blocked: %s", run_id[:8], reason)
            return

        limit = int(getattr(self._settings, "FORGE_RUN_AUTO_REVIVE_LIMIT", 2) or 0)
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            status = run.status
            cancelled = bool(run.cancel_requested)
            attempts = revive_count(run.evidence)

        resumable = status in RESUMABLE_ADVANCE_STATUSES
        if cancelled or not resumable or limit <= 0 or attempts >= limit:
            exhausted = resumable and limit > 0 and attempts >= limit
            prefix = "auto_revive_exhausted: " if exhausted else ""
            await self._transition(run_id, FlowStatus.BLOCKED, reason=f"{prefix}{reason}"[:200])
            logger.warning("GitHub run %s -> blocked: %s", run_id[:8], reason)
            return
        await self._merge_run_evidence(run_id, arm_revive(attempts, reason))
        logger.warning(
            "GitHub run %s failed transiently (%s) — auto-revive %d/%d armed",
            run_id[:8],
            reason,
            attempts + 1,
            limit,
        )

    async def _revivable(self, run_id: str) -> bool | None:
        """True when the run's frozen backend can be re-fired in place.

        None — the run is gone; the caller parks it blocked rather than
        raising out of the terminalization path.
        """
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return None
            backend_name = str((run.evidence or {}).get("backend") or "").strip()
        return is_harness_backend(backend_name)

    async def evaluate_revival(self, now: datetime | None = None) -> None:
        """One revival pass over this repo's parked runs (Tier 1).

        Skips until the armed backoff elapses, then re-dispatches the frozen
        harness lane on the SAME branch (attempt base = last candidate).
        """
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            run_ids = (
                (
                    await session.execute(
                        select(FlowRun.id).where(
                            FlowRun.provider == "github",
                            FlowRun.github_repo_full_name == self._repo_full_name,
                            FlowRun.status.notin_(_TERMINAL_STATUS_VALUES),
                        )
                    )
                )
                .scalars()
                .all()
            )
        for run_id in run_ids:
            try:
                await self._revive_one(run_id, now)
            except Exception:
                # One broken run must not stall the reconciler loop.
                logger.exception("GitHub revival pass failed for run %s", run_id[:8])

    async def _revive_one(self, run_id: str, now: datetime) -> None:
        """Fire one due auto-revive — or stand down for it."""
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return
            status = run.status
            cancelled = bool(run.cancel_requested)
            evidence = dict(run.evidence or {})
            project_id = run.project_id
            issue_number = run.issue_iid or 0

        if not revive_evidence(evidence):
            return  # not parked for revival
        if cancelled:
            await self._transition(
                run_id, FlowStatus.CANCELLED, reason="cancelled while revival was scheduled"
            )
            return
        due = revive_at(evidence)
        if due is None or as_aware_utc(now) < as_aware_utc(due):
            return  # the bounded backoff still runs — skip until due
        if status not in RESUMABLE_ADVANCE_STATUSES or not await self._revivable(run_id):
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, f"auto_revive_unresumable: status {status}"
            )
            return

        count = revive_count(evidence)
        # One launch per intent (ADR-0005): drop the due stamp BEFORE firing,
        # so the next tick cannot double-dispatch while the advance leg runs.
        await self._merge_run_evidence(run_id, {"revive": {"count": count}})
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, "auto_revive", correlation_id=f"attempt-{count}"
            )
            await session.commit()

        logger.info(
            "GitHub run %s auto-revive %d firing — re-dispatching the same branch",
            run_id[:8],
            count,
        )
        await self._advance_harness(run_id, project_id=project_id, issue_number=issue_number)
        await self._complete_action(action_id, "succeeded", {"revive_count": count})


async def execute_github_run_command(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    metadata: dict,
    *,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
) -> None:
    """Execute a GitHub run command — the ``provider: github`` step dispatch.

    Wired from :func:`forge.runs.service.execute_run_command`. The
    *stack_factory* hook exists for tests to run the service over a fake
    client + stub agents (no network, no model).

    ``review_pr`` dispatches to the reactive review lane
    (:mod:`forge.reactive.github_review`) and ``debug_ci`` to the CI debug
    lane (:mod:`forge.reactive.ci_debug`) — separate lanes beside the
    durable run path below: no FlowRun, no RunSpec, one step in/one comment
    out. RunService/GitHubRunService state is untouched by them.
    """
    command = metadata.get("command")
    if command == "review_pr":
        from forge.reactive.github_review import execute_reactive_review

        await execute_reactive_review(settings, forge_config, session_factory, metadata)
        return
    if command == "debug_ci":
        from forge.reactive.ci_debug import execute_debug_ci_command

        await execute_debug_ci_command(settings, forge_config, session_factory, metadata)
        return
    if command not in {"start_run", "go", "cancel", "retry"}:
        logger.warning("Unknown GitHub run command %r — ignoring", command)
        return
    repo_full_name = str(metadata.get("repo_full_name") or "")
    if "/" not in repo_full_name:
        logger.error("GitHub command without repo_full_name — ignoring")
        return
    owner, repo = repo_full_name.split("/", 1)
    project_id = int(metadata.get("project_id") or 0)
    issue_number = int(metadata.get("issue_number") or 0)
    author_username = str(metadata.get("author_username") or "")
    note_text = str(metadata.get("note_text") or "")

    if stack_factory is None:
        stack_factory = lambda o, r: build_github_agents(  # noqa: E731 — trivial default
            settings, session_factory, o, r
        )
    stack = stack_factory(owner, repo)
    service = GitHubRunService(
        session_factory, settings, forge_config, stack=stack, repo_full_name=repo_full_name
    )

    try:
        if command == "start_run":
            issue = await stack.client.get_issue(owner, repo, issue_number)
            await service.start_run(
                project_id=project_id,
                issue_number=issue_number,
                issue_title=issue.title,
                issue_description=issue.description or "",
                author_username=author_username,
            )
        elif command == "go":
            await service.handle_go(
                project_id=project_id,
                issue_number=issue_number,
                note_text=note_text,
                author_username=author_username,
            )
        elif command == "retry":
            await service.handle_retry(
                project_id=project_id,
                issue_number=issue_number,
                note_text=note_text,
                author_username=author_username,
            )
        else:
            await service.handle_cancel(
                project_id=project_id,
                issue_number=issue_number,
                note_text=note_text,
                author_username=author_username,
            )
    finally:
        aclose = getattr(stack.client, "aclose", None)
        if aclose is not None:
            await aclose()


def _merge_evidence(evidence: dict | None, patch: dict) -> dict:
    """Shallow-merge *patch* into the run's evidence blob."""
    merged = dict(evidence or {})
    merged.update(patch)
    return merged


def _plan_evidence(run: FlowRun) -> tuple[str, list[str]]:
    """Read plan summary + files_hint back out of the run's evidence."""
    plan = (run.evidence or {}).get("plan") or {}
    summary = str(plan.get("summary") or "")
    hints = [str(hint) for hint in (plan.get("files_hint") or [])]
    return summary, hints


def _admission_denied_comment(run_id: str, actor: str) -> str:
    return (
        "## Forge — run not started\n\n"
        f"Run `{run_id[:8]}` was **not started**: admission denied — "
        f"@{actor} is not in the GitHub approver list (`FORGE_GITHUB_APPROVERS`).\n\n"
        "*This is an automated message.*"
    )


async def evaluate_github_waiting_ci(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
    now: datetime | None = None,
) -> None:
    """One verification pass over every GitHub run parked in `waiting_ci`.

    The twin of `evaluate_github_waiting_harness` for the R02 gate: PR
    checks on the candidate sha decide whether the run continues to review
    (verified, or honestly unverified when no CI exists) or blocks.
    """
    from sqlalchemy import select

    from forge.durable.controller import FlowStatus
    from forge.durable.models import FlowRun

    if stack_factory is None:
        stack_factory = lambda o, r: build_github_agents(  # noqa: E731
            settings, session_factory, o, r
        )
    async with session_factory() as session:
        runs = (
            (
                await session.execute(
                    select(FlowRun).where(
                        FlowRun.provider == "github",
                        FlowRun.status == FlowStatus.WAITING_CI.value,
                    )
                )
            )
            .scalars()
            .all()
        )
    for run in runs:
        repo_full_name = str(run.github_repo_full_name or "").strip()
        if "/" not in repo_full_name:
            logger.warning("GitHub waiting_ci run %s without repo identity", run.id[:8])
            continue
        owner, repo = repo_full_name.split("/", 1)
        service = GitHubRunService(
            session_factory,
            settings,
            forge_config,
            stack=stack_factory(owner, repo),
            repo_full_name=repo_full_name,
        )
        try:
            await service.evaluate_waiting_ci_one(run.id, now=now)
        except Exception:
            logger.exception("GitHub verification reconcile failed for run %s", run.id[:8])


async def run_github_harness_reconciler(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: float = 15,
    shutdown_event: asyncio.Event | None = None,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
) -> None:
    """Periodic tick driving the Actions harness lane to convergence.

    Plain asyncio task for ``asyncio.gather`` in the worker (same shape as
    :func:`forge.runs.reconciler.run_reconciler`). Exits immediately when
    the GitHub adapter is disabled or carries no credentials — a GitLab-only
    deployment never pays for it.
    """
    if not bool(getattr(settings, "FORGE_GITHUB_ENABLED", False)):
        return
    try:
        from forge.integrations.github_flow import credentials_from_settings

        credentials_from_settings(settings)  # fail fast, before the loop
    except ValueError:
        logger.info("GitHub credentials not configured — harness reconciler not started")
        return
    if not str(getattr(settings, "FORGE_GITHUB_HARNESS_WORKFLOW", "") or "").strip():
        return

    shutdown_event = shutdown_event or asyncio.Event()
    logger.info("GitHub harness reconciler started (interval=%ss)", interval_seconds)
    while not shutdown_event.is_set():
        try:
            await evaluate_github_waiting_harness(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            # A failed pass must never kill the reconciler task.
            logger.exception("GitHub harness reconciler pass failed")
        try:
            await evaluate_github_revival(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            logger.exception("GitHub revival reconciler pass failed")
        try:
            await evaluate_github_waiting_ci(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            logger.exception("GitHub verification reconciler pass failed")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break  # Event set — clean shutdown.
        except asyncio.TimeoutError:
            pass  # Interval elapsed — next tick.
    logger.info("GitHub harness reconciler stopped")


async def evaluate_github_waiting_harness(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
) -> None:
    """One worker-side reconciler pass over GitHub harness runs (E3b).

    Groups the ``provider=github`` runs parked in ``waiting_harness`` by
    repository and gives each repo's service one
    :meth:`GitHubRunService.evaluate_waiting_harness` tick — the twin of the
    GitLab ``RunService.evaluate_waiting_harness`` slot in
    :func:`forge.runs.reconciler.run_reconciler`. Silent no-op when the
    GitHub adapter is disabled or no harness lane is configured.
    """
    if not str(getattr(settings, "FORGE_GITHUB_HARNESS_WORKFLOW", "") or "").strip():
        return
    repos = await _repos_with_waiting_harness(session_factory)
    if not repos:
        return
    if stack_factory is None:
        stack_factory = lambda o, r: build_github_agents(  # noqa: E731 — trivial default
            settings, session_factory, o, r
        )
    for repo_full_name in repos:
        owner, _, repo = repo_full_name.partition("/")
        stack = stack_factory(owner, repo)
        try:
            service = GitHubRunService(
                session_factory, settings, forge_config, stack=stack, repo_full_name=repo_full_name
            )
            await service.evaluate_waiting_harness(now=now)
        except Exception:
            # One broken repo must not stall the reconciler pass.
            logger.exception("GitHub harness reconcile failed for %s", repo_full_name)
        finally:
            aclose = getattr(stack.client, "aclose", None)
            if aclose is not None:
                await aclose()


async def evaluate_github_revival(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
) -> None:
    """One revival pass over every parked GitHub run (Tier 1).

    The twin of :meth:`GitHubRunService.evaluate_revival` at the worker
    level, like ``evaluate_github_waiting_harness``: groups the runs carrying
    ``revive`` evidence by repository and gives each repo's service a tick.
    """
    runs = []
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(FlowRun).where(
                        FlowRun.provider == "github",
                        FlowRun.status.notin_(_TERMINAL_STATUS_VALUES),
                    )
                )
            )
            .scalars()
            .all()
        )
        for run in rows:
            if revive_evidence(run.evidence):
                runs.append((run.id, str(run.github_repo_full_name or "")))
    if not runs:
        return
    if stack_factory is None:
        stack_factory = lambda o, r: build_github_agents(  # noqa: E731 — trivial default
            settings, session_factory, o, r
        )
    now = now or datetime.now(timezone.utc)
    for run_id, repo_full_name in runs:
        if "/" not in repo_full_name:
            logger.warning("GitHub revival run %s without repo identity", run_id[:8])
            continue
        owner, _, repo = repo_full_name.partition("/")
        stack = stack_factory(owner, repo)
        try:
            service = GitHubRunService(
                session_factory, settings, forge_config, stack=stack, repo_full_name=repo_full_name
            )
            await service._revive_one(run_id, now)
        except Exception:
            # One broken run must not stall the reconciler pass.
            logger.exception("GitHub revival failed for run %s", run_id[:8])
        finally:
            aclose = getattr(stack.client, "aclose", None)
            if aclose is not None:
                await aclose()


async def _repos_with_waiting_harness(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    """Distinct GitHub repos that hold a run parked in ``waiting_harness``."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(FlowRun.github_repo_full_name)
                    .where(
                        FlowRun.provider == "github",
                        FlowRun.status == FlowStatus.WAITING_HARNESS.value,
                        FlowRun.cancel_requested.is_(False),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    return [row for row in rows if row]


__all__ = [
    "GitHubRunService",
    "execute_github_run_command",
    "evaluate_github_waiting_harness",
    "run_github_harness_reconciler",
]
