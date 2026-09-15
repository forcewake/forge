"""AzureRunService — the AZ-2 Azure DevOps path to gate parity (ADR-0024).

Mirrors the GitHub gate machinery (:mod:`forge.runs.github_service`) onto
FlowRun rows with ``provider='azure_devops'``: an ``/implement`` on a work
item creates a durable run, plans, posts the PLAN as a markdown work-item
comment, freezes the RunSpec (v2, harness selection included), opens the
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
  the reconciler that polls it arrives with AZ-3. Empty → the builtin
  in-worker proposer path as on GitHub.
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

import difflib
import logging
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable
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
from forge.factory.llm import LLMClient, LLMError, LLMResponseError, truncate_chars
from forge.factory.planner import LLMPlanner, PLAN_SUMMARY_CHARS
from forge.factory.reviewer import (
    REVIEWER_MAX_DIFF_CHARS,
    REVIEWER_MAX_INPUT_CHARS,
    REVIEWER_TIER,
    LLMReviewer,
    ReviewVerdict,
)
from forge.factory.implementer import LLMImplementer
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
from forge.repository import ChangeSet, Operation
from forge.runs.admission import approvers_for, check_admission
from forge.runs.backends import HARNESS_NAME
from forge.runs.candidate import attempt_base_for
from forge.runs.harness_selection import (
    SHIPPED_DRIVERS,
    HarnessSelection,
    compile_harness_selection,
    implementation_block,
    resolve_preference,
    selection_from_spec_document,
    validate_preference,
)
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

#: The verification surface note while the AZ-3 reconciler/executor is not
#: wired: Azure Repos ignores YAML ``pr:`` triggers — PR CI is governed by
#: branch policies ("Build validation"), the ADR-0008 enforcement point.
_VERIFICATION_NOTE = (
    "Branch-policy Build validation on the PR is the verification surface — "
    "no required checks are enforced yet."
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
        result = await self._llm.complete(
            tier=REVIEWER_TIER,
            system=_REVIEW_SYSTEM_PROMPT,
            user=truncate_chars(user, REVIEWER_MAX_INPUT_CHARS),
            role="reviewer",
            flow_run_id=flow_run_id,
            json_mode=True,
        )
        return LLMReviewer._parse(result.text)

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
        data = await self._client.get_tree(project, repo, sha, recursive=True)
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
            if run.provider != "azure_devops" or run.issue_iid != issue_number:
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
                if run is None or run.provider != "azure_devops" or run.issue_iid != issue_number:
                    logger.info(
                        "Azure DevOps /cancel references unknown run %s — ignoring",
                        requested[:8],
                    )
                    return
            else:
                # Short id (plan comments show the 8-char form): resolve by
                # prefix among the work item's runs; ambiguity means no action.
                runs = (
                    (
                        await session.execute(
                            select(FlowRun)
                            .where(
                                FlowRun.provider == "azure_devops",
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

    # ------------------------------------------------------------------
    # Publish leg: gate → lane dispatch OR builtin CAS publish
    # ------------------------------------------------------------------

    async def _advance_publish(self, run_id: str, *, project_id: int, issue_number: int) -> None:
        """One gate-approved publish cycle.

        Pipelines lane (ADR-0024 §6): a connection onboarded for lane
        execution (``FORGE_AZDO_LANE_PIPELINE_ID``) dispatches the harness
        and parks in ``waiting_harness`` — the AZ-3 reconciler drives the
        rest. Builtin (default): propose + publish synchronously as on
        GitHub, ending at ``ready_for_human``.
        """
        if self._lane_pipeline_id():
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
                "Azure DevOps run %s cancelled before publication — dropping in-flight leg",
                run_id[:8],
            )
            return

        issue_title = await self._read_work_item_title(issue_number)
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
                plan_summary=plan_summary,
                attempt_base=base_sha or None,
            )
        except Exception as exc:
            await self._to_terminal(run_id, FlowStatus.FAILED, f"proposal_failed: {exc}")
            return

        outcome = await self._publish_changeset(
            run_id,
            issue_number=issue_number,
            changeset=changeset,
            base_branch=self._target_branch(),
            expected_head=base_sha or None,
        )

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
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(run_id, FlowStatus.EVALUATING_CI, reason=_VERIFICATION_NOTE)
            await session.commit()

        await self._review_and_ready(
            run_id,
            project_id=project_id,
            issue_number=issue_number,
            pr_id=outcome.pr_id,
            candidate_sha=commit_oid,
            base_sha=base_sha,
        )

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
    ) -> None:
        """Dispatch the lane pipeline and park the run in ``waiting_harness``.

        ``proposing`` = ensure the factory branch at the frozen attempt base
        → Runs-API dispatch with the string templateParameters the lane
        template takes (run_id / attempt_base / driver / model /
        work_item_id) → ``waiting_harness`` with the journaled
        :class:`AzurePipelinesHandle` in the run's evidence. The AZ-3
        reconciler polls from here — the wait is worker-free, like
        ``waiting_ci``. The dispatched driver is the one frozen in the
        RunSpec (ADR-0023 §6).
        """
        async with self._session_factory() as session:
            run = await self._get_run(session, run_id)
            attempt_base = attempt_base_for(run)
            already_waiting = run.status == FlowStatus.WAITING_HARNESS.value
        if driver is None:
            driver = await self._frozen_harness_driver(run_id) or self._harness_driver()
        branch = azure_factory_branch(issue_number, run_id)
        handle = AzurePipelinesHandle(
            provider="azure_devops",
            project=self._project,
            repo=self._repo,
            pipeline_id=self._lane_pipeline_id() or 0,
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
                    "model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
                    "work_item_id": str(issue_number),
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
    ) -> AzurePublishOutcome:
        """Ensure the factory branch, CAS-push the changeset, open the Draft PR.

        ``expected_head`` pins the frozen base the plan was approved against.
        The branch is cut at that exact commit (``oldObjectId`` = 40 zeros)
        and the push carries it as the CAS — any concurrent movement is
        rejected with ``staleObjectId`` under HTTP 200 and mapped to a drift
        outcome (ADR-0024 §4). Draft PR creation is find-free (the AZ-1
        client has no list-PRs surface) and therefore never replayed — the
        consume-once gate above is the exactly-once guard.
        """
        branch = azure_factory_branch(issue_number, run_id)
        if expected_head is None:
            expected_head = await self._stack.client.get_branch_head(
                self._project, self._repo, base_branch
            )

        try:
            await self._ensure_factory_branch(branch, expected_head)
            result = await self._stack.client.push_commits(
                self._project,
                self._repo,
                branch,
                expected_old_sha=expected_head,
                commits=_changeset_to_commits(changeset),
            )
        except AzureDevOpsDriftError as exc:
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
        §5). A failed creation must not undo the commit; the caller records
        the commit and the reconciler story owns PR re-ensurance."""
        title = f"forge: implement #{issue_number} (run {short_run_id(run_id)})"
        description = (
            f"Draft implementation by forge for work item #{issue_number} "
            f"(run {short_run_id(run_id)}).\n\n"
            "*Merging is a human decision — forge never merges.*"
        )
        try:
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
    ) -> None:
        """No required checks enforced → reviewing → ready_for_human."""
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.transition(
                run_id, FlowStatus.REVIEWING, reason="readonly review of the PR diff"
            )
            await session.commit()

        plan_summary, _ = await self._read_plan_evidence(run_id)
        issue_title = await self._read_work_item_title(issue_number)
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
        logger.info("Azure DevOps run %s is ready_for_human at %s", run_id[:8], candidate_sha[:8])

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

        TODO(ADR-0023 §5): pass the planner's structured proposal from
        LLMPlanner.plan's output once that surface exists — same
        integration point as the GitHub service. None keeps the compiler
        defaults.
        """
        preference = resolve_preference(self._config, self._settings)
        lane = self._lane_pipeline_id()
        validate_preference(preference, self._harness_driver() if lane else None)
        return compile_harness_selection(
            preference,
            f"ci_harness:{self._harness_driver()}" if lane else "builtin",
            set(SHIPPED_DRIVERS),
            None,
        )

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
        plan_digest: str,
        task_digest: str,
        allowed_paths: list[str] | None = None,
        harness_selection: HarnessSelection | None = None,
    ) -> dict:
        """The immutable RunSpec document frozen at plan acceptance (F14).

        With the lane configured, backend_config carries the frozen
        execution profile: backend ``ci_harness``, the lane pipeline id and
        the driver — the dispatch inputs later come FROM this document.
        """
        selection = harness_selection or self._compile_harness_selection()
        lane = self._lane_pipeline_id()
        backend_config: dict = {
            "backend": "ci_harness" if lane else "builtin",
            "model": str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or ""),
            "target_branch": self._target_branch(),
            **selection.as_document(),
        }
        if lane:
            backend_config["lane_pipeline_id"] = lane
            backend_config["driver"] = selection.harness
        document: dict = {
            "subject": {
                "provider": "azure_devops",
                "project": self._project,
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

    async def _read_work_item_title(self, issue_number: int) -> str:
        """Fetch the work item title; fall back to a neutral label on read
        failure."""
        try:
            work_item = await self._stack.client.get_work_item(self._project, issue_number)
            fields = work_item.get("fields") or {}
            title = str(fields.get("System.Title") or "")
            if title:
                return title
        except Exception:
            logger.warning(
                "Could not read title of work item %s — using fallback",
                issue_number,
                exc_info=True,
            )
        return f"work item {issue_number}"

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
        self, project_id: int, issue_number: int, body: str, run_id: str, kind: str
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
        """Park the run in ``blocked``/``failed`` with an operator-facing reason."""
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

        await execute_azure_reactive_review(settings, forge_config, session_factory, metadata)
        return
    if command == "debug_ci":
        from forge.reactive.azure_ci_debug import execute_azure_debug_ci_command

        await execute_azure_debug_ci_command(settings, forge_config, session_factory, metadata)
        return
    if command not in {"start_run", "go", "cancel"}:
        logger.warning("Unknown Azure DevOps run command %r — ignoring", command)
        return
    project = str(metadata.get("project") or "")
    repo_full_name = str(metadata.get("repo_full_name") or "")
    if "/" not in repo_full_name:
        # Work-item commands carry no repository (the payload has none) —
        # resolve the connection's target repo for the project.
        repo_full_name = f"{project}/{await _resolve_repo_name(forge_config, project)}"
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


async def _resolve_repo_name(forge_config: ForgeConfig, project: str) -> str:
    """The target repo for repo-less work-item commands.

    ``forge.yml``'s ``azure_devops.default_repos`` mapping (project → repo)
    wins; the fallback is AzDO's default — the repository created with the
    project shares its name. Resolution is validated by the first repo API
    call the run makes; a wrong name fails the run visibly. The frozen AZ-1
    client has no repositories-list surface — the proper fix is a
    ``list_repositories`` client method (AZ-4 onboarding).
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
    return project


_HTML_TAG_RE = re.compile(r"<[^>]*>")


def _strip_html(html: str) -> str:
    """Defensive HTML → text for ``System.Description`` (work-item fields are
    HTML — tags and entities never belong in a prompt)."""
    text = _HTML_TAG_RE.sub(" ", html or "")
    return " ".join(unescape(text).split())


def _changeset_to_commits(changeset: ChangeSet) -> list[CommitPayload]:
    """Map a ChangeSet onto the push-API commit shape (research §3.1).

    Azure paths are repository-absolute (``/src/app.py``); operations map
    create→add, update→edit, delete→delete.
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
    return [CommitPayload(comment=changeset.commit_message, changes=file_changes)]


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
    """Link the work item to the Draft PR via the WIT ArtifactLink PATCH.

    The reliable programmatic link (research §4.6): a JSON-Patch add of an
    ``ArtifactLink`` relation whose vstfs URL embeds the project/repository/
    PR ids (``%2F``-separated). True when the PATCH succeeded — a failure is
    the caller's evidence note, never a publish failure.
    """
    repository = pr.get("repository") or {}
    project_ref = repository.get("project") or {}
    project_id_guid = str(project_ref.get("id") or "")
    repository_id = str(repository.get("id") or "")
    pr_id = int(pr.get("pullRequestId") or 0)
    if not project_id_guid or not repository_id or not pr_id:
        return False
    artifact_url = f"vstfs:///Git/PullRequestId/{project_id_guid}%2F{repository_id}%2F{pr_id}"
    try:
        await client._request(  # noqa: SLF001 — thin helper over the frozen client's transport
            "PATCH",
            f"/{project}/_apis/wit/workItems/{work_item_id}",
            headers={"Content-Type": "application/json-patch+json"},
            json=[
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "ArtifactLink",
                        "url": artifact_url,
                        "attributes": {"name": "Pull Request"},
                    },
                }
            ],
        )
    except AzureDevOpsError as exc:
        logger.warning("Work-item link PATCH failed for #%s → PR %s: %s", work_item_id, pr_id, exc)
        return False
    return True


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


__all__ = [
    "AzureAgents",
    "AzurePRReviewer",
    "AzurePipelinesHandle",
    "AzurePublishOutcome",
    "AzureRunService",
    "azure_factory_branch",
    "build_azure_agents",
    "execute_azure_run_command",
]
