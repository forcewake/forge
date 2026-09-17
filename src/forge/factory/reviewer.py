"""LLM reviewer agent (ADR-0004/0008): readonly review of the candidate diff.

Strictly read-only: the reviewer receives the candidate diff (via
``compare_commits`` between the approved base snapshot and the candidate) plus
the issue title and plan summary — never write tools, never the writer, never
a way to change anything. Its verdict is recorded with the SHA it reviewed
(ADR-0008: the review approves a *specific* SHA).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from forge.factory.llm import LLMClient, LLMResponseError, parse_json, truncate_chars

if TYPE_CHECKING:
    from forge.config import Settings
    from forge.gitlab.client import GitLabClient

logger = logging.getLogger(__name__)

#: The strong tier: review quality gates ready_for_human.
REVIEWER_TIER = "strong"

#: The reviewer model is stochastic (LIVE: glm returned prose instead of
#: JSON on 2 of 4 review calls, blocking otherwise-green runs at
#: ``review_failed``). One bounded re-ask — same prompt, same contract,
#: every attempt journaled to ``llm_calls`` — is honest; weakening the
#: verdict because the model had a bad sample is not (ADR-0008).
REVIEW_ATTEMPTS = 2


async def review_with_retry(
    llm: LLMClient,
    *,
    system: str,
    user: str,
    parse: Any,
    flow_run_id: str | None = None,
) -> Any:
    """Run the review call, re-asking once when the output won't parse.

    *parse* is a callable turning the model text into the verdict; only
    :class:`LLMResponseError` (an unusable response — invalid JSON in JSON
    mode, unparseable or wrong-shape output) triggers a retry, from the
    call itself or from *parse*; real API/network failures propagate
    immediately.
    """
    last_error: LLMResponseError | None = None
    for attempt in range(1, REVIEW_ATTEMPTS + 1):
        try:
            result = await llm.complete(
                tier=REVIEWER_TIER,
                system=system,
                user=user,
                role="reviewer",
                flow_run_id=flow_run_id,
                json_mode=True,
            )
            return parse(result.text)
        except LLMResponseError as exc:
            last_error = exc
            logger.warning("review parse failed (attempt %d/%d): %s", attempt, REVIEW_ATTEMPTS, exc)
    assert last_error is not None
    raise last_error


#: Deterministic input budget (chars) for the review prompt (ADR-0013).
REVIEWER_MAX_INPUT_CHARS = 20000

#: The diff section is capped separately, biggest files first.
REVIEWER_MAX_DIFF_CHARS = 16000

_SEVERITIES = ("info", "minor", "major")
_VERDICTS = ("ok", "concerns")

_SYSTEM_PROMPT = (
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
class ReviewFinding:
    """One review finding, bounded to what the schema allows."""

    severity: str
    file: str
    note: str


@dataclass(frozen=True)
class ReviewVerdict:
    """The reviewer's output, ready for the evidence ledger."""

    verdict: str  # "ok" | "concerns"
    summary: str
    findings: tuple[ReviewFinding, ...]


class LLMReviewer:
    """Reviews the candidate diff via the LiteLLM proxy (read-only)."""

    def __init__(
        self,
        llm: LLMClient,
        gitlab: GitLabClient,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._gitlab = gitlab
        self._settings = settings

    async def review(
        self,
        *,
        project_id: int,
        issue_title: str,
        plan_summary: str,
        base_sha: str,
        candidate_sha: str,
        flow_run_id: str | None = None,
    ) -> ReviewVerdict:
        """Review base_sha..candidate_sha and return the parsed verdict."""
        diff = await self._candidate_diff(project_id, base_sha, candidate_sha)
        result_user = (
            f"Issue title: {issue_title}\n\n"
            f"Plan summary:\n{plan_summary or '(no plan summary available)'}\n\n"
            f"Candidate diff ({base_sha[:8]}..{candidate_sha[:8]}):\n{diff}"
        )
        return await review_with_retry(
            self._llm,
            system=_SYSTEM_PROMPT,
            user=truncate_chars(result_user, REVIEWER_MAX_INPUT_CHARS),
            parse=self._parse,
            flow_run_id=flow_run_id,
        )

    # ------------------------------------------------------------------

    async def _candidate_diff(
        self,
        project_id: int,
        base_sha: str,
        candidate_sha: str,
    ) -> str:
        """Render base..candidate as unified diff text, biggest files first."""
        try:
            comparison = await self._gitlab.compare_commits(
                project_id, from_sha=base_sha, to_sha=candidate_sha
            )
        except Exception:
            logger.warning(
                "Compare read failed for project %s — reviewing without diff",
                project_id,
                exc_info=True,
            )
            return "(diff unavailable)"

        diffs = comparison.get("diffs") or []
        diffs = sorted(diffs, key=lambda d: len(str(d.get("diff") or "")), reverse=True)
        parts: list[str] = []
        for entry in diffs:
            old_path = entry.get("old_path", "")
            new_path = entry.get("new_path", "")
            diff_text = str(entry.get("diff") or "")
            if not diff_text:
                continue
            parts.append(f"diff --git a/{old_path} b/{new_path}")
            parts.append(diff_text)
        return truncate_chars("\n".join(parts), REVIEWER_MAX_DIFF_CHARS)

    @staticmethod
    def _parse(text: str) -> ReviewVerdict:
        parsed = parse_json(text)
        verdict = str(parsed.get("verdict", "")).strip().lower()
        if verdict not in _VERDICTS:
            raise LLMResponseError(f"review verdict {verdict!r} is not one of {_VERDICTS}")
        summary = str(parsed.get("summary", "")).strip()
        findings = tuple(_parse_finding(raw) for raw in (parsed.get("findings") or []))
        return ReviewVerdict(verdict=verdict, summary=summary, findings=findings)


def _parse_finding(raw: Any) -> ReviewFinding:
    if not isinstance(raw, dict):
        raise LLMResponseError("review finding must be a JSON object")
    severity = str(raw.get("severity", "info")).strip().lower()
    if severity not in _SEVERITIES:
        severity = "info"
    return ReviewFinding(
        severity=severity,
        file=str(raw.get("file", "")),
        note=str(raw.get("note", "")),
    )


__all__ = [
    "REVIEW_ATTEMPTS",
    "REVIEWER_MAX_DIFF_CHARS",
    "REVIEWER_MAX_INPUT_CHARS",
    "REVIEWER_TIER",
    "LLMReviewer",
    "review_with_retry",
    "ReviewFinding",
    "ReviewVerdict",
]
