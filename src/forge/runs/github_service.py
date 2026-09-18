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
- **Event-driven operator busywork**: an ``issues.edited`` whose text drifted
  from the frozen snapshot replans a gate-waiting run (or notes a mid-flight
  edit, never yanking an approved run — ``handle_issue_edited``); removing
  the trigger label cancels gate-waiting runs (``handle_label_removed``); a
  successor run closes the Draft PRs of failed/blocked/cancelled
  predecessors (``close_superseded_draft_prs``).

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
from forge.runs.publisher import spec_allowed_paths
from forge.runs.revival import (
    build_retry_context,
    evaluate_revivals,
    has_active_run,
    resolve_retry_target,
    retry_rejection,
    terminalize_failure,
)
from forge.runs.service import (
    RUN_SPEC_SCHEMA_VERSION,
    _CANCEL_RE,
    _DECISION_TTL_FALLBACK_SECONDS,
    _GO_RE,
    _RETRY_RE,
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
        """``/retry [run-id]``: operator revival of a dead run on the Actions lane.

        The exact GitLab ``handle_retry_note`` semantics (Tier 2): bare, the
        latest ``failed``/``blocked`` run for the issue; the explicit revival
        graph edge walks it to ``proposing``; one operator-granted commit
        cycle; the SAME branch re-dispatched with the terminal reason and the
        last verification evidence as the repair context.
        """
        match = _RETRY_RE.search(note_text or "")
        if match is None:
            return
        if author_username not in self._approvers():
            logger.info(
                "GitHub /retry from @%s who is not in the GitHub approver list — ignoring",
                author_username,
            )
            return
        if issue_number is None:
            logger.info("GitHub /retry off-issue — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        rejection = ""
        async with self._session_factory() as session:
            run = await resolve_retry_target(
                session,
                provider="github",
                project_id=project_id,
                issue_iid=issue_number,
                requested=requested,
                repo_full_name=self._repo_full_name,
            )
            if run is not None:
                run_id = run.id
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
            logger.info(
                "GitHub /retry on %s#%s — no retryable run", self._repo_full_name, issue_number
            )
            return
        if rejection:
            await self._post_journaled_note(
                project_id, issue_number, f"🔁 {rejection}", run_id, "retry_rejected_note"
            )
            return

        # Intent-first journal (ADR-0005), then the durable walk.
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, "retry_requested", correlation_id=f"issue-{issue_number}"
            )
            await controller.revive_transition(
                run_id,
                reason=f"retry requested by @{author_username}",
                authorized_by=f"operator:@{author_username}",
            )
            run = await self._get_run(session, run_id)
            run.commit_cycle = cycle + 1
            await session.commit()

        branch = github_factory_branch(issue_number, run_id)
        logger.info(
            "GitHub run %s retried by @%s — re-dispatching %s (cycle %d)",
            run_id[:8],
            author_username,
            branch,
            cycle + 1,
        )
        await self._post_journaled_note(
            project_id,
            issue_number,
            f"## 🔁 Run `{run_id[:8]}` retried by @{author_username}\n\n"
            f"- Branch: `{branch}` — the work continues in place, no re-planning\n"
            f"- Commit cycle: {cycle + 1}\n\n*This is an automated message.*",
            run_id,
            "retry_ack_note",
        )

        repair_context = build_retry_context(self._settings, status_reason, evidence)
        repair_reason = f"retry by @{author_username}: {status_reason or status}"
        try:
            await self._advance_harness(
                run_id,
                project_id=project_id,
                issue_number=issue_number,
                repair_context=repair_context,
                repair_reason=repair_reason,
            )
        except Exception as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        await self._complete_action(action_id, "succeeded", {"backend": "ci_harness"})

    async def evaluate_revival(self, now: datetime | None = None) -> None:
        """One revival pass over this repo's transiently dead runs (Tier 1)."""
        await evaluate_revivals(
            self._session_factory,
            self._settings,
            provider="github",
            redispatch=self._redispatch_revival,
            now=now,
            log=logger,
        )

    async def _redispatch_revival(self, run_id: str) -> None:
        """Re-dispatch a revived run — same branch, attempt base = last candidate."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            project_id = run.project_id
            issue_number = run.issue_iid
        if issue_number is None:
            logger.warning("GitHub revival of run %s without an issue — skipping", run_id[:8])
            return
        await self._advance_harness(run_id, project_id=project_id, issue_number=issue_number)

    # ------------------------------------------------------------------
    # Issue-edit replan + label-off cancel (operator busywork, event-driven)
    # ------------------------------------------------------------------

    async def handle_issue_edited(
        self,
        *,
        project_id: int,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        author_username: str,
    ) -> str | None:
        """``issues.edited``: keep the waiting plan honest — no operator round trip.

        Three cases, decided against the issue-text snapshot frozen at plan
        time (the RunSpec's ``task_digest``):

        - the run is still ``waiting_approval`` and its gate is unconsumed:
          the waiting plan is stale. The stale run is cancelled durably
          (cancel-as-revoke, F13), a fresh run plans from the new text (its
          plan comment included) and a note says the plan was regenerated —
          no ``/cancel`` + ``/implement`` by hand.
        - the run is beyond the gate: the agent executes the APPROVED
          snapshot — never yanked mid-flight. One informational note says the
          edit is not in the current plan.
        - the text matches the snapshot: a redelivered edit (or an edit back
          to the planned text) — nothing went stale, nothing happens.

        Returns the id of the run that owns the issue afterwards (None when
        the edit was ignored).
        """
        admission = check_admission(
            self._settings, self._config, project_id, author_username, provider="github"
        )
        if not admission.allowed:
            # ADR-0009: forge reacts to an edit only for an actor it would
            # let start a run — anyone else's edit never yanks or replans.
            logger.info(
                "GitHub issue edit by @%s on %s#%s ignored — not admitted (%s)",
                author_username,
                self._repo_full_name,
                issue_number,
                admission.reason,
            )
            return None

        run = await self._find_active_run(project_id, issue_number)
        stale_run_id: str | None = None
        if run is not None:
            edited_digest = task_digest_of(issue_title, issue_body)
            if await self._frozen_task_digest(run.id) == edited_digest:
                logger.info(
                    "GitHub issue edit on %s#%s matches run %s's snapshot — ignoring",
                    self._repo_full_name,
                    issue_number,
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
                    "GitHub run %s superseded by an issue edit — replanning %s#%s",
                    stale_run_id[:8],
                    self._repo_full_name,
                    issue_number,
                )
            else:
                # Approved / in flight: the change is NOT pulled into the
                # approved plan.
                await self._post_journaled_note(
                    project_id,
                    issue_number,
                    f"Issue edited while run `{run.id[:8]}` is in flight — the change is "
                    "**not** in the approved plan. The run keeps executing its approved "
                    "snapshot; run `/cancel` and `/implement` if it should pick the change "
                    "up.\n\n*This is an automated message.*",
                    run.id,
                    "issue_edited_note",
                )
                return run.id
        elif not await self._replan_interrupted(project_id, issue_number):
            logger.info(
                "GitHub issue edit on %s#%s — no active run",
                self._repo_full_name,
                issue_number,
            )
            return None

        new_run_id = await self.start_run(
            project_id=project_id,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_description=issue_body,
            author_username=author_username,
        )
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
            issue_number,
            f"{origin}"
            f"regenerated from the current issue body as run `{new_run_id[:8]}`. "
            f"Approve with `/go {new_run_id}`.\n\n*This is an automated message.*",
            new_run_id,
            "replan_note",
        )
        return new_run_id

    async def _replan_interrupted(self, project_id: int, issue_number: int) -> bool:
        """Whether an edit-triggered replan on this issue died mid-step.

        The command step retries with backoff, and the retry must be able to
        finish what the ``202`` promised: the stale run is already cancelled,
        so a plain ``no active run`` would leave the issue run-less. Two
        shapes are retried — the superseded cancellation itself (``start_run``
        never created the fresh run), and a ``planning_failed`` run it did
        create (the fresh attempt plans again, exactly like a retried
        ``/implement``).
        """
        async with self._session_factory() as session:
            run = (
                (
                    await session.execute(
                        select(FlowRun)
                        .where(
                            FlowRun.provider == "github",
                            FlowRun.project_id == project_id,
                            FlowRun.issue_iid == issue_number,
                        )
                        .order_by(FlowRun.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if run is None:
            return False
        if run.status == FlowStatus.CANCELLED.value:
            return (run.status_reason or "").startswith("superseded by issue edit")
        if run.status == FlowStatus.FAILED.value:
            return (run.status_reason or "").startswith("planning_failed")
        return False

    async def handle_label_removed(
        self, *, project_id: int, issue_number: int, author_username: str
    ) -> int:
        """``issues.unlabeled`` (trigger label): label-off = cancel at the gate.

        Symmetry with label-on = plan (ADR-0020 §4): removing the trigger
        label cancels runs still parked in ``waiting_approval`` — the plan
        was never approved, so nothing executed is lost. Runs past the gate
        are untouched: the approval consumed that plan, the label no longer
        owns it. Returns the number of cancelled runs.
        """
        if author_username not in self._approvers():
            logger.info(
                "GitHub label removal by @%s on %s#%s ignored — not an approver",
                author_username,
                self._repo_full_name,
                issue_number,
            )
            return 0
        async with self._session_factory() as session:
            runs = (
                (
                    await session.execute(
                        select(FlowRun).where(
                            FlowRun.provider == "github",
                            FlowRun.github_repo_full_name == self._repo_full_name,
                            FlowRun.issue_iid == issue_number,
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
                issue_number,
                f"Run `{run_id[:8]}` **cancelled** — the `forge` label was removed by "
                f"@{author_username} while its plan waited for approval. Re-add the label "
                "(or run `/implement`) to plan again.\n\n*This is an automated message.*",
                run_id,
                "cancel_note",
            )
            logger.info(
                "GitHub run %s cancelled — trigger label removed by @%s",
                run_id[:8],
                author_username,
            )
        return len(run_ids)

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
        digest = (spec.document or {}).get("task_digest")
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

    async def _revoke_publication_grant(self, run_id: str) -> None:
        """Cancel-as-revoke's durable core (F13, ADR-0018 §4).

        Sets ``cancel_requested`` — the flag an in-flight publication leg
        re-reads before writing — and withdraws the run's scheduled steps so
        no worker picks them up later. The terminal transition stays the
        caller's (``/cancel`` also stops a dispatched Actions run first).
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

    # ------------------------------------------------------------------
    # Superseded-PR janitor
    # ------------------------------------------------------------------

    async def close_superseded_draft_prs(
        self, *, project_id: int, issue_number: int, successor_run_id: str
    ) -> int:
        """Close Draft PRs left open by terminal runs of the same issue.

        The janitor behind the lingering-Draft-PR chore: when a successor run
        reaches ``waiting_harness`` (harness lane) or publishes its Draft PR
        (builtin lane), any OPEN Draft PR on a ``failed``/``blocked``/
        ``cancelled`` predecessor's factory branch is closed with a
        superseded note naming the successor run. ``ready_for_human`` is a
        terminal status too, but its PR is the LIVE deliverable — never
        touched. Best-effort by contract: one stale PR that cannot be closed
        is logged, never allowed to break the successor's leg. Returns the
        number of PRs closed.
        """
        stale_statuses = (
            FlowStatus.FAILED.value,
            FlowStatus.BLOCKED.value,
            FlowStatus.CANCELLED.value,
        )
        async with self._session_factory() as session:
            stale_runs = (
                (
                    await session.execute(
                        select(FlowRun).where(
                            FlowRun.provider == "github",
                            FlowRun.project_id == project_id,
                            FlowRun.issue_iid == issue_number,
                            FlowRun.status.in_(stale_statuses),
                            FlowRun.id != successor_run_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            stale_meta = [
                (
                    run.id,
                    run.status,
                    str(
                        ((run.evidence or {}).get("published_candidate") or {}).get("branch") or ""
                    ),
                    int(
                        ((run.evidence or {}).get("published_candidate") or {}).get("pr_number")
                        or run.mr_iid
                        or 0
                    ),
                )
                for run in stale_runs
            ]

        closed = 0
        for stale_run_id, stale_status, branch, pr_number in stale_meta:
            action_id = None
            try:
                branch = branch or github_factory_branch(issue_number, stale_run_id)
                pr = await self._stack.client.get_pr_by_head(self._owner, self._repo, branch)
                if pr is None or not pr.get("draft"):
                    # Nothing open (or a human readied it) — nothing to janitor.
                    continue
                body = (
                    "## 🧹 Superseded\n\n"
                    f"This Draft PR belongs to run `{stale_run_id[:8]}` (**{stale_status}**) — "
                    f"run `{successor_run_id[:8]}` now owns this issue and publishes its own "
                    "Draft PR.\n\n*This is an automated message.*"
                )
                # ADR-0005: intent-first journal on the run being superseded.
                async with self._session_factory() as session:
                    controller = Controller(session)
                    action_id = await controller.record_action(
                        stale_run_id, "supersede_draft_pr", correlation_id=f"issue-{issue_number}"
                    )
                    await session.commit()
                if pr_number:
                    await self._stack.client.create_issue_comment(
                        self._owner, self._repo, pr_number, body
                    )
                await self._stack.client.close_pull_request(
                    self._owner, self._repo, int(pr["number"])
                )
                await self._complete_action(
                    action_id,
                    "succeeded",
                    {"pr_number": pr["number"], "superseded_by": successor_run_id},
                )
                closed += 1
                logger.info(
                    "Closed superseded Draft PR #%s of run %s (superseded by %s)",
                    pr["number"],
                    stale_run_id[:8],
                    successor_run_id[:8],
                )
            except Exception:
                logger.warning(
                    "Could not close the superseded Draft PR of run %s — successor %s is "
                    "unaffected",
                    stale_run_id[:8],
                    successor_run_id[:8],
                    exc_info=True,
                )
                if action_id is not None:
                    try:
                        await self._complete_action(action_id, "failed", {"error": "close_failed"})
                    except Exception:
                        logger.warning(
                            "Supersede journal completion failed for run %s",
                            stale_run_id[:8],
                            exc_info=True,
                        )
        return closed

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

        # The builtin lane never parks in waiting_harness — publishing the
        # Draft PR is this run claiming the issue, so the janitor runs here.
        await self.close_superseded_draft_prs(
            project_id=project_id, issue_number=issue_number, successor_run_id=run_id
        )

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

        # Superseded-PR janitor: this run now owns the issue — a dead
        # predecessor's Draft PR must not linger open until an operator
        # closes it by hand.
        await self.close_superseded_draft_prs(
            project_id=project_id, issue_number=issue_number, successor_run_id=run_id
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
        """Poll one waiting_harness run through its journaled Actions handle.

        R17 (deadline-before-I/O): the FIRST operation of every evaluation is
        a local deadline/cancel check over the journaled handle — no provider
        call is made once the harness budget is spent, so a permanently
        erroring (or silently stalled) Actions API can never hold a run past
        its ``harness_timeout``. A poll failure therefore cannot extend the
        deadline either: the deadline derives only from the journaled
        ``started_at``, never from poll outcomes.
        """
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return
            evidence = dict(run.evidence or {})
            project_id = run.project_id
            issue_number = run.issue_iid or 0
            cancel_requested = bool(run.cancel_requested)

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

        # --- R17: local deadline / grant check BEFORE any provider I/O ----
        if cancel_requested:
            # F13: the publication grant is revoked — stand down without
            # touching the provider. The terminal transition stays /cancel's
            # (handle_cancel also stops the Actions run); the late candidate
            # is recorded as superseded either way.
            await self._merge_run_evidence(
                run_id,
                {"superseded": {"reason": "cancelled", "attempt_base": handle.attempt_base}},
            )
            logger.info("Run %s cancelled — harness evaluation stood down pre-poll", run_id[:8])
            return
        timeout = int(getattr(self._settings, "FORGE_HARNESS_TIMEOUT_SECONDS", 1800) or 1800)
        started = _parse_journaled_time(handle.started_at)
        if started is not None and as_aware_utc(now) > as_aware_utc(started) + timedelta(
            seconds=timeout
        ):
            await self._handle_harness_failure(
                run_id,
                project_id,
                issue_number,
                HarnessOutcome.failed("infrastructure", "harness_timeout"),
            )
            return

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
                                "discovery_attempts": 0,
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
                    # R17 (bounded discovery): a dispatch that never surfaces
                    # must not retry forever — after the configured number of
                    # fruitless discovery ticks the run parks blocked with the
                    # precise reason instead of polling until heat death.
                    attempts = (
                        int((evidence.get("harness") or {}).get("discovery_attempts") or 0) + 1
                    )
                    cap = int(
                        getattr(self._settings, "FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS", 20) or 20
                    )
                    if attempts >= cap:
                        await self._to_terminal(
                            run_id,
                            FlowStatus.BLOCKED,
                            "harness_infrastructure: dispatch never observed — "
                            f"discovery found no workflow_dispatch run after {attempts} attempts",
                        )
                        return
                    harness_fragment = dict(evidence.get("harness") or {})
                    harness_fragment["discovery_attempts"] = attempts
                    await self._merge_run_evidence(run_id, {"harness": harness_fragment})
                    return  # remaining discovery attempts retry next tick
            outcome = await executor.poll(handle, now=now)
        except Exception:
            logger.exception(
                "Actions harness poll failed for run %s — keeping it waiting", run_id[:8]
            )
            return

        if outcome.status == "running":
            return  # keep waiting — the durable deadline decides the rest

        if outcome.status == "failed":
            await self._handle_harness_failure(run_id, project_id, issue_number, outcome)
            return

        await self._publish_harness_candidate(run_id, project_id, issue_number, outcome, handle)

    async def _handle_harness_failure(
        self,
        run_id: str,
        project_id: int,
        issue_number: int,
        outcome: HarnessOutcome,
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
            run_id,
            project_id=project_id,
            issue_number=issue_number,
            failure_kind=kind,
            failure_reason=outcome.reason,
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
        # F13 (ADR-0018 §4) + R17 liveness: a late candidate for a run that
        # already reached ANY terminal state (cancelled, failed, blocked,
        # ready) is superseded — recorded as evidence only, never published,
        # and a terminal run is never revived by the callback. The
        # publication grant is gone the moment the run left the active set.
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            if run is None:
                logger.info("Run %s vanished — late harness candidate dropped", run_id[:8])
                return
            status = run.status
            revoked = bool(run.cancel_requested or status in {s.value for s in TERMINAL_STATUSES})
            plan_digest = run.plan_digest or ""
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
                "Run %s is %s — Actions candidate on %s recorded as superseded",
                run_id[:8],
                reason,
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

        R17 (deadline-before-I/O): the verification budget and the cancel
        grant are evaluated LOCALLY before the provider is touched — a
        permanently erroring checks API can keep the run waiting only up to
        ``FORGE_VERIFICATION_TIMEOUT_SECONDS``, never past it.
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
            updated_at = run.updated_at  # the WAITING_CI transition moment
            cancel_requested = bool(run.cancel_requested)

        if not candidate_sha:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "verification without a candidate sha"
            )
            return

        # F13: the grant was revoked mid-wait — no provider call, no publish.
        if cancel_requested:
            logger.info("Run %s cancelled — late verification pass ignored", run_id[:8])
            return

        # R17: the local deadline fires even when the checks API keeps
        # erroring (the stalled-provider stall this bound exists for).
        deadline = int(getattr(self._settings, "FORGE_VERIFICATION_TIMEOUT_SECONDS", 1800) or 1800)
        started = as_aware_utc(updated_at) if updated_at is not None else None
        if started is not None and (as_aware_utc(now) - started).total_seconds() > deadline:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "verification_timeout: checks did not conclude"
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
            # Bounded (R17): the deadline itself is enforced pre-I/O above —
            # a checks API that never answers cannot hold the run forever.
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

        Mirrors the GitLab service: a ``failed`` terminalization is classified
        first (Tier-1 revival) — transient causes schedule a bounded
        auto-revive, fatal ones park ``blocked`` with the precise reason.
        """
        if status is FlowStatus.FAILED:
            await terminalize_failure(
                self._session_factory, self._settings, run_id, reason=reason, log=logger
            )
            return
        await self._transition(run_id, status, reason=reason[:200])
        logger.warning("GitHub run %s -> %s: %s", run_id[:8], status.value, reason)


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
    if command not in {"start_run", "go", "cancel", "retry", "issue_edited", "unlabeled"}:
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
        elif command == "cancel":
            await service.handle_cancel(
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
        elif command == "issue_edited":
            # The edited text travels in the command metadata (the webhook
            # payload's issue object) — no extra API read on the hot path.
            await service.handle_issue_edited(
                project_id=project_id,
                issue_number=issue_number,
                issue_title=str(metadata.get("issue_title") or ""),
                issue_body=str(metadata.get("issue_body") or ""),
                author_username=author_username,
            )
        else:
            await service.handle_label_removed(
                project_id=project_id,
                issue_number=issue_number,
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


def _parse_journaled_time(raw: str) -> datetime | None:
    """Parse a journaled ISO timestamp (handle ``started_at``); None if broken."""
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


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
            await evaluate_github_waiting_ci(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            logger.exception("GitHub verification reconciler pass failed")
        try:
            # Tier-1 auto-revive: re-dispatch runs whose transient-failure
            # backoff has elapsed (forge.runs.revival).
            await evaluate_github_revival(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            logger.exception("GitHub revival reconciler pass failed")

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


async def evaluate_github_revival(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
    now: datetime | None = None,
) -> None:
    """One Tier-1 auto-revive pass over every GitHub run whose backoff elapsed.

    The twin of the GitLab reconciler's ``evaluate_revival`` leg: the same
    classification, the same ``FORGE_RUN_AUTO_REVIVE_LIMIT`` budget, the same
    journaled ``auto_revive`` walk — only the re-dispatch is the Actions lane.
    """
    if stack_factory is None:
        stack_factory = lambda o, r: build_github_agents(  # noqa: E731 — trivial default
            settings, session_factory, o, r
        )

    async def redispatch(run_id: str) -> None:
        async with session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return
            repo_full_name = str(run.github_repo_full_name or "")
            issue_number = run.issue_iid
        owner, _, repo = repo_full_name.partition("/")
        if not repo or issue_number is None:
            logger.warning("GitHub revival of run %s without repo/issue — skipping", run_id[:8])
            return
        service = GitHubRunService(
            session_factory,
            settings,
            forge_config,
            stack=stack_factory(owner, repo),
            repo_full_name=repo_full_name,
        )
        await service._redispatch_revival(run_id)

    await evaluate_revivals(
        session_factory, settings, provider="github", redispatch=redispatch, now=now, log=logger
    )


__all__ = [
    "GitHubRunService",
    "execute_github_run_command",
    "evaluate_github_revival",
    "evaluate_github_waiting_harness",
    "run_github_harness_reconciler",
]
