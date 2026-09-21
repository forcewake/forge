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
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from forge.config import ForgeConfig, Settings, parse_budget_profiles
from forge.durable import (
    ActionLog,
    BudgetLimits,
    Controller,
    DEFAULT_SETTLE_WINDOW_SECONDS,
    FlowRun,
    FlowStatus,
    GateAlreadyConsumed,
    GateApproval,
    OPEN_STATES,
    PublicationIntent,
    RunNotFound,
    RunSpec,
    SettleDecision,
    StepRun,
    as_aware_utc,
    budget_block_reason,
    build_source_event_id,
    classify_probe,
    commit_matches,
    complete_intent,
    consume_approval,
    due_intents,
    find_open_intent,
    ingest_usage_receipt,
    is_valid,
    load_budget_guard,
    mark_dispatched,
    mint_operation_key,
    open_budget,
    open_budget_from_spec,
    ProbeObservation,
    ProbeVerdict,
    record_approval,
    record_intent,
    resolve_budget_limits,
    settle_negative_probe,
    short_run_id,
)
from forge.durable.controller import TERMINAL_STATUSES, InvalidTransition
from forge.execution.github_actions import ActionsHandle, GitHubActionsExecutor
from forge.factory.implementer import IMPLEMENTER_TIER
from forge.factory.llm import LLMError, LLMResponseError
from forge.factory.planner import PLAN_SUMMARY_CHARS
from forge.harnesses.brief_envelope import build_brief_envelope, render_approved_sections
from forge.integrations.github import GitHubAPIError
from forge.integrations.github_flow import (
    GitHubAgents,
    GitHubPublishOutcome,
    build_github_agents,
    github_factory_branch,
)
from forge.orchestrator.project_config import ConfigReadResult, read_project_config
from forge.repository import (
    Change,
    ChangeSet,
    Operation,
    changeset_from_document,
    changeset_to_document,
    validate_changeset,
)
from forge.runs.admission import approvers_for, check_admission
from forge.runs.backends import HARNESS_NAME, HarnessOutcome, is_harness_backend
from forge.runs.candidate import attempt_base_for
from forge.runs.checkpoints import load_step_output, record_step_output, step_input_digest
from forge.runs.consistency import (
    assert_ready_invariants,
    ready_evidence,
    ready_reason,
    verified_verdict,
)
from forge.runs.execution_profile import derive_from_reader
from forge.runs.harness_selection import (
    BudgetCeilings,
    resolve_available_drivers,
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
    CONFIG_BLOCK_PREFIXES,
    RECONCILE_RE,
    STATUS_RE,
    WHY_BLOCKED_RE,
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
from forge.runs.revival import RevivalInFlight
from forge.runs.spec import (
    EXECUTABLE_SPEC_SCHEMA_VERSION,
    ExecutableRunSpec,
    SpecInvalid,
    SpecLegacy,
    load_verified_spec,
)
from forge.runs.verification import (
    GITHUB_CODE_FAILURE_CONCLUSIONS,
    GITHUB_INFRA_CONCLUSIONS,
    GITHUB_SUCCESS_CONCLUSIONS,
    PRODUCER_GITHUB_CHECKS,
    VERIFICATION_INFRA_REASON,
    epoch_started_at,
    verification_epoch,
    waived_conclusions_from_settings,
)
from forge.runs.usecases import (
    OUTCOME_BLOCK,
    OUTCOME_REPAIR,
    OUTCOME_WAIT,
    observe_verification,
)
from forge.runs.service import (
    _CANCEL_RE,
    _DECISION_TTL_FALLBACK_SECONDS,
    _GO_RE,
    _RETRY_RE,
    budget_enforcement_for_backend,
    canonical_json_digest,
    plan_digest_of,
    task_digest_of,
)

logger = logging.getLogger(__name__)

#: The evidence-comment note while E3b (Actions executor) is not wired.
_VERIFICATION_NOTE = (
    "Actions checks on the head are the verification surface — no required checks are enforced yet."
)

#: ADR-0027: the GitHub situational detail after the shared "unverified — "
#: ready-reason prefix (forge.runs.consistency) — no CI checks exist on the
#: repo (R02).
UNVERIFIED_DETAIL = "no CI configured"


def _harness_lane_run(run: Mapping[str, Any], harness_workflow: str) -> bool:
    """Whether one workflow-run payload IS the harness lane (execution).

    A01: the lane is matched by workflow IDENTITY — the run's ``path``,
    which encodes the workflow FILENAME frozen in the spec
    (``.github/workflows/<harness_workflow>``) — never by the mutable
    display ``name``. The display name is only the legacy fallback for
    payloads that carry no path (old GHES), so a decoy workflow merely
    NAMED like the harness file can never mask the verification surface.
    """
    if not harness_workflow:
        return False
    path = str(run.get("path") or "").strip()
    if path:
        return path == f".github/workflows/{harness_workflow}"
    return str(run.get("name") or "").strip() == harness_workflow


def _run_int(run: Mapping[str, Any], key: str) -> int:
    """An int field of a workflow-run payload, 0 when absent/unparseable."""
    raw = run.get(key)
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _run_order_key(run: Mapping[str, Any]) -> tuple[int, int]:
    """Newest-RUN-first ordering key (run number, run id).

    B02: ``run_attempt`` must NOT lead the key — an attempt number counts
    attempts WITHIN one workflow run and is incomparable across runs: an
    old run re-executed to attempt 3 outranked a NEWER run's attempt 1 and
    a stale success masked a fresh failure. The authoritative occurrence is
    the newest RUN; each listed run already carries its own latest
    attempt's conclusion (the attempt rides the evidence surface only).
    """
    return (_run_int(run, "run_number"), _run_int(run, "id"))


def _workflow_identity(run: Mapping[str, Any]) -> str:
    """The stable identity of a run's workflow — B02.

    Display names are mutable and COLLIDE (two workflows may share a name);
    merging observations by name let one workflow's conclusion overwrite
    another's. The identity is the workflow id (present on every Actions
    run payload), falling back to the path, then the name only as the
    legacy last resort (GHES payloads without ids).
    """
    wid = str(run.get("workflow_id") or "").strip()
    if wid:
        return f"id:{wid}"
    path = str(run.get("path") or "").strip()
    if path:
        return f"path:{path}"
    return f"name:{run.get('name') or ''}"


#: Backoff the publication-intent scanner applies to an open intent whose
#: probe says "nothing landed, head intact" — the run's own publish leg owns
#: the re-dispatch; the scanner just stops polling the provider hot.
_INTENT_PROBE_BACKOFF_SECONDS = 60

#: R07: post-gate states a crashed worker leaves a run in. A re-delivered or
#: re-claimed ``/go`` command re-drives the publish leg (the recovery
#: driver) instead of ignoring the duplicate: ``_advance_publish`` probes the
#: durable publication intent first (R11), so the re-drive adopts a landed
#: commit, resolves a moved ref, or re-dispatches with the SAME operation key
#: — and its propose checkpoint (``_propose_for_publish``) means the
#: re-dispatch never calls the model a second time.
_RESUMABLE_PUBLISH_STATUSES = frozenset(
    {"proposing", "validating", "committing", "ensuring_draft_mr"}
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

    async def _settle_negative_probe(
        self, intent: PublicationIntent, *, now: datetime | None = None
    ) -> SettleDecision:
        """The A12 certainty machine for a negative probe (own transaction).

        WAIT opens/extends the ``probing`` window; REDISPATCH releases a
        CAS-protected window (``probing → dispatched``) so the branch-wide
        CAS of ``createCommitOnBranch`` — not a read — refuses any
        duplicate. PARK_UNKNOWN is unreachable on this adapter.
        """
        async with self._session_factory() as session:
            decision = await settle_negative_probe(
                session,
                intent,
                now=now,
                window_seconds=self._publish_settle_seconds(),
            )
            await session.commit()
        return decision

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

        # The plan leg (A13-typed config read → plan → freeze → gate), shared
        # with the config-block recovery pass.
        await self._plan_and_publish(
            run_id,
            project_id=project_id,
            issue_number=issue_number,
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
        issue_number: int,
        issue_title: str,
        issue_description: str,
        author_username: str,
    ) -> None:
        """The planning leg: typed config read → plan → freeze → human gate.

        Shared by the fresh ``/implement`` path and the A13 config-block
        recovery pass; ends at ``waiting_approval`` (or parks the run
        ``blocked(config_…)`` BEFORE any paid call).
        """
        # v0.7 monorepo path scoping, A13: the repo's `.forge.yml`
        # ``implement.paths`` globs shape the plan prompt and are frozen into
        # the RunSpec the candidate validation enforces. The repository
        # reader duck-types the typed config reader's surface (its
        # ``project_id`` argument is accepted and ignored). The read is
        # TYPED (R14 pattern): only a provider-confirmed absence earns the
        # documented default profile; an unreadable or invalid config parks
        # the run blocked(config_…) — a read failure must never WIDEN the
        # run's scope, and nothing was paid or committed while it waits.
        config_read = await read_project_config(
            self._stack.reader, project_id, ref=self._target_branch()
        )
        if config_read.needs_block:
            await self._park_config_blocked(
                run_id,
                project_id=project_id,
                issue_number=issue_number,
                config_read=config_read,
                issue_title=issue_title,
                issue_description=issue_description,
                author_username=author_username,
            )
            return
        path_scope = list(config_read.config.implement_paths) if config_read.config else []
        # F22/R13 (A02 parity): the run's numeric budget is resolved and
        # opened BEFORE the first paid call — the class comes from the
        # harness decision compiled at plan time (ADR-0023 §2) and the class
        # names a numeric profile resolved AT FREEZE TIME; nothing
        # configured → no ceilings. The guard binds to the stack's shared
        # LLM client so the planner itself reserves.
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
        try:
            plan = await self._stack.planner.plan(
                issue_title,
                issue_description,
                flow_run_id=run_id,
                path_scope=path_scope or None,
            )
        except (LLMError, LLMResponseError) as exc:
            # (planning failure handling unchanged below)
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

        # B11: the task-aware selection reads the CURRENT plan — the
        # pre-plan selection above only reserved the planning budget; the
        # planner's proposal (its ``last_plan`` is THIS plan now) is
        # compiled into the frozen selection below, so a reused planner
        # object can never leak a PREVIOUS run's plan into this decision.
        harness_selection = self._compile_harness_selection()
        if (
            budget_limits is not None
            and harness_selection.budget_ceilings != self._budget_ceilings_of(budget_limits)
        ):
            # B11: the pre-plan budget row froze its limits at open time
            # (idempotent by run). A proposal-driven class change must not
            # freeze a spec whose ceilings the open budget cannot enforce —
            # the selection keeps the planner's choice, its ENFORCEABLE
            # ceilings stay the opened ones, and the pin is loud.
            harness_selection = replace(
                harness_selection,
                budget_ceilings=self._budget_ceilings_of(budget_limits),
                reason=(
                    f"{harness_selection.reason} "
                    "(budget pinned to the pre-plan class — idempotent budget row)"
                ).strip(),
            )

        digest = plan_digest_of(plan)
        task_digest = task_digest_of(issue_title, issue_description)
        now = datetime.now(timezone.utc)
        base_sha = await self._read_base_sha()
        plan_summary = self._plan_summary(plan)
        plan_files_hint = self._plan_files_hint()
        # A18: derive the execution profile from the TARGET repo at freeze
        # time — toolchain pins from its own lock, install strategy, honest
        # ci_contract — and freeze its digest into the spec, so the gate
        # approves the exact build/test contract the lane must run. The
        # derivation is typed-best-effort (never raises, never parks the
        # run): an unreadable lock freezes the unknown-honest record.
        profile_digest = (
            await derive_from_reader(
                self._stack.reader,
                project_id=project_id,
                ref=base_sha or self._target_branch(),
            )
        ).profile_digest

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
                        "summary": plan_summary,
                        "files_hint": plan_files_hint,
                    },
                    # R13 §4 (A02 parity): the honest enforcement record —
                    # what the frozen budget enforces on THIS lane, and which
                    # ceilings ride in the spec. Absent when no finite
                    # profile applies.
                    **(
                        {
                            "budget": {
                                "budget_class": harness_selection.budget_class,
                                "enforcement": budget_enforcement_for_backend(
                                    "ci_harness" if self._harness_workflow() else "builtin"
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
            # F14/R04 (ADR-0018 §1, A02): freeze the EXECUTABLE RunSpec at
            # plan acceptance — the same typed v3 document as the GitLab
            # path: the task text, the plan artifact, the model route, the
            # path policy, the required checks, the budgets and the
            # backend/driver. The gate binds its digest, so /go approves
            # exactly the bytes the run will execute.
            spec_document = self._build_run_spec_document(
                project_id=project_id,
                issue_number=issue_number,
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
            # A03: freeze the approved BriefEnvelope beside the spec — the
            # run's evidence carries the approved brief bytes (task title,
            # task description, plan text; each sha256-digested) bound by
            # envelope_digest = sha256 over the canonical envelope JSON
            # (run_id + task bytes + plan bytes + spec_digest). The lane
            # re-verifies the plan comment's rendered sections against the
            # dispatched digest before it renders a brief — an edited
            # comment/issue can never change the execution input silently.
            envelope = build_brief_envelope(
                run_id=run_id,
                task_title=issue_title,
                task_description=issue_description,
                plan_text=plan,
                spec_digest=spec_digest,
            )
            run.evidence = _merge_evidence(run.evidence, {"brief_envelope": envelope})
            await session.commit()

        # F22 (ADR-0018 §5): open the run's budget from the spec (idempotent
        # — a pre-paid open above already froze the limits; this only
        # backfills the spec_digest provenance, and a no-profile run opens
        # nothing).
        async with self._session_factory() as session:
            await open_budget_from_spec(
                session, run_id=run_id, spec_document=spec_document, spec_digest=spec_digest
            )
            await session.commit()

        await self._post_journaled_note(
            project_id,
            issue_number,
            self._plan_comment(
                run_id,
                plan,
                digest,
                harness_selection,
                task_title=issue_title,
                task_description=issue_description,
            ),
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
        resuming = False

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            if (
                run is None
                or run.provider != "github"
                # A07: the full subject — project as well as repo identity.
                or run.project_id != project_id
                or run.github_repo_full_name != self._repo_full_name
            ):
                logger.info("GitHub /go references unknown run %s — ignoring", run_id[:8])
                return
            if run.issue_iid != issue_number:
                logger.info(
                    "GitHub /go for run %s posted on a different issue — ignoring", run_id[:8]
                )
                return
            if run.status in _RESUMABLE_PUBLISH_STATUSES:
                # R07: the gate is consumed and a crashed worker left the run
                # mid-publish; the re-claimed command step is the recovery
                # driver. The leg below probes the publication intent before
                # any commit-API call (R11) and replays the propose
                # checkpoint — the resume never re-pays for derived work.
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
                if gate is not None and gate.consumed_at is not None:
                    resuming = True
                else:
                    logger.info(
                        "GitHub /go for run %s in %s with an unconsumed gate — ignoring",
                        run_id[:8],
                        run.status,
                    )
                    return
            elif run.status != FlowStatus.WAITING_APPROVAL.value:
                # Already advanced (or terminal) — duplicate /go delivery.
                logger.info(
                    "GitHub /go for run %s in status %s — ignoring duplicate",
                    run_id[:8],
                    run.status,
                )
                return

            if not resuming:
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

        if resuming:
            logger.info(
                "GitHub run %s found %s after a worker crash — resuming the publish leg",
                run_id[:8],
                run.status,
            )
            await self._advance_publish(run_id, project_id=project_id, issue_number=issue_number)
            return

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
                    or run.project_id != project_id
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
                # A07: the same subject scope as the bare and full-id forms —
                # project included, so a same-iid run of another project (or
                # another connection's same-named repo) never resolves here.
                runs = (
                    (
                        await session.execute(
                            select(FlowRun)
                            .where(
                                FlowRun.provider == "github",
                                FlowRun.project_id == project_id,
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
        delivery_id: str | None = None,
    ) -> None:
        """``/retry [run-id]``: operator revival of a dead run on the Actions lane.

        The exact GitLab ``handle_retry_note`` semantics (Tier 2): bare, the
        latest ``failed``/``blocked`` run for the issue; the explicit revival
        graph edge walks it to ``proposing``; one operator-granted commit
        cycle; the SAME branch re-dispatched with the terminal reason and the
        last verification evidence as the repair context.

        A11: one durable, idempotent transition — the attempt record commits
        with the CAS walk, keyed by the webhook delivery id (same id twice is
        a no-op; a different id while an attempt is in flight is refused).
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
        attempt_key = retry_delivery_key(delivery_id)
        run_id: str | None = None
        # A07: the dispatch leg below aims at the VERIFIED run's subject, read
        # back from the matched run — never the command context. The scoped
        # resolution makes the two equal; reading them from the run keeps the
        # dispatch honest even if resolution were ever widened.
        retry_project_id = 0
        retry_issue_number = 0
        rejection = ""
        status = ""
        status_reason = ""
        cycle = 1
        evidence: dict = {}
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
                retry_project_id = int(run.project_id)
                retry_issue_number = int(run.issue_iid or 0)
                # A11 delivery identity first: a redelivered /retry is a
                # no-op; a different delivery while an attempt is in flight
                # is refused before any status-based wording can mislead.
                if attempt_key is not None:
                    existing = await find_revival_attempt(
                        session, run_id=run.id, kind="retry_requested", idempotency_key=attempt_key
                    )
                    if existing is not None:
                        logger.info(
                            "GitHub /retry delivery %s already delivered for run %s — no-op",
                            delivery_id,
                            run_id[:8],
                        )
                        return
                    if await open_revival_attempt(session, run_id=run.id) is not None:
                        rejection = retry_in_flight_rejection(run.id)
                if not rejection:
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

        # One durable, idempotent transition (A11): attempt + CAS walk commit
        # atomically.
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
                        "GitHub /retry delivery redelivered for run %s (index arbiter) — no-op",
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
                await session.commit()
        except RevivalInFlight:
            await self._post_journaled_note(
                project_id,
                issue_number,
                f"🔁 {retry_in_flight_rejection(run_id)}",
                run_id,
                "retry_rejected_note",
            )
            return
        action_id = attempt.action_id

        branch = github_factory_branch(retry_issue_number, run_id)
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

        # A11: claim the dispatch (pending → dispatched) before the leg runs.
        async with self._session_factory() as session:
            claimed = await claim_attempt_dispatch(session, action_id)
            await session.commit()
        if not claimed:
            logger.warning(
                "GitHub run %s revival dispatch already claimed — standing down", run_id[:8]
            )
            return

        repair_context = build_retry_context(self._settings, status_reason, evidence)
        repair_reason = f"retry by @{author_username}: {status_reason or status}"
        try:
            await self._advance_harness(
                run_id,
                project_id=retry_project_id,
                issue_number=retry_issue_number,
                repair_context=repair_context,
                repair_reason=repair_reason,
            )
        except Exception as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        await self._complete_action(action_id, "succeeded", {"backend": "ci_harness"})

    async def evaluate_revival_recovery(self, now: datetime | None = None) -> int:
        """One recovery pass over this repo lane's stranded revival attempts (A11)."""
        return await evaluate_attempt_recovery(
            self._session_factory,
            provider="github",
            redispatch=self._redispatch_revival,
            now=now,
            log=logger,
            # B08: this service's adapters are bound to ONE repo — never
            # touch another repository's runs with them.
            repo_full_name=self._repo_full_name,
        )

    # ------------------------------------------------------------------
    # R29 operator surface around dead/stuck runs
    # ------------------------------------------------------------------

    async def handle_status(
        self,
        *,
        project_id: int,
        issue_number: int,
        note_text: str,
        author_username: str,
    ) -> None:
        """``/status [run-id]``: READ-ONLY run snapshot (R29).

        The exact GitLab ``handle_status_note`` semantics: bare, the issue's
        latest run of any state; the reply is composed from durable state
        only — no transitions, no model calls, no provider effects beyond
        the journaled reply comment.
        """
        match = STATUS_RE.search(note_text or "")
        if match is None:
            return
        if issue_number is None:
            logger.info("GitHub /status off-issue — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        async with self._session_factory() as session:
            run = await resolve_status_target(
                session,
                provider="github",
                project_id=project_id,
                issue_iid=issue_number,
                requested=requested,
                repo_full_name=self._repo_full_name,
            )
            if run is None:
                body = (
                    "## Forge — status\n\nNo forge run found on this issue yet. "
                    "Start one with `/implement`.\n\n*This is an automated message.*"
                )
                logger.info("GitHub /status on %s#%s — no run", self._repo_full_name, issue_number)
            else:
                body = format_status_reply(await collect_status_snapshot(session, run))
                run_id = run.id
        await self._post_journaled_note(
            project_id,
            issue_number,
            body,
            run_id,
            "status_note",
        )

    async def handle_why_blocked(
        self,
        *,
        project_id: int,
        issue_number: int,
        note_text: str,
        author_username: str,
    ) -> None:
        """``/why-blocked [run-id]``: READ-ONLY precise cause (R29)."""
        match = WHY_BLOCKED_RE.search(note_text or "")
        if match is None:
            return
        if issue_number is None:
            logger.info("GitHub /why-blocked off-issue — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        async with self._session_factory() as session:
            run = await resolve_status_target(
                session,
                provider="github",
                project_id=project_id,
                issue_iid=issue_number,
                requested=requested,
                repo_full_name=self._repo_full_name,
            )
            if run is None:
                body = (
                    "## Forge — why blocked\n\nNo forge run found on this issue yet. "
                    "Start one with `/implement`.\n\n*This is an automated message.*"
                )
                logger.info(
                    "GitHub /why-blocked on %s#%s — no run", self._repo_full_name, issue_number
                )
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
            issue_number,
            body,
            run_id,
            "why_blocked_note",
        )

    async def handle_reconcile(
        self,
        *,
        project_id: int,
        issue_number: int,
        note_text: str,
        author_username: str,
    ) -> None:
        """``/reconcile <run-id>``: drive the R11 recovery explicitly (R29).

        The exact GitLab ``handle_reconcile_note`` semantics: approver-gated
        like ``/retry`` (NOT a generic revival), run id REQUIRED, refuses
        runs without a publication intent, drives the existing
        ``_resolve_one_publication_intent`` probe pass and replies with the
        resolution.
        """
        match = RECONCILE_RE.search(note_text or "")
        if match is None:
            return
        if author_username not in self._approvers():
            logger.info(
                "GitHub /reconcile from @%s who is not in the GitHub approver list — ignoring",
                author_username,
            )
            return
        if issue_number is None:
            logger.info("GitHub /reconcile off-issue — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        intents = []
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
                intents = await intents_for_run(session, run.id)
        if run_id is None:
            logger.info("GitHub /reconcile references unknown run %s — ignoring", requested[:8])
            return
        if not intents:
            await self._post_journaled_note(
                project_id,
                issue_number,
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
                logger.exception("Reconcile pass failed for intent %s", intent.id[:8])
        async with self._session_factory() as session:
            resolved = [await session.get(PublicationIntent, intent.id) for intent in intents]
        await self._post_journaled_note(
            project_id,
            issue_number,
            format_reconcile_reply(run_id, [row for row in resolved if row is not None]),
            run_id,
            "reconcile_note",
        )

    async def evaluate_revival(self, now: datetime | None = None) -> None:
        """One revival pass over this repo's transiently dead runs (Tier 1)."""
        await evaluate_revivals(
            self._session_factory,
            self._settings,
            provider="github",
            redispatch=self._redispatch_revival,
            now=now,
            log=logger,
            repo_full_name=self._repo_full_name,  # B08: bound adapters, one repo
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
    # A13 config-block gate + reconciler recovery pass
    # ------------------------------------------------------------------

    async def _read_start_config(self, project_id: int) -> ConfigReadResult:
        """The typed `.forge.yml` read a run's start path scopes from (A13)."""
        return await read_project_config(self._stack.reader, project_id, ref=self._target_branch())

    async def _park_config_blocked(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_number: int,
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
                        "repo_full_name": self._repo_full_name,
                        "issue_title": issue_title,
                        "issue_description": issue_description,
                        "author_username": author_username,
                    }
                },
            )
            await session.commit()
        await self._post_journaled_note(
            project_id,
            issue_number,
            self._config_blocked_comment(run_id, reason),
            run_id,
            "config_blocked",
        )
        logger.warning(
            "GitHub run %s parked %s — no planning call was made; the reconciler retries",
            run_id[:8],
            reason,
        )

    @staticmethod
    def _config_blocked_comment(run_id: str, reason: str) -> str:
        return (
            f"Run `{run_id[:8]}` is **blocked**: {reason}\n\n"
            f"The repository's `.forge.yml` could not be read. Forge never widens a run's "
            "path scope because a config read failed — the run stays parked with no "
            "model calls and no commits until the config is readable. The reconciler "
            f"retries the read automatically; `/retry {run_id[:8]}` forces it sooner."
            "\n\n*This is an automated message.*"
        )

    async def evaluate_config_recovery(self, now: datetime | None = None) -> None:
        """One recovery pass over this repo's runs parked ``blocked(config_…)`` (A13).

        Retries the typed config read for each; a run whose config is
        readable again (or provider-confirmed absent) walks back to
        ``preflight`` through the fenced plan-restart edge and re-plans —
        with zero paid calls while it waited.
        """
        await evaluate_config_blocks(
            self._session_factory,
            provider="github",
            reread=self._read_start_config,
            replan=self._resume_config_blocked,
            log=logger,
            # B08: recover only THIS repo's runs — a provider-wide scan would
            # drive another repository's run through this repo's bound
            # reader/client (wrong config, wrong task text).
            repo_full_name=self._repo_full_name,
        )

    async def _resume_config_blocked(self, run_id: str, stash: dict) -> None:
        """Re-enter planning for a recovered config-blocked run."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            project_id = run.project_id
            issue_number = run.issue_iid
        await self._plan_and_publish(
            run_id,
            project_id=project_id,
            issue_number=issue_number or 0,
            issue_title=str(stash.get("issue_title") or ""),
            issue_description=str(stash.get("issue_description") or ""),
            author_username=str(stash.get("author_username") or ""),
        )

    # ------------------------------------------------------------------
    # Publication-intent recovery scanner (R11)
    # ------------------------------------------------------------------

    #: Run statuses where a publish leg is still in progress — the only
    #: states the scanner may advance a run from on adoption.
    _PUBLISHING_STATUSES = frozenset(
        {
            FlowStatus.PROPOSING.value,
            FlowStatus.VALIDATING.value,
            FlowStatus.COMMITTING.value,
            FlowStatus.ENSURING_DRAFT_MR.value,
        }
    )

    async def resolve_publication_intents(self, *, now: datetime | None = None) -> int:
        """One recovery pass over this repo's due publication intents (R11).

        For every OPEN intent (a crash/stall left its outcome unrecorded):
        probe the remote by identity and resolve —
        ``adopted`` (exactly one marker+parent match): the run advances to
        ``waiting_ci`` on the FOUND commit exactly as if it had published
        itself — unless the run is superseded/terminal (R10), in which case
        the intent resolves ``duplicated`` and the run is never revived;
        ``duplicated`` (head moved by someone else): intent resolved, run
        parked ``blocked`` (branch_drift contract, never force);
        ``unknown`` (≥2 matches): run parked ``blocked(unknown_outcome)``
        with an operator instruction — never guessed;
        ``redispatch`` (nothing landed, head intact): A12 — the intent
        first settles in the effect-certainty window (a negative probe is
        not proof of absence); once the window expires still-negative the
        CAS-protected same-key redispatch is released to the run's own
        probe-first publish leg — the branch-wide CAS refuses any duplicate.
        The scanner never POSTs (it holds no candidate content), it only
        backs the next probe off.
        """
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            intents = await due_intents(
                session, provider="github", repo=self._repo_full_name, now=now
            )
        resolved = 0
        for intent in intents:
            try:
                if await self._resolve_one_publication_intent(intent, now=now):
                    resolved += 1
            except Exception:
                # One broken intent must not stall the recovery pass.
                logger.exception("Publication-intent resolution failed for %s", intent.id[:8])
        return resolved

    async def _resolve_one_publication_intent(
        self, intent: PublicationIntent, *, now: datetime
    ) -> bool:
        run = await self._load_run(intent.run_id)
        if run is None:
            await self._complete_intent(
                intent.id, "duplicated", remote_result={"reason": "run_vanished"}
            )
            return True
        if run.cancel_requested or run.status in {s.value for s in TERMINAL_STATUSES}:
            # R10 superseded interplay: the publication grant is gone — a
            # landed commit is superseded evidence, never adopted-into-READY.
            await self._complete_intent(
                intent.id,
                "duplicated",
                remote_result={"reason": "run_superseded", "run_status": run.status},
            )
            await self._merge_run_evidence(
                intent.run_id,
                {
                    "superseded": {
                        "reason": "cancelled_during_publication"
                        if run.cancel_requested
                        else f"run already {run.status}",
                        "attempt_base": intent.expected_parent_oid,
                    }
                },
            )
            return True
        if intent.status == "requested":
            # mark_dispatched commits BEFORE any dispatch, so a requested
            # intent has no effect to probe — the run's own publish leg
            # (probe-first) owns it.
            return False

        verdict, hits = await self._probe_intent(intent)
        if verdict is ProbeVerdict.ADOPT:
            await self._complete_intent(
                intent.id,
                "adopted",
                provider_object_id=hits[0],
                remote_result={"sha": hits[0], "reconciled": True},
            )
            await self._adopt_committed_candidate(
                run,
                intent,
                commit_oid=hits[0],
                reconcile_note=f"adopted previous attempt's commit {hits[0][:8]}",
            )
            logger.warning(
                "Recovered publication intent %s for run %s — adopted commit %s",
                intent.id[:8],
                intent.run_id[:8],
                hits[0][:8],
            )
            return True
        if verdict is ProbeVerdict.DUPLICATED:
            await self._complete_intent(
                intent.id, "duplicated", remote_result={"branch": intent.target_ref}
            )
            if run.status in self._PUBLISHING_STATUSES:
                await self._to_terminal(
                    intent.run_id,
                    FlowStatus.BLOCKED,
                    f"branch_drift: {intent.target_ref} moved away from the intent "
                    "(reconciled by the publication-intent scanner)",
                )
            return True
        if verdict is ProbeVerdict.UNKNOWN:
            await self._complete_intent(intent.id, "unknown", remote_result={"matches": hits})
            if run.status in self._PUBLISHING_STATUSES:
                await self._to_terminal(intent.run_id, FlowStatus.BLOCKED, "commit_unknown_outcome")
                issue_number = run.issue_iid or 0
                await self._post_journaled_note(
                    int(run.project_id),
                    issue_number,
                    f"Run `{intent.run_id[:8]}` publication outcome is **unresolved** — an "
                    f"operator must inspect branch `{intent.target_ref}` and reconcile "
                    "manually; forge will not re-publish over an unknown outcome.\n\n"
                    "*This is an automated message.*",
                    intent.run_id,
                    "publish_unknown_outcome",
                )
            return True

        # REDISPATCH — A12: a negative probe proves nothing landed YET, not
        # that no first effect is in flight. Route through the effect-
        # certainty settle machine: WAIT parks/extends the ``probing``
        # window; an exhausted window on this CAS-protected adapter RELEASES
        # the same-key dispatch (``probing → dispatched``) — the branch-wide
        # CAS refuses a duplicate, so the release is inherently safe. The
        # run's own probe-first publish leg owns the POST; this scanner
        # never dispatches, it only backs the next probe off.
        decision = await self._settle_negative_probe(intent, now=now)
        if decision is SettleDecision.PARK_UNKNOWN:
            # Defensive: unreachable on a CAS-protected adapter — resolve as
            # the honest unknown rather than ever dispatching off reads.
            await self._complete_intent(intent.id, "unknown", remote_result={"matches": hits})
            if run.status in self._PUBLISHING_STATUSES:
                await self._to_terminal(intent.run_id, FlowStatus.BLOCKED, "commit_unknown_outcome")
            return False
        async with self._session_factory() as session:
            row = await session.get(PublicationIntent, intent.id)
            if row is not None:
                row.next_probe_at = now + timedelta(seconds=_INTENT_PROBE_BACKOFF_SECONDS)
                await session.commit()
        return False

    async def _adopt_committed_candidate(
        self,
        run: FlowRun,
        intent: PublicationIntent,
        *,
        commit_oid: str,
        reconcile_note: str,
    ) -> None:
        """Advance a mid-publication run onto a probe-adopted commit.

        The same walk a fresh publish leg would do (evidence, Draft PR,
        waiting_ci) — the run cannot tell a replayed walk from the original.
        A run that already moved past publishing (waiting_ci and beyond) is
        left alone: the intent is resolved, nothing else is touched.
        """
        if run.status not in self._PUBLISHING_STATUSES:
            return
        pr = await self._stack.flow.ensure_draft_pr(
            self._owner,
            self._repo,
            intent.target_ref,
            self._target_branch(),
            run.issue_iid or 0,
            run.id,
        )
        pr_number = int(pr["number"]) if pr else None
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
                        intent.run_id, status, reason=f"publication intent: {reconcile_note}"
                    )
                except InvalidTransition:
                    pass  # already past this stage — resume the walk
            run_row = await self._get_run(session, intent.run_id)
            run_row.mr_iid = pr_number
            if commit_oid not in list(run_row.candidate_shas or []):
                run_row.candidate_shas = list(run_row.candidate_shas or []) + [commit_oid]
            run_row.evidence = _merge_evidence(
                run_row.evidence,
                {
                    "published_candidate": {
                        "sha": commit_oid,
                        "base": intent.expected_parent_oid,
                        "branch": intent.target_ref,
                        "pr_number": pr_number,
                        "reconciled": True,
                    }
                },
            )
            await session.commit()

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
            # R10: bump the fence so ExecutionClaims minted before this
            # revoke are fenced out of publishing and guarded transitions.
            run.cancellation_generation = (run.cancellation_generation or 0) + 1
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

    # ------------------------------------------------------------------
    # Publication intents (R11): identity before the HTTP effect
    # ------------------------------------------------------------------

    def _intent_scope(self, run: FlowRun) -> str:
        """The logical retry scope of the run's current publication attempt."""
        return f"cycle-{run.commit_cycle or 1}"

    async def _publication_intent(
        self,
        run: FlowRun,
        *,
        branch: str,
        expected_head: str | None,
    ) -> PublicationIntent | None:
        """The run's OPEN publication intent for this branch, if any.

        An open intent means a previous attempt's outcome was never durably
        recorded — the caller must PROBE before any new dispatch (never a
        blind POST as reconciliation).
        """
        async with self._session_factory() as session:
            return await find_open_intent(
                session,
                run_id=run.id,
                provider="github",
                repo=self._repo_full_name,
                target_ref=branch,
                operation="commit",
                idempotency_scope=self._intent_scope(run),
            )

    async def _record_publication_intent(
        self,
        run: FlowRun,
        *,
        branch: str,
        expected_head: str | None,
        content_digest: str | None = None,
    ) -> PublicationIntent:
        """Create the ``requested`` intent — BEFORE any commit-API call.

        The row is committed in its own transaction (the run state walk it
        belongs to already committed); its ``operation_key`` is minted here
        exactly once and reused by every retry of this attempt.
        """
        async with self._session_factory() as session:
            intent = await record_intent(
                session,
                run_id=run.id,
                provider="github",
                repo=self._repo_full_name,
                target_ref=branch,
                idempotency_scope=self._intent_scope(run),
                operation_key=mint_operation_key(),
                commit_cycle=run.commit_cycle or 1,
                content_digest=content_digest,
                expected_parent_oid=expected_head,
                expected_head=expected_head,
            )
            await session.commit()
            return intent

    async def _probe_intent(self, intent: PublicationIntent) -> tuple[ProbeVerdict, list[str]]:
        """Classify one intent's outcome against the live remote (read-only).

        The identity tuple per the research doc: exactly one commit carrying
        this intent's ``(forge-op:<key>)`` marker whose parent list equals
        the intent-time expected parent proves the effect landed.

        A provider-confirmed 404 on the branch reads is NOT inconclusive:
        nothing can have landed on a branch that provably does not exist —
        the verdict is REDISPATCH (the run's leg re-creates the branch and
        re-dispatches with the SAME key). Any other read failure stays
        UNKNOWN — no dispatch may be derived from it.
        """
        owner, repo = self._owner, self._repo
        try:
            head = await self._stack.client.get_branch_head(owner, repo, intent.target_ref)
            commits = await self._stack.client.list_commits(owner, repo, intent.target_ref)
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                logger.info(
                    "Publication-intent probe: branch %r is confirmed absent — "
                    "nothing landed (redispatch)",
                    intent.target_ref,
                )
                return ProbeVerdict.REDISPATCH, []
            logger.exception(
                "Publication-intent probe read failed for %s@%s",
                intent.target_ref,
                self._repo_full_name,
            )
            return ProbeVerdict.UNKNOWN, []
        except Exception:
            # A failed probe read is inconclusive, not negative — no dispatch
            # may be derived from it.
            logger.exception(
                "Publication-intent probe read failed for %s@%s",
                intent.target_ref,
                self._repo_full_name,
            )
            return ProbeVerdict.UNKNOWN, []
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
        return verdict, hits

    async def _complete_intent(
        self,
        intent_id: str,
        status: str,
        *,
        provider_object_id: str | None = None,
        remote_result: dict | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await complete_intent(
                session,
                intent_id,
                status,
                provider_object_id=provider_object_id,
                remote_result=remote_result,
            )
            await session.commit()

    async def _mark_intent_dispatched(self, intent: PublicationIntent) -> None:
        async with self._session_factory() as session:
            await mark_dispatched(session, intent.id)
            await session.commit()

    async def _load_run(self, run_id: str) -> FlowRun:
        """The run row in a fresh session (intent-identity reads)."""
        async with self._session_factory() as session:
            return await self._get_run(session, run_id)

    async def _complete_intent_from_outcome(
        self, intent_id: str, outcome: GitHubPublishOutcome
    ) -> None:
        """Map a publish outcome onto the intent's terminal state.

        ``ok`` + ``adopted`` → ``adopted`` (a probe found the effect);
        ``ok`` → ``committed``; ``drift`` → ``duplicated`` (the ref moved
        away from the intent); any other failure → ``failed``.
        """
        if outcome.ok:
            await self._complete_intent(
                intent_id,
                "adopted" if outcome.adopted else "committed",
                provider_object_id=outcome.commit_oid,
                remote_result={
                    "sha": outcome.commit_oid,
                    "pr_number": outcome.pr_number,
                    "expected_head": outcome.expected_head_oid,
                },
            )
        elif outcome.drift:
            await self._complete_intent(
                intent_id, "duplicated", remote_result={"reason": outcome.reason}
            )
        else:
            await self._complete_intent(
                intent_id, "failed", remote_result={"reason": outcome.reason}
            )

    async def _advance_publish(self, run_id: str, *, project_id: int, issue_number: int) -> None:
        """One gate-approved publish cycle, ending at ``ready_for_human``.

        Harness lane (ADR-0020): the FROZEN backend is ``ci_harness`` (the
        repo was onboarded for Actions at plan acceptance) — the leg
        dispatches the harness and parks in ``waiting_harness`` — the
        reconciler drives the rest. Builtin (default): propose + publish
        synchronously as before.

        R04/A02: the frozen executable spec is THE approved input — the
        task text, the plan artifact and the model route come from it, never
        from live settings or a live issue re-read. A missing, tampered or
        legacy (v2) spec parks the run (``spec_invalid`` / ``spec_legacy``),
        never a silent fallback.
        """
        spec = await self._spec_or_block(run_id)
        if spec is None:
            return
        if spec.backend == "ci_harness":
            await self._advance_harness(run_id, project_id=project_id, issue_number=issue_number)
            return

        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
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

        # R11: resolve the publication intent BEFORE any commit-API call.
        # A previous attempt's open intent is probed by identity (marker +
        # expected parent): its landed commit is adopted, never duplicated;
        # only a proven-nothing-landed probe falls through to a dispatch —
        # with the SAME stable operation key.
        branch = github_factory_branch(issue_number, run_id)
        expected_head = base_sha or None
        run_row = await self._load_run(run_id)
        intent = await self._publication_intent(run_row, branch=branch, expected_head=expected_head)
        if intent is None:
            intent = await self._record_publication_intent(
                run_row, branch=branch, expected_head=expected_head
            )
        else:
            verdict, hits = await self._probe_intent(intent)
            if verdict is ProbeVerdict.ADOPT:
                # The live R11 case: a previous attempt LANDED (its response
                # was lost / the process stalled) — adopt it and finish the
                # leg on the found commit. Zero new commit-API calls.
                await self._complete_intent(
                    intent.id,
                    "adopted",
                    provider_object_id=hits[0],
                    remote_result={"sha": hits[0], "reconciled": True},
                )
                pr = await self._stack.flow.ensure_draft_pr(
                    self._owner,
                    self._repo,
                    branch,
                    self._target_branch(),
                    issue_number,
                    run_id,
                )
                outcome = GitHubPublishOutcome(
                    ok=True,
                    commit_oid=hits[0],
                    expected_head_oid=expected_head,
                    branch=branch,
                    pr_number=int(pr["number"]) if pr else None,
                    pr_url=pr.get("html_url") if pr else None,
                    pr_draft=bool(pr.get("draft")) if pr else None,
                    adopted=True,
                )
                await self._finish_publish_leg(
                    run_id, project_id, issue_number, outcome, plan_digest
                )
                return
            if verdict is ProbeVerdict.DUPLICATED:
                # Someone else owns the ref now — the drift contract applies:
                # the intent resolves duplicated and the run stops here, the
                # same outcome a CAS refusal would produce (minus the POST).
                await self._complete_intent(
                    intent.id, "duplicated", remote_result={"branch": branch}
                )
                await self._to_terminal(
                    run_id,
                    FlowStatus.BLOCKED,
                    f"branch_drift: {branch} moved away from the publication intent",
                )
                return
            elif verdict is ProbeVerdict.UNKNOWN:
                await self._complete_intent(intent.id, "unknown", remote_result={"matches": hits})
                await self._to_terminal(run_id, FlowStatus.BLOCKED, "commit_unknown_outcome")
                await self._post_journaled_note(
                    project_id,
                    issue_number,
                    f"Run `{run_id[:8]}` publication outcome is **unresolved**: the remote "
                    "probe found ambiguous evidence for this attempt. An operator must "
                    f"inspect branch `{branch}` and reconcile manually — forge will not "
                    "re-publish over an unknown outcome.\n\n*This is an automated message.*",
                    run_id,
                    "publish_unknown_outcome",
                )
                return
            # REDISPATCH (nothing landed, head intact) falls through to the
            # dispatch below, reusing the intent's stable operation key. A12:
            # no certainty-window wait is required on this leg — the write
            # below is a branch-wide CAS (``expectedHeadOid``), so a redispatch
            # carrying the unchanged head is INHERENTLY duplicate-safe (a slow
            # first write would move the head and the CAS would refuse the
            # duplicate). The CAS, not a read, is the exactly-once guard here.

        await self._mark_intent_dispatched(intent)
        # R04: no live issue re-read — the implementer executes the FROZEN
        # task text the approver saw, whatever the issue shows now.
        issue_title = spec.task_title
        # R07 bounded step ``propose``: the publish leg proposes through the
        # checkpoint store — a REDISPATCH re-drive (a crashed attempt whose
        # intent probe proved nothing landed) replays the persisted manifest
        # with the SAME operation key instead of calling the model again.
        expected_head = base_sha or await self._stack.client.get_branch_head(
            self._owner, self._repo, self._target_branch()
        )
        changeset = await self._propose_for_publish(
            run_id,
            issue_number=issue_number,
            issue_title=issue_title,
            plan_summary=spec.plan_summary,
            task_text=spec.task_text,
            model_route=spec.model_route,
            expected_head=expected_head,
        )

        # D04: the propose above is a LONG paid operation — a cancel that
        # landed during it revokes the grant, and the branch CAS checks the
        # EXPECTED HEAD, not this run's right to write. Re-check the grant
        # immediately before the native dispatch: cancel-before-dispatch
        # forbids the write; a cancel AFTER dispatch stays the honest
        # unknown/superseded path.
        if await self._publication_revoked(run_id):
            logger.warning(
                "GitHub run %s cancelled DURING propose — publication refused (zero native writes)",
                run_id[:8],
            )
            return

        outcome = await self._publish_candidate_run_aware(
            run_id,
            issue_number=issue_number,
            changeset=changeset,
            expected_head=expected_head or None,
            operation_key=intent.operation_key,
        )
        await self._complete_intent_from_outcome(intent.id, outcome)

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
        if getattr(outcome, "superseded", False):
            # The commit DID land, but a cancel won the publication race —
            # the publisher stamped superseded evidence on the run. Walking
            # toward ensuring_draft_mr/READY would resurrect a cancelled
            # run (R10).
            return
        await self._finish_publish_leg(run_id, project_id, issue_number, outcome, plan_digest)

    async def _propose_for_publish(
        self,
        run_id: str,
        *,
        issue_number: int,
        issue_title: str,
        plan_summary: str,
        expected_head: str | None,
        task_text: str = "",
        model_route: str = "",
    ) -> ChangeSet:
        """The builtin lane's ``propose`` step, checkpointed (R07).

        The proposal manifest is persisted under
        ``(run_id, cycle=1, step="propose", input_digest)`` the moment the
        proposer returns — before the publish mutation is scheduled — so a
        re-driven leg replays the manifest instead of re-calling the model.
        A moved base (or edited plan summary) changes the digest and
        legitimately re-proposes. R04/A02: *task_text* and *model_route*
        come from the frozen executable spec (the service passes them); the
        model sees exactly the approved input, never a live issue read.
        """
        digest = step_input_digest(
            {"base": expected_head or "", "issue": issue_number, "plan_summary": plan_summary}
        )
        recorded = await load_step_output(
            self._session_factory, run_id=run_id, step="propose", input_digest=digest
        )
        if recorded is not None:
            changeset = changeset_from_document(recorded.get("manifest"))
            if changeset is not None:
                logger.info(
                    "GitHub run %s replays its persisted proposal (%d changes) — no second "
                    "model call",
                    run_id[:8],
                    len(changeset.changes),
                )
                return changeset
        # The implementer only reads id/issue_iid/base_sha off the run (the
        # same minimal stub ``GitHubPublishFlow.publish_proposal`` passed).
        run_stub: Any = SimpleNamespace(
            id=run_id, issue_iid=issue_number, project_id=0, base_sha=expected_head or ""
        )
        changeset = await self._stack.implementer.propose(
            run_stub,
            issue_title,
            plan_summary=plan_summary,
            attempt_base=expected_head,
            task_text=task_text or None,
            model_route=model_route or None,
        )
        # CHECKPOINT FIRST: the paid proposal becomes durable before the
        # publish mutation (and its intent machinery) is scheduled.
        await record_step_output(
            self._session_factory,
            run_id=run_id,
            step="propose",
            input_digest=digest,
            output={"manifest": changeset_to_document(changeset)},
        )
        return changeset

    async def _finish_publish_leg(
        self,
        run_id: str,
        project_id: int,
        issue_number: int,
        outcome: GitHubPublishOutcome,
        plan_digest: str,
    ) -> None:
        """The shared publish-leg tail: candidate evidence → Draft PR → waiting_ci.

        Runs identically for a fresh commit and an ADOPTED previous attempt's
        commit (R11) — the run advances to ``waiting_ci`` on the found sha.
        """
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
            self._candidate_comment(outcome, plan_digest),
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
        driver / model inputs → ``waiting_harness`` with the journaled
        :class:`ActionsHandle` in the run's evidence. The reconciler polls
        from here — the wait is worker-free, like ``waiting_ci``.

        R13: an exhausted budget (or a spent wall clock) starts no new
        episode — the dispatch is the only enforcement point a
        non-intercepted lane has, so it is checked before any I/O.

        A02/R04: the workflow filename, the model input and the dispatched
        driver come from the digest-verified executable spec (ADR-0023 §6) —
        never live settings. A missing/tampered spec blocks the run
        (``spec_invalid``); a legacy v2 spec parks
        ``spec_legacy: re-approval required``. *driver* overrides the frozen
        selection for a fallback advance. Called for a fallback the run is
        already ``waiting_harness`` — it stays parked, only the handle moves
        (ADR-0004 has no waiting_harness self-loop).
        """
        # R13: dispatch-time budget gate (partial enforcement — episode count
        # and wall clock are the axes this lane can honestly enforce).
        block = await self._budget_episode_block(run_id)
        if block is not None:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, block)
            return
        spec = await self._spec_or_block(run_id)
        if spec is None:
            return
        workflow = spec.harness_workflow
        if not workflow:
            # The frozen backend says ci_harness but the document names no
            # workflow — a corrupt dispatch contract, never a live-settings
            # guess.
            await self._to_terminal(
                run_id, FlowStatus.FAILED, "backend_config: no harness workflow in the spec"
            )
            return
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            attempt_base = attempt_base_for(run)
            already_waiting = run.status == FlowStatus.WAITING_HARNESS.value
            # A03: the approved brief envelope rides the dispatch — the lane
            # re-computes the digest over the plan comment's APPROVED
            # sections against these values and fails closed on mismatch.
            # Empty on a legacy replay (run frozen pre-A03): the lane then
            # runs its loud, unenforced fallback.
            envelope_document = (run.evidence or {}).get("brief_envelope")
            envelope = envelope_document if isinstance(envelope_document, dict) else {}
            envelope_digest = str(envelope.get("envelope_digest") or "")
            spec_digest = str(run.spec_digest or "")
        if driver is None:
            driver = spec.harness_driver
        branch = github_factory_branch(issue_number, run_id)
        executor = GitHubActionsExecutor(self._stack.client, self._settings)
        handle = ActionsHandle(
            provider="github",
            owner=self._owner,
            repo=self._repo,
            workflow=workflow,
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
            plan_note_id = await self._journaled_plan_note_id(run_id)
            correlated = await executor.launch(
                handle,
                inputs={
                    "run_id": run_id,
                    # R04/A02: the model input is the route frozen in the
                    # spec — the gate approved exactly this execution shape.
                    "model": spec.harness_model,
                    # The lane renders its brief from the issue's forge plan
                    # comment (fetched read-only) — it needs the issue
                    # number, never the plan TEXT (no input size limits).
                    "issue_number": str(issue_number),
                    # R05 interim: the lane binds its brief to the EXACT
                    # approved plan comment, addressed by the id journaled
                    # when this run's plan was posted (no heuristic scan).
                    # Empty when the journal has no note (legacy replay) —
                    # the lane then falls back to the scan, unenforced.
                    "plan_note_id": str(plan_note_id) if plan_note_id else "",
                    # A03: the approved brief envelope — the lane extracts
                    # the approved task/plan sections from that comment,
                    # re-computes this digest over them (+ run id + the
                    # frozen spec digest below) and refuses the brief on
                    # mismatch ("re-approval required"). Both empty on a
                    # legacy replay → the lane's loud unenforced fallback.
                    "envelope_digest": envelope_digest,
                    "spec_digest": spec_digest,
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

    async def _journaled_plan_note_id(self, run_id: str) -> int | None:
        """The approved plan comment's note id, journaled at post time (R05).

        The plan comment is posted via :meth:`_post_journaled_note` with kind
        ``post_plan_note``; the created note id lands in that action's
        ``remote_result`` (ADR-0005 journal — the run's evidence of exactly
        which comment carries the approved plan). The lane binds its brief to
        EXACTLY that comment: dispatching the id here ends the heuristic
        plan-comment discovery in the harness lane. None (dispatch the input
        empty → the lane's loud unenforced scan) when the journal has no
        succeeded ``post_plan_note`` row for the run — a legacy replay, or a
        plan-posting whose outcome was never journaled.
        """
        async with self._session_factory() as session:
            action = (
                (
                    await session.execute(
                        select(ActionLog)
                        .where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "post_plan_note",
                            ActionLog.status == "succeeded",
                        )
                        .order_by(ActionLog.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if action is None:
            return None
        raw = (action.remote_result or {}).get("note_id")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

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

        R04/A02: the commit-cycle ceiling is the one frozen in the spec —
        a post-approval settings change cannot extend the approved budget.
        A missing/tampered/legacy spec parks the run instead of guessing.
        """
        spec = await self._spec_or_block(run_id)
        if spec is None:
            return False
        if spec.backend != "ci_harness":
            # ADR-0015/A02: the frozen backend is builtin — the run has no
            # harness repair leg, and a workflow onboarded after the freeze
            # must never upgrade it to a dispatch the gate did not approve.
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                f"quality_contract: {failure_reason} — the frozen builtin lane "
                "has no repair dispatch",
            )
            return False
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
            max_cycles = spec.commit_cycles
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
        """One reconciler pass over THIS repository's ``waiting_harness`` runs.

        NXT-31: the scan itself is repository-scoped. The outer worker pass
        (:func:`evaluate_github_waiting_harness`) groups waiting runs by
        repository and builds one bound service per repository — without
        this predicate the bound scanner selected EVERY provider run and
        service A could drive repository B's run through A's reader,
        publisher and fallback. A run without a repository subject matches
        no bound service and is left parked (it was previously processed by
        whichever repo happened to tick — the very routing defect).
        """
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            run_ids = (
                (
                    await session.execute(
                        select(FlowRun.id).where(
                            FlowRun.provider == "github",
                            FlowRun.status == FlowStatus.WAITING_HARNESS.value,
                            # NXT-31 (B08): this service's adapters are bound
                            # to ONE repository — the scan admits only that
                            # repository's runs.
                            FlowRun.github_repo_full_name == self._repo_full_name,
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

        NXT-31: the handler is repository-scoped BEFORE anything else — the
        run's subject must equal the bound service's subject, and the check
        precedes the spec load (which can write a ``blocked`` transition),
        every provider call, publication and fallback. A foreign run passed
        directly to this handler is refused without any repository read or
        write; the same agreement is re-checked against the journaled
        ActionsHandle below, so a wrong-repository handle can never route
        artifacts into this repository.

        R17 (deadline-before-I/O): the FIRST operation of every evaluation is
        a local deadline/cancel check over the journaled handle — no provider
        call is made once the harness budget is spent, so a permanently
        erroring (or silently stalled) Actions API can never hold a run past
        its ``harness_timeout``. A poll failure therefore cannot extend the
        deadline either: the deadline derives only from the journaled
        ``started_at``, never from poll outcomes.

        R04/A02: the timeout is the ``harness_timeout`` frozen in the spec;
        a missing/tampered/legacy spec parks the run instead of guessing.
        """
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return
            if run.provider != "github" or run.github_repo_full_name != self._repo_full_name:
                logger.warning(
                    "Actions harness reconcile refused run %s of %s — this service is bound "
                    "to %s (NXT-31)",
                    run_id[:8],
                    run.github_repo_full_name or "<no subject>",
                    self._repo_full_name,
                )
                return
            evidence = dict(run.evidence or {})
            project_id = run.project_id
            issue_number = run.issue_iid or 0
            cancel_requested = bool(run.cancel_requested)

        spec = await self._spec_or_block(run_id)
        if spec is None:
            return

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
        handle_subject = f"{handle.owner}/{handle.repo}"
        if handle_subject != self._repo_full_name:
            # NXT-31: run subject and service subject agree, but the
            # journaled handle points elsewhere — refuse before any poll,
            # publication or fallback can act through the wrong adapter.
            logger.warning(
                "Actions harness reconcile refused run %s — its handle targets %s, not the "
                "bound repository %s (NXT-31)",
                run_id[:8],
                handle_subject,
                self._repo_full_name,
            )
            return

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
        timeout = spec.harness_timeout
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

        R23: a failed/cancelled attempt's partial usage receipt is ingested
        FIRST, identity-arbitrated exactly once — the lane burned the tokens
        whether or not the candidate was adopted, and the fallback gate
        below must see the spend before it re-dispatches anything.
        """
        await self._record_harness_usage(run_id, outcome)
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

    async def _record_harness_usage(self, run_id: str, outcome: HarnessOutcome) -> None:
        """R23: ingest the Actions attempt's usage receipt exactly once.

        The receipt (from the candidate bundle on a publish leg, or the
        partial receipt the executor attached to a failed/cancelled outcome)
        is identity-arbitrated by ``usage_receipts`` — a repeated poll,
        crash-retry or re-download of the same artifact replays the same
        ``(run, attempt, receipt_id)`` and moves nothing; a repair
        re-dispatch (new attempt) costs again. Unknown receipts are still
        recorded — the attempt's cost lands in the unknown bucket, never
        silently as zero.
        """
        bundle = outcome.bundle
        usage = bundle.usage if bundle is not None else outcome.usage
        if usage is None:
            return
        async with self._session_factory() as session:
            _, created = await ingest_usage_receipt(
                session,
                run_id=run_id,
                usage=usage,
                model_fallback=str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or "unknown"),
            )
            await session.commit()
        if created:
            logger.info(
                "Run %s ingested harness usage receipt (attempt %s, %s)",
                run_id[:8],
                usage.attempt_id or "<unidentified>",
                usage.completeness,
            )
        else:
            logger.info(
                "Run %s re-polled harness receipt (attempt %s) — already ingested, no-op",
                run_id[:8],
                usage.attempt_id or "<unidentified>",
            )

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
            # R23: superseded still spent — the late attempt's receipt is
            # ingested (once, identity-arbitrated) even though nothing
            # publishes.
            await self._record_harness_usage(run_id, outcome)
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
            # R23: the rejected attempt's spend still lands on the ledger
            # before the repair/terminal decision below.
            await self._record_harness_usage(run_id, outcome)
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
            await self._record_harness_usage(run_id, outcome)
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "changeset_invalid: " + "; ".join(violations)
            )
            return

        if not await _fence_valid():
            logger.info("Run %s fenced out of the Actions publish — standing down", run_id[:8])
            await self._record_harness_usage(run_id, outcome)
            return

        # R11: resolve the publication intent BEFORE the commit-API call —
        # an open intent from a crashed attempt is probed and ADOPTED (its
        # landed commit becomes the candidate), never duplicated; a fresh
        # attempt persists the intent first and reuses its stable key.
        branch = github_factory_branch(issue_number, run_id)
        intent = await self._publication_intent(
            await self._load_run(run_id),
            branch=branch,
            expected_head=bundle.attempt_base_oid,
        )
        if intent is None:
            intent = await self._record_publication_intent(
                await self._load_run(run_id),
                branch=branch,
                expected_head=bundle.attempt_base_oid,
            )
        else:
            verdict, hits = await self._probe_intent(intent)
            if verdict is ProbeVerdict.ADOPT:
                await self._complete_intent(
                    intent.id,
                    "adopted",
                    provider_object_id=hits[0],
                    remote_result={"sha": hits[0], "reconciled": True},
                )
                pr = await self._stack.flow.ensure_draft_pr(
                    self._owner,
                    self._repo,
                    branch,
                    self._target_branch(),
                    issue_number,
                    run_id,
                )
                publish_outcome = GitHubPublishOutcome(
                    ok=True,
                    commit_oid=hits[0],
                    expected_head_oid=bundle.attempt_base_oid,
                    branch=branch,
                    pr_number=int(pr["number"]) if pr else None,
                    pr_url=pr.get("html_url") if pr else None,
                    pr_draft=bool(pr.get("draft")) if pr else None,
                    adopted=True,
                )
                logger.warning(
                    "Run %s adopting previous attempt's commit %s (intent %s)",
                    run_id[:8],
                    hits[0][:8],
                    intent.id[:8],
                )
                await self._record_harness_usage(run_id, outcome)
                await self._finish_harness_publish_leg(
                    run_id, project_id, issue_number, publish_outcome, plan_digest, handle
                )
                return
            if verdict is ProbeVerdict.DUPLICATED:
                await self._complete_intent(
                    intent.id, "duplicated", remote_result={"branch": branch}
                )
                await self._to_terminal(
                    run_id,
                    FlowStatus.BLOCKED,
                    f"branch_drift: {branch} moved away from the publication intent",
                )
                return
            elif verdict is ProbeVerdict.UNKNOWN:
                await self._complete_intent(intent.id, "unknown", remote_result={"matches": hits})
                await self._to_terminal(run_id, FlowStatus.BLOCKED, "commit_unknown_outcome")
                await self._post_journaled_note(
                    project_id,
                    issue_number,
                    f"Run `{run_id[:8]}` publication outcome is **unresolved** — an "
                    "operator must inspect branch "
                    f"`{branch}` and reconcile manually.\n\n*This is an automated message.*",
                    run_id,
                    "publish_unknown_outcome",
                )
                return
            # REDISPATCH falls through to the CAS dispatch below (A12: the
            # branch-wide CAS makes the same-key redispatch inherently
            # duplicate-safe on this leg — no certainty-window wait needed).

        await self._mark_intent_dispatched(intent)
        # R23: the receipt records the LANE's spend, not the publish's
        # outcome — ingest it before the CAS write so a failed/unknown
        # publish still leaves the attempt's cost on the ledger.
        await self._record_harness_usage(run_id, outcome)
        publish_outcome = await self._publish_candidate_run_aware(
            run_id,
            issue_number=issue_number,
            changeset=changeset,
            expected_head=bundle.attempt_base_oid,
            operation_key=intent.operation_key,
        )
        await self._complete_intent_from_outcome(intent.id, publish_outcome)
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
        await self._finish_harness_publish_leg(
            run_id, project_id, issue_number, publish_outcome, plan_digest, handle
        )

    async def _publish_candidate_run_aware(
        self,
        run_id: str,
        *,
        issue_number: int,
        changeset: "ChangeSet",
        expected_head: str | None,
        operation_key: str | None,
    ) -> "GitHubPublishOutcome":
        """D11: the RUN-AWARE publication entry — the only way this service
        publishes. Loads the approved policy by run identity (the frozen
        spec's allowed_paths — D03), re-checks the grant immediately before
        dispatch (D04), and hands the validated bundle to the transport.
        The bridge is never called with a caller-supplied scope on this
        path: ``None`` may not mean whole-repository here."""
        allowed_paths = await self._read_spec_allowed_paths(run_id)
        if await self._publication_revoked(run_id):
            from forge.integrations.github_flow import GitHubPublishOutcome as _Outcome

            return _Outcome(
                ok=False,
                reason="publication_refused: run cancelled before dispatch",
                expected_head_oid=expected_head or "",
                branch=github_factory_branch(issue_number, run_id),
            )

        async def _final_boundary_guard() -> bool:
            """FND-02: re-check the grant at the NATATIVE-effect boundary —
            after the bridge's awaited reads (branch head, blob hydration)
            and branch setup, immediately before the commit-API call."""
            if await self._publication_revoked(run_id):
                logger.warning(
                    "GitHub run %s cancelled during publish reads — native "
                    "write refused at the final boundary",
                    run_id[:8],
                )
                return False
            return True

        return await self._stack.flow.publish_changeset(
            owner=self._owner,
            repo=self._repo,
            issue_number=issue_number,
            run_id=run_id,
            changeset=changeset,
            base_branch=self._target_branch(),
            expected_head=expected_head,
            operation_key=operation_key,
            allowed_paths=allowed_paths,
            pre_dispatch_guard=_final_boundary_guard,
        )

    async def _finish_harness_publish_leg(
        self,
        run_id: str,
        project_id: int,
        issue_number: int,
        publish_outcome: GitHubPublishOutcome,
        plan_digest: str,
        handle: ActionsHandle,
    ) -> None:
        """The shared Actions-candidate tail: evidence → Draft PR → waiting_ci.

        Runs identically for a fresh commit and an ADOPTED previous attempt's
        commit (R11).
        """
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
            self._candidate_comment(publish_outcome, plan_digest),
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
        """One verification pass over a waiting_ci run (R02, A01).

        The candidate's PR checks are an independent gate judged by the
        POSITIVE-PROOF contract (:mod:`forge.runs.verification`): pending →
        keep waiting (bounded); a conclusion that blames the change →
        ADR-0008 classification (repair if budget remains, else blocked);
        cancelled/timed_out → infrastructure (blocked, NEVER repaired); the
        required checks (the FROZEN spec list) absent/skipped/neutral → an
        honest ``unknown`` verdict that keeps waiting — a green optional
        workflow never substitutes; every required check proven successful
        → review. NO checks at all → review as honestly **unverified**
        (evidence records it; the ready reason says so — never presented as
        verified).

        A01 identity rules: the harness lane is excluded by workflow
        PATH/IDENTITY (the spec-frozen FILENAME on the run's ``path``),
        never by display name; the verdict binds to the head sha the
        PROVIDER verified, and ``_review_and_ready`` re-reads the actual PR
        head and the cancellation grant before any READY.

        R17 (deadline-before-I/O): the verification budget and the cancel
        grant are evaluated LOCALLY before the provider is touched — a
        permanently erroring checks API can keep the run waiting only up to
        ``FORGE_VERIFICATION_TIMEOUT_SECONDS``, never past it.

        R04/A02: the frozen executable spec is read digest-verified on every
        pass — it names the harness workflow that is execution (excluded
        from the verification surface) and carries the required-checks
        contract this gate enforces. A missing/tampered spec blocks the run
        (``spec_invalid``); a legacy v2 spec parks
        ``spec_legacy: re-approval required``.

        ADR-0027 slice 2: the verdict semantics are the ONE shared
        ObserveVerification use case (:mod:`forge.runs.usecases`); this
        adapter fetches the native evidence, normalizes it (lane exclusion,
        newest-attempt ordering, conclusion vocabulary) and applies the
        decision with GitHub's situational wording.
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
            evidence = dict(run.evidence or {})
            cancel_requested = bool(run.cancel_requested)

        spec = await self._spec_or_block(run_id)
        if spec is None:
            return

        # B01: the deadline anchors to the verification EPOCH — the moment
        # waiting began for THIS candidate — never to ``updated_at`` (every
        # observation merges evidence and slides ``updated_at``; a poll
        # every 15s extended the 1800s deadline forever). A new candidate
        # starts a new epoch; repeated observations never touch it.
        epoch, epoch_changed = verification_epoch(evidence, candidate_sha, now)
        if epoch_changed:
            await self._merge_run_evidence(run_id, {"verification_epoch": epoch})
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
        started = epoch_started_at(epoch)
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
        # A01: the harness lane itself is execution, not verification —
        # excluded by workflow IDENTITY (the spec-frozen FILENAME matched on
        # the run's `path`), never by the mutable display name.
        harness_workflow = spec.harness_workflow
        checks = [r for r in runs if not _harness_lane_run(r, harness_workflow)]
        # The adapter-normalized input (empty observations = the no-CI form).
        observations: dict[str, str | None]
        surface: tuple[dict, ...]
        subject_head_oid: str
        # C01: names whose display name matched MULTIPLE distinct workflow
        # identities with DISAGREEING conclusions — built by the collapse
        # below (has_pending too: it is decided AFTER the authoritative
        # occurrences are chosen, over the proof set only).
        ambiguous_checks: dict[str, tuple[str, ...]] = {}
        has_pending = False

        if not checks:
            # RACE GUARD: a just-opened PR's checks take a few seconds to
            # register (LIVE-found on the dogfood cycle: the verifier polled
            # before ci.yml had started and wrongly concluded no-CI). Hold
            # the run for a grace window before declaring not_configured.
            # `or 120` would turn a deliberate 0 into the default — check None only.
            _raw = getattr(self._settings, "FORGE_VERIFICATION_GRACE_SECONDS", None)
            grace = 120 if _raw is None else int(_raw)

            started = epoch_started_at(epoch)  # B01: epoch, not updated_at
            now_aware = as_aware_utc(now)
            if started is not None and (now_aware - started).total_seconds() < grace:
                return  # keep waiting — checks may still register
            # No independent CI configured on this repo — fall through with
            # empty observations: the use case records the honest
            # not_configured verdict (R02) and walks the run to review.
            logger.info("No CI checks configured for %s — review as unverified", run_id[:8])
            observations = {}
            surface = ()
            subject_head_oid = ""
        else:
            # A01 positive-proof normalization, B02 authority rules: the
            # authoritative occurrence of each check is its NEWEST RUN
            # (never its newest ATTEMPT across runs), and checks are keyed
            # by workflow IDENTITY — display names collide.
            by_identity: dict[str, Mapping[str, Any]] = {}
            for workflow_run in sorted(checks, key=_run_order_key, reverse=True):
                by_identity.setdefault(_workflow_identity(workflow_run), workflow_run)
            # identities ordered newest-first by their authoritative run —
            # API response order never decides (B02).
            ordered = sorted(
                (dict(run) for run in by_identity.values()), key=_run_order_key, reverse=True
            )
            # C01: collapse to names for the verdict — but a name claimed by
            # several identities with DISAGREEING conclusions is AMBIGUOUS:
            # which workflow is "the check" cannot be decided by run number.
            name_conclusions: dict[str, dict[str, str | None]] = {}
            for workflow_run in ordered:
                name = str(workflow_run.get("name") or "check")
                conclusion = str(workflow_run.get("conclusion") or "") or None
                name_conclusions.setdefault(name, {})[_workflow_identity(workflow_run)] = conclusion
            for name, by_ident in name_conclusions.items():
                distinct = {c for c in by_ident.values()}
                if len(distinct) > 1:
                    ambiguous_checks[name] = tuple(sorted({str(c) for c in distinct}))

            # C01: pending is decided over the AUTHORITATIVE occurrences and
            # the PROOF SET (the frozen required names; all names when no
            # contract was frozen) — a stale or optional pending run no
            # longer holds completed required checks hostage.
            proof_names = frozenset(spec.required_jobs) if spec.required_jobs else None
            for workflow_run in ordered:
                if (workflow_run.get("status") or "") == "completed":
                    continue
                name = str(workflow_run.get("name") or "check")
                if proof_names is None or name in proof_names:
                    has_pending = True
                    break

            if has_pending:
                # Bounded (R17): the deadline itself is enforced pre-I/O above —
                # a checks API that never answers cannot hold the run forever.
                return

            observations = {}  # typed at the branch head above

            for workflow_run in ordered:
                observations.setdefault(
                    str(workflow_run.get("name") or "check"),
                    str(workflow_run.get("conclusion") or "") or None,
                )
            surface = tuple(
                {
                    "name": str(workflow_run.get("name") or "check"),
                    "conclusion": str(workflow_run.get("conclusion") or "") or None,
                    # A01/B02: the FULL check identity rides the surface — the
                    # native run id, the workflow identity and the attempt that
                    # produced this conclusion (the verified sha itself is the
                    # fragment-level ``tested_oid``).
                    "workflow": _workflow_identity(workflow_run),
                    **({"run_id": _run_int(workflow_run, "id")} if workflow_run.get("id") else {}),
                    **(
                        {"attempt": _run_int(workflow_run, "run_attempt")}
                        if workflow_run.get("run_attempt")
                        else {}
                    ),
                }
                for workflow_run in ordered
            )
            subject_head_oid = str(ordered[0].get("head_sha") or "")

        # ADR-0027 slice 2: the verdict semantics are the ONE shared
        # ObserveVerification use case (forge.runs.usecases) — the adapter
        # supplied the normalized observations/surface and GitHub's
        # conclusion vocabulary; the required-checks contract rides frozen
        # in the spec.
        decision = observe_verification(
            spec=spec,
            provider=PRODUCER_GITHUB_CHECKS,
            observations=observations,
            candidate_sha=candidate_sha,
            subject_head_oid=subject_head_oid,
            surface=surface,
            pending=has_pending,
            success_conclusions=GITHUB_SUCCESS_CONCLUSIONS,
            code_failure_conclusions=GITHUB_CODE_FAILURE_CONCLUSIONS,
            infra_conclusions=GITHUB_INFRA_CONCLUSIONS,
            waived_conclusions=frozenset(spec.waived_conclusions),  # B06: frozen at approval
            ambiguous_checks=ambiguous_checks or None,  # C01: name collisions
            now=now,
        )

        if decision.verdict is not None:
            # ADR-0027: the unified R02 evidence shape (one key set on every
            # provider — ``tested_oid`` is the sha the provider verified).
            await self._merge_run_evidence(run_id, {"verification": decision.evidence()})

        if decision.outcome == OUTCOME_WAIT:
            if decision.verdict is not None:
                logger.info(
                    "Run %s required checks unproven (%s) — keeping it waiting",
                    run_id[:8],
                    decision.verdict.summary,
                )
            # A pending/unproven run keeps waiting (the R17 deadline is the
            # bound); the recorded unknown verdict is never a green check.
            return

        if decision.outcome == OUTCOME_BLOCK:
            # A cancelled/timed_out workflow run is evidence the EXECUTION
            # died — infrastructure, never a code failure. The repair budget
            # is for code failures only; the run blocks visibly instead.
            names = ", ".join(decision.failing)
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                f"{VERIFICATION_INFRA_REASON}: checks {names} were cancelled or timed out "
                "— infrastructure, not a code failure (no repair)",
            )
            return

        if decision.outcome == OUTCOME_REPAIR:
            # ADR-0008: only a conclusion that blames the change drives the
            # bounded repair loop (required checks prove, optional ones
            # neither block nor substitute — A01).
            names = ", ".join(decision.failing)
            await self._begin_repair(
                run_id,
                project_id=run.project_id,
                issue_number=issue_number,
                failure_kind="code",
                failure_reason=f"checks failed ({names})",
            )
            return

        # OUTCOME_REVIEW: the gate concluded — evaluating_ci, then the review
        # leg (verified, or honestly unverified when no CI is configured).
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(
                run_id,
                FlowStatus.EVALUATING_CI,
                reason=decision.reason,
            )
            await session.commit()
        await self._review_and_ready(
            run_id,
            project_id=run.project_id,
            issue_number=issue_number,
            pr_number=run.mr_iid or 0,
            candidate_sha=candidate_sha,
            base_sha=run.base_sha or "",
            verified=decision.verified,
            verification_evidence=decision.evidence(),
        )

    async def resume_verification(self, run_id: str) -> None:
        """Re-drive a run stranded in ``evaluating_ci``/``reviewing`` (R07).

        A crashed verification pass used to strand the run: the scanner only
        picked ``waiting_ci`` up. The resume walks from the run's PERSISTED
        evidence — the checks verdict (bound to the candidate sha) and the
        review, which ``_review_and_ready`` replays without a second model
        call. A crash before either result was recorded simply lets that
        step run its first (and only) pass here.
        """
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return
            if run.status not in (FlowStatus.EVALUATING_CI.value, FlowStatus.REVIEWING.value):
                return  # moved on/cancelled elsewhere — superseded, never revived
            candidate_shas = list(run.candidate_shas or [])
            candidate_sha = candidate_shas[-1] if candidate_shas else (run.base_sha or "")
            verification = dict((run.evidence or {}).get("verification") or {})
            cancel_requested = bool(run.cancel_requested)
            project_id = run.project_id
            issue_number = run.issue_iid or 0
        if cancel_requested:
            logger.info("GitHub run %s cancelled — stranded verification stood down", run_id[:8])
            return
        if not candidate_sha:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "verification without a candidate sha"
            )
            return
        # R02: the recorded verdict counts only when bound to THIS candidate
        # (the shared reading, ADR-0027 — both evidence key spellings
        # tolerated for pre-consolidation rows).
        verified = verified_verdict(verification, candidate_sha)
        await self._review_and_ready(
            run_id,
            project_id=project_id,
            issue_number=issue_number,
            pr_number=run.mr_iid or 0,
            candidate_sha=candidate_sha,
            base_sha=run.base_sha or "",
            verified=verified,
            verification_evidence=verification,
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
        verification_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        """Reviewing → ready_for_human.

        `verified=False` (no independent CI configured) is honest: the run
        still reaches the human, but the reason and evidence say
        `unverified` instead of implying checks passed (R02). The reason
        wording and the finalization iron checks come from
        :mod:`forge.runs.consistency` (ADR-0027) — the GitLab leg's one
        source, not a GitHub fork of it.

        R04/A02: the review brief reads the FROZEN task title and plan
        artifact from the executable spec — never a live issue read. A
        missing/tampered/legacy spec parks the run instead of guessing.
        """
        spec = await self._spec_or_block(run_id)
        if spec is None:
            return
        try:
            async with self._session_factory() as session:
                controller = Controller(session)
                await controller.transition(
                    run_id, FlowStatus.REVIEWING, reason="readonly review of the PR diff"
                )
                await session.commit()
        except InvalidTransition:
            # R07: a crashed pass already made the move — resume the leg.
            pass

        # R07 bounded step ``review``: a review already persisted for THIS
        # candidate sha is replayed — the model runs exactly once per
        # (run, candidate). A different sha legitimately re-reviews.
        plan_summary = spec.plan_summary
        issue_title = spec.task_title
        stored = await self._read_review_evidence(run_id)
        if (
            isinstance(stored, dict)
            and stored.get("sha") == candidate_sha
            and str(stored.get("verdict") or "")
        ):
            verdict = str(stored.get("verdict") or "")
            summary = str(stored.get("summary") or "")
            raw_findings = stored.get("findings")
            findings = [
                {
                    "severity": str(f.get("severity")),
                    "file": str(f.get("file")),
                    "note": str(f.get("note")),
                }
                for f in (raw_findings or [])
                if isinstance(f, dict)
            ]
            logger.info(
                "GitHub run %s replays its persisted review of %s — no second model call",
                run_id[:8],
                candidate_sha[:8],
            )
        else:
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
            stored = await self._read_review_evidence(run_id)

        # Self-check: the recorded review must be bound to the candidate sha.
        if stored is None or stored.get("sha") != candidate_sha:
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                "review_sha_mismatch: review is not bound to the candidate sha (ADR-0008)",
            )
            return

        # A01 (F19 parity with the GitLab leg): fresh-head + cancellation
        # re-check BEFORE verified-ready. The LLM review takes time; a human
        # push (or a cancel) that lands during it invalidates the
        # candidate-specific result — the evidence becomes superseded and
        # the run never reaches READY on a stale candidate.
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None or run.status != FlowStatus.REVIEWING.value:
                return  # moved on/cancelled elsewhere — superseded, never revived
            if run.cancel_requested:
                logger.info("Run %s cancelled during review — the leg stands down", run_id[:8])
                return
        observed_head, enforceable = await self._observed_candidate_head(run_id, issue_number)
        if not enforceable:
            # B07: an unreadable head must be VISIBLE, not silently dropped —
            # the ready evidence records freshness_unknown (the verdict itself
            # is already provider-proven for the exact sha; the divergence
            # from the GitLab leg's fail-closed read is deliberate, above).
            async with self._session_factory() as session:
                run = await self._get_run(session, run_id)
                prior = dict((run.evidence or {}).get("verification") or {})
            prior["freshness"] = "unknown"
            prior["summary"] = (
                str(prior.get("summary") or "")
                + " | freshness_unknown: the PR head could not be re-read"
            ).strip(" |")
            await self._merge_run_evidence(run_id, {"verification": prior})
        if enforceable and observed_head != candidate_sha:
            await self._merge_run_evidence(
                run_id,
                {
                    "verification": ready_evidence(
                        False,
                        observed_head or "unknown",
                        PRODUCER_GITHUB_CHECKS,
                        summary="superseded: the PR head moved past the reviewed candidate",
                        status="unknown",
                    )
                },
            )
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                "candidate_drift_after_review: PR head moved past the reviewed candidate",
            )
            return

        verified = bool(verified)
        # ADR-0027: one reason source and the iron finalization checks,
        # shared with the GitLab/Azure legs (forge.runs.consistency).
        reason = ready_reason(verified, verdict, UNVERIFIED_DETAIL)
        assert_ready_invariants(
            FlowStatus.READY_FOR_HUMAN.value,
            {"verification": dict(verification_evidence or {})},
            candidate_sha,
            reviewed_sha=str((stored or {}).get("sha") or ""),
            reason=reason,
        )
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.READY_FOR_HUMAN, reason=reason)
            await session.commit()
        logger.info("GitHub run %s is ready_for_human at %s", run_id[:8], candidate_sha[:8])

    async def _observed_candidate_head(
        self, run_id: str, issue_number: int
    ) -> tuple[str | None, bool]:
        """The candidate branch's ACTUAL head oid, re-read from the provider.

        A01: the freshness check compares the REVIEWED candidate against the
        PR head (the deliverable a human push moves); when no open PR
        exists, the branch head is the fallback signal. Returns
        ``(observed_oid, enforceable)`` — ``enforceable=False`` means the
        provider could not answer (no PR, gone branch, transport error), and
        the freshness check NEVER blocks on its own read failures: the
        verification verdict above is already provider-proven for the exact
        sha, and a transient read must not veto it (the divergence from the
        GitLab leg's fail-closed read is deliberate — its branch always
        exists; the GitHub deliverable can be legitimately absent).
        """
        branch = github_factory_branch(issue_number, run_id)
        try:
            pr = await self._stack.client.get_pr_by_head(self._owner, self._repo, branch)
        except GitHubAPIError as exc:
            logger.warning(
                "Run %s PR head read failed (%s) — freshness unconfirmable", run_id[:8], exc
            )
            return None, False
        if pr is not None:
            return str((pr.get("head") or {}).get("sha") or ""), True
        try:
            head = await self._stack.client.get_branch_head(self._owner, self._repo, branch)
        except GitHubAPIError as exc:
            logger.warning(
                "Run %s branch head read failed (%s) — freshness unconfirmable",
                run_id[:8],
                exc,
            )
            return None, False
        return head, True

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
        set is validated there.

        R31/A02 (GitLab parity): the compilable lanes are the project's
        available-driver manifest (:func:`resolve_available_drivers`; unset
        — the shipped driver set, exactly what the compiler was handed
        before the manifest existed), so a driver the project did not
        onboard is never selected — not by the preference, not by the
        planner's proposal. R31 §5: the planner's structured proposal
        ({"harness", "budget_class", "reason"}) is honored when the planner
        output carries one (``last_plan``) — policy-constrained ranking
        inside preference ∩ available.

        R13/A02: the budget class's numeric profile resolves AT FREEZE TIME
        and rides ON the selection — the gate approves exactly these
        ceilings. ``None`` (no finite profile) attaches nothing.
        """
        preference = resolve_preference(self._config, self._settings)
        workflow = self._harness_workflow()
        validate_preference(preference, self._harness_driver() if workflow else None)
        # D06: None (no manifest anywhere) widens to the shipped legacy set;
        # a DECLARED set — even empty — is the boundary (C06 raises on a
        # disjoint configured driver below).
        _resolved = resolve_available_drivers(self._config, self._settings)
        available = _resolved if _resolved is not None else set(SHIPPED_DRIVERS)
        selection = compile_harness_selection(
            preference,
            f"ci_harness:{self._harness_driver()}" if workflow else "builtin",
            available,
            self._planner_harness_proposal(),
        )
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

    @staticmethod
    def _budget_ceilings_of(limits) -> "BudgetCeilings":
        from forge.runs.harness_selection import BudgetCeilings

        return BudgetCeilings(
            max_calls=limits.max_calls,
            max_tokens=limits.max_tokens,
            wallclock_s=limits.wallclock_s,
        )

    def _planner_harness_proposal(self) -> dict | None:
        """R31: the planner's optional harness proposal, leniently read.

        Mirrors the GitLab ``_planner_harness_proposal`` discipline: the
        stack planner's ``last_plan`` may carry ``harness`` /
        ``budget_class`` / ``reason``; anything missing, non-string or empty
        yields no proposal. The planner is never the authority — every field
        is re-validated by :func:`compile_harness_selection`.
        """
        last_plan = getattr(self._stack.planner, "last_plan", None)
        if not isinstance(last_plan, dict):
            return None
        proposal: dict[str, str] = {}
        for key in ("harness", "budget_class", "reason"):
            value = str(last_plan.get(key) or "").strip()
            if value:
                proposal[key] = value
        return proposal or None

    def _budget_profiles(self) -> dict[str, dict[str, Any]]:
        """The configured numeric budget profiles (R13): forge.yml first
        (``budget_profiles:``), else the FORGE_BUDGET_PROFILES JSON — the
        same precedence as the GitLab lane."""
        from_config = self._config.budget_profiles
        if from_config:
            return from_config
        return parse_budget_profiles(
            str(getattr(self._settings, "FORGE_BUDGET_PROFILES", "") or "")
        )

    def _limits_of_selection(self, selection: HarnessSelection) -> BudgetLimits | None:
        """C02: the selection's RESOLVED ceilings as the canonical limits.

        The compile/pin path already resolved the class profile (and pinned
        it to the opened RunBudget when the planner moved the class) —
        re-resolving by class NAME here is exactly how the spec and the
        open budget could disagree. A selection without ceilings (no
        finite profile) freezes none.
        """
        ceilings = selection.budget_ceilings
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

    async def _apply_run_budget(self, run_id: str) -> None:
        """Bind the run's budget guard to the stack's factory agents (F22).

        The stack's agents share one ``LLMClient``; the budget lives in the
        database and is re-loaded per execution leg. ``None`` (no budget row
        — unlimited run) clears any previous binding. Agents without an
        ``LLMClient`` (stubs) are skipped.
        """
        guard = await load_budget_guard(self._session_factory, run_id)
        for agent in (self._stack.planner, self._stack.implementer, self._stack.reviewer):
            client = getattr(agent, "_llm", None)
            if client is not None and hasattr(client, "set_budget"):
                client.set_budget(guard)

    async def _budget_episode_block(
        self, run_id: str, *, now: datetime | None = None
    ) -> str | None:
        """R13: why no new work may start against this run's budget, or None.

        The dispatch-time gate for the harness lane (A02 parity with the
        GitLab lane): its model calls happen inside the CI job forge cannot
        intercept, so the wall clock and the start of new episodes are
        enforced HERE, at the dispatch boundary.
        """
        async with self._session_factory() as session:
            block = await budget_block_reason(session, run_id, now=now)
            if block is not None:
                # A wall-clock expiry flips the budget exhausted inside this
                # session — commit so the stop is durable and visible.
                await session.commit()
            return block

    async def _load_executable_spec(self, run_id: str) -> ExecutableRunSpec:
        """The digest-verified executable spec for *run* (R04, A02 parity).

        The one consumption read every post-approval leg shares: it
        re-computes the canonical digest of the stored document and checks
        it against both the row and the digest the gate froze into
        ``run.spec_digest``. A missing, tampered or corrupt spec raises
        :class:`SpecInvalid`; a legacy (pre-v3) row raises
        :class:`SpecLegacy` (A02 policy: re-approval required, never a
        silent v3 re-interpretation) — callers park the run either way;
        there is no fallback to live settings.
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

    async def _spec_or_block(self, run_id: str) -> ExecutableRunSpec | None:
        """The verified spec, or the run parked and ``None`` (A02 policy).

        ``blocked(spec_legacy: re-approval required)`` for a pre-executable
        stored spec; ``blocked(spec_invalid: ...)`` for a missing/tampered
        one. Never falls back to live settings.
        """
        try:
            return await self._load_executable_spec(run_id)
        except SpecLegacy as exc:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"spec_legacy: {exc}")
        except SpecInvalid as exc:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"spec_invalid: {exc}")
        return None

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
        """The immutable, EXECUTABLE RunSpec document (F14, R04, A02).

        The same typed v3 document the GitLab lane freezes
        (:class:`~forge.runs.spec.ExecutableRunSpec`): the task text, the
        plan artifact, the model route, the tool/path policy, the required
        checks (A01 consumes the proof semantics; the REQUIRED list is
        frozen now), the budgets (lifecycle limits always; the R13 numeric
        ceilings when a finite profile resolves) and the backend/driver.
        ``harness_workflow`` (ADR-0020 §3) freezes the Actions dispatch
        contract so a spec change means a different harness run.
        ``allowed_paths`` (v0.7 monorepo scoping) is present only for scoped
        runs. Post-approval legs read the stored document through
        :meth:`_load_executable_spec` (digest-verified on every read), never
        live Settings. A13: ``config_read`` freezes the path scope's
        provenance (status, ref, content digest) so a restart validates
        against the approved snapshot instead of the live file. A18:
        ``profile_digest`` freezes the execution profile — the toolchain
        pins, install strategy and honest ci_contract derived from the
        target repo (forge.runs.execution_profile) — so the gate approves
        the exact build/test contract the lane must run.
        """
        selection = harness_selection or self._compile_harness_selection()
        workflow = self._harness_workflow()
        backend = "ci_harness" if workflow else "builtin"
        # R13/A02/C02: the budget's numeric profile is resolved AT FREEZE
        # TIME and stored IN the spec — the gate approves exactly these
        # ceilings and the honest enforcement level of this lane. ``None``
        # freezes no ceiling fields at all (byte-compatible). C02: the
        # CANONICAL numbers are the selection's RESOLVED ceilings (pinned to
        # the already-opened RunBudget when the planner moved the class) —
        # never re-resolved from the class NAME here.
        limits = self._limits_of_selection(selection)
        enforcement = budget_enforcement_for_backend(backend) if limits is not None else ""
        spec = ExecutableRunSpec.freeze(
            provider="github",
            project_id=project_id,
            issue_iid=issue_number,
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
            harness_workflow=workflow,
            config_status=str(config_read.provenance_status) if config_read else "",
            config_ref=config_read.ref if config_read else "",
            config_sha256=config_read.content_sha256 if config_read else "",
            profile_digest=profile_digest,
        )
        return spec.to_document()

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

    def _plan_comment(
        self,
        run_id: str,
        plan: str,
        digest: str,
        harness_selection: HarnessSelection,
        *,
        task_title: str = "",
        task_description: str = "",
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
        # A03: the comment is the human-readable representation of the
        # approved bytes — the task + plan sections ride between machine
        # markers (invisible on GitHub) so the lane can extract the EXACT
        # approved spans and re-verify them against the frozen envelope
        # digest before rendering the brief.
        sections = render_approved_sections(
            task_title=task_title,
            task_description=task_description,
            plan_text=plan,
        )
        return (
            f"## Forge plan — run `{run_id[:8]}`\n\n"
            f"{sections}\n"
            f"{implementation}\n"
            "---\n\n"
            f"**Plan digest:** `{digest}`\n\n"
            f"Approve this exact plan by commenting `{mention} /go {run_id}`.\n\n"
            f"Approvers: {approver_note}.\n\n"
            "*This is an automated message.*"
        )

    @staticmethod
    def _candidate_comment(outcome, plan_digest: str) -> str:
        """B13: the post-publish status projection — the candidate is
        published and verification is PENDING; the run is NOT ready for
        human review yet (the verified/unverified ready note follows after
        verification + review)."""
        pr_url = outcome.pr_url or "(PR url unavailable)"
        return (
            "## Forge candidate published\n\n"
            f"- **Pull request:** {pr_url}\n"
            f"- **Candidate commit:** `{outcome.commit_oid}`\n"
            f"- **Plan digest:** `{plan_digest}`\n"
            "- **Status:** verification pending — an update follows with the "
            "verification evidence and the review\n\n"
            "*This is an automated message.*"
        )

    async def _post_journaled_note(
        self, project_id: int, issue_number: int, body: str, run_id: str | None, kind: str
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
    if command not in {
        "start_run",
        "go",
        "cancel",
        "retry",
        "issue_edited",
        "unlabeled",
        "status",
        "why_blocked",
        "reconcile",
    }:
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
                # A11: the comment id is the delivery identity — the same
                # redelivered webhook dedupes to a no-op at the attempt.
                delivery_id=str(metadata.get("note_id") or "") or None,
            )
        elif command == "status":
            await service.handle_status(
                project_id=project_id,
                issue_number=issue_number,
                note_text=note_text,
                author_username=author_username,
            )
        elif command == "why_blocked":
            await service.handle_why_blocked(
                project_id=project_id,
                issue_number=issue_number,
                note_text=note_text,
                author_username=author_username,
            )
        elif command == "reconcile":
            await service.handle_reconcile(
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
        elif command == "unlabeled":
            await service.handle_label_removed(
                project_id=project_id,
                issue_number=issue_number,
                author_username=author_username,
            )
        else:
            logger.warning("Unknown GitHub run command %r — ignoring", command)
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
    """One verification pass over every GitHub run parked in `waiting_ci` —
    plus the runs a crashed pass stranded in `evaluating_ci`/`reviewing`
    (R07: resumed from the persisted evidence; the review replay never
    calls the model a second time).

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
                        FlowRun.status.in_(
                            [
                                FlowStatus.WAITING_CI.value,
                                FlowStatus.EVALUATING_CI.value,
                                FlowStatus.REVIEWING.value,
                            ]
                        ),
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
            if run.status == FlowStatus.WAITING_CI.value:
                await service.evaluate_waiting_ci_one(run.id, now=now)
            else:
                await service.resume_verification(run.id)
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
            # A11 revival-attempt recovery: re-drive the dispatch of revival
            # attempts whose worker died before the backend call.
            await evaluate_github_revival_recovery(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            logger.exception("GitHub revival-attempt recovery pass failed")

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


def _github_revival_redispatch(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    stack_factory: Callable[[str, str], GitHubAgents],
) -> Callable[[str], Awaitable[None]]:
    """The Actions-lane re-dispatch leg shared by the Tier-1 revive pass and
    the A11 recovery scan: rebuild the repo's service from the run's durable
    subject and hand the run to :meth:`GitHubRunService._redispatch_revival`."""

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

    return redispatch


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

    await evaluate_revivals(
        session_factory,
        settings,
        provider="github",
        redispatch=_github_revival_redispatch(
            settings, forge_config, session_factory, stack_factory
        ),
        now=now,
        log=logger,
    )


async def evaluate_github_revival_recovery(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
    now: datetime | None = None,
) -> int:
    """One A11 recovery pass over stranded GitHub revival attempts.

    The twin of the GitLab reconciler's ``evaluate_revival_recovery`` slot:
    a worker that died between the revive commit and the Actions dispatch
    (or inside the dispatch leg, journal unfinished) has its dispatch
    re-driven exactly once.
    """
    if stack_factory is None:
        stack_factory = lambda o, r: build_github_agents(  # noqa: E731 — trivial default
            settings, session_factory, o, r
        )
    return await evaluate_attempt_recovery(
        session_factory,
        provider="github",
        redispatch=_github_revival_redispatch(
            settings, forge_config, session_factory, stack_factory
        ),
        now=now,
        log=logger,
    )


async def evaluate_github_config_recovery(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
) -> None:
    """One A13 pass: retry the `.forge.yml` read of config-blocked GitHub runs.

    The twin of the GitLab reconciler's ``evaluate_config_recovery`` slot:
    every repo holding a run parked ``blocked(config_…)`` gets one
    :meth:`GitHubRunService.evaluate_config_recovery` tick — a recovered
    read re-enters planning through the fenced plan-restart edge; a still
    failing read leaves the run parked, unpaid.
    """
    if stack_factory is None:
        stack_factory = lambda o, r: build_github_agents(  # noqa: E731 — trivial default
            settings, session_factory, o, r
        )
    for repo_full_name in await _repos_with_config_blocked(session_factory):
        owner, _, repo = repo_full_name.partition("/")
        if not repo:
            logger.warning("Config-blocked run with malformed repo %r — skipping", repo_full_name)
            continue
        stack = stack_factory(owner, repo)
        try:
            service = GitHubRunService(
                session_factory, settings, forge_config, stack=stack, repo_full_name=repo_full_name
            )
            await service.evaluate_config_recovery()
        except Exception:
            # One broken repo must not stall the recovery pass.
            logger.exception("Config-block recovery failed for %s", repo_full_name)
        finally:
            aclose = getattr(stack.client, "aclose", None)
            if aclose is not None:
                await aclose()


async def _repos_with_config_blocked(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    """Distinct GitHub repos that hold a run parked ``blocked(config_…)``."""
    async with session_factory() as session:
        runs = (
            (
                await session.execute(
                    select(FlowRun).where(
                        FlowRun.provider == "github",
                        FlowRun.status == FlowStatus.BLOCKED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
    repos: list[str] = []
    for run in runs:
        if not str(run.status_reason or "").startswith(CONFIG_BLOCK_PREFIXES):
            continue
        stash = (run.evidence or {}).get("config_block") or {}
        repo = str(stash.get("repo_full_name") or run.github_repo_full_name or "").strip()
        if repo:
            repos.append(repo)
    return list(dict.fromkeys(repos))


async def _repos_with_due_publication_intents(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    """Distinct GitHub repos that hold an open publication intent (R11)."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(PublicationIntent.repo)
                    .where(
                        PublicationIntent.provider == "github",
                        PublicationIntent.status.in_(OPEN_STATES),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    return [row for row in rows if row]


async def evaluate_github_publication_intents(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
    now: datetime | None = None,
) -> None:
    """One recovery pass over every GitHub repo with open publication intents.

    The post-restart half of the R11 fix: a worker that died between the
    remote commit and the journal completion leaves the intent ``dispatched``
    — this pass (the reconciler loop) probes the remote by identity and
    resolves it: the run ADVANCES on the landed commit instead of blocking
    on a re-publish ``branch_drift`` (the live failure this week).
    """
    if stack_factory is None:
        stack_factory = lambda o, r: build_github_agents(  # noqa: E731
            settings, session_factory, o, r
        )
    for repo_full_name in await _repos_with_due_publication_intents(session_factory):
        owner, _, repo = repo_full_name.partition("/")
        if not repo:
            logger.warning("Publication intent with malformed repo %r — skipping", repo_full_name)
            continue
        stack = stack_factory(owner, repo)
        try:
            service = GitHubRunService(
                session_factory, settings, forge_config, stack=stack, repo_full_name=repo_full_name
            )
            await service.resolve_publication_intents(now=now)
        except Exception:
            # One broken repo must not stall the recovery pass.
            logger.exception("Publication-intent recovery failed for %s", repo_full_name)
        finally:
            aclose = getattr(stack.client, "aclose", None)
            if aclose is not None:
                await aclose()


async def run_github_publication_intents_reconciler(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: float = 15,
    shutdown_event: asyncio.Event | None = None,
    stack_factory: Callable[[str, str], GitHubAgents] | None = None,
) -> None:
    """Periodic R11 recovery tick: resolve stranded publication intents.

    Plain asyncio task for the worker's ``asyncio.gather`` (same shape as
    :func:`run_github_harness_reconciler`). Runs for EVERY GitHub deployment
    — builtin lane included: the lost-response window is lane-independent.
    Exits when the GitHub adapter is disabled or carries no credentials.
    """
    if not bool(getattr(settings, "FORGE_GITHUB_ENABLED", False)):
        return
    try:
        from forge.integrations.github_flow import credentials_from_settings

        credentials_from_settings(settings)  # fail fast, before the loop
    except ValueError:
        logger.info("GitHub credentials not configured — publication-intent reconciler not started")
        return

    shutdown_event = shutdown_event or asyncio.Event()
    logger.info("GitHub publication-intent reconciler started (interval=%ss)", interval_seconds)
    while not shutdown_event.is_set():
        try:
            await evaluate_github_publication_intents(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            # A failed pass must never kill the reconciler task.
            logger.exception("GitHub publication-intent reconciler pass failed")
        try:
            # A13 config-gate recovery rides this always-on GitHub loop —
            # lane-independent, like the R11 recovery above.
            await evaluate_github_config_recovery(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            logger.exception("GitHub config-block recovery pass failed")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break  # Event set — clean shutdown.
        except asyncio.TimeoutError:
            pass
    logger.info("GitHub publication-intent reconciler stopped")


__all__ = [
    "GitHubRunService",
    "evaluate_github_config_recovery",
    "evaluate_github_publication_intents",
    "evaluate_github_revival",
    "evaluate_github_revival_recovery",
    "evaluate_github_waiting_harness",
    "execute_github_run_command",
    "run_github_harness_reconciler",
    "run_github_publication_intents_reconciler",
]
