"""GitHub reactive review engine (v0.7, feature F1 of the research port).

When a Draft PR in a forge-tracked repo is opened or pushed to
(``pull_request`` ``opened``/``synchronize``), the webhook ingress schedules
one durable step (the same inbox/step mechanics as run commands — ADR-0017)
whose executor is :func:`execute_reactive_review` below. The engine:

1. **Guards** against self-review: Bot senders, bot-authored PRs and the
   ``forge/`` head-branch prefix (E1's durable flow publishes forge commits
   there — the reactive reviewer must never review forge's own runs) are
   skipped with a log line, before any paid call.
2. **Posts a sticky progress comment** before the LLM call using the hidden
   HTML marker ``<!-- forge:review:{pr_number} -->`` (github-reactive
   research §3.3/§4 prior art): an existing marker comment is PATCHed in
   place, never re-posted, so re-deliveries spawn no duplicates.
3. **Detects the incremental delta for free**: ``list_reviews`` filtered to
   forge's own identity gives the last reviewed ``commit_id``; on
   ``synchronize`` the payload's ``before``/``after`` SHAs name the delta,
   read via :meth:`~forge.integrations.github.GitHubClient.get_compare`.
   No prior forge review → the full PR diff (:meth:`...get_pr_files`). A
   head SHA forge already reviewed is skipped (idempotent re-delivery).
4. **Runs the readonly reviewer** (the strong-tier, same JSON contract as
   the durable lane's :class:`~forge.integrations.github_flow.GitHubPRReviewer`)
   over the diff — full or delta — and posts the result as a NATIVE GitHub
   review: findings that carry a resolvable ``path``+``line`` become inline
   review comments (capped at :data:`REVIEW_MAX_COMMENTS`; the overflow
   folds into the summary body's ``<details>`` block), the summary carries
   verdict + severity stats + an evidence footer with the run digest, and
   the body is hard-capped under GitHub's 65,536-char limit.
5. **Maps the verdict to the review event**: ``REQUEST_CHANGES`` only when a
   finding is marked ``critical`` (the reactive policy analog of the
   durable lane's RunSpec critical rule — no RunSpec exists on this lane),
   ``COMMENT`` otherwise. Severity uses the GitLab reactive scale
   (``suggestion``/``warning``/``critical``) so review quality is identical
   across providers.

No budget is applied (no RunSpec): model usage lands in the ``llm_calls``
ledger with driver completeness like every other call (ADR-0013).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from forge.factory.llm import LLMClient, LLMError, LLMResponseError, parse_json, truncate_chars
from forge.factory.reviewer import REVIEWER_MAX_DIFF_CHARS, REVIEWER_MAX_INPUT_CHARS, REVIEWER_TIER
from forge.integrations.github import GITHUB_BODY_MAX_CHARS, GitHubClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from forge.config import ForgeConfig, Settings

logger = logging.getLogger(__name__)

#: The hidden sticky-comment marker (research §3.3): every forge progress
#: comment on a PR embeds exactly this HTML comment; the engine finds its
#: own comment by it and PATCHes in place instead of posting a new one.
REVIEW_MARKER_TEMPLATE = "<!-- forge:review:{pr_number} -->"

#: Inline comments per review. GitHub documents no hard cap on
#: ``comments[]``, but content-creation secondary rate limits and the
#: check-run annotation batch size (50/request) make 50 the workable
#: ceiling (research §1.1/§2) — overflow folds into the summary body.
REVIEW_MAX_COMMENTS = 50

#: Summary bodies are capped below GitHub's 65,536-char hard limit with
#: headroom for the rendered markdown overhead.
REVIEW_BODY_SOFT_CAP = 60_000

#: Reactive severities — the GitLab reactive lane's scale
#: (forge.agents.models.InlineComment), not the readonly verdict's
#: info/minor/major: "critical" is what flips the review to
#: REQUEST_CHANGES.
REACTIVE_SEVERITIES = ("suggestion", "warning", "critical")

_SEVERITY_EMOJI = {
    "suggestion": "\U0001f4a1",  # 💡
    "warning": "\u26a0\ufe0f",  # ⚠️
    "critical": "\U0001f6a8",  # 🚨
}

_OVERALL_EMOJI = {
    "ok": "\u2705",  # ✅
    "concerns": "\u26a0\ufe0f",  # ⚠️
}

#: Same JSON review contract as the durable lane (forge.factory.reviewer /
#: github_flow), extended with the reactive severity scale and an optional
#: 1-based line anchor so findings can be posted INLINE on the diff.
_REACTIVE_SYSTEM_PROMPT = (
    "You are the readonly review agent of a code-writing bot. You review a "
    "candidate diff against the issue and plan it implements. You cannot "
    "change anything; you only judge.\n"
    "Respond with ONLY a JSON object:\n"
    '{"verdict": "ok" | "concerns", "summary": "<1-3 sentences>", '
    '"findings": [{"severity": "suggestion"|"warning"|"critical", '
    '"file": "<path>", "line": <line in the NEW file version, or null>, '
    '"note": "<what and why>"}]}\n'
    'Use "ok" when the diff is a sound implementation of the issue; use '
    '"concerns" when a human should look closely before merging. Never '
    'invent files that are not in the diff. Use "line" only for a line '
    "the diff actually touches."
)


@dataclass(frozen=True)
class ReactiveFinding:
    """One reactive finding: the readonly shape plus an optional line anchor."""

    severity: str
    file: str
    note: str
    line: int | None = None


@dataclass(frozen=True)
class ReactiveReviewVerdict:
    """The reactive reviewer's output (verdict/summary/findings)."""

    verdict: str  # "ok" | "concerns"
    summary: str
    findings: tuple[ReactiveFinding, ...]


class GitHubReactiveReviewer:
    """Strong-tier diff review over the PR surface (full or delta).

    The reactive twin of :class:`~forge.integrations.github_flow.GitHubPRReviewer`:
    same input discipline (biggest-files-first, deterministic caps), same
    JSON contract family, plus a line anchor per finding so the engine can
    post inline review comments. *before_sha* switches the diff surface
    from the full PR files to the ``before...after`` compare delta.
    """

    def __init__(self, llm: LLMClient, client: GitHubClient, settings: Settings | None = None):
        self._llm = llm
        self._client = client
        self._settings = settings

    async def build_diff(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        before_sha: str | None = None,
        after_sha: str = "HEAD",
    ) -> tuple[str, list[dict[str, Any]]]:
        """Render the reviewed diff text plus its changed-file entries.

        Incremental (``before_sha`` set) reads the ``before...after``
        compare delta; full reads the PR's files. Files whose ``patch``
        GitHub omitted (binary, oversized — research §2) are reviewed from
        the surrounding entries but can never anchor an inline comment.
        """
        try:
            if before_sha is not None:
                comparison = await self._client.get_compare(owner, repo, before_sha, after_sha)
                files = [dict(f) for f in (comparison.get("files") or [])]
                label = f"{before_sha[:8]}..{after_sha[:8]}"
            else:
                files = await self._client.get_pr_files(owner, repo, pr_number)
                label = f"PR #{pr_number} (full)"
        except Exception:
            logger.warning(
                "Diff read failed for %s/%s#%s — reviewing without diff",
                owner,
                repo,
                pr_number,
                exc_info=True,
            )
            return "(diff unavailable)", []

        ordered = sorted(files, key=lambda f: len(str(f.get("patch") or "")), reverse=True)
        parts: list[str] = []
        for entry in ordered:
            patch = str(entry.get("patch") or "")
            if not patch:
                continue
            name = str(entry.get("filename") or "")
            parts.append(f"diff --git a/{name} b/{name}")
            parts.append(patch)
        text = truncate_chars("\n".join(parts), REVIEWER_MAX_DIFF_CHARS)
        logger.info(
            "Reactive review diff for %s/%s#%s: %s (%d files)",
            owner,
            repo,
            pr_number,
            label,
            len(files),
        )
        return text, files

    async def review(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        diff: str,
        head_sha: str,
        flow_run_id: str | None = None,
    ) -> ReactiveReviewVerdict:
        """Review the rendered diff and return the parsed verdict."""
        user = (
            f"Pull request: {owner}/{repo}#{pr_number}\n"
            f"Reviewed head: {head_sha}\n\n"
            f"Candidate diff:\n{diff}"
        )
        result = await self._llm.complete(
            tier=REVIEWER_TIER,
            system=_REACTIVE_SYSTEM_PROMPT,
            user=truncate_chars(user, REVIEWER_MAX_INPUT_CHARS),
            role="reviewer",
            flow_run_id=flow_run_id,
            json_mode=True,
        )
        return self._parse(result.text)

    @staticmethod
    def _parse(text: str) -> ReactiveReviewVerdict:
        parsed = parse_json(text)
        verdict = str(parsed.get("verdict", "")).strip().lower()
        if verdict not in ("ok", "concerns"):
            raise LLMResponseError(f"review verdict {verdict!r} is not one of ('ok', 'concerns')")
        summary = str(parsed.get("summary", "")).strip()
        findings = tuple(_parse_finding(raw) for raw in (parsed.get("findings") or []))
        return ReactiveReviewVerdict(verdict=verdict, summary=summary, findings=findings)


def _parse_finding(raw: Any) -> ReactiveFinding:
    if not isinstance(raw, dict):
        raise LLMResponseError("review finding must be a JSON object")
    severity = str(raw.get("severity", "suggestion")).strip().lower()
    if severity not in REACTIVE_SEVERITIES:
        severity = "suggestion"
    line = raw.get("line")
    if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
        line = None
    return ReactiveFinding(
        severity=severity,
        file=str(raw.get("file", "")),
        note=str(raw.get("note", "")),
        line=line,
    )


class GitHubReactiveReviewEngine:
    """Turns a ``pull_request`` webhook into one posted GitHub review."""

    def __init__(
        self,
        settings: Settings,
        forge_config: ForgeConfig,
        session_factory: "async_sessionmaker[AsyncSession]",
        *,
        client: GitHubClient,
        reviewer: GitHubReactiveReviewer,
        repo_full_name: str,
    ) -> None:
        self._settings = settings
        self._config = forge_config
        self._session_factory = session_factory
        self._client = client
        self._reviewer = reviewer
        self._repo_full_name = repo_full_name

    @property
    def _owner(self) -> str:
        return self._repo_full_name.split("/", 1)[0]

    @property
    def _repo(self) -> str:
        return self._repo_full_name.split("/", 1)[1]

    @property
    def _bot_login(self) -> str:
        return str(self._settings.FORGE_BOT_USERNAME or "forge-bot")

    async def run(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Execute one reactive review step; returns a small outcome record."""
        pr_number = int(metadata.get("pr_number") or 0)
        head_sha = str(metadata.get("head_sha") or metadata.get("after_sha") or "")
        head_branch = str(metadata.get("head_branch") or "")
        before_sha = str(metadata.get("before_sha") or "")
        action = str(metadata.get("action") or "synchronize")
        sender_type = str(metadata.get("sender_type") or "User")
        pr_author_type = str(metadata.get("pr_author_type") or "User")

        # -- recursion guard (research §6.2) ---------------------------------
        # Ingress already filters; this is the durable defense-in-depth: E1's
        # flow publishes forge commits on forge/* branches, and forge's own
        # PRs/comments must never re-trigger the reviewer.
        if sender_type == "Bot":
            logger.info(
                "Reactive review skipped on %s#%s — Bot sender", self._repo_full_name, pr_number
            )
            return {"status": "skipped", "reason": "bot_sender"}
        if pr_author_type == "Bot":
            logger.info(
                "Reactive review skipped on %s#%s — bot-authored PR",
                self._repo_full_name,
                pr_number,
            )
            return {"status": "skipped", "reason": "bot_authored_pr"}
        if head_branch.startswith("forge/"):
            # The durable flow's own candidate branches: reviewing them would
            # have forge review forge (E1 boundary, module docstring).
            logger.info(
                "Reactive review skipped on %s#%s — forge-owned branch %s",
                self._repo_full_name,
                pr_number,
                head_branch,
            )
            return {"status": "skipped", "reason": "forge_branch"}

        if not pr_number or not head_sha:
            logger.warning("Reactive review metadata incomplete for %s", self._repo_full_name)
            return {"status": "skipped", "reason": "incomplete_metadata"}

        # -- sticky progress, BEFORE the LLM call (research §3.3) ------------
        marker = REVIEW_MARKER_TEMPLATE.format(pr_number=pr_number)
        progress = await self._upsert_progress_comment(pr_number, marker)

        # -- full vs incremental (research §1.4) ------------------------------
        last_reviewed = await self._last_reviewed_sha(pr_number)
        if last_reviewed == head_sha:
            logger.info(
                "PR %s#%d already reviewed at %s — skipping duplicate",
                self._repo_full_name,
                pr_number,
                head_sha[:8],
            )
            await self._finalize_progress(
                progress, marker, "\u2705 forge already reviewed this head — nothing new to review."
            )
            return {"status": "skipped", "reason": "already_reviewed"}

        incremental = bool(last_reviewed) and action == "synchronize" and bool(before_sha)
        delta_base = before_sha if incremental else None
        if incremental:
            logger.info(
                "Incremental reactive review of %s#%d: %s..%s",
                self._repo_full_name,
                pr_number,
                before_sha[:8],
                head_sha[:8],
            )

        # -- review the diff ---------------------------------------------------
        diff, files = await self._reviewer.build_diff(
            self._owner,
            self._repo,
            pr_number,
            before_sha=delta_base,
            after_sha=head_sha,
        )
        if incremental and not files:
            # Qodo's documented incremental behavior (research §7): a delta
            # with no file changes exits without calling the model.
            logger.info(
                "No file changes in %s..%s on %s#%d — skipping LLM review",
                before_sha[:8],
                head_sha[:8],
                self._repo_full_name,
                pr_number,
            )
            await self._finalize_progress(
                progress, marker, "\u2705 No file changes since the last review."
            )
            return {"status": "skipped", "reason": "empty_delta"}

        try:
            verdict = await self._reviewer.review(
                owner=self._owner,
                repo=self._repo,
                pr_number=pr_number,
                diff=diff,
                head_sha=head_sha,
            )
        except (LLMError, LLMResponseError):
            await self._finalize_progress(
                progress,
                marker,
                "\u26a0\ufe0f forge's review failed — it will retry automatically.",
            )
            raise

        # -- post the native review -------------------------------------------
        event = _review_event(verdict)
        body, comments = _render_review(
            verdict,
            files,
            head_sha=head_sha,
            before_sha=delta_base,
            incremental=incremental,
            pr_number=pr_number,
            repo_full_name=self._repo_full_name,
        )
        review = await self._client.create_review(
            self._owner,
            self._repo,
            pr_number,
            body,
            comments,
            event=event,
            commit_id=head_sha,
        )
        logger.info(
            "Reactive review posted on %s#%d at %s: %s, %d inline comment(s)",
            self._repo_full_name,
            pr_number,
            head_sha[:8],
            event,
            len(comments),
        )

        await self._finalize_progress(
            progress,
            marker,
            (
                f"\u2705 Review posted ({event.lower().replace('_', ' ')}): "
                f"{verdict.verdict}, {len(comments)} inline comment(s) at `{head_sha[:8]}`."
            ),
        )
        return {
            "status": "reviewed",
            "event": event,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "inline_comments": len(comments),
            "review_id": review.get("id"),
        }

    # ------------------------------------------------------------------
    # Sticky progress comment (hidden-marker convention, research §3.3)
    # ------------------------------------------------------------------

    async def _upsert_progress_comment(self, pr_number: int, marker: str) -> dict[str, Any] | None:
        """Post forge's progress comment, or PATCH the marker comment in place."""
        body = f"{marker} \u23f3 forge is reviewing this PR\u2026"
        try:
            for comment in await self._client.get_issue_comments(
                self._owner, self._repo, pr_number
            ):
                if marker not in str(comment.get("body") or ""):
                    continue
                if _author_login(comment) != self._bot_login:
                    continue  # someone quoted our marker — only PATCH our own
                updated = await self._client.update_issue_comment(
                    self._owner, self._repo, int(comment["id"]), body
                )
                logger.debug("Progress comment %s updated in place", comment.get("id"))
                return dict(updated)
            created = await self._client.create_issue_comment(
                self._owner, self._repo, pr_number, body
            )
            return dict(created)
        except Exception:
            # Progress is best-effort cosmetics — a failed sticky comment
            # must not stop the review.
            logger.warning("Progress comment upsert failed on PR #%d", pr_number, exc_info=True)
            return None

    async def _finalize_progress(
        self, progress: dict[str, Any] | None, marker: str, final_line: str
    ) -> None:
        """Leave the sticky comment at its final state (edit-in-place)."""
        if progress is None or progress.get("id") is None:
            return
        try:
            await self._client.update_issue_comment(
                self._owner, self._repo, int(progress["id"]), f"{marker} {final_line}"
            )
        except Exception:
            logger.debug("Progress comment finalization failed", exc_info=True)

    async def _last_reviewed_sha(self, pr_number: int) -> str | None:
        """The head SHA of forge's latest own review, or None (research §1.4)."""
        try:
            reviews = await self._client.list_reviews(self._owner, self._repo, pr_number)
        except Exception:
            logger.warning(
                "Review listing failed on PR #%d — reviewing in full", pr_number, exc_info=True
            )
            return None
        own = [
            review
            for review in reviews
            if _author_login(review) == self._bot_login and review.get("commit_id")
        ]
        if not own:
            return None
        return str(own[-1]["commit_id"])


def _author_login(obj: dict[str, Any]) -> str:
    return str((obj.get("user") or {}).get("login") or "")


def _review_event(verdict: ReactiveReviewVerdict) -> str:
    """``REQUEST_CHANGES`` only for critical findings, ``COMMENT`` otherwise.

    The reactive policy analog of the durable lane's RunSpec critical rule:
    no RunSpec exists on this lane, so the severity itself is the policy.
    """
    if any(finding.severity == "critical" for finding in verdict.findings):
        return "REQUEST_CHANGES"
    return "COMMENT"


def _render_review(
    verdict: ReactiveReviewVerdict,
    files: list[dict[str, Any]],
    *,
    head_sha: str,
    before_sha: str | None,
    incremental: bool,
    pr_number: int,
    repo_full_name: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Render the review summary body and its inline comments.

    Findings with a line anchor on a patched file become inline review
    comments (RIGHT side: findings reference new-file lines), capped at
    :data:`REVIEW_MAX_COMMENTS`; everything else — unanchored findings and
    the overflow — folds into the body's ``<details>`` block. The body is
    hard-capped under GitHub's 65,536-char limit with a truncation footer.
    """
    header = (
        "\U0001f504 Forge Incremental Review" if incremental else "\U0001f916 Forge Code Review"
    )
    overall = _OVERALL_EMOJI.get(verdict.verdict, "\U0001f4a1")

    counts = {severity: 0 for severity in REACTIVE_SEVERITIES}
    for finding in verdict.findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    patched = {str(f.get("filename") or "") for f in files if f.get("patch")}
    inline = [
        finding
        for finding in verdict.findings
        if finding.line is not None and finding.file in patched
    ]
    posted, folded = inline[:REVIEW_MAX_COMMENTS], inline[REVIEW_MAX_COMMENTS:]
    unanchored = [
        finding
        for finding in verdict.findings
        if not (finding.line is not None and finding.file in patched)
    ]
    folded = [*unanchored, *folded]

    comments = [
        {
            "path": finding.file,
            "line": finding.line,
            "side": "RIGHT",
            "body": (
                f"{_SEVERITY_EMOJI.get(finding.severity, '\U0001f4a1')} "
                f"**{finding.severity}** \u2014 {finding.note}"
            ),
        }
        for finding in posted
    ]

    range_line = ""
    if incremental and before_sha:
        range_line = f"\n\n*Reviewed delta: `{before_sha[:8]}..{head_sha[:8]}` only.*"

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
    ]
    if folded:
        parts.append(
            f"\n*{len(folded)} finding(s) folded into this summary* "
            "(no diff line anchor, or above the inline-comment cap):"
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
    parts.append(range_line.strip())
    digest = review_digest(repo_full_name, pr_number, head_sha, verdict)
    parts.append("")
    parts.append(
        f"*Reviewed by Forge \u00b7 reactive review \u00b7 digest `{digest}` \u00b7 "
        f"head `{head_sha[:8]}`*"
    )

    body = "\n".join(part for part in parts if part is not None)
    if len(body) > min(REVIEW_BODY_SOFT_CAP, GITHUB_BODY_MAX_CHARS):
        cut = min(REVIEW_BODY_SOFT_CAP, GITHUB_BODY_MAX_CHARS - 200)
        footer = "*… [truncated — full report exceeded GitHub's body limit]*"
        body = body[:cut].rstrip() + "\n\n" + footer
    return body, comments


def review_digest(
    repo_full_name: str, pr_number: int, head_sha: str, verdict: ReactiveReviewVerdict
) -> str:
    """Stable evidence digest of one reactive review (sha256, first 16 hex).

    The evidence footer's correlation key: same repo/PR/head/verdict always
    digest the same, so operators can match a posted review against logs and
    the ``llm_calls`` ledger without exposing prompt content.
    """
    material = "|".join(
        (
            repo_full_name,
            str(pr_number),
            head_sha,
            verdict.verdict,
            str(len(verdict.findings)),
            verdict.summary,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


async def execute_reactive_review(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: "async_sessionmaker[AsyncSession]",
    metadata: dict[str, Any],
    *,
    stack_factory: Any | None = None,
) -> dict[str, Any] | None:
    """Execute a ``review_pr`` step payload — the reactive lane's dispatch.

    Wired from :func:`forge.runs.github_service.execute_github_run_command`
    (which routes ``provider: github`` commands); the *stack_factory* hook
    exists for tests to run the engine over a fake client + fake LLM.
    """
    repo_full_name = str(metadata.get("repo_full_name") or "")
    if "/" not in repo_full_name:
        logger.error("Reactive review without repo_full_name — ignoring")
        return None
    owner, repo = repo_full_name.split("/", 1)

    if stack_factory is None:
        from forge.integrations.github_flow import credentials_from_settings

        client = GitHubClient(
            base_url=getattr(settings, "FORGE_GITHUB_API_URL", "https://api.github.com"),
            token_provider=credentials_from_settings(settings),
        )
        llm = LLMClient(settings=settings, session_factory=session_factory)
        reviewer = GitHubReactiveReviewer(llm, client, settings=settings)
        stack = (client, reviewer)
    else:
        stack = stack_factory(owner, repo)
    client, reviewer = stack

    engine = GitHubReactiveReviewEngine(
        settings,
        forge_config,
        session_factory,
        client=client,
        reviewer=reviewer,
        repo_full_name=repo_full_name,
    )
    try:
        return await engine.run(metadata)
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()


__all__ = [
    "GITHUB_BODY_MAX_CHARS",
    "REACTIVE_SEVERITIES",
    "REVIEW_BODY_SOFT_CAP",
    "REVIEW_MARKER_TEMPLATE",
    "REVIEW_MAX_COMMENTS",
    "GitHubReactiveReviewEngine",
    "GitHubReactiveReviewer",
    "ReactiveFinding",
    "ReactiveReviewVerdict",
    "execute_reactive_review",
    "review_digest",
]
