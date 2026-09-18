"""AzureRunService — the AZ-2 Azure DevOps path to gate parity (ADR-0024).

Mirrors the GitHub gate machinery (:mod:`forge.runs.github_service`) onto
FlowRun rows with ``provider='azure_devops'``: an ``/implement`` on a work
item creates a durable run, plans, posts the PLAN as a markdown work-item
comment, freezes the executable RunSpec (v3 — task text, plan artifact,
model route, verification contract and budgets, harness selection
included), opens the
pending decision (plan/base/spec/task digests + ``FORGE_DECISION_TTL_SECONDS``)
and parks the run in ``waiting_approval``. An approver's ``/go <run-id>``
consumes the decision exactly once and drives the publish leg. ``/cancel``
mirrors cancel-as-revoke semantics (F13).

Azure-specific deviations, all deliberate:

- **Admission/approvers are AzDO identities** (uniqueName form) —
  ``FORGE_AZDO_APPROVERS`` when set, else the shared ``FORGE_APPROVERS``
  fallback (never merged): a GitLab username in the shared list can never
  approve an Azure DevOps run.
- **One active run per (project, work item)** reuses the partial unique
  index ``uq_active_run_per_issue`` over ``flow_runs.(project_id,
  issue_iid)`` — ``project_id`` carries the deterministic project-GUID key
  (:func:`forge.gateway.azure_webhook.azure_project_key`) and ``issue_iid``
  the work-item id.
- **Pipelines lane (ADR-0024 §6)**: when ``FORGE_AZDO_LANE_PIPELINE_ID``
  names the lane pipeline, an approved /go cuts the factory branch at the
  frozen attempt base (:meth:`AzureDevOpsClient.create_branch_from`),
  dispatches the lane via the Runs API with string templateParameters
  (run_id / attempt_base / driver / model / work_item_id) and parks the run
  in ``waiting_harness`` with a journaled :class:`AzurePipelinesHandle` —
  the reconciler polls it and adopts the candidate through the SAME trusted
  publisher. Empty → the builtin in-worker proposer path as on GitHub.
- **Verification gate (R02)**: every publication parks in ``waiting_ci`` —
  the PR's builds are an independent verification gate. The reconciler
  pass (``evaluate_waiting_ci_one`` / :func:`evaluate_azure_waiting_ci`)
  polls the Builds API for completed builds of the candidate commit (the
  lane pipeline itself is execution, not verification, and is excluded),
  and only a verified (or honestly unverified) run continues to review.
  Red → repair-in-place via ``_begin_repair`` while commit cycles remain,
  else ``blocked(quality_contract)``; green → review; no builds after the
  grace window → review as **unverified**, labeled as such in the evidence
  and the ready reason.
- **The trusted publisher maps to the native CAS** (ADR-0024 §4): publish =
  create branch (``oldObjectId`` = 40 zeros) then one push with
  ``expected_old_sha`` — a moved head is rejected with
  ``updateStatus: staleObjectId`` under HTTP 200, surfaced as
  :class:`AzureDevOpsDriftError` and mapped to the drift outcome (never
  retried, never force-pushed).
- **Work-item linking**: the PR-create body's ``workItemRefs`` is unreliable
  (research §4.6), so the reliable WIT ArtifactLink PATCH is attempted after
  the Draft PR exists; a failed link is recorded in the evidence and never
  fails the publish.

Durability rules are unchanged (ADR-0005): every transition goes through
:class:`forge.durable.Controller`; external writes (comments, pushes, PRs)
are journaled intent-first in ``action_log``.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from forge.config import ForgeConfig, Settings, parse_budget_profiles
from forge.durable import (
    BudgetLimits,
    Controller,
    FlowRun,
    FlowStatus,
    GateAlreadyConsumed,
    GateApproval,
    OPEN_STATES,
    PublicationIntent,
    RunNotFound,
    RunSpec,
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
    is_valid,
    load_budget_guard,
    mark_dispatched,
    message_with_marker,
    mint_operation_key,
    open_budget,
    open_budget_from_spec,
    resolve_budget_limits,
    ProbeObservation,
    ProbeVerdict,
    record_approval,
    record_intent,
    short_run_id,
)
from forge.durable.controller import TERMINAL_STATUSES, InvalidTransition
from forge.factory.llm import LLMClient, LLMError, LLMResponseError, truncate_chars
from forge.factory.planner import LLMPlanner, PLAN_SUMMARY_CHARS
from forge.factory.reviewer import (
    REVIEWER_MAX_DIFF_CHARS,
    REVIEWER_MAX_INPUT_CHARS,
    LLMReviewer,
    ReviewVerdict,
    review_with_retry,
)
from forge.factory.implementer import IMPLEMENTER_TIER, LLMImplementer
from forge.execution.azure_pipelines import (
    AzurePipelinesExecutor,
    AzurePipelinesHandle as ExecutorHandle,
)
from forge.integrations.azure import (
    AzureDevOpsClient,
    AzureDevOpsDriftError,
    AzureDevOpsError,
    AzureDevOpsNotFoundError,
    AzureRepositoryReader,
    CommitPayload,
    FileChange,
    PipelineRun,
)
from forge.orchestrator.project_config import ProjectConfig, load_project_config
from forge.repository import Change, ChangeSet, Operation
from forge.runs.admission import approvers_for, check_admission
from forge.runs.backends import HARNESS_NAME, is_harness_backend
from forge.runs.candidate import attempt_base_for
from forge.repository.changeset import validate_changeset
from forge.runs.consistency import (
    assert_ready_invariants,
    ready_evidence,
    ready_reason,
)
from forge.runs.harness_selection import (
    BudgetCeilings,
    resolve_available_drivers,
    SHIPPED_DRIVERS,
    HarnessSelection,
    compile_harness_selection,
    implementation_block,
    resolve_preference,
    validate_preference,
)
from forge.runs.revival import (
    RECONCILE_RE,
    STATUS_RE,
    WHY_BLOCKED_RE,
    build_retry_context,
    collect_status_snapshot,
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
    SpecLegacy,
    load_verified_spec,
)
from forge.runs.verification import PRODUCER_AZURE_BUILD
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

#: The verification surface note (R02): Azure Repos ignores YAML ``pr:``
#: triggers — PR CI is governed by branch policies ("Build validation") and
#: any pipeline building the candidate commit. The run parks in
#: ``waiting_ci`` until those builds conclude (or is honestly labeled
#: unverified when none exist).
_VERIFICATION_NOTE = (
    "Branch-policy Build validation on the PR is the verification surface — "
    "the run waits for the candidate commit's builds before review."
)

#: ADR-0027: the Azure DevOps situational detail after the shared
#: "unverified — " ready-reason prefix (forge.runs.consistency) — no CI
#: build ran for the candidate commit (R02).
UNVERIFIED_DETAIL = "no CI configured"

#: Backoff the publication-intent scanner applies to an open intent whose
#: probe says "nothing landed, head intact" — the run's own publish leg owns
#: the re-push; the scanner just stops polling the provider hot.
_INTENT_PROBE_BACKOFF_SECONDS = 60

#: Build ``status`` values that mean the build has not concluded yet
#: (research §6.2: ``state``/``result`` in the Runs API, ``status``/
#: ``result`` on the Build object).
_ACTIVE_BUILD_STATUSES: frozenset[str] = frozenset(
    {"notStarted", "inProgress", "postponed", "cancelling"}
)

#: Build ``result`` values that blame the change (ADR-0008): not fully green
#: is not green — ``partiallySucceeded`` fails branch-policy validation too.
_RED_BUILD_RESULTS: frozenset[str] = frozenset(
    {"failed", "canceled", "partiallySucceeded", "abandoned"}
)


def azure_factory_branch(issue_number: int, run_id: str) -> str:
    """The run-owned Azure DevOps branch: ``forge/<work-item>/<run-id[:8]>``.

    Same scheme as :func:`forge.integrations.github_flow.github_factory_branch`
    — the ``forge/`` prefix marks forge-owned refs (the ingress bot-loop and
    reactive guards key off it); no user-supplied text appears in the
    mandatory part of the ref.
    """
    return f"forge/{issue_number or 0}/{short_run_id(run_id)}"


@dataclass(frozen=True)
class AzurePipelinesHandle:
    """The journaled lane execution handle (the ActionsHandle analog).

    Opaque JSON in ``flow_runs.evidence`` so a run survives restarts.
    ``run_id`` is the Pipelines run id — equal to the underlying build id
    (research §6.3) — and 0 until correlation succeeds directly from the
    dispatch response.
    """

    provider: str
    project: str
    repo: str
    pipeline_id: int
    run_id: int
    branch: str
    attempt_base: str
    run_spec_digest: str
    driver: str
    forge_run_id: str
    started_at: str

    def to_json(self) -> str:
        import json

        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> AzurePipelinesHandle:
        import json

        data = json.loads(raw)
        return cls(
            provider=str(data["provider"]),
            project=str(data["project"]),
            repo=str(data["repo"]),
            pipeline_id=int(data.get("pipeline_id") or 0),
            run_id=int(data.get("run_id") or 0),
            branch=str(data["branch"]),
            attempt_base=str(data["attempt_base"]),
            run_spec_digest=str(data.get("run_spec_digest") or ""),
            driver=str(data.get("driver") or ""),
            forge_run_id=str(data.get("forge_run_id") or ""),
            started_at=str(data["started_at"]),
        )

    def with_run_id(self, run_id: int) -> AzurePipelinesHandle:
        return replace(self, run_id=run_id)


@dataclass(frozen=True)
class AzurePublishOutcome:
    """The publish leg's verdict, with the three revisions kept distinct.

    Mirrors :class:`forge.integrations.github_flow.GitHubPublishOutcome`;
    ``commit_oid`` is the factory-branch head AFTER the push (read from the
    push response's ``newObjectId``), ``expected_head_oid`` the frozen base
    the CAS pinned.
    """

    ok: bool
    reason: str = ""
    commit_oid: str | None = None
    expected_head_oid: str | None = None
    branch: str | None = None
    pr_id: int | None = None
    pr_url: str | None = None
    #: The WIT ArtifactLink work-item→PR PATCH outcome (None = not attempted).
    work_item_linked: bool | None = None
    #: A CAS rejection (``staleObjectId`` et al.): reported, never retried
    #: silently (ADR-0024 §4).
    drift: bool = False
    #: R11: True when ``commit_oid`` is a PREVIOUS attempt's landed push
    #: found by the identity probe (exact operation marker + expected
    #: parent) after a lost response or stale CAS — adopted, never re-posted.
    adopted: bool = False


@dataclass(frozen=True)
class AzureAgents:
    """The constructed Azure DevOps stack one connection/repo runs with."""

    client: AzureDevOpsClient
    reader: AzureRepositoryReader
    planner: LLMPlanner
    implementer: LLMImplementer
    reviewer: "AzurePRReviewer"


def azure_credentials_from_settings(settings: Settings) -> str:
    """The PAT forge acts with on Azure DevOps (ADR-0024 §2)."""
    pat = getattr(settings, "FORGE_AZDO_PAT", None)
    token = pat.get_secret_value() if pat is not None else ""
    if not token:
        raise ValueError("FORGE_AZDO_PAT not configured — Azure DevOps run service unavailable")
    return token


def build_azure_agents(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    project: str,
    repo: str,
    *,
    client: AzureDevOpsClient | None = None,
) -> AzureAgents:
    """Ensure planner/reviewer/reader construction for one Azure repo.

    The single place the LLM-driven defaults are built for the Azure DevOps
    path (the twin of :func:`forge.integrations.github_flow.build_github_agents`):
    the reader duck-types the shared read surface, so the planner/implementer
    run unchanged; the reviewer reads the PR diff over the trees/items APIs.
    """
    if client is None:
        client = AzureDevOpsClient(
            base_url=str(getattr(settings, "FORGE_AZDO_ORG_URL", "") or ""),
            token=azure_credentials_from_settings(settings),
        )
    reader = AzureRepositoryReader(client, project, repo)
    llm = LLMClient(settings=settings, session_factory=session_factory)
    planner = LLMPlanner(llm, settings=settings)
    implementer = LLMImplementer(llm, gitlab=reader, settings=settings)
    reviewer = AzurePRReviewer(llm, client, settings=settings)
    return AzureAgents(
        client=client,
        reader=reader,
        planner=planner,
        implementer=implementer,
        reviewer=reviewer,
    )


# Kept identical to forge.factory.reviewer._SYSTEM_PROMPT — the same JSON
# review contract as the GitLab/GitHub paths (ADR-0008).
_REVIEW_SYSTEM_PROMPT = (
    "You are the readonly review agent of a code-writing bot. You review a "
    "candidate diff against the issue and plan it implements. You cannot "
    "change anything; you only judge.\n"
    "Respond with ONLY a JSON object:\n"
    '{"verdict": "ok" | "concerns", "summary": "<1-3 sentences>", '
    '"findings": [{"severity": "info"|"minor"|"major", "file": "<path>", '
    '"note": "<what and why>"}]}\n'
    'Use "ok" when the diff is a sound implementation of the issue; use '
    '"concerns" when a human should look closely before merging. Never '
    "invent files that are not in the diff."
)


class AzurePRReviewer:
    """Readonly review of the published candidate over the PR diff (AZ-2).

    The Azure twin of :class:`forge.integrations.github_flow.GitHubPRReviewer`:
    instead of a GitLab compare or GitHub's patch list it derives the changed
    paths from the trees API at the base and candidate SHAs and renders a
    unified diff over the items API. The per-iteration changes endpoint
    (``GET .../iterations/{id}/changes``) carries paths but not content and
    is tied to the PR's iteration lifecycle, while the durable review targets
    the two explicit SHAs the gate approved — so the diff is rendered
    client-side at exactly those SHAs. The verdict is recorded with the SHA
    it reviewed — the review approves a *specific* commit.
    """

    def __init__(
        self,
        llm: LLMClient,
        client: AzureDevOpsClient,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._client = client
        self._settings = settings

    async def review(
        self,
        *,
        project: str,
        repo: str,
        pr_id: int,
        issue_title: str,
        plan_summary: str,
        base_sha: str,
        candidate_sha: str,
        flow_run_id: str | None = None,
    ) -> ReviewVerdict:
        """Review the PR diff and return the parsed verdict."""
        diff = await self._pr_diff(project, repo, base_sha, candidate_sha)
        user = (
            f"Work item title: {issue_title}\n\n"
            f"Plan summary:\n{plan_summary or '(no plan summary available)'}\n\n"
            f"Candidate diff ({base_sha[:8]}..{candidate_sha[:8]}):\n{diff}"
        )
        result = await review_with_retry(
            self._llm,
            system=_REVIEW_SYSTEM_PROMPT,
            user=truncate_chars(user, REVIEWER_MAX_INPUT_CHARS),
            parse=LLMReviewer._parse,
            flow_run_id=flow_run_id,
        )
        return result

    async def _pr_diff(self, project: str, repo: str, base_sha: str, candidate_sha: str) -> str:
        """Render the candidate's changed files as diff text, biggest first."""
        try:
            base_tree = await self._tree_entries(project, repo, base_sha)
            candidate_tree = await self._tree_entries(project, repo, candidate_sha)
        except Exception:
            logger.warning(
                "Tree read failed for %s/%s — reviewing without diff",
                project,
                repo,
                exc_info=True,
            )
            return "(diff unavailable)"

        changed: list[str] = []
        for path, object_id in candidate_tree.items():
            if base_tree.get(path) != object_id:
                changed.append(path)
        for path, object_id in base_tree.items():
            if path not in candidate_tree and object_id:
                changed.append(path)  # deleted in the candidate

        rendered: list[tuple[str, str]] = []
        for path in changed:
            old = await self._content_at(project, repo, path, base_sha)
            new = await self._content_at(project, repo, path, candidate_sha)
            if old is None and new is None:
                continue
            patch = "\n".join(
                difflib.unified_diff(
                    (old or "").splitlines(),
                    (new or "").splitlines(),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                    lineterm="",
                )
            )
            rendered.append((path, patch))
        # Biggest diffs first — the same budget policy as the GitHub reviewer.
        rendered.sort(key=lambda entry: len(entry[1]), reverse=True)
        parts: list[str] = []
        for _path, patch in rendered:
            if patch:
                parts.append(patch)
        return truncate_chars("\n".join(parts), REVIEWER_MAX_DIFF_CHARS)

    async def _tree_entries(self, project: str, repo: str, sha: str) -> dict[str, str]:
        """path -> objectId for every blob at *sha* (refuses truncated trees)."""
        # LIVE-found (ADR-0024 lab): the trees API rejects a COMMIT sha
        # ("Expected a Tree, but objectId … resolved to a Commit") — resolve
        # commit -> treeId first.
        try:
            data = await self._client.get_tree(project, repo, sha, recursive=True)
        except AzureDevOpsError as exc:
            if "resolved to a Commit" not in str(exc):
                raise
            commit = await self._client.get_commit(project, repo, sha)
            tree_id = str((commit or {}).get("treeId") or "")
            if not tree_id:
                raise
            data = await self._client.get_tree(project, repo, tree_id, recursive=True)
        if isinstance(data, dict) and data.get("truncated"):
            raise AzureDevOpsError(
                200, f"tree listing at {sha!r} is truncated — refusing a partial diff"
            )
        entries: list[dict] = []
        value = data.get("treeEntries") if isinstance(data, dict) else None
        if value is None and isinstance(data, dict):
            value = data.get("value")
        if isinstance(value, list):
            entries = [entry for entry in value if isinstance(entry, dict)]
        return {
            str(entry.get("relativePath") or ""): str(entry.get("objectId") or "")
            for entry in entries
            if entry.get("gitObjectType") == "blob" and entry.get("relativePath")
        }

    async def _content_at(self, project: str, repo: str, path: str, sha: str) -> str | None:
        """The file's text at *sha*, or None when absent at that version."""
        try:
            data = await self._client.get_item(
                project, repo, path, version=sha, version_type="commit"
            )
        except AzureDevOpsNotFoundError:
            return None
        content = data.get("content")
        return str(content) if content is not None else None


class AzureRunService:
    """Coordinates the Azure agents, the controller and one run's lifecycle."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        config: ForgeConfig | None = None,
        *,
        stack: AzureAgents,
        repo_full_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._config = config or ForgeConfig()
        self._stack = stack
        self._repo_full_name = repo_full_name

    @property
    def _project(self) -> str:
        return self._repo_full_name.partition("/")[0]

    @property
    def _repo(self) -> str:
        return self._repo_full_name.partition("/")[2]

    # ------------------------------------------------------------------
    # Commands (dispatched from execute_azure_run_command)
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
        # One active run per (project, work item): the partial unique index is
        # the invariant of last resort; this pre-check produces the friendly
        # refusal comment instead of a raw IntegrityError.
        active = await self._find_active_run(project_id, issue_number)
        if active is not None:
            await self._post_journaled_comment(
                project_id,
                issue_number,
                self._active_run_comment(active),
                active.id,
                "duplicate_implement",
            )
            logger.info(
                "Azure DevOps /implement on %s#%s ignored — run %s is already %s",
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
                        provider="azure_devops",
                        # The durable subject columns are int-typed (the GitHub
                        # schema); the Azure string identity — project/repo —
                        # travels in the run's evidence subject block below.
                        github_issue_number=issue_number,
                    )
                )
                await controller.transition(run_id, FlowStatus.PREFLIGHT)
                await session.commit()
        except IntegrityError:
            # F12 (ADR-0017): a concurrent /implement won the (project, work
            # item) slot between the active-run check and this insert — adopt.
            existing = await self._find_active_run(project_id, issue_number)
            if existing is None:
                raise
            await self._post_journaled_comment(
                project_id,
                issue_number,
                self._active_run_comment(existing),
                existing.id,
                "duplicate_implement",
            )
            logger.info(
                "Azure DevOps /implement on %s#%s lost the creation race — run %s is active",
                self._repo_full_name,
                issue_number,
                existing.id[:8],
            )
            return existing.id

        # ADR-0018 §3: admission before the first paid call. The Azure DevOps
        # connection's own approver list (FORGE_AZDO_APPROVERS, falling back
        # to FORGE_APPROVERS) carries the AzDO identities on this path.
        admission = check_admission(
            self._settings, self._config, project_id, author_username, provider="azure_devops"
        )
        if not admission.allowed:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, f"admission_denied: {admission.reason}"
            )
            await self._post_journaled_comment(
                project_id,
                issue_number,
                _admission_denied_comment(run_id, author_username),
                run_id,
                "admission_denied",
            )
            logger.warning(
                "Azure DevOps run %s denied admission for @%s: %s",
                run_id[:8],
                author_username,
                admission.reason,
            )
            return run_id

        # v0.7 monorepo path scoping (same as the GitHub path): the repo's
        # `.forge.yml` `implement.paths` globs shape the plan prompt and are
        # frozen into the RunSpec. A read failure degrades to unscoped.
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
            await self._to_terminal(run_id, FlowStatus.FAILED, f"planning_failed: {exc}")
            await self._post_journaled_comment(
                project_id,
                issue_number,
                f"Run `{run_id[:8]}` **failed** at planning: {exc}\n\n"
                "*This is an automated message.*",
                run_id,
                "planning_failed",
            )
            raise

        digest = plan_digest_of(plan)
        # The task snapshot digest freezes the STRIPPED description:
        # System.Description is HTML, and the #29 edit handler
        # (``handle_issue_edited``) digests the stripped text — a caller
        # passing raw HTML must freeze the comparable snapshot, not a
        # tag-encoding-sensitive one.
        task_description = _strip_html(issue_description)
        task_digest = task_digest_of(issue_title, task_description)
        now = datetime.now(timezone.utc)
        base_sha = await self._read_base_sha()
        plan_summary = self._plan_summary(plan)
        plan_files_hint = self._plan_files_hint()

        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.PLANNING)
            run = await self._get_run(session, run_id)
            run.plan_digest = digest
            run.base_sha = base_sha
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "subject": {
                        "provider": "azure_devops",
                        "project": self._project,
                        "repo_full_name": self._repo_full_name,
                    },
                    "backend": "builtin",
                    "harness_selection": harness_selection.as_document(),
                    "plan": {
                        "digest": digest,
                        "summary": plan_summary,
                        "files_hint": plan_files_hint,
                    },
                    # R13 §4 (A02 parity): the honest enforcement record —
                    # what the frozen budget enforces on THIS lane, and
                    # which ceilings ride in the spec. Absent when no
                    # finite profile applies.
                    **(
                        {
                            "budget": {
                                "budget_class": harness_selection.budget_class,
                                "enforcement": budget_enforcement_for_backend(
                                    "ci_harness" if self._lane_pipeline_id() else "builtin"
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
            # and GitHub paths: the task text, the plan artifact, the model
            # route, the path policy, the required checks, the budgets and
            # the backend/driver. The gate binds its digest, so /go
            # approves exactly the bytes the run will execute.
            spec_document = self._build_run_spec_document(
                project_id=project_id,
                issue_number=issue_number,
                base_sha=base_sha,
                task_title=issue_title,
                task_description=task_description,
                task_digest=task_digest,
                plan_summary=plan_summary,
                plan_files_hint=plan_files_hint,
                plan_digest=digest,
                allowed_paths=path_scope,
                harness_selection=harness_selection,
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

        # F22 (ADR-0018 §5): open the run's budget from the spec (idempotent
        # — a pre-paid open above already froze the limits; this only
        # backfills the spec_digest provenance, and a no-profile run opens
        # nothing).
        async with self._session_factory() as session:
            await open_budget_from_spec(
                session, run_id=run_id, spec_document=spec_document, spec_digest=spec_digest
            )
            await session.commit()

        await self._post_journaled_comment(
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
            "Azure DevOps run %s started for %s#%d (by @%s) — waiting for /go",
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

        Idempotent: a re-delivered comment finds the run already out of
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
                # A07: the full subject — provider AND project, like every
                # other explicit-id resolution on this lane.
                run.provider != "azure_devops"
                or run.project_id != project_id
                or run.issue_iid != issue_number
            ):
                logger.info("Azure DevOps /go references unknown run %s — ignoring", run_id[:8])
                return
            if run.status != FlowStatus.WAITING_APPROVAL.value:
                # Already advanced (or terminal) — duplicate /go delivery.
                logger.info(
                    "Azure DevOps /go for run %s in status %s — ignoring duplicate",
                    run_id[:8],
                    run.status,
                )
                return

            # ADR-0009: authority comes from trusted configuration.
            if author_username not in self._approvers():
                logger.info(
                    "Azure DevOps /go from @%s who is not in the AzDO approver list — ignoring",
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
                logger.info(
                    "No pending decision for Azure DevOps run %s — ignoring /go", run_id[:8]
                )
                return
            if not is_valid(
                gate,
                now,
                plan_digest=run.plan_digest or "",
                base_sha=run.base_sha or "",
                policy_digest=self._policy_digest(),
                spec_digest=run.spec_digest,
            ):
                # The work-item thread is the only operator surface on Azure
                # DevOps: an expired or drifted decision blocks the run
                # visibly.
                expired = as_aware_utc(gate.expires_at) <= as_aware_utc(now)
                reason = (
                    "decision_expired: the approval window closed — run /implement again"
                    if expired
                    else "decision_drift: the approved plan/policy changed — run /implement again"
                )
                await self._transition_in_session(session, run.id, FlowStatus.BLOCKED, reason)
                await self._post_journaled_comment(
                    project_id,
                    issue_number,
                    f"Run `{run_id[:8]}` was **blocked**: {reason}.\n\n"
                    "*This is an automated message.*",
                    run_id,
                    "decision_expired" if expired else "decision_drift",
                )
                logger.info(
                    "Azure DevOps decision for run %s expired/drifted — blocked", run_id[:8]
                )
                return

            try:
                await consume_approval(session, gate.id, now)
            except GateAlreadyConsumed:
                logger.info("Azure DevOps gate for run %s already consumed — ignoring", run_id[:8])
                return
            await self._transition_in_session(
                session, run.id, FlowStatus.PROPOSING, f"approved by @{author_username}"
            )

        logger.info(
            "Azure DevOps gate for run %s consumed by @%s — publishing",
            run_id[:8],
            author_username,
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
        """``/cancel [run-id]``: cancel-as-revoke (F13) on the Azure path.

        Without an explicit run id the latest ACTIVE run for the work item is
        cancelled. The durable ``cancel_requested`` flag revokes the
        publication grant (in-flight publish legs re-read it and stand down)
        and scheduled steps are withdrawn, so late results are superseded.
        """
        match = _CANCEL_RE.search(note_text or "")
        if match is None:
            return
        if author_username not in self._approvers():
            logger.info(
                "Azure DevOps /cancel from @%s who is not in the AzDO approver list — ignoring",
                author_username,
            )
            return

        requested = (match.group(1) or "").lower()
        async with self._session_factory() as session:
            if not requested:
                run = await self._find_active_run(project_id, issue_number)
                if run is None:
                    logger.info(
                        "Azure DevOps /cancel on %s#%s — no active run",
                        self._repo_full_name,
                        issue_number,
                    )
                    return
            elif len(requested) == 32:
                run = await session.get(FlowRun, requested)
                if (
                    run is None
                    or run.provider != "azure_devops"
                    or run.project_id != project_id
                    or run.issue_iid != issue_number
                ):
                    logger.info(
                        "Azure DevOps /cancel references unknown run %s — ignoring",
                        requested[:8],
                    )
                    return
            else:
                # Short id (plan comments show the 8-char form): resolve by
                # prefix among the work item's runs; ambiguity means no action.
                # A07: the same subject scope as the bare and full-id forms —
                # project included, so a same-iid run of another project never
                # resolves here.
                runs = (
                    (
                        await session.execute(
                            select(FlowRun)
                            .where(
                                FlowRun.provider == "azure_devops",
                                FlowRun.project_id == project_id,
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
                        "Azure DevOps /cancel prefix %s matches %d runs — ignoring",
                        requested[:8],
                        len(runs),
                    )
                    return
                run = runs[0]
            run_id = run.id
            status = run.status
            evidence = dict(run.evidence or {})

        if status in {s.value for s in TERMINAL_STATUSES}:
            logger.info(
                "Azure DevOps /cancel for terminal run %s (%s) — ignoring", run_id[:8], status
            )
            return

        # F13 (ADR-0018 §4): revoke the publication grant first — the flag is
        # what an in-flight publish leg re-reads before writing — then
        # withdraw scheduled steps so no worker picks them up later.
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            run.cancel_requested = True
            # R10: bump the fence so pre-revoke claims lose their grant.
            run.cancellation_generation = (run.cancellation_generation or 0) + 1
            await session.execute(
                update(StepRun)
                .where(StepRun.flow_run_id == run_id, StepRun.status == "scheduled")
                .values(status="cancelled")
            )
            await session.commit()

        # Pipelines lane: also stop the lane run itself (best-effort — the
        # grant revocation above is the safety property; a late candidate is
        # superseded, never published). The Runs area has no cancel: the
        # journaled run id IS the build id (research §6.5).
        raw_handle = str((evidence.get("harness") or {}).get("handle") or "")
        if raw_handle:
            try:
                handle = AzurePipelinesHandle.from_json(raw_handle)
                if handle.run_id:
                    await self._stack.client.cancel_build(self._project, handle.run_id)
            except Exception:
                logger.warning(
                    "Pipelines lane cancel failed for run %s — grant already revoked",
                    run_id[:8],
                    exc_info=True,
                )

        await self._transition(
            run_id, FlowStatus.CANCELLED, reason=f"cancelled by @{author_username}"
        )
        await self._post_journaled_comment(
            project_id,
            issue_number,
            f"Run `{run_id[:8]}` **cancelled** by @{author_username}. "
            "Any in-flight publication was stood down.\n\n*This is an automated message.*",
            run_id,
            "cancel_note",
        )
        logger.info("Azure DevOps run %s cancelled by @%s", run_id[:8], author_username)

    async def handle_retry(
        self,
        *,
        project_id: int,
        issue_number: int,
        note_text: str,
        author_username: str,
    ) -> None:
        """``/retry [run-id]``: operator revival of a dead run on the AzDO lane.

        The exact GitLab ``handle_retry_note`` semantics (Tier 2): bare, the
        latest ``failed``/``blocked`` run for the work item; the explicit
        revival graph edge walks it to ``proposing``; one operator-granted
        commit cycle; the SAME branch re-dispatched with the terminal reason
        and the last verification evidence as the repair context.
        """
        match = _RETRY_RE.search(note_text or "")
        if match is None:
            return
        if author_username not in self._approvers():
            logger.info(
                "Azure DevOps /retry from @%s who is not in the AzDO approver list — ignoring",
                author_username,
            )
            return
        if issue_number is None:
            logger.info("Azure DevOps /retry off-work-item — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        # A07: the dispatch legs below aim at the VERIFIED run's subject, read
        # back from the matched run — never the command context. The scoped
        # resolution makes the two equal; reading them from the run keeps the
        # dispatch honest even if resolution were ever widened.
        retry_project_id = 0
        retry_issue_number = 0
        rejection = ""
        async with self._session_factory() as session:
            run = await resolve_retry_target(
                session,
                provider="azure_devops",
                project_id=project_id,
                issue_iid=issue_number,
                requested=requested,
            )
            if run is not None:
                run_id = run.id
                retry_project_id = int(run.project_id)
                retry_issue_number = int(run.issue_iid or 0)
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
                "Azure DevOps /retry on %s#%s — no retryable run",
                self._repo_full_name,
                issue_number,
            )
            return
        if rejection:
            await self._post_journaled_comment(
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

        branch = azure_factory_branch(retry_issue_number, run_id)
        logger.info(
            "Azure DevOps run %s retried by @%s — re-dispatching %s (cycle %d)",
            run_id[:8],
            author_username,
            branch,
            cycle + 1,
        )
        await self._post_journaled_comment(
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
            if self._lane_pipeline_id():
                await self._advance_harness(
                    run_id,
                    project_id=retry_project_id,
                    issue_number=retry_issue_number,
                    repair_context=repair_context,
                )
            else:
                logger.info(
                    "Azure DevOps retry of builtin run %s — %s",
                    run_id[:8],
                    repair_reason,
                )
                await self._advance_publish(
                    run_id, project_id=retry_project_id, issue_number=retry_issue_number
                )
        except Exception as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        await self._complete_action(action_id, "succeeded", {"backend": "ci_harness"})

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

        The exact GitLab ``handle_status_note`` semantics: bare, the work
        item's latest run of any state; durable-state reply only — no
        transitions, no model calls, no provider effects beyond the
        journaled reply comment.
        """
        match = STATUS_RE.search(note_text or "")
        if match is None:
            return
        if issue_number is None:
            logger.info("Azure DevOps /status off-work-item — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        async with self._session_factory() as session:
            run = await resolve_status_target(
                session,
                provider="azure_devops",
                project_id=project_id,
                issue_iid=issue_number,
                requested=requested,
            )
            if run is None:
                body = (
                    "## Forge — status\n\nNo forge run found on this work item yet. "
                    "Start one with `/implement`.\n\n*This is an automated message.*"
                )
                logger.info(
                    "Azure DevOps /status on %s#%s — no run", self._repo_full_name, issue_number
                )
            else:
                body = format_status_reply(await collect_status_snapshot(session, run))
                run_id = run.id
        await self._post_journaled_comment(
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
            logger.info("Azure DevOps /why-blocked off-work-item — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        async with self._session_factory() as session:
            run = await resolve_status_target(
                session,
                provider="azure_devops",
                project_id=project_id,
                issue_iid=issue_number,
                requested=requested,
            )
            if run is None:
                body = (
                    "## Forge — why blocked\n\nNo forge run found on this work item yet. "
                    "Start one with `/implement`.\n\n*This is an automated message.*"
                )
                logger.info(
                    "Azure DevOps /why-blocked on %s#%s — no run",
                    self._repo_full_name,
                    issue_number,
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
        await self._post_journaled_comment(
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
                "Azure DevOps /reconcile from @%s who is not in the AzDO approver list — ignoring",
                author_username,
            )
            return
        if issue_number is None:
            logger.info("Azure DevOps /reconcile off-work-item — ignoring")
            return

        requested = (match.group(1) or "").lower()
        run_id: str | None = None
        intents = []
        async with self._session_factory() as session:
            run = await resolve_retry_target(
                session,
                provider="azure_devops",
                project_id=project_id,
                issue_iid=issue_number,
                requested=requested,
            )
            if run is not None:
                run_id = run.id
                intents = await intents_for_run(session, run.id)
        if run_id is None:
            logger.info(
                "Azure DevOps /reconcile references unknown run %s — ignoring", requested[:8]
            )
            return
        if not intents:
            await self._post_journaled_comment(
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
        await self._post_journaled_comment(
            project_id,
            issue_number,
            format_reconcile_reply(run_id, [row for row in resolved if row is not None]),
            run_id,
            "reconcile_note",
        )

    # ------------------------------------------------------------------
    # Issue-edit replan + tag-off cancel (operator busywork, event-driven)
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
        """``workitem.updated``: keep the waiting plan honest (#29).

        The AzDO mirror of ``GitHubRunService.handle_issue_edited`` — the
        same three cases against the issue-text snapshot frozen at plan
        time (the RunSpec's ``task_digest``): replan a stale gate-waiting
        run, note once past the gate, ignore a no-op edit.

        AzDO deltas, both deliberate: ``System.Description`` is HTML, so
        the comparison text is stripped exactly like ``start_run`` freezes
        it; and a ``workitem.updated`` delivery can travel sparse (a
        "changed fields only" subscription omits untouched fields), so a
        missing title/body is repaired with ONE API read instead of
        digesting empty strings as if the fields had been blanked.
        """
        admission = check_admission(
            self._settings, self._config, project_id, author_username, provider="azure_devops"
        )
        if not admission.allowed:
            # ADR-0009: forge reacts to an edit only for an actor it would
            # let start a run — anyone else's edit never yanks or replans.
            logger.info(
                "Azure DevOps work-item edit by @%s on %s#%s ignored — not admitted (%s)",
                author_username,
                self._repo_full_name,
                issue_number,
                admission.reason,
            )
            return None

        title, body = issue_title, issue_body
        if not title or not body:
            try:
                fields = (await self._stack.client.get_work_item(self._project, issue_number)).get(
                    "fields"
                ) or {}
                title = title or str(fields.get("System.Title") or "")
                body = body or str(fields.get("System.Description") or "")
            except Exception:
                logger.warning(
                    "Work item %s text read failed — comparing the delivery text only",
                    issue_number,
                    exc_info=True,
                )
        edited_digest = task_digest_of(title, _strip_html(body))

        run = await self._find_active_run(project_id, issue_number)
        stale_run_id: str | None = None
        if run is not None:
            if await self._frozen_task_digest(run.id) == edited_digest:
                logger.info(
                    "Azure DevOps work-item edit on #%s matches run %s's snapshot — ignoring",
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
                    "Azure DevOps run %s superseded by a work-item edit — replanning #%s",
                    stale_run_id[:8],
                    issue_number,
                )
            else:
                # Approved / in flight: the change is NOT pulled into the
                # approved plan.
                await self._post_journaled_comment(
                    project_id,
                    issue_number,
                    f"Work item edited while run `{run.id[:8]}` is in flight — the change is "
                    "**not** in the approved plan. The run keeps executing its approved "
                    "snapshot; run `/cancel` and `/implement` if it should pick the change "
                    "up.\n\n*This is an automated message.*",
                    run.id,
                    "issue_edited_note",
                )
                return run.id
        elif not await self._replan_interrupted(project_id, issue_number):
            logger.info("Azure DevOps work-item edit on #%s — no active run", issue_number)
            return None

        new_run_id = await self.start_run(
            project_id=project_id,
            issue_number=issue_number,
            issue_title=title,
            issue_description=_strip_html(body),
            author_username=author_username,
        )
        # A retried replan (the first attempt died mid-step) has no stale run
        # of its own to name — the note then just says where the plan came from.
        if stale_run_id is not None:
            origin = (
                f"The plan of run `{stale_run_id[:8]}` was **stale** — the work item was edited "
                "while its plan waited for approval. It was cancelled and the plan "
            )
        else:
            origin = (
                "The work item was edited while its plan waited for approval — that plan was "
                "stale, so the plan "
            )
        await self._post_journaled_comment(
            project_id,
            issue_number,
            f"{origin}"
            f"regenerated from the current work item as run `{new_run_id[:8]}`. "
            f"Approve with `/go {new_run_id}`.\n\n*This is an automated message.*",
            new_run_id,
            "replan_note",
        )
        return new_run_id

    async def handle_label_removed(
        self, *, project_id: int, issue_number: int, author_username: str
    ) -> int:
        """Trigger-tag removal: label-off = cancel at the gate (#29).

        The AzDO mirror of ``GitHubRunService.handle_label_removed`` —
        symmetry with label-on = plan (ADR-0020 §4): runs still parked in
        ``waiting_approval`` are cancelled (the plan was never approved, so
        nothing executed is lost); runs past the gate are untouched — the
        approval consumed that plan, the tag no longer owns it. Returns the
        number of cancelled runs.

        The ingress detection that routes here keys on the trigger tag
        being absent from a ``workitem.updated`` delivery's
        ``System.Tags`` — its exact reach is documented on
        ``azure_webhook.normalize_workitem_updated``.
        """
        if author_username not in self._approvers():
            logger.info(
                "Azure DevOps tag removal by @%s on #%s ignored — not an approver",
                author_username,
                issue_number,
            )
            return 0
        async with self._session_factory() as session:
            runs = (
                (
                    await session.execute(
                        select(FlowRun).where(
                            FlowRun.provider == "azure_devops",
                            FlowRun.project_id == project_id,
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
            await self._post_journaled_comment(
                project_id,
                issue_number,
                f"Run `{run_id[:8]}` **cancelled** — the `forge` tag was removed by "
                f"@{author_username} while its plan waited for approval. Re-add the tag "
                "(or run `/implement`) to plan again.\n\n*This is an automated message.*",
                run_id,
                "cancel_note",
            )
            logger.info(
                "Azure DevOps run %s cancelled — trigger tag removed by @%s",
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
        document = spec.document if isinstance(spec.document, dict) else {}
        digest = document.get("task_digest")
        return str(digest) if digest else None

    async def _gate_consumed(self, run_id: str) -> bool:
        """Whether the run's latest gate decision is already consumed.

        Status alone is not proof: between ``consume_approval`` and the
        PROPOSING transition commit the run still reads ``waiting_approval`` —
        an edit in exactly that window must not cancel an approved run
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
        caller's.
        """
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            run.cancel_requested = True
            # R10: bump the fence so pre-revoke claims lose their grant.
            run.cancellation_generation = (run.cancellation_generation or 0) + 1
            await session.execute(
                update(StepRun)
                .where(StepRun.flow_run_id == run_id, StepRun.status == "scheduled")
                .values(status="cancelled")
            )
            await session.commit()

    async def _replan_interrupted(self, project_id: int, issue_number: int) -> bool:
        """Whether an edit-triggered replan on this work item died mid-step.

        The command step retries with backoff, and the retry must be able to
        finish what the ``202`` promised: the stale run is already cancelled,
        so a plain ``no active run`` would leave the work item run-less. Two
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
                            FlowRun.provider == "azure_devops",
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

    async def _redispatch_revival(self, run_id: str) -> None:
        """Re-dispatch a revived run — same branch, attempt base = last candidate."""
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            project_id = run.project_id
            issue_number = run.issue_iid
        if issue_number is None:
            logger.warning("AzDO revival of run %s without a work item — skipping", run_id[:8])
            return
        if self._lane_pipeline_id():
            await self._advance_harness(run_id, project_id=project_id, issue_number=issue_number)
        else:
            await self._advance_publish(run_id, project_id=project_id, issue_number=issue_number)

    # ------------------------------------------------------------------
    # Publish leg: gate → lane dispatch OR builtin CAS publish
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Publication intents (R11): identity before the HTTP effect
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

    def _intent_scope(self, run: FlowRun) -> str:
        """The logical retry scope of the run's current publication attempt."""
        return f"cycle-{run.commit_cycle or 1}"

    def _intent_repo(self) -> str:
        """The provider-scoped subject the intents are keyed on."""
        return f"{self._project}/{self._repo}"

    async def _load_run(self, run_id: str) -> FlowRun:
        """The run row in a fresh session (intent-identity reads)."""
        async with self._session_factory() as session:
            return await self._get_run(session, run_id)

    async def _publication_intent(
        self, run: FlowRun, *, branch: str, expected_head: str | None
    ) -> PublicationIntent | None:
        """The run's OPEN publication intent for this branch, if any.

        An open intent means a previous attempt's outcome was never durably
        recorded — the caller must PROBE before any new push (never a blind
        replay as reconciliation).
        """
        async with self._session_factory() as session:
            return await find_open_intent(
                session,
                run_id=run.id,
                provider="azure_devops",
                repo=self._intent_repo(),
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
    ) -> PublicationIntent:
        """Create the ``requested`` intent — BEFORE any push-API call.

        The row is committed in its own transaction; its ``operation_key``
        is minted here exactly once and reused by every retry of this
        attempt.
        """
        async with self._session_factory() as session:
            intent = await record_intent(
                session,
                run_id=run.id,
                provider="azure_devops",
                repo=self._intent_repo(),
                target_ref=branch,
                idempotency_scope=self._intent_scope(run),
                operation_key=mint_operation_key(),
                commit_cycle=run.commit_cycle or 1,
                expected_parent_oid=expected_head,
                expected_head=expected_head,
            )
            await session.commit()
            return intent

    async def _probe_intent(self, intent: PublicationIntent) -> tuple[ProbeVerdict, list[str]]:
        """Classify one intent's outcome against the live remote (read-only).

        The identity tuple: exactly one commit carrying this intent's
        ``(forge-op:<key>)`` marker whose parent equals the intent-time
        expected parent proves the push landed.
        """
        try:
            head = await self._stack.client.get_branch_head(
                self._project, self._repo, intent.target_ref
            )
            commits = await self._stack.client.list_commits(
                self._project, self._repo, intent.target_ref
            )
        except Exception:
            # A failed probe read is inconclusive, not negative — no push
            # may be derived from it.
            logger.exception(
                "Publication-intent probe read failed for %s@%s",
                intent.target_ref,
                self._intent_repo(),
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

    async def _complete_intent_from_outcome(
        self, intent_id: str, outcome: AzurePublishOutcome
    ) -> None:
        """Map a push outcome onto the intent's terminal state.

        ``ok`` + ``adopted`` → ``adopted`` (a probe found the effect);
        ``ok`` → ``committed``; ``drift`` → ``duplicated``; any other
        failure → ``failed``.
        """
        if outcome.ok:
            await self._complete_intent(
                intent_id,
                "adopted" if outcome.adopted else "committed",
                provider_object_id=outcome.commit_oid,
                remote_result={
                    "commit_id": outcome.commit_oid,
                    "pr_id": outcome.pr_id,
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

    async def resolve_publication_intents(self, *, now: datetime | None = None) -> int:
        """One recovery pass over this repo's due publication intents (R11).

        Twin of
        :meth:`forge.runs.github_service.GitHubRunService.resolve_publication_intents`:
        probe by identity, adopt (and advance the run) / duplicated /
        unknown; REDISPATCH stays with the run's own publish leg — the
        scanner never pushes.
        """
        now = now or datetime.now(timezone.utc)
        async with self._session_factory() as session:
            intents = await due_intents(
                session, provider="azure_devops", repo=self._intent_repo(), now=now
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
            # landed push is superseded evidence, never adopted-into-READY.
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
            return False  # never dispatched — the run's own probe-first leg owns it

        verdict, hits = await self._probe_intent(intent)
        if verdict is ProbeVerdict.ADOPT:
            await self._complete_intent(
                intent.id,
                "adopted",
                provider_object_id=hits[0],
                remote_result={"commit_id": hits[0], "reconciled": True},
            )
            await self._adopt_committed_candidate(run, intent, commit_oid=hits[0])
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
            return True

        # REDISPATCH: nothing landed, head intact — back the probe off.
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
    ) -> None:
        """Advance a mid-publication run onto a probe-adopted commit (R11)."""
        if run.status not in self._PUBLISHING_STATUSES:
            return
        pr = await self._ensure_draft_pr(
            intent.target_ref, self._target_branch(), run.issue_iid or 0, run.id
        )
        pr_id = int(pr.get("pullRequestId") or 0) if pr else None
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
                        reason=f"publication intent: adopted commit {commit_oid[:8]}",
                    )
                except InvalidTransition:
                    pass  # already past this stage — resume the walk
            run_row = await self._get_run(session, intent.run_id)
            run_row.mr_iid = pr_id
            if commit_oid not in list(run_row.candidate_shas or []):
                run_row.candidate_shas = list(run_row.candidate_shas or []) + [commit_oid]
            run_row.evidence = _merge_evidence(
                run_row.evidence,
                {
                    "published_candidate": {
                        "sha": commit_oid,
                        "base": intent.expected_parent_oid,
                        "branch": intent.target_ref,
                        "pr_id": pr_id,
                        "reconciled": True,
                    }
                },
            )
            await session.commit()

    async def _advance_publish(self, run_id: str, *, project_id: int, issue_number: int) -> None:
        """One gate-approved publish cycle.

        Pipelines lane (ADR-0024 §6): the FROZEN backend is ``ci_harness``
        (the connection was onboarded for lane execution at plan acceptance)
        — the leg dispatches the harness and parks in ``waiting_harness`` —
        the AZ-3 reconciler drives the rest. Builtin (default): propose +
        publish synchronously as on GitHub, ending at ``ready_for_human``.

        R04/A02: the frozen executable spec is THE approved input — the
        task text, the plan artifact and the model route come from it, never
        from live settings or a live work-item re-read. A missing, tampered
        or legacy (v2) spec parks the run (``spec_invalid`` /
        ``spec_legacy``), never a silent fallback.
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
                "Azure DevOps run %s cancelled before publication — dropping in-flight leg",
                run_id[:8],
            )
            return

        # R04/A02: no live work-item re-read — the implementer executes the
        # FROZEN task text the approver saw, whatever the work item shows
        # now.
        issue_title = spec.task_title
        # A minimal stub — the implementer only uses id/issue_iid/base_sha of
        # it (the same duck-typed contract GitHubPublishFlow relies on; typed
        # loosely at the call like the flow does).
        run_stub: Any = _RunStub(
            id=run_id, issue_iid=issue_number, project_id=project_id, base_sha=base_sha
        )
        proposer: Any = self._stack.implementer
        try:
            changeset: ChangeSet = await proposer.propose(
                run_stub,
                issue_title,
                plan_summary=spec.plan_summary,
                attempt_base=base_sha or None,
                task_text=spec.task_text,
                model_route=spec.model_route,
            )
        except Exception as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"proposal_failed: {exc}")
            return

        # R11: resolve the publication intent BEFORE any push-API call — an
        # open intent from a crashed attempt is probed and ADOPTED, never
        # duplicated; a fresh attempt persists the intent first and reuses
        # its stable operation key.
        branch = azure_factory_branch(issue_number, run_id)
        expected_head = base_sha or None
        intent = await self._publication_intent(
            await self._load_run(run_id),
            branch=branch,
            expected_head=expected_head,
        )
        if intent is None:
            intent = await self._record_publication_intent(
                await self._load_run(run_id),
                branch=branch,
                expected_head=expected_head,
            )
        else:
            verdict, hits = await self._probe_intent(intent)
            if verdict is ProbeVerdict.ADOPT:
                await self._complete_intent(
                    intent.id,
                    "adopted",
                    provider_object_id=hits[0],
                    remote_result={"commit_id": hits[0], "reconciled": True},
                )
                pr = await self._ensure_draft_pr(
                    branch, self._target_branch(), issue_number, run_id
                )
                outcome = AzurePublishOutcome(
                    ok=True,
                    commit_oid=hits[0],
                    expected_head_oid=expected_head,
                    branch=branch,
                    pr_id=int(pr.get("pullRequestId") or 0) if pr else None,
                    pr_url=_pr_web_url(pr),
                    adopted=True,
                )
                await self._finish_publish_leg(
                    run_id, project_id, issue_number, outcome, plan_digest
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
                await self._post_journaled_comment(
                    project_id,
                    issue_number,
                    f"Run `{run_id[:8]}` publication outcome is **unresolved** — an operator "
                    f"must inspect branch `{branch}` and reconcile manually.\n\n"
                    "*This is an automated message.*",
                    run_id,
                    "publish_unknown_outcome",
                )
                return

        await self._mark_intent_dispatched(intent)
        outcome = await self._publish_changeset(
            run_id,
            issue_number=issue_number,
            changeset=changeset,
            base_branch=self._target_branch(),
            expected_head=base_sha or None,
            operation_key=intent.operation_key,
        )
        await self._complete_intent_from_outcome(intent.id, outcome)

        if not outcome.ok:
            reason = outcome.reason or "publish_failed"
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED if outcome.drift else FlowStatus.FAILED, reason
            )
            await self._post_journaled_comment(
                project_id,
                issue_number,
                f"Run `{run_id[:8]}` could not publish: {reason}\n\n"
                "*This is an automated message.*",
                run_id,
                "publish_failed",
            )
            return
        await self._finish_publish_leg(run_id, project_id, issue_number, outcome, plan_digest)

    async def _finish_publish_leg(
        self,
        run_id: str,
        project_id: int,
        issue_number: int,
        outcome: AzurePublishOutcome,
        plan_digest: str,
    ) -> None:
        """The shared publish-leg tail: candidate evidence → Draft PR → waiting_ci.

        Runs identically for a fresh push and an ADOPTED previous attempt's
        commit (R11) — the run advances to ``waiting_ci`` on the found sha.
        """
        commit_oid = outcome.commit_oid or ""
        async with self._session_factory() as session:
            controller = Controller(session)
            # Walk the intermediate states the publish leg covered in one
            # synchronous pass: propose → validate → commit → ensure Draft
            # PR. Each journals its outbox row atomically.
            await controller.transition(
                run_id,
                FlowStatus.VALIDATING,
                reason=f"CAS commit pinned to {(outcome.expected_head_oid or '')[:8]}",
            )
            await controller.transition(
                run_id, FlowStatus.COMMITTING, reason=f"candidate {commit_oid[:8]}"
            )
            await controller.transition(
                run_id, FlowStatus.ENSURING_DRAFT_MR, reason=f"Draft PR #{outcome.pr_id}"
            )
            run = await self._get_run(session, run_id)
            run.mr_iid = outcome.pr_id
            run.candidate_shas = list(run.candidate_shas or []) + [commit_oid]
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "published_candidate": {
                        "sha": commit_oid,
                        "base": outcome.expected_head_oid,
                        "branch": outcome.branch,
                        "pr_id": outcome.pr_id,
                        "pr_url": outcome.pr_url,
                        "work_item_link": outcome.work_item_linked,
                    }
                },
            )
            await session.commit()

        await self._post_journaled_comment(
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
                reason=f"Draft PR #{outcome.pr_id} for {commit_oid[:8]}",
            )
            await session.commit()
        # R02: STOP at waiting_ci. The PR's builds are an independent
        # verification gate — the Azure reconciler polls this run and only a
        # verified (or honestly unverified) run continues to review.

    # ------------------------------------------------------------------
    # Pipelines lane leg (ADR-0024 §6): dispatch → waiting_harness
    # ------------------------------------------------------------------

    async def _advance_harness(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_number: int,
        driver: str | None = None,
        repair_context: str = "",
    ) -> None:
        """Dispatch the lane pipeline and park the run in ``waiting_harness``.

        ``proposing`` = ensure the factory branch at the frozen attempt base
        → Runs-API dispatch with the string templateParameters the lane
        template takes (run_id / attempt_base / driver / model /
        work_item_id) → ``waiting_harness`` with the journaled
        :class:`AzurePipelinesHandle` in the run's evidence. The AZ-3
        reconciler polls from here — the wait is worker-free, like
        ``waiting_ci``.

        R13: an exhausted budget (or a spent wall clock) starts no new
        episode — the dispatch is the only enforcement point a
        non-intercepted lane has, so it is checked before any I/O.

        A02/R04: the lane pipeline id, the model input and the dispatched
        driver come from the digest-verified executable spec (the frozen
        pipeline contract) — never live settings. A missing/tampered spec
        blocks the run (``spec_invalid``); a legacy v2 spec parks
        ``spec_legacy: re-approval required``. *driver* overrides the frozen
        selection for a fallback advance.
        """
        # R13: dispatch-time budget gate (partial enforcement — episode
        # count and wall clock are the axes this lane can honestly enforce).
        block = await self._budget_episode_block(run_id)
        if block is not None:
            await self._to_terminal(run_id, FlowStatus.BLOCKED, block)
            return
        spec = await self._spec_or_block(run_id)
        if spec is None:
            return
        pipeline_id = spec.lane_pipeline_id
        if pipeline_id is None:
            # The frozen backend says ci_harness but the document names no
            # lane pipeline — a corrupt dispatch contract, never a
            # live-settings guess.
            await self._to_terminal(
                run_id, FlowStatus.FAILED, "backend_config: no lane pipeline id in the spec"
            )
            return
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            attempt_base = attempt_base_for(run)
            already_waiting = run.status == FlowStatus.WAITING_HARNESS.value
        if driver is None:
            driver = spec.harness_driver
        branch = azure_factory_branch(issue_number, run_id)
        handle = AzurePipelinesHandle(
            provider="azure_devops",
            project=self._project,
            repo=self._repo,
            pipeline_id=pipeline_id,
            run_id=0,
            branch=branch,
            attempt_base=attempt_base,
            run_spec_digest=run.spec_digest or "",
            driver=driver,
            forge_run_id=run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        # Intent-first journal for the lane dispatch (ADR-0005).
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(run_id, "harness_start")
            await session.commit()

        try:
            await self._ensure_harness_branch(branch, attempt_base)
            lane_run: PipelineRun = await self._stack.client.run_pipeline(
                self._project,
                handle.pipeline_id,
                ref_name=f"refs/heads/{branch}",
                template_parameters={
                    # Template parameters cross the REST boundary as strings
                    # (research §6.2 — the lane template takes everything as
                    # strings).
                    "run_id": run_id,
                    "attempt_base": attempt_base,
                    "driver": driver,
                    # R04/A02: the model input is the route frozen in the
                    # spec — the gate approved exactly this execution shape.
                    "model": spec.harness_model,
                    "work_item_id": str(issue_number),
                    # Bounded verification-failure context on a repair
                    # re-dispatch; empty on cycle 1.
                    **({"repair_context": repair_context[:2000]} if repair_context else {}),
                },
            )
        except Exception as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            await self._to_terminal(run_id, FlowStatus.FAILED, f"harness_start_failed: {exc}")
            return

        correlated = handle.with_run_id(lane_run.run_id)
        await self._complete_action(
            action_id,
            "succeeded",
            {
                "pipeline_id": correlated.pipeline_id,
                "pipeline_run_id": correlated.run_id or None,
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
                        f"lane pipeline {correlated.pipeline_id}"
                        + (f" run {correlated.run_id}" if correlated.run_id else " (run pending)")
                    ),
                )
                run = await self._get_run(session, run_id)
            # The durable handle: the AZ-3 reconciler restarts from exactly
            # here (pipeline id, run id, attempt base, started_at).
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "backend": "ci_harness",
                    "harness": {
                        "handle": correlated.to_json(),
                        "pipeline_id": correlated.pipeline_id,
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
            "Run %s delegated to the Pipelines lane (pipeline %s, run %s, driver %s)"
            " — waiting_harness",
            run_id[:8],
            correlated.pipeline_id,
            correlated.run_id or "pending",
            correlated.driver,
        )

        # Taken-in-work ack (same contract as the GitHub lane).
        if correlated.run_id:
            run_url = (
                f"{str(self._settings.FORGE_AZDO_ORG_URL).rstrip('/')}/{self._project}"
                f"/_build/results?buildId={correlated.run_id}"
            )
            await self._post_journaled_comment(
                project_id,
                issue_number,
                (
                    f"## 🔨 Run `{run_id[:8]}` taken into work\n\n"
                    f"- Agent: **{correlated.driver}** in Azure Pipelines\n"
                    f"- Branch: `{branch}`\n"
                    f"- [▶ watch the pipeline live]({run_url})\n\n"
                    "*This is an automated message.*"
                ),
                run_id,
                "taken_in_work_note",
            )

    async def _ensure_harness_branch(self, branch: str, attempt_base: str) -> None:
        """Cut the factory branch at the frozen attempt base; pre-existence is
        idempotent re-entry (a previous leg cut it)."""
        try:
            await self._stack.client.get_branch_head(self._project, self._repo, branch)
        except AzureDevOpsNotFoundError:
            await self._stack.client.create_branch_from(
                self._project, self._repo, branch, attempt_base
            )

    # ------------------------------------------------------------------
    # Builtin publish: CAS push → Draft PR → review
    # ------------------------------------------------------------------

    async def _publish_changeset(
        self,
        run_id: str,
        *,
        issue_number: int,
        changeset: ChangeSet,
        base_branch: str,
        expected_head: str | None,
        operation_key: str | None = None,
    ) -> AzurePublishOutcome:
        """Ensure the factory branch, CAS-push the changeset, open the Draft PR.

        ``expected_head`` pins the frozen base the plan was approved against.
        The branch is cut at that exact commit (``oldObjectId`` = 40 zeros)
        and the push carries it as the CAS — any concurrent movement is
        rejected with ``staleObjectId`` under HTTP 200 and mapped to a drift
        outcome (ADR-0024 §4). Draft PR creation is find-by-head-first (the
        AZ-4 dedupe: an open draft on the same source branch is adopted,
        never duplicated) and the create itself is never replayed — the
        consume-once gate above is the exactly-once guard.
        """
        branch = azure_factory_branch(issue_number, run_id)
        if expected_head is None:
            expected_head = await self._stack.client.get_branch_head(
                self._project, self._repo, base_branch
            )
        key = operation_key or uuid4().hex[:12]
        commits = _changeset_to_commits(changeset, operation_key=key)

        try:
            await self._ensure_factory_branch(branch, expected_head)
            result = await self._stack.client.push_commits(
                self._project,
                self._repo,
                branch,
                expected_old_sha=expected_head,
                commits=commits,
            )
        except AzureDevOpsDriftError as exc:
            # staleObjectId proves only that the ref moved — NOT that nothing
            # landed (research §5): a previous attempt's push may have gone
            # through with its response lost. Probe for THIS intent's marker
            # + expected parent before declaring drift; adopt one match.
            adopted_oid = await self._probe_for_intent(
                branch, operation_key=key, expected_head=expected_head
            )
            if adopted_oid is not None:
                logger.warning(
                    "Azure DevOps push on %s stale but previous attempt's commit %s found "
                    "by marker (forge-op:%s) — adopting",
                    branch,
                    adopted_oid[:8],
                    key,
                )
                pr = await self._ensure_draft_pr(branch, base_branch, issue_number, run_id)
                return AzurePublishOutcome(
                    ok=True,
                    commit_oid=adopted_oid,
                    expected_head_oid=expected_head,
                    branch=branch,
                    pr_id=int(pr.get("pullRequestId") or 0) if pr else None,
                    pr_url=_pr_web_url(pr),
                    adopted=True,
                )
            logger.warning(
                "Azure DevOps publish on %s drifted (%s) — reporting, not retrying",
                branch,
                exc.status,
            )
            return AzurePublishOutcome(
                ok=False,
                reason=f"branch_drift: {exc}",
                expected_head_oid=expected_head,
                branch=branch,
                drift=True,
            )
        except AzureDevOpsError as exc:
            return AzurePublishOutcome(
                ok=False,
                reason=f"publish_failed: {exc}",
                expected_head_oid=expected_head,
                branch=branch,
            )
        commit_oid = _push_new_object_id(result) or ""

        pr = await self._ensure_draft_pr(branch, base_branch, issue_number, run_id)
        work_item_linked: bool | None = None
        if pr is not None:
            # The create-body workItemRefs is unreliable (research §4.6) — the
            # reliable link is the WIT ArtifactLink PATCH, best-effort: a
            # failed link is evidence, never a publish failure.
            work_item_linked = await _link_work_item_to_pr(
                self._stack.client, self._project, issue_number, pr
            )
        return AzurePublishOutcome(
            ok=True,
            commit_oid=commit_oid,
            expected_head_oid=expected_head,
            branch=branch,
            pr_id=int(pr.get("pullRequestId") or 0) if pr else None,
            pr_url=_pr_web_url(pr),
            work_item_linked=work_item_linked,
        )

    async def _probe_for_intent(
        self, branch: str, *, operation_key: str, expected_head: str
    ) -> str | None:
        """The branch commit carrying THIS intent's marker + parent, or None.

        Exactly one commit whose comment contains ``(forge-op:<key>)`` AND
        whose parent is *expected_head* proves a previous attempt of this
        intent landed — its id is adopted. Zero or several matches return
        None (drift / inconclusive).
        """
        try:
            commits = await self._stack.client.list_commits(self._project, self._repo, branch)
        except AzureDevOpsError:
            logger.exception("Intent probe read failed for %s@%s", branch, self._repo)
            return None
        hits = commit_matches(
            commits, operation_key=operation_key, expected_parent_oid=expected_head
        )
        return hits[0] if len(hits) == 1 else None

    async def _ensure_factory_branch(self, branch: str, sha: str) -> None:
        """Create the factory branch at *sha*; tolerate pre-existence.

        A pre-existing branch (a previous publish leg's re-entry) is fine —
        the push CAS below decides whether the head still matches.
        """
        try:
            await self._stack.client.get_branch_head(self._project, self._repo, branch)
        except AzureDevOpsNotFoundError:
            await self._stack.client.create_branch_from(self._project, self._repo, branch, sha)

    async def _ensure_draft_pr(
        self, branch: str, base_branch: str, issue_number: int, run_id: str
    ) -> dict | None:
        """Open the Draft PR (isDraft — the only kind forge creates, ADR-0024
        §5). Find-by-head-first: an OPEN draft already on this source branch
        (a previous leg's creation that the run row lost track of) is
        adopted instead of forked — ``create_draft_pr`` is non-idempotent.
        A failed creation must not undo the commit; the caller records
        the commit and the reconciler story owns PR re-ensurance."""
        title = f"forge: implement #{issue_number} (run {short_run_id(run_id)})"
        description = (
            f"Draft implementation by forge for work item #{issue_number} "
            f"(run {short_run_id(run_id)}).\n\n"
            "*Merging is a human decision — forge never merges.*"
        )
        try:
            existing = await self._stack.client.find_draft_pr_by_head(
                self._project, self._repo, branch
            )
            if existing is not None:
                logger.info(
                    "Open draft PR %s already exists on %s — adopting it",
                    existing.get("pullRequestId"),
                    branch,
                )
                return existing
            return await self._stack.client.create_draft_pr(
                self._project,
                self._repo,
                branch,
                base_branch,
                title,
                description,
            )
        except AzureDevOpsError as exc:
            logger.error("Draft PR creation failed for %s: %s", branch, exc)
            return None

    async def _review_and_ready(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_number: int,
        pr_id: int | None,
        candidate_sha: str,
        base_sha: str,
        verified: bool = True,
        verification_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        """Reviewing → ready_for_human.

        ``verified=False`` (no CI built the candidate) is honest: the run
        still reaches the human, but the reason says ``unverified`` instead
        of implying builds passed (R02). The reason wording and the
        finalization iron checks come from :mod:`forge.runs.consistency`
        (ADR-0027) — the GitLab leg's one source, not an Azure fork of it.

        R04/A02: the review brief reads the FROZEN task title and plan
        artifact from the executable spec — never a live work-item read. A
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
                "Azure DevOps run %s replays its persisted review of %s — no second model call",
                run_id[:8],
                candidate_sha[:8],
            )
        else:
            try:
                review = await self._stack.reviewer.review(
                    project=self._project,
                    repo=self._repo,
                    pr_id=pr_id or 0,
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

        # ADR-0027: one reason source and the iron finalization checks,
        # shared with the GitLab/GitHub legs (forge.runs.consistency).
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
        logger.info("Azure DevOps run %s is ready_for_human at %s", run_id[:8], candidate_sha[:8])

    # ------------------------------------------------------------------
    # Pipelines lane adoption (the AZ-3 reconciler's drive end)
    # ------------------------------------------------------------------

    async def evaluate_waiting_harness_one(self, run_id: str, now: datetime | None = None) -> None:
        """Poll one waiting_harness run through its journaled lane handle.

        The twin of ``GitHubRunService._evaluate_harness_one``: a
        dispatched-but-uncorrelated handle is re-discovered first
        (ADR-0005), a running lane keeps waiting on the durable deadline,
        an infrastructure/code failure blocks ``harness_{kind}`` (the
        frozen-chain fallback is a follow-up on this provider), and a
        succeeded lane hands its candidate bundle to the SAME trusted
        publication tail the builtin path uses.

        R17 (deadline-before-I/O): the FIRST operations of every evaluation
        are local deadline/cancel checks over the journaled handle — no
        provider call is made once the harness budget is spent, so a
        permanently erroring Pipelines API can never hold a run past its
        ``harness_timeout``. Discovery is bounded the same way (R17): a
        dispatch that never surfaces retries only up to
        ``FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS`` before the run parks
        blocked with the precise reason.

        R04/A02: the timeout is the ``harness_timeout`` frozen in the spec;
        a missing/tampered/legacy spec parks the run instead of guessing.
        """
        now = now or datetime.now(timezone.utc)
        spec = await self._spec_or_block(run_id)
        if spec is None:
            return
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
        journaled = AzurePipelinesHandle.from_json(raw_handle)

        # --- R17: local deadline / grant check BEFORE any provider I/O ----
        if cancel_requested:
            # F13: the publication grant is revoked — stand down without
            # touching the provider. The late candidate is recorded as
            # superseded either way.
            await self._merge_run_evidence(
                run_id,
                {
                    "superseded": {
                        "reason": "cancelled",
                        "attempt_base": journaled.attempt_base,
                    }
                },
            )
            logger.info("Run %s cancelled — harness evaluation stood down pre-poll", run_id[:8])
            return
        timeout = spec.harness_timeout
        started = _parse_journaled_time(journaled.started_at)
        if started is not None and as_aware_utc(now) > as_aware_utc(started) + timedelta(
            seconds=timeout
        ):
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "harness_infrastructure: harness_timeout"
            )
            return

        executor = AzurePipelinesExecutor(self._stack.client, self._settings)

        # The journal speaks the service-handle dialect (AZ-2); the executor
        # speaks the execution-handle one (AZ-3). Convert at the boundary;
        # the run id adopted from discovery journals back in the service
        # dialect so the on-disk contract stays stable.
        def _to_executor(h: AzurePipelinesHandle) -> ExecutorHandle:
            return ExecutorHandle(
                provider="azure_devops",
                project=h.project,
                repo_id=h.repo,
                pipeline_id=h.pipeline_id,
                run_id=h.run_id,
                branch=f"refs/heads/{h.branch}",
                attempt_base_sha=h.attempt_base,
                run_spec_digest=h.run_spec_digest,
                driver=h.driver,
                model="",
                work_item_id="",
                forge_run_id=h.forge_run_id,
                started_at=h.started_at,
            )

        try:
            if not journaled.run_id:
                discovered = await executor.reconcile_launch(_to_executor(journaled))
                if discovered.run_id:
                    journaled = journaled.with_run_id(discovered.run_id)
                    await self._merge_run_evidence(
                        run_id,
                        {
                            "harness": {
                                **(evidence.get("harness") or {}),
                                "handle": journaled.to_json(),
                                "run_id": journaled.run_id,
                                "discovery_attempts": 0,
                            }
                        },
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
                            f"discovery found no pipeline run after {attempts} attempts",
                        )
                        return
                    harness_fragment = dict(evidence.get("harness") or {})
                    harness_fragment["discovery_attempts"] = attempts
                    await self._merge_run_evidence(run_id, {"harness": harness_fragment})
                    return  # remaining discovery attempts retry next tick
            outcome = await executor.poll(_to_executor(journaled), now=now)
        except Exception:
            logger.exception(
                "Pipelines harness poll failed for run %s — keeping it waiting", run_id[:8]
            )
            return

        if outcome.status == "running":
            return  # keep waiting — the durable deadline decides the rest

        if outcome.status == "failed":
            kind = outcome.failure_kind or "code"
            await self._to_terminal(run_id, FlowStatus.BLOCKED, f"harness_{kind}: {outcome.reason}")
            return

        await self._publish_harness_candidate(run_id, project_id, issue_number, outcome, journaled)

    async def _publish_harness_candidate(
        self,
        run_id: str,
        project_id: int,
        issue_number: int,
        outcome: Any,
        handle: AzurePipelinesHandle,
    ) -> None:
        """Well-formed candidate bundle → trusted CAS publish → PR → review.

        The bundle crosses the SAME boundary as the builtin path
        (ADR-0016 §2): strict materialization against the authoritative
        attempt-base contents (via the repository reader), spec-scope
        validation, then ONE branch-CAS push through ``_publish_changeset``
        — which also posts the evidence comment and runs review →
        ready_for_human.
        """
        bundle = outcome.bundle
        if bundle is None:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "harness outcome without candidate bundle"
            )
            return
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
                "Run %s cancelled — Pipelines candidate on %s recorded as superseded",
                run_id[:8],
                bundle.attempt_base_oid[:8],
            )
            return

        await self._transition(
            run_id,
            FlowStatus.COMMITTING,
            reason=f"publishing Pipelines candidate on {bundle.attempt_base_oid[:8]}",
        )

        # Authoritative full-content reads at the attempt base for modify
        # entries (the same strict materialization the GitHub lane uses).
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
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, f"harness_candidate_invalid: {reason}: {exc}"
            )
            return

        branch = azure_factory_branch(issue_number, run_id)
        changeset = ChangeSet(
            branch=branch,
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

        # R11: resolve the publication intent BEFORE the push-API call — an
        # open intent from a crashed attempt is probed and ADOPTED, never
        # duplicated; a fresh attempt persists the intent first and reuses
        # its stable operation key.
        branch = azure_factory_branch(issue_number, run_id)
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
                    remote_result={"commit_id": hits[0], "reconciled": True},
                )
                pr = await self._ensure_draft_pr(
                    branch, self._target_branch(), issue_number, run_id
                )
                publish_outcome = AzurePublishOutcome(
                    ok=True,
                    commit_oid=hits[0],
                    expected_head_oid=bundle.attempt_base_oid,
                    branch=branch,
                    pr_id=int(pr.get("pullRequestId") or 0) if pr else None,
                    pr_url=_pr_web_url(pr),
                    adopted=True,
                )
                logger.warning(
                    "Run %s adopting previous attempt's commit %s (intent %s)",
                    run_id[:8],
                    hits[0][:8],
                    intent.id[:8],
                )
                await self._finish_harness_publish_leg(
                    run_id, project_id, issue_number, publish_outcome, handle
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
                return

        await self._mark_intent_dispatched(intent)
        publish_outcome = await self._publish_changeset(
            run_id,
            issue_number=issue_number,
            changeset=changeset,
            base_branch=self._target_branch(),
            expected_head=bundle.attempt_base_oid,
            operation_key=intent.operation_key,
        )
        await self._complete_intent_from_outcome(intent.id, publish_outcome)
        if not publish_outcome.ok:
            await self._to_terminal(
                run_id,
                FlowStatus.BLOCKED,
                f"harness_publish_failed: {publish_outcome.reason or 'unknown'}",
            )
            return
        await self._finish_harness_publish_leg(
            run_id, project_id, issue_number, publish_outcome, handle
        )

    async def _finish_harness_publish_leg(
        self,
        run_id: str,
        project_id: int,
        issue_number: int,
        publish_outcome: AzurePublishOutcome,
        handle: AzurePipelinesHandle,
    ) -> None:
        """The shared Pipelines-candidate tail: evidence → Draft PR → waiting_ci.

        Runs identically for a fresh push and an ADOPTED previous attempt's
        commit (R11).
        """

        # The publication tail — the SAME walk the builtin leg runs after
        # _publish_changeset: state walk, candidate evidence, the work-item
        # evidence comment, then review → ready_for_human.
        commit_oid = publish_outcome.commit_oid or ""
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(
                run_id,
                FlowStatus.ENSURING_DRAFT_MR,
                reason=f"Draft PR #{publish_outcome.pr_id}",
            )
            run = await self._get_run(session, run_id)
            run.mr_iid = publish_outcome.pr_id
            run.candidate_shas = list(run.candidate_shas or []) + [commit_oid]
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "published_candidate": {
                        "sha": commit_oid,
                        "base": publish_outcome.expected_head_oid,
                        "branch": publish_outcome.branch,
                        "pr_id": publish_outcome.pr_id,
                        "pr_url": publish_outcome.pr_url,
                        "work_item_link": publish_outcome.work_item_linked,
                        "lane_run_id": handle.run_id,
                    }
                },
            )
            await session.commit()

        plan_digest = run.plan_digest or ""
        await self._post_journaled_comment(
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
                reason=f"Draft PR #{publish_outcome.pr_id} for {commit_oid[:8]}",
            )
            await session.commit()
        # R02: STOP at waiting_ci. The published candidate's builds are an
        # independent verification gate (the lane run was execution, not
        # verification) — the Azure reconciler polls this run and only a
        # verified (or honestly unverified) run continues to review.

    # ------------------------------------------------------------------
    # Verification gate (R02): the waiting_ci reconciler pass
    # ------------------------------------------------------------------

    async def evaluate_waiting_ci_one(self, run_id: str, now: datetime | None = None) -> None:
        """One verification pass over a waiting_ci run (R02).

        The candidate commit's builds are an independent gate: pending →
        keep waiting (bounded); any red build → ADR-0008 repair-in-place
        while commit cycles remain, else ``blocked(quality_contract)``; all
        green → review; NO builds at all after the grace window → review as
        honestly **unverified** (evidence records it; the ready reason says
        so — never presented as verified).

        R17 (deadline-before-I/O): the verification budget and the cancel
        grant are evaluated LOCALLY before the provider is touched — a
        permanently erroring Builds API can keep the run waiting only up to
        ``FORGE_VERIFICATION_TIMEOUT_SECONDS``, never past it.

        R04/A02: the frozen executable spec is read digest-verified on every
        pass — it names the lane pipeline that is execution (excluded from
        the verification surface) and carries the required-checks contract
        (the check-proof semantics are a later issue; the REQUIRED list is
        frozen now). A missing/tampered spec blocks the run
        (``spec_invalid``); a legacy v2 spec parks
        ``spec_legacy: re-approval required``.
        """
        now = now or datetime.now(timezone.utc)
        spec = await self._spec_or_block(run_id)
        if spec is None:
            return
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return
            if run.status != FlowStatus.WAITING_CI.value:
                return  # cancelled/advanced elsewhere — superseded, never revived
            candidate_shas = list(run.candidate_shas or [])
            candidate_sha = candidate_shas[-1] if candidate_shas else (run.base_sha or "")
            issue_number = run.issue_iid or 0
            project_id = run.project_id
            waiting_since = run.updated_at  # the WAITING_CI transition moment
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

        # R17: the local deadline fires even when the Builds API keeps
        # erroring (the stalled-provider stall this bound exists for).
        deadline = int(getattr(self._settings, "FORGE_VERIFICATION_TIMEOUT_SECONDS", 1800) or 1800)
        started = as_aware_utc(waiting_since) if waiting_since is not None else None
        if started is not None and (as_aware_utc(now) - started).total_seconds() > deadline:
            await self._to_terminal(
                run_id, FlowStatus.BLOCKED, "verification_timeout: builds did not conclude"
            )
            return

        try:
            builds = await self._candidate_builds(
                candidate_sha, since=waiting_since, lane_pipeline_id=spec.lane_pipeline_id
            )
        except Exception:
            logger.exception("Builds read failed for %s — keeping it waiting", run_id[:8])
            return

        if not builds:
            # RACE GUARD: a just-pushed branch's CI build takes a few seconds
            # to queue (branch policies create the validation build on PR
            # creation). Hold the run for a grace window before declaring
            # not_configured — a deliberate 0 must stay 0.
            _raw = getattr(self._settings, "FORGE_VERIFICATION_GRACE_SECONDS", None)
            grace = 120 if _raw is None else int(_raw)
            started = as_aware_utc(waiting_since) if waiting_since is not None else None
            now_aware = as_aware_utc(now)
            if started is not None and (now_aware - started).total_seconds() < grace:
                return  # keep waiting — builds may still queue
            # No CI build ran for this candidate — proceed to review as
            # honestly unverified (R02: never presented as verified).
            verification_fragment = ready_evidence(
                False,
                candidate_sha,
                PRODUCER_AZURE_BUILD,
                summary="no CI build ran for the candidate commit",
                status="not_configured",
            )
            await self._merge_run_evidence(
                run_id,
                {"verification": verification_fragment},
            )
            logger.info("No CI builds configured for %s — review as unverified", run_id[:8])
            await self._transition(
                run_id, FlowStatus.EVALUATING_CI, reason="no CI configured — unverified"
            )
            await self._review_and_ready(
                run_id,
                project_id=project_id,
                issue_number=issue_number,
                pr_id=run.mr_iid,
                candidate_sha=candidate_sha,
                base_sha=run.base_sha or "",
                verified=False,
                verification_evidence=verification_fragment,
            )
            return

        pending = [b for b in builds if str(b.get("status") or "") in _ACTIVE_BUILD_STATUSES]
        if pending:
            # Bounded (R17): the deadline itself is enforced pre-I/O above —
            # a Builds API that never answers cannot hold the run forever.
            return

        red = [b for b in builds if str(b.get("result") or "") in _RED_BUILD_RESULTS]
        if red:
            # ADR-0008: an independent build failure blames the change —
            # bounded repair while cycles remain, else an honest blocked
            # state with the failing build names.
            names = ", ".join(sorted({_build_name(b) for b in red}))
            await self._begin_repair(
                run_id,
                project_id=project_id,
                issue_number=issue_number,
                failure_kind="code",
                failure_reason=f"builds failed ({names})",
            )
            return

        # ADR-0027: the unified R02 evidence shape via forge.runs.consistency.
        verification_fragment = ready_evidence(
            True,
            candidate_sha,
            PRODUCER_AZURE_BUILD,
            summary="all candidate builds succeeded",
            surface=(
                {"name": _build_name(b), "result": str(b.get("result") or "")} for b in builds
            ),
        )
        await self._merge_run_evidence(
            run_id,
            {"verification": verification_fragment},
        )
        await self._transition(
            run_id, FlowStatus.EVALUATING_CI, reason="candidate builds succeeded"
        )
        await self._review_and_ready(
            run_id,
            project_id=project_id,
            issue_number=issue_number,
            pr_id=run.mr_iid,
            candidate_sha=candidate_sha,
            base_sha=run.base_sha or "",
            verified=True,
            verification_evidence=verification_fragment,
        )

    async def _candidate_builds(
        self,
        candidate_sha: str,
        *,
        since: datetime | None,
        lane_pipeline_id: int | None = None,
    ) -> list[dict]:
        """Completed/running builds whose sourceVersion IS the candidate commit.

        The Builds API has no ``sourceVersion`` filter (research §6.6
        correction #1) — the commit correlation is client-side over the
        documented ``repositoryId``/``minTime``/``$top`` query, exactly like
        the executor's discovery. The lane pipeline is execution, not
        verification, and is excluded (the GitHub harness-workflow rule) by
        the pipeline id FROZEN in the spec — *lane_pipeline_id* (A02); a
        live-settings read here would let a post-approval lane change move
        the verification surface.
        """
        min_time = as_aware_utc(since) - timedelta(minutes=10) if since is not None else None
        builds = await self._stack.client.list_builds_by_repository(
            self._project,
            self._repo,
            min_time=min_time,
            top=25,
        )
        wanted = candidate_sha.lower()
        lane = lane_pipeline_id
        matched: list[dict] = []
        for build in builds:
            if str(build.get("sourceVersion") or "").lower() != wanted:
                continue
            if lane is not None and _build_definition_id(build) == lane:
                continue
            matched.append(build)
        return matched

    async def _begin_repair(
        self,
        run_id: str,
        *,
        project_id: int,
        issue_number: int,
        failure_kind: str,
        failure_reason: str,
    ) -> bool:
        """Red-dispatch the lane as a bounded repair cycle (ADR-0008).

        The exact GitHub ``_begin_repair`` semantics on the Pipelines lane:
        only while commit cycles remain; the walk is
        waiting_ci → evaluating_ci → proposing (graph-legal edges); the
        cycle counter bumps durably and ``_advance_harness`` re-dispatches
        with the bounded failure context riding as a dispatch input, so the
        lane agent fixes its own candidate. Cycles exhausted → an honest
        ``blocked(quality_contract)``.

        R04/A02: the commit-cycle ceiling is the one frozen in the spec —
        a post-approval settings change cannot extend the approved budget.
        A missing/tampered/legacy spec parks the run instead of guessing.
        """
        spec = await self._spec_or_block(run_id)
        if spec is None:
            return False
        if spec.backend != "ci_harness":
            # ADR-0015/A02: the frozen backend is builtin — the run has no
            # harness repair leg, and a lane onboarded after the freeze must
            # never upgrade it to a dispatch the gate did not approve.
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
        )
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _find_active_run(self, project_id: int, issue_number: int) -> FlowRun | None:
        """The latest non-terminal Azure DevOps run for the (project, work
        item), or None."""
        terminal = {status.value for status in TERMINAL_STATUSES}
        async with self._session_factory() as session:
            run = (
                (
                    await session.execute(
                        select(FlowRun)
                        .where(
                            FlowRun.provider == "azure_devops",
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
            "## Forge — a run is already active on this work item\n\n"
            f"Run `{run.id}` is **{run.status}** — a new `/implement` would fork the "
            "branch and Draft PR.\n\n"
            f"- Approve it: `/go {run.id}`\n"
            f"- Cancel it first: `/cancel {run.id}`"
        )

    def _approvers(self) -> list[str]:
        """The trusted approver list (AzDO identities, connection-scoped).

        ``FORGE_AZDO_APPROVERS`` when set, else the shared ``FORGE_APPROVERS``
        fallback; sorted so the policy digest is insensitive to order.
        """
        return sorted(approvers_for("azure_devops", self._settings))

    def _required_jobs(self) -> list[str]:
        raw = getattr(self._settings, "FORGE_REQUIRED_JOBS", "") or ""
        return [name.strip() for name in raw.split(",") if name.strip()]

    async def _read_spec_allowed_paths(self, run_id: str) -> list[str]:
        """The frozen RunSpec's allowed_paths scope (v0.7 monorepo scoping)."""
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
            return []
        document = spec.document if isinstance(spec.document, dict) else {}
        return [str(g) for g in (document.get("allowed_paths") or [])]

    def _target_branch(self) -> str:
        return str(getattr(self._settings, "FORGE_TARGET_BRANCH", "main") or "main")

    def _lane_pipeline_id(self) -> int | None:
        """The lane pipeline id, or None for the builtin lane (ADR-0024 §6)."""
        raw = getattr(self._settings, "FORGE_AZDO_LANE_PIPELINE_ID", None)
        return int(raw) if raw else None

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

        The lane here is the Pipelines pipeline (``FORGE_AZDO_LANE_PIPELINE_ID``);
        with it set the compiled backend is ``ci_harness:<driver>`` and the
        driver id set is validated (tighten-only, ADR-0015). Without it the
        builtin lane dispatches no harness, so only the id set is validated.

        R31/A02 (GitLab parity): the compilable lanes are the project's
        available-driver manifest (:func:`resolve_available_drivers`; unset
        — the shipped driver set), so a driver the project did not onboard
        is never selected — not by the preference, not by the planner's
        proposal ({"harness", "budget_class", "reason"} from the stack
        planner's ``last_plan``, honored as policy-constrained ranking).
        R13/A02: the budget class's numeric profile resolves AT FREEZE TIME
        and rides ON the selection — ``None`` (no finite profile) attaches
        nothing.
        """
        preference = resolve_preference(self._config, self._settings)
        lane = self._lane_pipeline_id()
        validate_preference(preference, self._harness_driver() if lane else None)
        available = resolve_available_drivers(self._config, self._settings) or set(SHIPPED_DRIVERS)
        selection = compile_harness_selection(
            preference,
            f"ci_harness:{self._harness_driver()}" if lane else "builtin",
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

    def _planner_harness_proposal(self) -> dict | None:
        """R31: the planner's optional harness proposal, leniently read.

        Mirrors the GitLab/GitHub discipline: the stack planner's
        ``last_plan`` may carry ``harness`` / ``budget_class`` / ``reason``;
        anything missing, non-string or empty yields no proposal. The
        planner is never the authority — every field is re-validated by
        :func:`compile_harness_selection`.
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

        The dispatch-time gate for the Pipelines lane (A02 parity with the
        GitLab/GitHub lanes): its model calls happen inside the pipeline job
        forge cannot intercept, so the wall clock and the start of new
        episodes are enforced HERE, at the dispatch boundary.
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

        The lane pipeline id is part of the policy (the
        ``harness_workflow`` analog, ADR-0020 §3): a changed dispatch target
        invalidates approval like any other execution profile change.
        """
        lane = self._lane_pipeline_id()
        document = {
            "approvers": self._approvers(),
            "target_branch": self._target_branch(),
            "required_jobs": self._required_jobs(),
            "implementer_backend": "ci_harness" if lane else "builtin",
            "harness_model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
            "harness_preference": resolve_preference(self._config, self._settings),
            "harness_fallback": bool(getattr(self._settings, "FORGE_HARNESS_FALLBACK", False)),
        }
        if lane:
            document["lane_pipeline_id"] = lane
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
    ) -> dict:
        """The immutable, EXECUTABLE RunSpec document (F14, R04, A02).

        The same typed v3 document the GitLab and GitHub lanes freeze
        (:class:`~forge.runs.spec.ExecutableRunSpec`): the task text, the
        plan artifact, the model route, the tool/path policy, the required
        checks (the check-proof semantics are a later issue; the REQUIRED
        list is frozen now), the budgets (lifecycle limits always; the R13
        numeric ceilings when a finite profile resolves) and the
        backend/driver. ``lane_pipeline_id`` freezes the Pipelines dispatch
        contract so a spec change means a different lane run.
        ``allowed_paths`` (v0.7 monorepo scoping) is present only for scoped
        runs. Post-approval legs read the stored document through
        :meth:`_load_executable_spec` (digest-verified on every read), never
        live Settings.
        """
        selection = harness_selection or self._compile_harness_selection()
        lane = self._lane_pipeline_id()
        backend = "ci_harness" if lane else "builtin"
        # R13/A02: the budget class's numeric profile is resolved AT FREEZE
        # TIME and stored IN the spec — the gate approves exactly these
        # ceilings and the honest enforcement level of this lane. ``None``
        # freezes no ceiling fields at all (byte-compatible).
        limits = self._budget_limits_for_class(selection.budget_class)
        enforcement = budget_enforcement_for_backend(backend) if limits is not None else ""
        spec = ExecutableRunSpec.freeze(
            provider="azure_devops",
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
            lane_pipeline_id=lane,
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
                self._project, self._repo, self._target_branch()
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

    def _plan_comment(
        self, run_id: str, plan: str, digest: str, harness_selection: HarnessSelection
    ) -> str:
        # Bare commands suffice (the bot identity's mentions would notify an
        # unrelated account) — the same rule as the GitHub plan comment.
        approvers = self._approvers()
        approver_note = (
            ", ".join(f"`{name}`" for name in approvers)
            or "none configured — set `FORGE_AZDO_APPROVERS`"
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
            f"Approve this exact plan by commenting `/go {run_id}`.\n\n"
            f"Approvers: {approver_note}.\n\n"
            "*This is an automated message.*"
        )

    @staticmethod
    def _evidence_comment(outcome: AzurePublishOutcome, plan_digest: str) -> str:
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

    async def _post_journaled_comment(
        self, project_id: int, issue_number: int, body: str, run_id: str | None, kind: str
    ) -> None:
        """Post a work-item comment with intent/outcome journaling (ADR-0005)."""
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, kind, correlation_id=f"workitem-{issue_number}"
            )
            await session.commit()
        try:
            note = await self._stack.client.add_work_item_comment(self._project, issue_number, body)
        except Exception as exc:
            await self._complete_action(action_id, "failed", {"error": str(exc)})
            raise
        note_id = note.get("commentId") if isinstance(note, dict) else None
        await self._complete_action(action_id, "succeeded", {"comment_id": note_id})

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
        logger.warning("Azure DevOps run %s -> %s: %s", run_id[:8], status.value, reason)


# Kept identical to the GitHub dispatch: start_run fetches the subject, go
# and cancel pass the note through.
async def execute_azure_run_command(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    metadata: dict,
    *,
    stack_factory: Callable[[str, str], AzureAgents] | None = None,
) -> None:
    """Execute an Azure DevOps run command — the ``provider: azure_devops`` step
    dispatch.

    Wired from :func:`forge.runs.service.execute_run_command` (ADR-0024
    AZ-2). The *stack_factory* hook exists for tests to run the service over
    a fake client + stub agents (no network, no model).

    ``review_pr`` dispatches to the reactive review lane
    (:mod:`forge.reactive.azure_review`) and ``debug_ci`` to the CI debug
    lane (:mod:`forge.reactive.azure_ci_debug`) — separate lanes beside the
    durable run path below: no FlowRun, no RunSpec, one step in/one comment
    out (the same shape as the GitHub dispatch).
    """
    command = metadata.get("command")
    if command == "review_pr":
        from forge.reactive.azure_review import execute_azure_reactive_review

        await execute_azure_reactive_review(
            settings, forge_config, session_factory, metadata, stack_factory=stack_factory
        )
        return
    if command == "debug_ci":
        from forge.reactive.azure_ci_debug import execute_azure_debug_ci_command

        await execute_azure_debug_ci_command(settings, forge_config, session_factory, metadata)
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
        logger.warning("Unknown Azure DevOps run command %r — ignoring", command)
        return
    project = str(metadata.get("project") or "")
    repo_full_name = str(metadata.get("repo_full_name") or "")
    if "/" not in repo_full_name:
        # Work-item commands carry no repository (the payload has none) —
        # resolve the connection's target repo for the project.
        repo_full_name = f"{project}/{await _resolve_repo_name(settings, forge_config, project)}"
    project_name, _, repo_name = repo_full_name.partition("/")
    project_id = int(metadata.get("project_id") or 0)
    issue_number = int(metadata.get("issue_number") or 0)
    author_username = str(metadata.get("author_username") or "")
    note_text = str(metadata.get("note_text") or "")

    if stack_factory is None:
        stack_factory = lambda p, r: build_azure_agents(  # noqa: E731 — trivial default
            settings, session_factory, p, r
        )
    stack = stack_factory(project_name, repo_name)
    service = AzureRunService(
        session_factory, settings, forge_config, stack=stack, repo_full_name=repo_full_name
    )

    try:
        if command == "start_run":
            work_item = await stack.client.get_work_item(project_name, issue_number)
            fields = work_item.get("fields") or {}
            await service.start_run(
                project_id=project_id,
                issue_number=issue_number,
                issue_title=str(fields.get("System.Title") or f"work item {issue_number}"),
                # System.Description is HTML — stripped defensively before it
                # enters a prompt (research §5.1).
                issue_description=_strip_html(str(fields.get("System.Description") or "")),
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
        elif command == "cancel":
            await service.handle_cancel(
                project_id=project_id,
                issue_number=issue_number,
                note_text=note_text,
                author_username=author_username,
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
            # payload's fields); a sparse delivery is repaired by ONE API
            # read inside the handler.
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
            logger.warning("Unknown Azure DevOps run command %r — ignoring", command)
    finally:
        aclose = getattr(stack.client, "aclose", None)
        if aclose is not None:
            await aclose()


async def _resolve_repo_name(
    settings: Settings,
    forge_config: ForgeConfig,
    project: str,
    *,
    client: AzureDevOpsClient | None = None,
) -> str:
    """The target repo for repo-less work-item commands.

    Resolution order, most-explicit first:

    1. ``forge.yml``'s ``azure_devops.default_repos`` mapping (project →
       repo, or the single ``default_repo``) — explicit human configuration
       wins because the API cannot disambiguate a multi-repo project.
    2. :meth:`~forge.integrations.azure.AzureDevOpsClient.list_repositories`
       — exactly one repository in the project, or one named like the
       project (AzDO's default-repository rule), resolves the name over the
       API. The AZ-1 client had no repositories-list surface and guessed the
       project name; this is that workaround's documented fix.
    3. The project name — AzDO's default repository shares it. Still
       validated by the first repo API call the run makes: a wrong name
       fails the run visibly.

    A repositories-list failure (unreachable org, unscoped PAT, no PAT on
    the connection) degrades to the fallback — never fails the command.
    *client* is the test seam; a real one is built (and closed) only when
    the API leg is reached.
    """
    azdo_config = forge_config.get("azure_devops")
    if isinstance(azdo_config, dict):
        repos = azdo_config.get("default_repos")
        if isinstance(repos, dict):
            repo = str(repos.get(project) or "").strip()
            if repo:
                return repo
        default = str(azdo_config.get("default_repo") or "").strip()
        if default:
            return default

    try:
        if client is None:
            client = AzureDevOpsClient(
                base_url=str(getattr(settings, "FORGE_AZDO_ORG_URL", "") or ""),
                token=azure_credentials_from_settings(settings),
            )
            owned_client = True
        else:
            owned_client = False
    except ValueError:
        # No usable PAT on the connection — nothing to resolve with.
        return project
    try:
        repositories = await client.list_repositories(project)
    except Exception:
        logger.warning(
            "Repository listing failed for project %s — falling back to the default name",
            project,
            exc_info=True,
        )
        return project
    finally:
        if owned_client:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()

    names = [str(repo.get("name") or "") for repo in repositories if repo.get("name")]
    if len(names) == 1:
        return names[0]
    if project in names:
        return project  # AzDO's default repository — the API-verified form
    if len(names) > 1:
        logger.warning(
            "Project %s has %d repositories and no azure_devops.default_repos mapping — "
            "using the default name %r",
            project,
            len(names),
            project,
        )
    return project


_HTML_TAG_RE = re.compile(r"<[^>]*>")


def _strip_html(html: str) -> str:
    """Defensive HTML → text for ``System.Description`` (work-item fields are
    HTML — tags and entities never belong in a prompt)."""
    text = _HTML_TAG_RE.sub(" ", html or "")
    return " ".join(unescape(text).split())


def _changeset_to_commits(
    changeset: ChangeSet, *, operation_key: str | None = None
) -> list[CommitPayload]:
    """Map a ChangeSet onto the push-API commit shape (research §3.1).

    Azure paths are repository-absolute (``/src/app.py``); operations map
    create→add, update→edit, delete→delete. ``operation_key`` (R11) stamps
    the frozen ``(forge-op:<key>)`` marker into the commit comment so the
    identity probe can attribute a found commit to its intent.
    """
    file_changes: list[FileChange] = []
    for change in changeset.changes:
        path = f"/{change.path}"
        if change.operation is Operation.DELETE:
            file_changes.append(FileChange(path=path, change_type="delete"))
        elif change.operation is Operation.CREATE:
            file_changes.append(
                FileChange(path=path, change_type="add", content=change.content or "")
            )
        else:
            file_changes.append(
                FileChange(path=path, change_type="edit", content=change.content or "")
            )
    comment = (
        message_with_marker(changeset.commit_message, operation_key)
        if operation_key
        else changeset.commit_message
    )
    return [CommitPayload(comment=comment, changes=file_changes)]


def _push_new_object_id(payload: dict) -> str:
    """The branch head AFTER the push (the pushed commit's id).

    The push response carries per-ref results (research §10.11: the wrapping
    envelope shape is not pinned by documented samples) — read
    ``newObjectId`` off the first ref update defensively, ``value``-wrapped
    or bare.
    """
    candidates: list[dict] = []
    if isinstance(payload, dict):
        wrapped = payload.get("value")
        if isinstance(wrapped, list):
            candidates = [entry for entry in wrapped if isinstance(entry, dict)]
        elif isinstance(payload.get("refUpdates"), list):
            candidates = [entry for entry in payload["refUpdates"] if isinstance(entry, dict)]
    for entry in candidates:
        new_id = entry.get("newObjectId")
        if isinstance(new_id, str) and new_id:
            return new_id
    return ""


def _pr_web_url(pr: dict | None) -> str | None:
    """The PR's web URL from ``_links.web.href``, defensively."""
    if not isinstance(pr, dict):
        return None
    links = pr.get("_links")
    if isinstance(links, dict):
        web = links.get("web")
        if isinstance(web, dict) and isinstance(web.get("href"), str):
            return web["href"]
    return None


async def _link_work_item_to_pr(
    client: AzureDevOpsClient, project: str, work_item_id: int, pr: dict
) -> bool:
    """Link the work item to the Draft PR; a failure is evidence, never fatal.

    Thin wrapper over
    :meth:`~forge.integrations.azure.AzureDevOpsClient.link_work_item_to_pr`
    (the WIT ArtifactLink PATCH, research §4.6): extracts the
    project/repository/PR ids off the created PR payload and maps the
    outcome onto the publish evidence's ``work_item_linked`` flag.
    """
    repository = pr.get("repository") or {}
    project_ref = repository.get("project") or {}
    project_id_guid = str(project_ref.get("id") or "")
    repository_id = str(repository.get("id") or "")
    pr_id = int(pr.get("pullRequestId") or 0)
    if not project_id_guid or not repository_id or not pr_id:
        return False
    try:
        await client.link_work_item_to_pr(
            project, work_item_id, project_id_guid, repository_id, pr_id
        )
    except AzureDevOpsError as exc:
        logger.warning("Work-item link PATCH failed for #%s → PR %s: %s", work_item_id, pr_id, exc)
        return False
    return True


def _build_definition_id(build: dict) -> int | None:
    """The pipeline definition id of a Build object (``definition.id``)."""
    definition = build.get("definition")
    if not isinstance(definition, dict):
        return None
    raw = definition.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _build_name(build: dict) -> str:
    """The human name of a build (definition name, else the build id)."""
    definition = build.get("definition")
    if isinstance(definition, dict) and definition.get("name"):
        return str(definition["name"])
    return f"build {build.get('id') or '?'}"


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
        f"`{actor}` is not in the Azure DevOps approver list (`FORGE_AZDO_APPROVERS`).\n\n"
        "*This is an automated message.*"
    )


@dataclass(frozen=True)
class _RunStub:
    """Minimal run surface the proposer reads (id/issue_iid/base_sha)."""

    id: str
    issue_iid: int
    project_id: int
    base_sha: str


async def evaluate_azure_revival(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stack_factory: Callable[[str, str], AzureAgents] | None = None,
    now: datetime | None = None,
) -> None:
    """One Tier-1 auto-revive pass over every AzDO run whose backoff elapsed.

    The twin of the GitLab reconciler's ``evaluate_revival`` leg: the same
    classification, the same ``FORGE_RUN_AUTO_REVIVE_LIMIT`` budget, the same
    journaled ``auto_revive`` walk — only the re-dispatch is the Pipelines
    lane (or the builtin publish path for a repo-less lane-less project).
    """
    if stack_factory is None:
        stack_factory = lambda p, r: build_azure_agents(  # noqa: E731 — trivial default
            settings, session_factory, p, r
        )

    async def redispatch(run_id: str) -> None:
        async with session_factory() as session:
            run = await session.get(FlowRun, run_id)
            if run is None:
                return
            # The journaled handle is the subject identity — the dispatch leg
            # pins project/repo on it; the column is the legacy fallback.
            handle_raw = str(((run.evidence or {}).get("harness") or {}).get("handle") or "")
            repo = ""
            try:
                if handle_raw:
                    parsed = AzurePipelinesHandle.from_json(handle_raw)
                    repo = f"{parsed.project}/{parsed.repo}"
            except Exception:
                repo = ""
            repo = repo or str(run.github_repo_full_name or "").strip()
        if "/" not in repo:
            logger.warning("AzDO revival of run %s without repo identity — skipping", run_id[:8])
            return
        project, repo_name = repo.split("/", 1)
        service = AzureRunService(
            session_factory,
            settings,
            forge_config,
            stack=stack_factory(project, repo_name),
            repo_full_name=repo,
        )
        await service._redispatch_revival(run_id)

    await evaluate_revivals(
        session_factory,
        settings,
        provider="azure_devops",
        redispatch=redispatch,
        now=now,
        log=logger,
    )


async def _repos_with_due_publication_intents(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    """Distinct AzDO ``project/repo`` subjects holding open intents (R11)."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(PublicationIntent.repo)
                    .where(
                        PublicationIntent.provider == "azure_devops",
                        PublicationIntent.status.in_(OPEN_STATES),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    return [row for row in rows if row]


async def evaluate_azure_publication_intents(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stack_factory: Callable[[str, str], AzureAgents] | None = None,
    now: datetime | None = None,
) -> None:
    """One recovery pass over every AzDO repo with open publication intents.

    The post-restart half of the R11 fix on the AzDO lane: a worker that
    died between the push and the journal completion leaves the intent
    ``dispatched`` — this pass probes by identity (marker + expected
    parent) and resolves it: the run ADVANCES on the landed push instead of
    blocking on a stale-CAS re-publish.
    """
    if stack_factory is None:
        stack_factory = lambda p, r: build_azure_agents(  # noqa: E731 — trivial default
            settings, session_factory, p, r
        )
    for repo_full_name in await _repos_with_due_publication_intents(session_factory):
        if "/" not in repo_full_name:
            logger.warning("Publication intent with malformed repo %r — skipping", repo_full_name)
            continue
        project, repo_name = repo_full_name.split("/", 1)
        stack = stack_factory(project, repo_name)
        try:
            service = AzureRunService(
                session_factory,
                settings,
                forge_config,
                stack=stack,
                repo_full_name=repo_full_name,
            )
            await service.resolve_publication_intents(now=now)
        except Exception:
            # One broken repo must not stall the recovery pass.
            logger.exception("Publication-intent recovery failed for %s", repo_full_name)


__all__ = [
    "AzureAgents",
    "AzurePRReviewer",
    "AzurePipelinesHandle",
    "AzurePublishOutcome",
    "AzureRunService",
    "azure_factory_branch",
    "build_azure_agents",
    "evaluate_azure_publication_intents",
    "evaluate_azure_waiting_ci",
    "execute_azure_run_command",
    "evaluate_azure_revival",
]


async def run_azure_harness_reconciler(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: float = 15,
    shutdown_event: asyncio.Event | None = None,
    stack_factory: Callable[[str, str], AzureAgents] | None = None,
) -> None:
    """Periodic tick driving the Pipelines lane AND the R02 gate to convergence.

    The twin of :func:`forge.runs.github_service.run_github_harness_reconciler`:
    a plain asyncio task for the worker's ``asyncio.gather``. Exits
    immediately when the Azure adapter is disabled or carries no
    credentials. Deliberately NOT gated on the lane pipeline: since R02 the
    builtin lane also parks in ``waiting_ci``, so every AzDO deployment
    needs the verification pass (the harness pass itself no-ops when no
    lane runs exist).
    """
    import asyncio

    if not bool(getattr(settings, "FORGE_AZDO_ENABLED", False)):
        return
    if not str(getattr(settings, "FORGE_AZDO_ORG_URL", "") or "").strip():
        return
    try:
        azure_credentials_from_settings(settings)
    except ValueError:
        logger.info("Azure credentials not configured — harness reconciler not started")
        return

    if stack_factory is None:
        stack_factory = lambda p, r: build_azure_agents(  # noqa: E731 — trivial default
            settings, session_factory, p, r
        )

    shutdown_event = shutdown_event or asyncio.Event()
    logger.info("Azure DevOps harness reconciler started (interval=%ss)", interval_seconds)
    while not shutdown_event.is_set():
        try:
            await evaluate_azure_waiting_harness(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            # A failed pass must never kill the reconciler task.
            logger.exception("Azure DevOps harness reconciler pass failed")
        try:
            # R02 verification gate: the waiting_ci runs' builds decide.
            await evaluate_azure_waiting_ci(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            logger.exception("Azure verification reconciler pass failed")
        try:
            # Tier-1 auto-revive: re-dispatch runs whose transient-failure
            # backoff has elapsed (forge.runs.revival).
            await evaluate_azure_revival(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            logger.exception("Azure DevOps revival reconciler pass failed")
        try:
            # R11 recovery: probe-and-resolve stranded publication intents.
            await evaluate_azure_publication_intents(
                settings, forge_config, session_factory, stack_factory=stack_factory
            )
        except Exception:
            logger.exception("Azure publication-intent reconciler pass failed")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("Azure DevOps harness reconciler stopped")


async def evaluate_azure_waiting_ci(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stack_factory: Callable[[str, str], AzureAgents] | None = None,
    now: datetime | None = None,
) -> None:
    """One verification pass over every AzDO run parked in `waiting_ci` (R02).

    The twin of `evaluate_github_waiting_ci`: builds of the candidate
    commit decide whether the run continues to review (verified, or
    honestly unverified when no CI built it) or repairs/blocks.
    """
    from sqlalchemy import select

    from forge.durable.controller import FlowStatus
    from forge.durable.models import FlowRun

    if stack_factory is None:
        stack_factory = lambda p, r: build_azure_agents(  # noqa: E731
            settings, session_factory, p, r
        )
    async with session_factory() as session:
        runs = (
            (
                await session.execute(
                    select(FlowRun).where(
                        FlowRun.provider == "azure_devops",
                        FlowRun.status == FlowStatus.WAITING_CI.value,
                    )
                )
            )
            .scalars()
            .all()
        )
    for run in runs:
        run_id = run.id
        # Subject identity resolution: the journaled handle is the lane
        # dispatch's pin; the builtin lane carries the subject block it froze
        # at plan time; the column is the legacy fallback.
        handle_raw = str(((run.evidence or {}).get("harness") or {}).get("handle") or "")
        repo = ""
        try:
            if handle_raw:
                parsed = AzurePipelinesHandle.from_json(handle_raw)
                repo = f"{parsed.project}/{parsed.repo}"
        except Exception:
            repo = ""
        repo = (
            repo
            or str((run.evidence or {}).get("subject", {}).get("repo_full_name") or "").strip()
            or str(run.github_repo_full_name or "").strip()
        )
        if "/" not in repo:
            logger.warning("AzDO waiting_ci run %s without repo identity", run_id[:8])
            continue
        project, repo_name = repo.split("/", 1)
        service = AzureRunService(
            session_factory,
            settings,
            forge_config,
            stack=stack_factory(project, repo_name),
            repo_full_name=repo,
        )
        try:
            await service.evaluate_waiting_ci_one(run_id, now=now)
        except Exception:
            logger.exception("AzDO verification reconcile failed for run %s", run_id[:8])


async def evaluate_azure_waiting_harness(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stack_factory: Callable[[str, str], AzureAgents] | None = None,
) -> None:
    """One reconcile pass over every ``waiting_harness`` AzDO run.

    Each run's journaled :class:`AzurePipelinesHandle` carries its own
    project/repo, so the pass groups runs by subject and drives each
    through its own service instance (the poll needs the client only —
    the LLM stack is built lazily by the publisher path on adoption).
    """
    from sqlalchemy import select

    from forge.durable.controller import FlowStatus
    from forge.durable.models import FlowRun

    if stack_factory is None:
        stack_factory = lambda p, r: build_azure_agents(  # noqa: E731 — trivial default
            settings, session_factory, p, r
        )
    async with session_factory() as session:
        runs = (
            (
                await session.execute(
                    select(FlowRun).where(
                        FlowRun.provider == "azure_devops",
                        FlowRun.status == FlowStatus.WAITING_HARNESS.value,
                    )
                )
            )
            .scalars()
            .all()
        )
    for run in runs:
        run_id = run.id
        # The journaled handle is the subject identity — the dispatch leg
        # pins project/repo on it; the column is the legacy fallback.
        handle_raw = str(((run.evidence or {}).get("harness") or {}).get("handle") or "")
        repo = ""
        try:
            if handle_raw:
                parsed = AzurePipelinesHandle.from_json(handle_raw)
                repo = f"{parsed.project}/{parsed.repo}"
        except Exception:
            repo = ""
        repo = repo or str(run.github_repo_full_name or "").strip()
        if "/" not in repo:
            logger.warning("AzDO waiting_harness run %s without repo identity", run_id[:8])
            continue
        project, repo_name = repo.split("/", 1)
        service = AzureRunService(
            session_factory,
            settings,
            forge_config,
            stack=stack_factory(project, repo_name),
            repo_full_name=repo,
        )
        try:
            await service.evaluate_waiting_harness_one(run_id)
        except Exception:
            logger.exception("AzDO harness reconcile failed for run %s", run_id[:8])
