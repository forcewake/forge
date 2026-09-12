"""GitLab write path: materialize a ChangeSet as a real branch + commit.

Journaling per ADR-0005: the commit intent is written to ``action_log``
*before* dispatch and completed with ``succeeded`` / ``failed`` /
``unknown_outcome`` after. A lost create-commit response is reconciled by
listing the branch commits and matching the commit message; only an exactly
one matching new commit resolves the outcome. Anything else stays
``unknown_outcome`` and the caller must block the run — never blind-retry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forge.durable import Controller
from forge.gitlab.client import CommitOutcomeUnknown, GitLabAPIError, GitLabClient
from forge.repository.changeset import ChangeSet, Operation

logger = logging.getLogger(__name__)


class WriteOutcome(StrEnum):
    COMMITTED = "committed"
    UNKNOWN = "unknown_outcome"


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    commit_sha: str | None


def _commit_actions(cs: ChangeSet) -> list[dict[str, Any]]:
    """Convert ChangeSet entries into GitLab Commits API actions."""
    actions: list[dict[str, Any]] = []
    for change in cs.changes:
        action: dict[str, Any] = {
            "action": change.operation.value,
            "file_path": change.path,
        }
        # delete actions carry no content; update may (full replacement text).
        if change.content is not None and change.operation is not Operation.DELETE:
            action["content"] = change.content
        actions.append(action)
    return actions


class ChangesetWriter:
    """Applies a validated ChangeSet to GitLab with journaled, reconcilable writes."""

    def __init__(
        self,
        gitlab: GitLabClient,
        session_factory: Any,
        project_id: int,
    ) -> None:
        self._gitlab = gitlab
        self._session_factory = session_factory
        self._project_id = project_id

    async def apply(
        self,
        flow_run_id: str,
        cs: ChangeSet,
        start_ref: str = "main",
    ) -> WriteResult:
        """Ensure the branch exists, commit the ChangeSet, journal the outcome.

        Returns :class:`WriteResult`; on ``unknown_outcome`` the caller must
        block the run (ADR-0005) — the commit may or may not exist.
        """
        await self._ensure_branch(cs.branch, start_ref)

        async with self._session_factory() as session:
            # (b) intent row before dispatch (ADR-0005), correlated by branch.
            controller = Controller(session)
            action_id = await controller.record_action(
                flow_run_id, "commit", correlation_id=cs.branch
            )
            await session.commit()

        try:
            commit = await self._gitlab.create_commit(
                self._project_id,
                cs.branch,
                _commit_actions(cs),
                cs.commit_message,
                start_branch=start_ref,
            )
        except CommitOutcomeUnknown:
            return await self._resolve_unknown(action_id, cs)
        except GitLabAPIError as exc:
            await self._complete(action_id, "failed", {"error": str(exc)})
            raise

        sha = commit.get("id")
        await self._complete(action_id, "succeeded", {"sha": sha})
        # Exact-SHA correlation: this is the sha all later CI evidence must match.
        return WriteResult(WriteOutcome.COMMITTED, sha)

    async def _ensure_branch(self, branch: str, start_ref: str) -> None:
        """Create the branch; tolerate 'already exists' (idempotent re-entry)."""
        try:
            await self._gitlab.create_branch(self._project_id, branch, start_ref)
        except GitLabAPIError as exc:
            if exc.status_code == 400 and "already exists" in exc.message.lower():
                # Branch pre-exists (retry after crash) — verify it is real.
                await self._gitlab.get_branch(self._project_id, branch)
                return
            raise

    async def _resolve_unknown(self, action_id: int, cs: ChangeSet) -> WriteResult:
        """Reconcile a lost create-commit response by matching the commit message.

        Exactly one matching commit on the branch proves the commit landed;
        zero or several matches stay unknown — the run must block, not retry.
        """
        logger.warning(
            "create_commit outcome unknown on branch %r — reconciling by commit message",
            cs.branch,
        )
        sha: str | None = None
        try:
            commits = await self._gitlab.list_commits(self._project_id, cs.branch)
            matches = [c for c in commits if c.get("message") == cs.commit_message]
            if len(matches) == 1:
                sha = matches[0]["sha"]
        except GitLabAPIError:
            logger.exception("Reconciliation read failed for branch %r", cs.branch)

        if sha is not None:
            await self._complete(action_id, "succeeded", {"sha": sha, "reconciled": True})
            return WriteResult(WriteOutcome.COMMITTED, sha)

        await self._complete(action_id, "unknown_outcome")
        return WriteResult(WriteOutcome.UNKNOWN, None)

    async def _complete(self, action_id: int, status: str, remote_result: dict | None = None):
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.complete_action(action_id, status, remote_result)  # type: ignore[arg-type]
            await session.commit()
