"""GitLab write path: materialize a ChangeSet as a real branch + commit.

Journaling per ADR-0005: the commit intent is written to ``action_log``
*before* dispatch and completed with ``succeeded`` / ``failed`` /
``unknown_outcome`` after. A lost create-commit response is reconciled by
listing the branch commits and requiring this attempt's unique
``(forge-op:...)`` message marker AND the expected parent OID; only exactly
one matching new commit resolves the outcome. Anything else stays
``unknown_outcome`` and the caller must block the run — never blind-retry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from forge.durable import Controller
from forge.gitlab.client import CommitOutcomeUnknown, GitLabAPIError, GitLabClient
from forge.repository.changeset import ChangeSet, Operation

logger = logging.getLogger(__name__)

#: A full 40-hex SHA is the only start_ref that pins a frozen base; any
#: branch/tag name is resolved by GitLab at branch-creation time and may
#: have moved by the time the commit lands.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class WriteOutcome(StrEnum):
    COMMITTED = "committed"
    UNKNOWN = "unknown_outcome"


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    commit_sha: str | None


class BranchDriftError(Exception):
    """The branch head is not the pinned base at dispatch time.

    Raised *before* any commit is attempted when the caller-supplied
    ``expected_head`` does not match the live branch head; carries the
    expected and actual OIDs.
    """

    def __init__(self, branch: str, expected: str, actual: str | None) -> None:
        self.branch = branch
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"branch {branch!r} head drifted: expected {expected!r}, actual {actual!r}"
        )


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


def _op_marker(operation_key: str) -> str:
    """Unique-per-apply marker appended to every commit message the writer makes."""
    return f"(forge-op:{operation_key})"


def _expected_parents(expected_parent: str | None) -> list[str]:
    """Parent-OID list a fresh commit must have (root commit -> no parents)."""
    return [] if expected_parent is None else [expected_parent]


@dataclass(frozen=True)
class _CommitIntent:
    """Everything reconciliation needs to attribute a possibly-lost commit."""

    branch: str
    commit_message: str
    operation_key: str
    expected_parent: str | None  # branch head OID captured at intent time
    expected_head: str | None  # caller-pinned head (drift guard) for the journal


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
        #: Operation key stamped into the commit message of the current (or
        #: last) apply() call; fresh uuid4-derived per call, injectable for tests.
        self.operation_key: str | None = None

    async def apply(
        self,
        flow_run_id: str,
        cs: ChangeSet,
        start_ref: str = "main",
        expected_head: str | None = None,
        operation_key: str | None = None,
    ) -> WriteResult:
        """Ensure the branch exists, commit the ChangeSet, journal the outcome.

        *start_ref* is passed through verbatim to :meth:`ensure_branch`: a
        full 40-hex SHA pins a frozen base, while a branch/tag name is
        resolved by GitLab when the factory branch is created and may have
        moved. When *expected_head* is given, the live branch head is
        fetched before dispatch and must equal it — else
        :class:`BranchDriftError` and nothing is committed.

        *operation_key* overrides the fresh uuid4-derived key stamped into
        the commit message each call (``(forge-op:<key>)``); reconciliation
        matches on it, so a previous repair cycle's commit — whose human
        prefix repeats — can never be misattributed.

        Returns :class:`WriteResult`; on ``unknown_outcome`` the caller must
        block the run (ADR-0005) — the commit may or may not exist.
        """
        self.operation_key = operation_key or uuid4().hex[:12]
        commit_message = f"{cs.commit_message} {_op_marker(self.operation_key)}"
        if _FULL_SHA_RE.fullmatch(start_ref):
            logger.debug("start_ref %s is a full SHA — frozen base", start_ref)
        await self.ensure_branch(cs.branch, start_ref)

        # One head fetch serves both guards: drift detection against
        # expected_head (before anything is committed) and the expected
        # parent OID reconciliation needs if the response is lost.
        branch_head = await self._branch_head(cs.branch)
        if expected_head is not None and branch_head != expected_head:
            raise BranchDriftError(cs.branch, expected_head, branch_head)

        async with self._session_factory() as session:
            # (b) intent row before dispatch (ADR-0005), correlated by branch.
            controller = Controller(session)
            action_id = await controller.record_action(
                flow_run_id, "commit", correlation_id=cs.branch
            )
            await session.commit()

        intent = _CommitIntent(
            branch=cs.branch,
            commit_message=commit_message,
            operation_key=self.operation_key,
            expected_parent=branch_head,
            expected_head=expected_head,
        )
        try:
            commit = await self._gitlab.create_commit(
                self._project_id,
                cs.branch,
                _commit_actions(cs),
                commit_message,
                # Never pass start_branch: on GitLab CE 18.x the Commits API
                # then tries to create the branch again and 400s with
                # "already exists" — ensure_branch has already guaranteed
                # the branch exists.
            )
        except CommitOutcomeUnknown:
            return await self._resolve_unknown(action_id, intent)
        except GitLabAPIError as exc:
            await self._complete(
                action_id, "failed", self._meta({"error": str(exc)}, expected_head)
            )
            raise

        sha = commit.get("id")
        await self._complete(action_id, "succeeded", self._meta({"sha": sha}, expected_head))
        # Exact-SHA correlation: this is the sha all later CI evidence must match.
        return WriteResult(WriteOutcome.COMMITTED, sha)

    async def ensure_branch(self, branch: str, start_ref: str) -> bool:
        """Create the branch; tolerate 'already exists' (idempotent re-entry).

        Public so the ci_harness backend can guarantee the factory branch
        exists before triggering the harness pipeline (ADR-0015).

        Returns True when this call created the branch, False when it
        pre-existed (retry after crash).
        """
        try:
            await self._gitlab.create_branch(self._project_id, branch, start_ref)
            return True
        except GitLabAPIError as exc:
            if exc.status_code == 400 and "already exists" in exc.message.lower():
                # Branch pre-exists (retry after crash) — verify it is real.
                await self._gitlab.get_branch(self._project_id, branch)
                return False
            raise

    async def _branch_head(self, branch: str) -> str | None:
        """Current head OID of *branch*, or None when it has no commits."""
        info = await self._gitlab.get_branch(self._project_id, branch)
        head = info.get("commit") or {}
        return head.get("id")

    async def _resolve_unknown(self, action_id: int, intent: _CommitIntent) -> WriteResult:
        """Reconcile a lost create-commit response by marker + parent.

        A commit proves this attempt landed only when its message contains
        this call's exact ``(forge-op:...)`` marker AND its parent is the
        branch head captured at intent time — the human message alone
        repeats across repair cycles. Exactly one such commit resolves as
        succeeded; zero or several stay unknown — the run must block, not
        retry (ADR-0005).
        """
        marker = _op_marker(intent.operation_key)
        logger.warning(
            "create_commit outcome unknown on branch %r — reconciling by marker %s + parent",
            intent.branch,
            marker,
        )
        sha: str | None = None
        try:
            commits = await self._gitlab.list_commits(self._project_id, intent.branch)
            matches = [
                c
                for c in commits
                if marker in (c.get("message") or "")
                and (c.get("parent_ids") or []) == _expected_parents(intent.expected_parent)
            ]
            if len(matches) == 1:
                sha = matches[0]["sha"]
        except GitLabAPIError:
            logger.exception("Reconciliation read failed for branch %r", intent.branch)

        if sha is not None:
            await self._complete(
                action_id,
                "succeeded",
                self._meta({"sha": sha, "reconciled": True}, intent.expected_head),
            )
            return WriteResult(WriteOutcome.COMMITTED, sha)

        remote_result: dict[str, Any] | None = None
        if intent.expected_head is not None:
            remote_result = {"expected_head": intent.expected_head}
        await self._complete(action_id, "unknown_outcome", remote_result)
        return WriteResult(WriteOutcome.UNKNOWN, None)

    @staticmethod
    def _meta(result: dict[str, Any], expected_head: str | None) -> dict[str, Any]:
        """Outcome metadata; carries the pinned head when one was enforced."""
        if expected_head is not None:
            result["expected_head"] = expected_head
        return result

    async def _complete(self, action_id: int, status: str, remote_result: dict | None = None):
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.complete_action(action_id, status, remote_result)  # type: ignore[arg-type]
            await session.commit()
