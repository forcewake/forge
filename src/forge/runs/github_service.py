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

- **Admission/approvers are GitHub logins** — the same ``FORGE_APPROVERS``
  configuration, matched against the comment author's login.
- **One active run per (repo, issue)** reuses the existing partial unique
  index ``uq_active_run_per_issue`` over ``flow_runs.(project_id,
  issue_iid)`` — for GitHub those columns carry the webhook's numeric
  repository id and the issue number.
- **No Actions executor yet (that is E3b)**: the run walks ``waiting_ci``
  synchronously; the evidence comment notes that Actions checks on the head
  are the verification surface — no required jobs are enforced yet.
- **Expired or drifted decisions block** the run with a friendly comment
  (``decision_expired`` / ``decision_drift``) instead of silently ignoring
  the /go: on GitHub the comment thread is the only operator surface.

Durability rules are unchanged (ADR-0005): every transition goes through
:class:`forge.durable.Controller`; external writes (comments, commits, PRs)
are journaled intent-first in ``action_log``.
"""

from __future__ import annotations

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
    RunSpec,
    StepRun,
    as_aware_utc,
    build_source_event_id,
    consume_approval,
    is_valid,
    record_approval,
)
from forge.durable.controller import TERMINAL_STATUSES
from forge.factory.llm import LLMError, LLMResponseError
from forge.factory.planner import PLAN_SUMMARY_CHARS
from forge.integrations.github_flow import GitHubAgents, build_github_agents
from forge.runs.admission import check_admission
from forge.runs.service import (
    RUN_SPEC_SCHEMA_VERSION,
    _CANCEL_RE,
    _DECISION_TTL_FALLBACK_SECONDS,
    _GO_RE,
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

        # ADR-0018 §3: admission before the first paid call. FORGE_APPROVERS
        # carry GitHub logins on this path.
        admission = check_admission(self._settings, self._config, project_id, author_username)
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

        try:
            plan = await self._stack.planner.plan(
                issue_title, issue_description, flow_run_id=run_id
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

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.PLANNING)
            run = await session.get(FlowRun, run_id)
            run.plan_digest = digest
            run.base_sha = base_sha
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "backend": "builtin",
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
            self._plan_comment(run_id, plan, digest),
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
            run = await session.get(FlowRun, run_id)
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
                    "GitHub /go from @%s who is not in FORGE_APPROVERS — ignoring",
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
                "GitHub /cancel from @%s who is not in FORGE_APPROVERS — ignoring",
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
            run = await session.get(FlowRun, run_id)
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
            issue_number,
            f"Run `{run_id[:8]}` **cancelled** by @{author_username}. "
            "Any in-flight publication was stood down.\n\n*This is an automated message.*",
            run_id,
            "cancel_note",
        )
        logger.info("GitHub run %s cancelled by @%s", run_id[:8], author_username)

    # ------------------------------------------------------------------
    # Publish leg: propose → CAS commit → Draft PR → evidence → review
    # ------------------------------------------------------------------

    async def _advance_publish(self, run_id: str, *, project_id: int, issue_number: int) -> None:
        """One gate-approved publish cycle, ending at ``ready_for_human``."""
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
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
            run = await session.get(FlowRun, run_id)
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
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.EVALUATING_CI, reason=_VERIFICATION_NOTE)
            await session.commit()

        await self._review_and_ready(
            run_id,
            project_id=project_id,
            issue_number=issue_number,
            pr_number=outcome.pr_number,
            candidate_sha=commit_oid,
            base_sha=base_sha,
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
    ) -> None:
        """No required checks enforced → reviewing → ready_for_human."""
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

        reason = (
            "review raised concerns — merge is a human decision"
            if verdict == "concerns"
            else "merge is a human decision"
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

    @staticmethod
    def _active_run_comment(run: FlowRun) -> str:
        return (
            "## Forge — a run is already active on this issue\n\n"
            f"Run `{run.id}` is **{run.status}** — a new `/implement` would fork the "
            "branch and Draft PR.\n\n"
            f"- Approve it: `@forge /go {run.id}`\n"
            f"- Cancel it first: `@forge /cancel {run.id}`"
        )

    def _approvers(self) -> list[str]:
        """The trusted approver list (GitHub logins, comma-separated)."""
        raw = getattr(self._settings, "FORGE_APPROVERS", "") or ""
        return [name.strip() for name in raw.split(",") if name.strip()]

    def _required_jobs(self) -> list[str]:
        raw = getattr(self._settings, "FORGE_REQUIRED_JOBS", "") or ""
        return [name.strip() for name in raw.split(",") if name.strip()]

    def _target_branch(self) -> str:
        return str(getattr(self._settings, "FORGE_TARGET_BRANCH", "main") or "main")

    def _policy_digest(self) -> str:
        """ADR-0009 + ADR-0018 §1: bind the effective execution policy."""
        document = {
            "approvers": self._approvers(),
            "target_branch": self._target_branch(),
            "required_jobs": self._required_jobs(),
            "implementer_backend": "builtin",
            "harness_model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
        }
        return canonical_json_digest(document)

    def _build_run_spec_document(
        self,
        *,
        project_id: int,
        issue_number: int,
        base_sha: str,
        plan_digest: str,
        task_digest: str,
    ) -> dict:
        """The immutable RunSpec document frozen at plan acceptance (F14)."""
        return {
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
            "backend_config": {
                "backend": "builtin",
                "model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
                "target_branch": self._target_branch(),
            },
            "budgets": {
                "commit_cycles": int(getattr(self._settings, "FORGE_MAX_COMMIT_CYCLES", 3) or 3),
                "harness_timeout": int(
                    getattr(self._settings, "FORGE_HARNESS_TIMEOUT_SECONDS", 1800) or 1800
                ),
            },
        }

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
            run = await session.get(FlowRun, run_id)
            return _plan_evidence(run)

    async def _read_review_evidence(self, run_id: str) -> dict | None:
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            review = (run.evidence or {}).get("review")
            return dict(review) if isinstance(review, dict) else None

    async def _merge_run_evidence(self, run_id: str, patch: dict) -> None:
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            run.evidence = _merge_evidence(run.evidence, patch)
            await session.commit()

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

    def _plan_comment(self, run_id: str, plan: str, digest: str) -> str:
        mention = str(getattr(self._settings, "FORGE_MENTION_PATTERN", "@forge") or "@forge")
        approvers = self._approvers()
        # Mentions stay OUTSIDE code spans: GitHub does not linkify (or
        # notify) @usernames inside backticks either.
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
    ) -> None:
        """Post an issue comment with intent/outcome journaling (ADR-0005)."""
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
        """Park the run in ``blocked``/``failed`` with an operator-facing reason."""
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
    """
    command = metadata.get("command")
    if command not in {"start_run", "go", "cancel"}:
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
        f"@{actor} is not in the approver list (`FORGE_APPROVERS`).\n\n"
        "*This is an automated message.*"
    )


__all__ = [
    "GitHubRunService",
    "execute_github_run_command",
]
