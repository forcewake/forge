"""Azure DevOps reactive review engine (ADR-0024, milestone AZ-3).

The AzDO twin of :mod:`forge.reactive.github_review`: a
``git.pullrequest.created`` / ``git.pullrequest.updated`` service hook
schedules one durable step (same inbox/step mechanics, ADR-0017) whose
executor is :func:`execute_azure_reactive_review`. The engine mirrors the
GitHub shape, adapted to the documented Azure DevOps ground truths:

1. **Guards** (recursion defense-in-depth; the ingress filters first):
   forge-bot senders, bot-authored PRs and the ``forge/`` head-branch
   prefix are skipped with a log line, before any paid call.
2. **Sticky thread, not a sticky comment**: forge's progress thread is
   found by the hidden marker ``<!-- forge:review:azdo:{pr_id} -->`` in a
   comment authored by forge (mirror of the GitHub marker pattern). AzDO
   threads cannot be PATCHed comment-by-comment the way GitHub issue
   comments can, so progress is carried by **thread status** (active while
   reviewing) plus a final reply carrying the review body — never a second
   thread, so re-deliveries spawn no duplicates.
3. **Incremental via iterations** (research §4.3 ground truth: the
   three-way SHAs live ONLY on ``GitPullRequestIteration`` —
   ``lastMergeCommit`` is a plain GitCommitRef). The engine lists
   iterations, reads the iteration/head it last reviewed out of the sticky
   thread's latest marker reply, and reviews
   ``reviewed.sourceRefCommit → latest.sourceRefCommit``; with no prior
   review the range is the full PR (``latest.commonRefCommit →
   latest.sourceRefCommit``). A head forge already reviewed is skipped.
   The diff itself is rendered client-side: AzDO change entries carry
   paths, not patches, so file contents are read at both SHAs
   (:meth:`~forge.integrations.azure.AzureDevOpsClient.get_item`) and
   unified-diffed — bounded by :data:`REVIEW_MAX_FILES` and the shared
   diff-char caps.
4. **Findings become PR threads**: positioned findings become inline
   threads (``threadContext`` file + 1-based lines, right side = head);
   the rest folds into the summary reply, capped at
   :data:`REVIEW_MAX_COMMENTS` and :data:`REVIEW_BODY_SOFT_CAP` (the
   GitHub engine's constants — AzDO documents no content cap, so the cap
   is defensive).
5. **Severity maps to thread status, never to votes** (ADR-0024 §5):
   critical/warning threads stay ``active`` (the display-only
   REQUEST_CHANGES analog — the summary carries the vote-note), plain
   suggestions are filed ``closed``. The client NEVER votes.

No budget is applied (no RunSpec on this lane): model usage lands in the
``llm_calls`` ledger with driver completeness like every other call
(ADR-0013).

The gateway normalizer produces the step metadata this engine consumes
(``project``, ``repo``, ``pr_id``, ``head_sha``, ``head_branch`` (full
ref), ``sender``, ``pr_author``); connection settings are the typed
``Settings`` fields AZ-2 landed (``FORGE_AZDO_BOT_NAME`` /
``FORGE_AZDO_ORG_URL`` / ``FORGE_AZDO_PAT``).
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import TYPE_CHECKING, Any

from forge.config import Settings
from forge.factory.llm import LLMClient, LLMError, LLMResponseError, truncate_chars
from forge.factory.reviewer import REVIEWER_MAX_DIFF_CHARS, REVIEWER_MAX_INPUT_CHARS
from forge.integrations.azure import AzureDevOpsClient, PrIteration
from forge.reactive.github_review import (
    REACTIVE_SEVERITIES,
    REVIEW_BODY_SOFT_CAP,
    REVIEW_MAX_COMMENTS,
    _OVERALL_EMOJI,
    _REACTIVE_SYSTEM_PROMPT,
    _SEVERITY_EMOJI,
    ReactiveFinding,
    ReactiveReviewVerdict,
    GitHubReactiveReviewer,
    review_digest,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

logger = logging.getLogger(__name__)

#: The hidden sticky-thread marker (mirror of the GitHub engine's marker):
#: every forge review thread on a PR embeds it in a comment; the engine
#: finds its own thread by it and reuses it instead of creating another.
REVIEW_MARKER_TEMPLATE = "<!-- forge:review:azdo:{pr_id} -->"

#: Thread-status numerics. Research §4.4 documents the member NAMES
#: (unknown/active/fixed/wontFix/closed/byDesign) and pins only
#: ``status: 1`` (active) with a documented sample; the remaining numbers
#: follow the documented enum order and stay defensive until the AZ-4 lab
#: run confirms them.
THREAD_STATUS_ACTIVE = 1
THREAD_STATUS_CLOSED = 5

#: Severity → thread status (ADR-0024 §5: the client NEVER votes —
#: display-only). critical/warning stay Active (the REQUEST_CHANGES
#: analog; the summary carries the vote-note); suggestions are filed
#: Closed (informational).
_SEVERITY_THREAD_STATUS = {
    "critical": THREAD_STATUS_ACTIVE,
    "warning": THREAD_STATUS_ACTIVE,
    "suggestion": THREAD_STATUS_CLOSED,
}

#: REVIEW_MAX_COMMENTS (imported above from the GitHub engine — one cap,
#: both lanes) bounds the inline finding threads; overflow folds into the
#: summary reply.

#: The forge/* head-branch prefix (the durable flow's own candidates).
_FORGE_BRANCH_PREFIX = "forge/"

#: Diff surface bound: each changed file costs one items-API content read
#: per side, so the file count is capped before the char caps apply.
REVIEW_MAX_FILES = 25

_MARKER_HEAD_RE = re.compile(r"head:([0-9a-fA-F]{40})")
_MARKER_ITERATION_RE = re.compile(r"iteration:(\d+)")

__all__ = [
    "REACTIVE_SEVERITIES",
    "REVIEW_MARKER_TEMPLATE",
    "REVIEW_MAX_COMMENTS",
    "AzureReactiveReviewEngine",
    "AzureReactiveReviewer",
    "THREAD_STATUS_ACTIVE",
    "THREAD_STATUS_CLOSED",
    "execute_azure_reactive_review",
]


# ----------------------------------------------------------------------
# Reviewer: the diff builder + LLM call (thin AzDO adapter over the
# shared reviewer contract)
# ----------------------------------------------------------------------


class AzureReactiveReviewer:
    """Strong-tier diff review over the PR surface (full or delta).

    The reactive twin on the AzDO boundary: same input discipline and JSON
    contract as :class:`~forge.reactive.github_review.
    GitHubReactiveReviewer` (the LLM call is verbatim), but the diff is
    BUILT, not fetched — AzDO exposes no GitHub-style per-file patch feed,
    so changed paths come from the iteration-changes API and contents are
    read at the before/after SHAs.
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

    async def build_diff(
        self,
        project: str,
        repo: str,
        pr_id: int,
        *,
        before_sha: str | None = None,
        after_sha: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """Render the reviewed diff text plus its changed-file entries.

        ``before_sha`` set reviews the ``before → after`` delta; a full
        review (no *before_sha*) diffs the latest iteration's merge base
        against its head (research §4.3). Entries carry ``path`` /
        ``patch``; files whose contents cannot be read are reviewed from
        the surrounding entries but can never anchor an inline thread.
        """
        try:
            iterations = await self._client.get_pr_iterations(project, repo, pr_id)
            latest = _latest_iteration(iterations)
            if latest is None:
                logger.warning(
                    "No PR iterations for %s/%s#%d — reviewing without diff", project, repo, pr_id
                )
                return "(diff unavailable)", []
            after = after_sha or latest.source_ref_commit or ""
            before = before_sha or latest.common_ref_commit or ""
            iteration_id = _iteration_for(iterations, after) or latest.id
            entries = await self._client.get_pr_iteration_changes(
                project, repo, pr_id, iteration_id
            )
            files = await self._render_entries(
                project, repo, before, after, entries[:REVIEW_MAX_FILES]
            )
        except Exception:
            logger.warning(
                "Diff read failed for %s/%s#%d — reviewing without diff",
                project,
                repo,
                pr_id,
                exc_info=True,
            )
            return "(diff unavailable)", []

        label = f"{before[:8]}..{after[:8]}" if before_sha else f"PR #{pr_id} (full)"
        text = truncate_chars(
            "\n".join(str(entry.get("patch") or "") for entry in files), REVIEWER_MAX_DIFF_CHARS
        )
        logger.info(
            "Reactive review diff for %s/%s#%d: %s (%d files)",
            project,
            repo,
            pr_id,
            label,
            len(files),
        )
        return text, files

    async def _render_entries(
        self, project: str, repo: str, before: str, after: str, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Change entries → rendered unified diffs (biggest-first)."""
        rendered: list[dict[str, Any]] = []
        for entry in entries:
            raw_item = entry.get("item")
            item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
            path = str(item.get("path") or "").lstrip("/")
            if not path:
                continue
            change_type = str(entry.get("changeType") or "edit").lower()
            original = str(entry.get("originalPath") or item.get("path") or "").lstrip("/")
            old_text = (
                ""
                if change_type == "add"
                else await self._content_at(project, repo, original, before)
            )
            new_text = (
                ""
                if change_type == "delete"
                else await self._content_at(project, repo, path, after)
            )
            patch = _unified_diff(path, old_text, new_text)
            if not patch:
                continue  # unchanged between the two SHAs — not part of the delta
            rendered.append({"path": path, "changeType": change_type, "patch": patch})
        # Deterministic input order: biggest patches first (GitHub parity).
        return sorted(rendered, key=lambda f: len(str(f.get("patch") or "")), reverse=True)

    async def _content_at(self, project: str, repo: str, path: str, sha: str) -> str:
        """File content at *sha*; unreadable/absent content is empty."""
        if not sha:
            return ""
        try:
            data = await self._client.get_item(project, repo, f"/{path}", version=sha)
        except Exception:
            logger.warning("Content read failed for %s at %s", path, sha[:8], exc_info=True)
            return ""
        content = data.get("content")
        return str(content) if isinstance(content, str) else ""

    async def review(
        self,
        *,
        project: str,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str,
        flow_run_id: str | None = None,
    ) -> ReactiveReviewVerdict:
        """Review the rendered diff and return the parsed verdict.

        Same tier, system prompt and JSON contract as the GitHub reactive
        reviewer (shared verbatim) — review quality is provider-agnostic.
        """
        user = (
            f"Pull request: {project}/{repo}#{pr_id}\n"
            f"Reviewed head: {head_sha}\n\n"
            f"Candidate diff:\n{diff}"
        )
        result = await self._llm.complete(
            tier="strong",
            system=_REACTIVE_SYSTEM_PROMPT,
            user=truncate_chars(user, REVIEWER_MAX_INPUT_CHARS),
            role="reviewer",
            flow_run_id=flow_run_id,
            json_mode=True,
        )
        # The shared reactive verdict parser (same contract, one code path).
        return GitHubReactiveReviewer._parse(result.text)


def _unified_diff(path: str, old_text: str, new_text: str) -> str:
    """A git-flavored unified diff between the two file versions."""
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff).rstrip("\n")


def _latest_iteration(iterations: list[PrIteration]) -> PrIteration | None:
    return max(iterations, key=lambda iteration: iteration.id, default=None)


def _iteration_for(iterations: list[PrIteration], source_sha: str) -> int | None:
    """The iteration whose head is *source_sha* (newest wins on ties)."""
    for iteration in sorted(iterations, key=lambda i: i.id, reverse=True):
        if iteration.source_ref_commit == source_sha:
            return iteration.id
    return None


# ----------------------------------------------------------------------
# Engine: webhook step → posted threads
# ----------------------------------------------------------------------


class AzureReactiveReviewEngine:
    """Turns a ``git.pullrequest.*`` webhook into posted review threads."""

    def __init__(
        self,
        settings: Settings,
        forge_config: Any,
        session_factory: "async_sessionmaker[AsyncSession] | None",
        *,
        client: AzureDevOpsClient,
        reviewer: AzureReactiveReviewer,
        project: str,
        repo: str,
    ) -> None:
        self._settings = settings
        self._config = forge_config
        self._session_factory = session_factory
        self._client = client
        self._reviewer = reviewer
        self._project = project
        self._repo = repo

    @property
    def _bot_identity(self) -> str:
        return str(self._settings.FORGE_AZDO_BOT_NAME or "forge-bot")

    async def run(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Execute one reactive review step; returns a small outcome record."""
        pr_id = int(metadata.get("pr_id") or metadata.get("pr_number") or 0)
        head_sha = str(metadata.get("head_sha") or "")
        head_branch = _bare_branch(str(metadata.get("head_branch") or ""))
        sender = str(metadata.get("sender") or "")
        pr_author = str(metadata.get("pr_author") or "")

        # -- recursion guard (defense in depth; ingress filters first) -----
        if _same_identity(sender, self._bot_identity):
            logger.info(
                "Reactive review skipped on %s/%s#%d — bot sender", self._project, self._repo, pr_id
            )
            return {"status": "skipped", "reason": "bot_sender"}
        if _same_identity(pr_author, self._bot_identity):
            logger.info(
                "Reactive review skipped on %s/%s#%d — bot-authored PR",
                self._project,
                self._repo,
                pr_id,
            )
            return {"status": "skipped", "reason": "bot_authored_pr"}
        if head_branch.startswith(_FORGE_BRANCH_PREFIX):
            # The durable flow's own candidate branches: reviewing them
            # would have forge review forge (E1 boundary).
            logger.info(
                "Reactive review skipped on %s/%s#%d — forge-owned branch %s",
                self._project,
                self._repo,
                pr_id,
                head_branch,
            )
            return {"status": "skipped", "reason": "forge_branch"}
        if not pr_id or not head_sha:
            logger.warning(
                "Reactive review metadata incomplete for %s/%s", self._project, self._repo
            )
            return {"status": "skipped", "reason": "incomplete_metadata"}

        # -- sticky thread (marker convention) ------------------------------
        thread = await self._find_own_thread(pr_id)
        reviewed = _reviewed_state(thread)

        if reviewed is not None and reviewed[0] == head_sha:
            logger.info(
                "PR %s/%s#%d already reviewed at %s — skipping duplicate",
                self._project,
                self._repo,
                pr_id,
                head_sha[:8],
            )
            await self._finalize(
                pr_id,
                thread,
                "\u2705 forge already reviewed this head — nothing new to review.",
                status=THREAD_STATUS_ACTIVE,
                reply=True,
            )
            return {"status": "skipped", "reason": "already_reviewed"}

        # -- full vs incremental (research §4.3: iterations only) -----------
        iterations = await self._safe_iterations(pr_id)
        latest = _latest_iteration(iterations)
        reviewed_iteration: PrIteration | None = None
        if reviewed is not None and reviewed[1] is not None:
            reviewed_iteration = next((it for it in iterations if it.id == reviewed[1]), None)
        incremental = (
            reviewed_iteration is not None
            and latest is not None
            and latest.id > reviewed_iteration.id
            and bool(reviewed_iteration.source_ref_commit)
        )
        delta_base = (
            reviewed_iteration.source_ref_commit
            if incremental and reviewed_iteration is not None
            else None
        )
        if incremental and latest is not None and reviewed_iteration is not None:
            logger.info(
                "Incremental reactive review of %s/%s#%d: %s..%s (iteration %d..%d)",
                self._project,
                self._repo,
                pr_id,
                str(delta_base)[:8],
                head_sha[:8],
                reviewed_iteration.id,
                latest.id,
            )

        # -- review the diff --------------------------------------------------
        diff, files = await self._reviewer.build_diff(
            self._project,
            self._repo,
            pr_id,
            before_sha=delta_base,
            after_sha=head_sha,
        )
        if incremental and not files:
            # A delta with no file changes exits without calling the model
            # (qodo's documented incremental behavior, GitHub-lane parity).
            logger.info(
                "No file changes in %s..%s on %s/%s#%d — skipping LLM review",
                str(delta_base)[:8],
                head_sha[:8],
                self._project,
                self._repo,
                pr_id,
            )
            await self._finalize(
                pr_id,
                thread,
                "\u2705 No file changes since the last review.",
                status=THREAD_STATUS_CLOSED,
            )
            return {"status": "skipped", "reason": "empty_delta"}

        try:
            verdict = await self._reviewer.review(
                project=self._project,
                repo=self._repo,
                pr_id=pr_id,
                diff=diff,
                head_sha=head_sha,
            )
        except (LLMError, LLMResponseError):
            await self._finalize(
                pr_id,
                thread,
                "\u26a0\ufe0f forge's review failed — it will retry automatically.",
                status=THREAD_STATUS_ACTIVE,
            )
            raise

        # -- post findings as threads -----------------------------------------
        posted, folded = _split_findings(verdict, {str(f.get("path") or "") for f in files})
        for finding in posted:
            await self._client.create_pr_thread(
                self._project,
                self._repo,
                pr_id,
                (
                    f"{_SEVERITY_EMOJI.get(finding.severity, '\U0001f4a1')} "
                    f"**{finding.severity}** \u2014 {finding.note}"
                ),
                status=_SEVERITY_THREAD_STATUS.get(finding.severity, THREAD_STATUS_ACTIVE),
                file_path=f"/{finding.file}",
                line_start=finding.line,
                line_end=finding.line,
            )

        body = _render_summary(
            verdict,
            pr_id=pr_id,
            head_sha=head_sha,
            before_sha=delta_base,
            incremental=incremental,
            project=self._project,
            repo=self._repo,
            folded=folded,
            latest_iteration=latest.id if latest is not None else None,
        )
        await self._finalize(
            pr_id,
            thread,
            body,
            status=(
                THREAD_STATUS_ACTIVE
                if verdict.verdict == "concerns"
                or any(f.severity == "critical" for f in verdict.findings)
                else THREAD_STATUS_CLOSED
            ),
        )
        logger.info(
            "Reactive review posted on %s/%s#%d at %s: %s, %d thread(s)",
            self._project,
            self._repo,
            pr_id,
            head_sha[:8],
            verdict.verdict,
            len(posted),
        )
        return {
            "status": "reviewed",
            "verdict": verdict.verdict,
            "pr_id": pr_id,
            "head_sha": head_sha,
            "finding_threads": len(posted),
            "iteration": latest.id if latest is not None else None,
        }

    # ------------------------------------------------------------------
    # Sticky thread (hidden-marker convention, mirror of the GitHub lane)
    # ------------------------------------------------------------------

    async def _find_own_thread(self, pr_id: int) -> dict[str, Any] | None:
        """Forge's review thread for *pr_id*, keyed by the hidden marker.

        Only threads whose MARKER-BEARING comment is authored by forge's
        identity count — someone quoting the marker must never hijack the
        thread.
        """
        marker = REVIEW_MARKER_TEMPLATE.format(pr_id=pr_id)
        try:
            for thread in await self._client.list_pr_threads(self._project, self._repo, pr_id):
                for comment in thread.get("comments") or []:
                    if marker not in str(comment.get("content") or ""):
                        continue
                    author = (comment.get("author") or {}).get("uniqueName")
                    if not _same_identity(str(author or ""), self._bot_identity):
                        break  # quoted marker in someone else's thread
                    return thread
        except Exception:
            # Thread lookup is best-effort cosmetics — a failed read must
            # not stop the review.
            logger.warning("Review thread lookup failed on PR #%d", pr_id, exc_info=True)
        return None

    async def _finalize(
        self,
        pr_id: int,
        thread: dict[str, Any] | None,
        body: str,
        *,
        status: int,
        reply: bool = True,
    ) -> None:
        """Leave the sticky thread at its final state (status, not edits).

        The marker carries the reviewed head/iteration so the NEXT event
        can compute the incremental delta; the reply carries the visible
        outcome. AzDO threads cannot be edited in place comment-by-comment
        like GitHub issue comments, so the progress transition rides on
        the thread STATUS plus one appended reply.
        """
        if thread is None:
            marker = REVIEW_MARKER_TEMPLATE.format(pr_id=pr_id)
            first = f"{marker} \u23f3 forge is reviewing this PR\u2026"
            try:
                thread = await self._client.create_pr_thread(
                    self._project, self._repo, pr_id, first, status=THREAD_STATUS_ACTIVE
                )
            except Exception:
                logger.warning("Review thread creation failed on PR #%d", pr_id, exc_info=True)
                return
        thread_id = int(thread.get("id") or 0)
        if not thread_id:
            return
        try:
            if reply:
                await self._client.reply_pr_thread(
                    self._project, self._repo, pr_id, thread_id, body
                )
            await self._client.update_thread_status(
                self._project, self._repo, pr_id, thread_id, status
            )
        except Exception:
            logger.debug("Review thread finalization failed", exc_info=True)

    async def _safe_iterations(self, pr_id: int) -> list[PrIteration]:
        try:
            return await self._client.get_pr_iterations(self._project, self._repo, pr_id)
        except Exception:
            logger.warning(
                "Iteration listing failed on PR #%d — reviewing in full", pr_id, exc_info=True
            )
            return []


def _bare_branch(ref: str) -> str:
    """``refs/heads/feature`` → ``feature`` (payloads carry full refs)."""
    return ref.removeprefix("refs/heads/")


def _same_identity(left: str, right: str) -> bool:
    """Case-insensitive ``uniqueName`` comparison (§2.8: THE identity)."""
    return bool(left) and bool(right) and left.strip().lower() == right.strip().lower()


def _reviewed_state(thread: dict[str, Any] | None) -> tuple[str, int | None] | None:
    """(head, iteration) forge last reviewed, from the thread's LATEST
    marker-bearing comment; None when the thread carries no marker."""
    if thread is None:
        return None
    marker_hit = False
    for comment in reversed(thread.get("comments") or []):
        content = str(comment.get("content") or "")
        head = _MARKER_HEAD_RE.search(content)
        if head is None:
            if REVIEW_MARKER_TEMPLATE in content:
                marker_hit = True  # marker without a head: the progress seed
            continue
        iteration = _MARKER_ITERATION_RE.search(content)
        return head.group(1).lower(), int(iteration.group(1)) if iteration else None
    return ("", None) if marker_hit else None


def _split_findings(
    verdict: ReactiveReviewVerdict, patched_paths: set[str]
) -> tuple[list[ReactiveFinding], list[ReactiveFinding]]:
    """Positioned findings on patched files (capped) vs everything else."""
    anchored = [
        finding
        for finding in verdict.findings
        if finding.line is not None and finding.file in patched_paths
    ]
    posted, overflow = anchored[:REVIEW_MAX_COMMENTS], anchored[REVIEW_MAX_COMMENTS:]
    unanchored = [
        finding
        for finding in verdict.findings
        if not (finding.line is not None and finding.file in patched_paths)
    ]
    return posted, [*unanchored, *overflow]


def _render_summary(
    verdict: ReactiveReviewVerdict,
    *,
    pr_id: int,
    head_sha: str,
    before_sha: str | None,
    incremental: bool,
    project: str,
    repo: str,
    folded: list[ReactiveFinding],
    latest_iteration: int | None,
) -> str:
    """The summary reply body: verdict, severity stats, folded findings.

    Mirrors the GitHub engine's body shape (severity table + fold block +
    evidence digest), capped defensively at :data:`REVIEW_BODY_SOFT_CAP`
    (AzDO documents no thread-comment length cap — the cap is the same
    defensive posture, not a documented limit).
    """
    header = (
        "\U0001f504 Forge Incremental Review" if incremental else "\U0001f916 Forge Code Review"
    )
    overall = _OVERALL_EMOJI.get(verdict.verdict, "\U0001f4a1")

    counts = {severity: 0 for severity in REACTIVE_SEVERITIES}
    for finding in verdict.findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    critical = counts.get("critical", 0) > 0
    parts: list[str] = [
        f"## {header}",
        "",
        f"**Overall:** {overall} {verdict.summary}",
        "",
        "| Severity | Count |",
        "|----------|-------|",
        f"| \U0001f4a1 Suggestions | {counts['suggestion']} |",
        f"| \u26a0\ufe0f Warnings | {counts['warning']} |",
        f"| \U0001f6a8 Critical | {counts['critical']} |",
        "",
    ]
    if critical:
        # The vote-note (ADR-0024 §5): forge never votes — the Active
        # threads ARE the REQUEST_CHANGES analog, display-only.
        parts.extend(
            [
                "\u26a0\ufe0f Critical findings stay as **Active** threads: forge never votes "
                "(display-only) — merge gates remain your branch policy's job.",
                "",
            ]
        )
    if folded:
        parts.append(
            f"*{len(folded)} finding(s) folded into this summary* "
            "(no diff line anchor, or above the thread cap):"
        )
        parts.append("")
        parts.append("<details><summary>Folded findings</summary>")
        parts.append("")
        for finding in folded:
            location = finding.file or "(repo-wide)"
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            parts.append(
                f"- {_SEVERITY_EMOJI.get(finding.severity, '\U0001f4a1')} "
                f"**{finding.severity}** `{location}` \u2014 {finding.note}"
            )
        parts.append("")
        parts.append("</details>")
        parts.append("")
    if incremental and before_sha:
        parts.append(f"*Reviewed delta: `{before_sha[:8]}..{head_sha[:8]}` only.*")
        parts.append("")
    digest = review_digest(f"{project}/{repo}", pr_id, head_sha, verdict)
    parts.append(
        f"*Reviewed by Forge \u00b7 reactive review \u00b7 digest `{digest}` \u00b7 head `{head_sha[:8]}`*"
    )
    body = "\n".join(part for part in parts)
    # The machine-readable anchor (the NEXT event's incremental range) is
    # appended AFTER the defensive cap so truncation can never cut it.
    anchor = ""
    if latest_iteration is not None:
        marker = REVIEW_MARKER_TEMPLATE.format(pr_id=pr_id)
        anchor = f"\n\n{marker} head:{head_sha} iteration:{latest_iteration}"
    limit = REVIEW_BODY_SOFT_CAP - len(anchor)
    if len(body) > limit:
        body = body[: limit - 200].rstrip() + "\n\n*… [truncated — report exceeded the body cap]*"
    return body + anchor


# ----------------------------------------------------------------------
# Step dispatch (the durable lane's executor seam)
# ----------------------------------------------------------------------


def _client_from_settings(settings: Settings) -> AzureDevOpsClient:
    """A client from the AZ-2 Settings fields (ADR-0024 §2)."""
    pat = settings.FORGE_AZDO_PAT
    token = pat.get_secret_value() if pat is not None else ""
    return AzureDevOpsClient(
        base_url=settings.FORGE_AZDO_ORG_URL,
        token=token,
    )


async def execute_azure_reactive_review(
    settings: Settings,
    forge_config: Any,
    session_factory: "async_sessionmaker[AsyncSession] | None",
    metadata: dict[str, Any],
    *,
    stack_factory: Any | None = None,
) -> dict[str, Any] | None:
    """Execute a ``review_pr`` step payload for provider ``azure_devops``.

    Wired from :func:`forge.runs.azure_service.execute_azure_run_command`;
    the *stack_factory* hook exists for tests to run the engine over a fake
    client + fake LLM, mirroring
    :func:`forge.reactive.github_review.execute_reactive_review`.
    """
    project = str(metadata.get("project") or "")
    repo = str(metadata.get("repo") or "")
    if not project or not repo:
        logger.error("Reactive review without project/repo — ignoring")
        return None

    if stack_factory is None:
        client = _client_from_settings(settings)
        llm = LLMClient(settings=settings, session_factory=session_factory)
        reviewer = AzureReactiveReviewer(llm, client, settings=settings)
        stack = (client, reviewer)
    else:
        stack = stack_factory(project, repo)
    client, reviewer = stack

    engine = AzureReactiveReviewEngine(
        settings,
        forge_config,
        session_factory,
        client=client,
        reviewer=reviewer,
        project=project,
        repo=repo,
    )
    try:
        return await engine.run(metadata)
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
