"""GitHub publish bridge: the publish leg behind the GitHub run service.

The GitHub vertical slice (ADR-0019 §3, review finding F32). Since E3a the
orchestration (``/implement`` → plan → human gate → ``/go`` → publish) lives
in :mod:`forge.runs.github_service` on FlowRun rows — this module keeps ONLY
the bridge pieces the service reuses:

- :func:`build_github_agents` — ensures the GitHubClient / repository reader
  / planner / implementer / reviewer construction (the LLM agents run
  against GitHub through the duck-typed reader);
- :class:`GitHubPublishFlow` — the publish leg: the ADR-0026 publication
  boundary (every candidate crosses strict materialization + write policy
  BEFORE any commit-API call), branch-CAS commit (``expectedHeadOid``),
  Draft PR find-by-head-first;
- :class:`GitHubPRReviewer` — the readonly review of the published PR diff.

Write-path semantics (ADR-0016 §3/§4 adapted to GitHub): the factory branch
``forge/<issue>/<run>`` is cut from the FROZEN base head the plan was made
against (that read + ``expectedHeadOid`` IS the concurrency contract —
GitLab's ``last_commit_id`` file-level CAS becomes a branch-wide CAS here),
and the Draft PR is created with ``draft: true``. The branch CAS turns a
would-be duplicate commit into ``STALE_DATA`` — surfaced as a drift outcome,
never a silent retry (research §3.4). Since ADR-0026 the SAME trusted
boundary guards every provider×backend: ``publish_proposal`` wraps the
proposal with :func:`bundle_from_changeset` and materializes it against
authoritative base contents (R08/R09 digest verification), and
:func:`validate_changeset` policy + ``allowed_paths`` run before the first
mutation — a violating candidate is a blocked outcome with zero commit-API
calls, and :meth:`GitHubPublishFlow.publish_validated` accepts only the
boundary's :class:`~forge.runs.publisher.ValidatedCandidate` wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast
from uuid import uuid4

import logging

from forge.config import Settings
from forge.durable import short_run_id
from forge.factory.implementer import (
    FORGE_MATERIALIZE_MAX_FILE_CHARS,
    LLMImplementer,
)
from forge.factory.llm import LLMClient
from forge.factory.planner import LLMPlanner
from forge.factory.reviewer import (
    REVIEWER_MAX_DIFF_CHARS,
    REVIEWER_MAX_INPUT_CHARS,
    LLMReviewer,
    ReviewVerdict,
    review_with_retry,
)
from forge.factory.llm import truncate_chars
from forge.integrations.github import (
    GitHubAPIError,
    GitHubAppCredentials,
    GitHubClient,
    GitHubRepositoryReader,
    GitHubStaleBranchError,
    GitHubStaticCredentials,
)
from forge.repository.changeset import ChangeSet, Operation
from forge.runs.candidate import CandidateBundle, CandidateError, bundle_from_changeset
from forge.runs.publisher import PolicyViolation, ValidatedCandidate, validate_candidate_bundle
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
    #: The blocked-class outcome (ADR-0016 §3 / ADR-0026): a concurrent
    #: writer moved the branch (``branch_drift`` — reported, never retried
    #: silently) OR the publication boundary rejected the candidate — the
    #: run may not publish onto this base, so callers land it in BLOCKED,
    #: never retry it.
    drift: bool = False
    #: True when the ADR-0026 boundary rejected the candidate: ``reason``
    #: starts with ``candidate_invalid`` / ``changeset_invalid`` and ZERO
    #: commit-API calls were made.
    invalid: bool = False


class BaseContentReader(Protocol):
    """The full-content read surface the boundary materializes against.

    :class:`~forge.integrations.github.GitHubRepositoryReader` satisfies it;
    the test fakes duck-type it directly.
    """

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str: ...


class GitHubPublishFlow:
    """Publish one candidate to GitHub: boundary, branch CAS commit, Draft PR."""

    def __init__(
        self,
        client: GitHubClient,
        proposer: Any | None = None,
        *,
        base_branch: str = "main",
        reader: BaseContentReader | None = None,
    ) -> None:
        self._client = client
        self._proposer = proposer
        self._base_branch = base_branch
        self._reader = reader

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
        expected_head: str | None = None,
        allowed_paths: list[str] | None = None,
    ) -> GitHubPublishOutcome:
        """Builtin-implementer leg: propose against the frozen base, publish.

        The proposer reads repository evidence through a
        :class:`GitHubRepositoryReader` (injected at construction), so the
        builtin implementer runs against GitHub unchanged. ``run`` is a
        minimal stub — the implementer only uses id/issue_iid/base_sha of it.

        ``expected_head`` is the FROZEN base the plan was approved against —
        pinned by the gate at plan time. When omitted the base head is read
        live (the caller owns that decision). ``allowed_paths`` is the
        RunSpec's frozen scope; the proposal crosses the SAME ADR-0026
        publication boundary as every other backend — policy validation
        (``validate_changeset`` / denied paths / size caps / the scope) and
        R08/R09 digest verification run BEFORE any commit-API call, and a
        violation is a blocked outcome with zero mutations (review R01).
        """
        if self._proposer is None:
            raise ValueError("publish_proposal requires a proposer")
        base = base_branch or self._base_branch
        if expected_head is None:
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
        bundle = bundle_from_changeset(changeset, attempt_base_oid=expected_head)
        return await self._publish_bundle(
            owner,
            repo,
            issue_number=issue_number,
            run_id=run_id,
            bundle=bundle,
            commit_message=changeset.commit_message,
            base_branch=base,
            expected_head=expected_head,
            allowed_paths=allowed_paths,
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
        allowed_paths: list[str] | None = None,
    ) -> GitHubPublishOutcome:
        """Boundary-validate *changeset*, ensure the factory branch, commit.

        The legacy transport entry: like every candidate (ADR-0026) it is
        NEVER trusted on its caller's claim of validation — it is wrapped
        into a :class:`~forge.runs.candidate.CandidateBundle`
        (:func:`bundle_from_changeset`) and re-run through the shared
        boundary (materialization + policy + scope) before the first
        mutation; a violating changeset is refused with zero commit-API
        calls.

        ``expected_head`` pins the base (fetched by the caller when the
        proposal was materialized against it); otherwise the base head is
        read here. The branch is cut from that exact commit, and the commit
        mutation carries it as ``expectedHeadOid`` — any concurrent movement
        fails the CAS and surfaces as a drift outcome.
        """
        base = base_branch or self._base_branch
        if expected_head is None:
            expected_head = await self._client.get_branch_head(owner, repo, base)
        bundle = bundle_from_changeset(changeset, attempt_base_oid=expected_head)
        return await self._publish_bundle(
            owner,
            repo,
            issue_number=issue_number,
            run_id=run_id,
            bundle=bundle,
            commit_message=changeset.commit_message,
            base_branch=base,
            expected_head=expected_head,
            allowed_paths=allowed_paths,
            title=title,
            body=body,
        )

    async def publish_validated(
        self,
        owner: str,
        repo: str,
        *,
        issue_number: int,
        run_id: str,
        candidate: ValidatedCandidate,
        base_branch: str | None = None,
        expected_head: str | None = None,
        title: str | None = None,
        body: str | None = None,
    ) -> GitHubPublishOutcome:
        """Commit an ALREADY-VALIDATED candidate (ADR-0026 transport contract).

        The only route from a candidate to the commit API: the
        :class:`~forge.runs.publisher.ValidatedCandidate` wrapper is issued
        exclusively by the publication boundary, and a raw ``ChangeSet`` is
        refused here by type — an unvalidated candidate cannot reach a
        write.
        """
        if not isinstance(candidate, ValidatedCandidate):
            raise TypeError(
                "publish_validated requires a ValidatedCandidate issued by the "
                "publication boundary (ADR-0026); route raw ChangeSets through "
                "publish_changeset / publish_proposal"
            )
        base = base_branch or self._base_branch
        branch = github_factory_branch(issue_number, run_id)
        changeset = candidate.changeset
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

    async def _publish_bundle(
        self,
        owner: str,
        repo: str,
        *,
        issue_number: int,
        run_id: str,
        bundle: CandidateBundle,
        commit_message: str,
        base_branch: str,
        expected_head: str,
        allowed_paths: list[str] | None = None,
        title: str | None = None,
        body: str | None = None,
    ) -> GitHubPublishOutcome:
        """The publication boundary of the GitHub transport (ADR-0026).

        Every candidate crosses :func:`validate_candidate_bundle` here —
        strict materialization against authoritative base contents read at
        the frozen *expected_head* (R08/R09 digest verification) plus the
        write policy and *allowed_paths* scope — BEFORE any commit-API
        call. A rejection is the blocked-class outcome with zero mutations.
        """
        branch = github_factory_branch(issue_number, run_id)
        try:
            base_contents = await self._base_contents(owner, repo, expected_head, bundle.paths)
            candidate = validate_candidate_bundle(
                bundle,
                base_contents=base_contents,
                branch=branch,
                commit_message=commit_message,
                allowed_paths=allowed_paths or [],
            )
        except CandidateError as exc:
            return GitHubPublishOutcome(
                ok=False,
                reason=f"candidate_invalid: {exc.reason}: {exc}",
                expected_head_oid=expected_head,
                branch=branch,
                drift=True,
                invalid=True,
            )
        except PolicyViolation as exc:
            return GitHubPublishOutcome(
                ok=False,
                reason="changeset_invalid: " + "; ".join(exc.violations),
                expected_head_oid=expected_head,
                branch=branch,
                drift=True,
                invalid=True,
            )
        return await self.publish_validated(
            owner,
            repo,
            issue_number=issue_number,
            run_id=run_id,
            candidate=candidate,
            base_branch=base_branch,
            expected_head=expected_head,
            title=title,
            body=body,
        )

    def _reader_for(self, owner: str, repo: str) -> BaseContentReader:
        """The full-content reader: the injected one, the client's own reader
        surface (the duck-typed fakes), or a fresh repository reader."""
        if self._reader is not None:
            return self._reader
        if getattr(self._client, "read_text", None) is not None:
            return cast(BaseContentReader, self._client)
        return GitHubRepositoryReader(self._client, owner, repo)

    async def _base_contents(
        self, owner: str, repo: str, ref: str, paths: list[str]
    ) -> dict[str, str]:
        """Authoritative full-content reads at the frozen base (no truncation).

        Missing paths are absent from the result — validation reports
        create/update/delete existence against it. A base file over the
        materialization cap rejects the candidate (``file_too_large``),
        exactly like the GitLab publisher's fetch (ADR-0016 §2).
        """
        reader = self._reader_for(owner, repo)
        contents: dict[str, str] = {}
        for path in dict.fromkeys(paths):
            try:
                text = await reader.read_text(path, ref=ref)
            except GitHubAPIError:
                continue  # not in the snapshot — validation reports existence
            if len(text) > FORGE_MATERIALIZE_MAX_FILE_CHARS:
                raise CandidateError(
                    "file_too_large",
                    f"{path}: base content is {len(text)} chars at {ref[:8]}, over the "
                    f"materialization cap of {FORGE_MATERIALIZE_MAX_FILE_CHARS}",
                )
            contents[path] = text
        return contents

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


class GitHubPRReviewer:
    """Readonly review of the published candidate over the PR diff (E3a).

    The GitHub twin of :class:`forge.factory.reviewer.LLMReviewer`: instead
    of a GitLab compare it reads the PR's changed-file patches, and feeds
    the SAME system prompt / JSON contract to the strong tier (ADR-0008).
    The verdict is recorded with the SHA it reviewed — the review approves a
    *specific* commit.
    """

    def __init__(
        self,
        llm: LLMClient,
        client: GitHubClient,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._client = client
        self._settings = settings

    async def review(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        issue_title: str,
        plan_summary: str,
        base_sha: str,
        candidate_sha: str,
        flow_run_id: str | None = None,
    ) -> ReviewVerdict:
        """Review the PR diff and return the parsed verdict."""
        diff = await self._pr_diff(owner, repo, pr_number)
        user = (
            f"Issue title: {issue_title}\n\n"
            f"Plan summary:\n{plan_summary or '(no plan summary available)'}\n\n"
            f"Candidate diff ({base_sha[:8]}..{candidate_sha[:8]}):\n{diff}"
        )
        return await review_with_retry(
            self._llm,
            system=_REVIEW_SYSTEM_PROMPT,
            user=truncate_chars(user, REVIEWER_MAX_INPUT_CHARS),
            parse=LLMReviewer._parse,
            flow_run_id=flow_run_id,
        )

    async def _pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Render the PR's file patches as diff text, biggest files first."""
        try:
            files = await self._client.get_pr_files(owner, repo, pr_number)
        except Exception:
            logger.warning(
                "PR files read failed for %s/%s#%s — reviewing without diff",
                owner,
                repo,
                pr_number,
                exc_info=True,
            )
            return "(diff unavailable)"
        ordered = sorted(files, key=lambda f: len(str(f.get("patch") or "")), reverse=True)
        parts: list[str] = []
        for entry in ordered:
            patch = str(entry.get("patch") or "")
            if not patch:
                continue
            parts.append(f"diff --git a/{entry.get('filename', '')} b/{entry.get('filename', '')}")
            parts.append(patch)
        return truncate_chars("\n".join(parts), REVIEWER_MAX_DIFF_CHARS)


# Kept identical to forge.factory.reviewer._SYSTEM_PROMPT — the same JSON
# review contract on the GitHub path (ADR-0008).
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


@dataclass(frozen=True)
class GitHubAgents:
    """The constructed GitHub stack one connection/repo runs with."""

    client: GitHubClient
    reader: GitHubRepositoryReader
    planner: LLMPlanner
    implementer: LLMImplementer
    reviewer: GitHubPRReviewer
    flow: GitHubPublishFlow


def build_github_agents(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    owner: str,
    repo: str,
    *,
    client: GitHubClient | None = None,
) -> GitHubAgents:
    """Ensure planner/reviewer/reader construction for one GitHub repo.

    The single place the LLM-driven defaults are built for the GitHub path
    (the twin of :func:`forge.runs.service.build_default_agents`): the reader
    duck-types the GitLab read surface, so the planner/implementer run
    unchanged; the reviewer reads PR diffs; the publish flow is bound to the
    same client.
    """
    if client is None:
        client = GitHubClient(
            base_url=getattr(settings, "FORGE_GITHUB_API_URL", "https://api.github.com"),
            token_provider=credentials_from_settings(settings),
        )
    reader = GitHubRepositoryReader(client, owner, repo)
    llm = LLMClient(settings=settings, session_factory=session_factory)
    planner = LLMPlanner(llm, settings=settings)
    implementer = LLMImplementer(llm, gitlab=reader, settings=settings)
    reviewer = GitHubPRReviewer(llm, client, settings=settings)
    flow = GitHubPublishFlow(
        client,
        proposer=implementer,
        base_branch=str(getattr(settings, "FORGE_TARGET_BRANCH", "main") or "main"),
        reader=reader,
    )
    return GitHubAgents(
        client=client,
        reader=reader,
        planner=planner,
        implementer=implementer,
        reviewer=reviewer,
        flow=flow,
    )


def credentials_from_settings(settings: Settings) -> Any:
    """The GitHub token provider configured on *settings* (App or PAT)."""
    key_setting = getattr(settings, "FORGE_GITHUB_PRIVATE_KEY", None)
    if key_setting is not None:
        pem = key_setting.get_secret_value()
        # Accept either the PEM text or a path to an existing .pem file.
        if not pem.lstrip().startswith("-----BEGIN"):
            pem_path = Path(pem).expanduser()
            if pem_path.is_file():
                pem = pem_path.read_text()
        return GitHubAppCredentials(
            app_id=str(getattr(settings, "FORGE_GITHUB_APP_ID", "") or ""),
            private_key=pem,
            installation_id=str(getattr(settings, "FORGE_GITHUB_INSTALLATION_ID", "") or ""),
            base_url=getattr(settings, "FORGE_GITHUB_API_URL", "https://api.github.com"),
        )

    # PAT mode (no App): a static provider over FORGE_GITHUB_TOKEN. The
    # token carries the creator's scopes — intended for personal/lab use;
    # production identity is the App above.
    token_setting = getattr(settings, "FORGE_GITHUB_TOKEN", None)
    if token_setting is not None:
        return GitHubStaticCredentials(token_setting.get_secret_value())

    raise ValueError(
        "GitHub credentials not configured: set FORGE_GITHUB_PRIVATE_KEY + "
        "FORGE_GITHUB_APP_ID + FORGE_GITHUB_INSTALLATION_ID (App mode) or "
        "FORGE_GITHUB_TOKEN (PAT mode)"
    )
