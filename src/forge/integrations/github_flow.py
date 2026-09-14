"""GitHub publish bridge: plan → (human gate deferred) → publish → Draft PR.

The GitHub vertical slice (ADR-0019 §3, review finding F32) sits BESIDE
RunService as a separate bridge until v0.5 extracts the source-adapter
contracts (docs/specs/contracts-v0.2.md). Honest boundaries of this slice:

- **The human gate is deferred.** A GitHub ``/implement`` currently plans and
  publishes without a pending decision — no approver set is enforced on
  GitHub subjects yet. ``FORGE_GITHUB_ENABLED`` defaults to false; enabling
  the integration accepts exactly that.
- **Runs are not FlowRun-backed yet.** Durability is limited to the webhook
  inbox row and the scheduled command step that triggered the flow. The
  remote effects stay idempotent anyway:
  * the run id is derived deterministically from the command's inbox
    identity, so a re-executed step re-derives the same factory branch;
  * the branch CAS (``expectedHeadOid``) turns a would-be duplicate commit
    into ``STALE_DATA`` — surfaced as a drift outcome, never a silent retry
    (research §3.4);
  * the Draft-PR lookup by head adopts an existing PR, never opens a second.

Write-path semantics (ADR-0016 §3/§4 adapted to GitHub): the factory branch
``forge/<issue>/<run>`` is cut from the EXPECTED base head fetched moments
before (that read + ``expectedHeadOid`` IS the concurrency contract —
GitLab's ``last_commit_id`` file-level CAS becomes a branch-wide CAS here),
and the Draft PR is created with ``draft: true``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from forge.config import ForgeConfig, Settings
from forge.durable import short_run_id
from forge.factory.implementer import LLMImplementer
from forge.factory.llm import LLMClient
from forge.factory.planner import LLMPlanner
from forge.integrations.github import (
    GitHubAPIError,
    GitHubClient,
    GitHubRepositoryReader,
    GitHubStaleBranchError,
)
from forge.repository.changeset import ChangeSet, Operation
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

logger = logging.getLogger(__name__)


def github_factory_branch(issue_number: int, run_id: str) -> str:
    """The run-owned GitHub branch: ``forge/<issue>/<run-id[:8]>``.

    GitHub-side twin of :func:`forge.durable.identity.factory_branch` (the
    ``forge/`` prefix marks the GitHub-native scheme; no user-supplied text
    appears in the mandatory part of the ref).
    """
    return f"forge/{issue_number or 0}/{short_run_id(run_id)}"


@dataclass(frozen=True)
class GitHubPublishOutcome:
    """The publish leg's verdict, with the three revisions kept distinct.

    ``commit_oid`` is the factory-branch head AFTER the commit (the PR head);
    ``expected_head_oid`` is the frozen base the CAS pinned (the branch's cut
    point). For GitHub Actions ``pull_request`` runs CI may test the synthetic
    merge revision instead of ``commit_oid`` (research §4.3) — the tested oid
    is a verification-reader concern and is deliberately NOT conflated here.
    """

    ok: bool
    reason: str = ""
    commit_oid: str | None = None
    expected_head_oid: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_draft: bool | None = None
    #: STALE_DATA: a concurrent writer moved the branch — reported, never
    #: retried silently (ADR-0016 §3).
    drift: bool = False


class GitHubPublishFlow:
    """Publish one candidate to GitHub: branch CAS commit, then Draft PR."""

    def __init__(
        self,
        client: GitHubClient,
        proposer: Any | None = None,
        *,
        base_branch: str = "main",
    ) -> None:
        self._client = client
        self._proposer = proposer
        self._base_branch = base_branch

    async def publish_proposal(
        self,
        *,
        owner: str,
        repo: str,
        issue_number: int,
        run_id: str,
        issue_title: str,
        plan_summary: str = "",
        base_branch: str | None = None,
    ) -> GitHubPublishOutcome:
        """Builtin-implementer leg: propose against the frozen base, publish.

        The proposer reads repository evidence through a
        :class:`GitHubRepositoryReader` (injected at construction), so the
        builtin implementer runs against GitHub unchanged. ``run`` is a
        minimal stub — the implementer only uses id/issue_iid/base_sha of it.
        """
        if self._proposer is None:
            raise ValueError("publish_proposal requires a proposer")
        base = base_branch or self._base_branch
        expected_head = await self._client.get_branch_head(owner, repo, base)
        run_stub = SimpleNamespace(
            id=run_id, issue_iid=issue_number, project_id=0, base_sha=expected_head
        )
        changeset = await self._proposer.propose(
            run_stub,
            issue_title,
            plan_summary=plan_summary,
            attempt_base=expected_head,
        )
        return await self.publish_changeset(
            owner,
            repo,
            issue_number=issue_number,
            run_id=run_id,
            changeset=changeset,
            base_branch=base,
            expected_head=expected_head,
        )

    async def publish_changeset(
        self,
        owner: str,
        repo: str,
        *,
        issue_number: int,
        run_id: str,
        changeset: ChangeSet,
        base_branch: str | None = None,
        expected_head: str | None = None,
        title: str | None = None,
        body: str | None = None,
    ) -> GitHubPublishOutcome:
        """Ensure the factory branch, CAS-commit the changeset, ensure Draft PR.

        ``expected_head`` pins the base (fetched by the caller when the
        proposal was materialized against it); otherwise the base head is
        read here. The branch is cut from that exact commit, and the commit
        mutation carries it as ``expectedHeadOid`` — any concurrent movement
        fails the CAS and surfaces as a drift outcome.
        """
        base = base_branch or self._base_branch
        branch = github_factory_branch(issue_number, run_id)
        if expected_head is None:
            expected_head = await self._client.get_branch_head(owner, repo, base)

        try:
            await self._ensure_branch(owner, repo, branch, expected_head)
        except GitHubAPIError as exc:
            return GitHubPublishOutcome(
                ok=False,
                reason=f"branch_create_failed: {exc}",
                expected_head_oid=expected_head,
                branch=branch,
            )

        additions = [
            (change.path, change.content or "")
            for change in changeset.changes
            if change.operation is not Operation.DELETE
        ]
        deletions = [
            change.path for change in changeset.changes if change.operation is Operation.DELETE
        ]
        # Correlation only — GitHub does NOT dedupe on clientMutationId
        # (research §3.4); the CAS is the exactly-once guard.
        operation_key = uuid4().hex[:12]
        try:
            result = await self._client.create_commit_on_branch(
                owner,
                repo,
                branch,
                headline=changeset.commit_message,
                additions=additions,
                deletions=deletions,
                expected_head_oid=expected_head,
                client_mutation_id=operation_key,
            )
        except GitHubStaleBranchError as exc:
            logger.warning(
                "GitHub publish on %s drifted (expected %s) — reporting, not retrying",
                branch,
                expected_head[:8] if expected_head else "?",
            )
            pr = await self._find_draft_pr(owner, repo, branch, base)
            return GitHubPublishOutcome(
                ok=False,
                reason=f"branch_drift: {exc}",
                expected_head_oid=expected_head,
                branch=branch,
                pr_number=int(pr["number"]) if pr else None,
                pr_url=pr.get("html_url") if pr else None,
                drift=True,
            )
        except GitHubAPIError as exc:
            return GitHubPublishOutcome(
                ok=False,
                reason=f"commit_failed: {exc}",
                expected_head_oid=expected_head,
                branch=branch,
            )
        commit_oid = str(result["oid"])

        pr = await self._ensure_draft_pr(
            owner, repo, branch, base, issue_number, run_id, title, body
        )
        return GitHubPublishOutcome(
            ok=True,
            commit_oid=commit_oid,
            expected_head_oid=expected_head,
            branch=branch,
            pr_number=int(pr["number"]) if pr else None,
            pr_url=pr.get("html_url") if pr else None,
            pr_draft=bool(pr.get("draft")) if pr else None,
        )

    async def _ensure_branch(self, owner: str, repo: str, branch: str, sha: str) -> bool:
        """Create the factory branch at *sha*; tolerate pre-existence.

        ``createCommitOnBranch`` requires the ref to already exist (research
        §3.1), so branch creation is explicit and idempotent: a 422
        "already exists" means a previous attempt (or re-execution) cut it.
        """
        try:
            await self._client.create_branch(owner, repo, branch, sha)
            return True
        except GitHubAPIError as exc:
            if exc.status_code == 422:
                return False  # idempotent re-entry — the ref is there
            raise

    async def _find_draft_pr(
        self, owner: str, repo: str, branch: str, base: str
    ) -> dict[str, Any] | None:
        """The open Draft PR already pointing at *branch*, or None."""
        try:
            return await self._client.get_pr_by_head(owner, repo, branch, base=base)
        except GitHubAPIError:
            return None

    async def _ensure_draft_pr(
        self,
        owner: str,
        repo: str,
        branch: str,
        base: str,
        issue_number: int,
        run_id: str,
        title: str | None,
        body: str | None,
    ) -> dict[str, Any] | None:
        """Find-by-head first, create only when none exists — never duplicate."""
        existing = await self._find_draft_pr(owner, repo, branch, base)
        if existing is not None:
            logger.info(
                "Draft PR #%s already open for %s — adopting, not duplicating",
                existing.get("number"),
                branch,
            )
            return existing
        default_title = f"forge: implement #{issue_number} (run {short_run_id(run_id)})"
        try:
            return await self._client.create_draft_pr(
                owner,
                repo,
                head=branch,
                base=base,
                title=title or default_title,
                body=body
                or (
                    f"Draft implementation by forge for #{issue_number} "
                    f"(run {short_run_id(run_id)}).\n\n"
                    "*Merging is a human decision — forge never merges.*"
                ),
            )
        except GitHubAPIError as exc:
            # The commit landed; a failed PR creation must not undo it —
            # report with the commit recorded (the reconciler re-ensures PRs).
            logger.error("Draft PR creation failed for %s: %s", branch, exc)
            return None


async def execute_github_run_command(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: async_sessionmaker[AsyncSession],
    metadata: dict[str, Any],
    *,
    flow_builder: Any | None = None,
) -> GitHubPublishOutcome | None:
    """Execute a GitHub run command — the ``provider: github`` step dispatch.

    Wired from :func:`forge.runs.service.execute_run_command`. ``go`` and
    ``cancel`` are logged and ignored for now: the human gate is deferred, so
    there is no pending decision to consume (see module docstring). The
    *flow_builder* hook exists for tests to run the flow over a fake client.
    """
    command = metadata.get("command")
    if command != "start_run":
        logger.info(
            "GitHub command %r not wired yet (human gate deferred, ADR-0019) — ignoring",
            command,
        )
        return None

    repo_full_name = str(metadata.get("repo_full_name") or "")
    if "/" not in repo_full_name:
        logger.error("GitHub start_run without repo_full_name — ignoring")
        return None
    owner, repo = repo_full_name.split("/", 1)
    issue_number = int(metadata.get("issue_number") or 0)
    # Deterministic run id from the command's inbox identity: a re-executed
    # step re-derives the same branch, and the CAS + PR-lookup keep the
    # remote effects exactly-once (module docstring).
    source_event_id = str(metadata.get("source_event_id") or "")
    run_id = source_event_id[:32] or uuid4().hex

    client = GitHubClient(
        base_url=getattr(settings, "FORGE_GITHUB_API_URL", "https://api.github.com"),
        token_provider=_credentials_from_settings(settings),
    )
    try:
        issue = await client.get_issue(owner, repo, issue_number)
        plan_summary = ""
        if flow_builder is not None:
            flow = flow_builder()
        else:
            reader = GitHubRepositoryReader(client, owner, repo)
            llm = LLMClient(settings=settings, session_factory=session_factory)
            planner = LLMPlanner(llm, settings=settings)
            plan = await planner.plan(issue.title, issue.description or "", flow_run_id=run_id)
            plan_summary = str(getattr(plan, "summary", "") or "")
            flow = GitHubPublishFlow(
                client,
                proposer=LLMImplementer(llm, gitlab=reader, settings=settings),
                base_branch=getattr(settings, "FORGE_TARGET_BRANCH", "main"),
            )
        outcome = await flow.publish_proposal(
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            run_id=run_id,
            issue_title=issue.title,
            plan_summary=plan_summary,
        )
        if outcome.ok:
            logger.info(
                "GitHub run %s published %s as PR #%s",
                short_run_id(run_id),
                (outcome.commit_oid or "?")[:8],
                outcome.pr_number,
            )
        else:
            logger.warning(
                "GitHub run %s publish failed: %s (drift=%s)",
                short_run_id(run_id),
                outcome.reason,
                outcome.drift,
            )
        return outcome
    finally:
        await client.aclose()


def _credentials_from_settings(settings: Settings) -> Any:
    from forge.integrations.github import GitHubAppCredentials

    key_setting = getattr(settings, "FORGE_GITHUB_PRIVATE_KEY", None)
    if key_setting is not None:
        return GitHubAppCredentials(
            app_id=str(getattr(settings, "FORGE_GITHUB_APP_ID", "") or ""),
            private_key=key_setting.get_secret_value(),
            installation_id=str(getattr(settings, "FORGE_GITHUB_INSTALLATION_ID", "") or ""),
            base_url=getattr(settings, "FORGE_GITHUB_API_URL", "https://api.github.com"),
        )

    # PAT mode (no App): a static provider over FORGE_GITHUB_TOKEN. The
    # token carries the creator's scopes — intended for personal/lab use;
    # production identity is the App above.
    token_setting = getattr(settings, "FORGE_GITHUB_TOKEN", None)
    if token_setting is not None:
        from forge.integrations.github import GitHubStaticCredentials

        return GitHubStaticCredentials(token_setting.get_secret_value())

    raise ValueError(
        "GitHub credentials not configured: set FORGE_GITHUB_PRIVATE_KEY + "
        "FORGE_GITHUB_APP_ID + FORGE_GITHUB_INSTALLATION_ID (App mode) or "
        "FORGE_GITHUB_TOKEN (PAT mode)"
    )
