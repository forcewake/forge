"""GitLab write path: materialize a ChangeSet as a real branch + commit.

Journaling per ADR-0005: the commit intent is written to ``action_log`` AND
``publication_intents`` *before* dispatch (one transaction — R11) and
completed with ``succeeded`` / ``failed`` / ``unknown_outcome`` after. The
publication intent's ``operation_key`` is minted ONCE per intent and rides
in the commit message as ``(forge-op:<key>)``; a lost create-commit response
is reconciled by listing the branch commits and requiring this intent's
exact marker AND the expected parent OID. Anything else stays
``unknown_outcome`` and the caller must block the run — never blind-retry.

Re-entry (the R11 recovery): when a previous attempt's intent is still open
(``requested``/``dispatched``/``probing`` — the process died around the
effect), ``apply`` PROBES the remote by identity BEFORE any new dispatch:
exactly one marker+parent match adopts the landed commit (no duplicate);
zero matches with the head intact re-dispatches with the SAME key; zero
matches with a moved head raises :class:`BranchDriftError` (intent
``duplicated``); an inconclusive probe stays unknown. A fresh per-call key
would make the landed commit unfindable — that was the bug.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from forge.durable import Controller
from forge.durable.intents import (
    ProbeObservation,
    ProbeVerdict,
    classify_probe,
    commit_matches,
    complete_intent,
    find_open_intent,
    mark_dispatched,
    message_with_marker,
    mint_operation_key,
    op_marker,
    record_intent,
)
from forge.durable.models import PublicationIntent
from forge.gitlab.client import CommitOutcomeUnknown, GitLabAPIError, GitLabClient
from forge.repository.changeset import ChangeSet, Operation

logger = logging.getLogger(__name__)

#: A full 40-hex SHA is the only start_ref that pins a frozen base; any
#: branch/tag name is resolved by GitLab at branch-creation time and may
#: have moved by the time the commit lands.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Backoff for an intent whose probe read failed — the recovery scanner
#: retries after this, the inline leg never spins on it.
_PROBE_RETRY_SECONDS = 30


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
        #: last) apply() call: the open intent's key on recovery re-entry,
        #: the fresh intent's key otherwise. Injectable for tests.
        self.operation_key: str | None = None

    async def apply(
        self,
        flow_run_id: str,
        cs: ChangeSet,
        start_ref: str = "main",
        expected_head: str | None = None,
        operation_key: str | None = None,
        *,
        provider: str = "gitlab",
        repo: str = "",
        idempotency_scope: str | None = None,
        commit_cycle: int = 1,
        content_digest: str | None = None,
    ) -> WriteResult:
        """Ensure the branch exists, commit the ChangeSet, journal the outcome.

        *start_ref* is passed through verbatim to :meth:`ensure_branch`: a
        full 40-hex SHA pins a frozen base, while a branch/tag name is
        resolved by GitLab when the factory branch is created and may have
        moved. When *expected_head* is given, the live branch head is
        fetched before dispatch and must equal it — else
        :class:`BranchDriftError` and nothing is committed.

        The publication INTENT (R11) is written in the same transaction as
        the ``action_log`` row, strictly before the HTTP effect; its
        ``operation_key`` — *operation_key* when supplied, else minted here
        exactly once — is stamped into the commit message as
        ``(forge-op:<key>)`` and reused by every retry of this intent. When
        an OPEN intent already exists for this identity (a crashed or
        stalled previous attempt), the remote is PROBED first: a landed
        previous attempt is adopted, never duplicated (see the module
        docstring). *repo*/*idempotency_scope*/*commit_cycle*/*
        content_digest* are probe-correlation metadata; *repo* defaults to
        the GitLab project id and the scope to ``cycle-<n>``.

        Returns :class:`WriteResult`; on ``unknown_outcome`` the caller must
        block the run (ADR-0005) — the commit may or may not exist.
        """
        repo_identity = str(repo) if repo else str(self._project_id)
        scope = idempotency_scope or f"cycle-{commit_cycle}"

        # The durable record of a previous attempt, if any — read BEFORE the
        # drift guard so a landed-but-unjournaled commit is adopted, not
        # misread as drift (the R11 live failure).
        open_intent = await self._open_intent(
            flow_run_id, provider, repo_identity, cs.branch, scope
        )
        if open_intent is not None:
            await self.ensure_branch(cs.branch, start_ref)
            branch_head = await self._branch_head(cs.branch)
            return await self._recover_open_intent(
                open_intent, cs, flow_run_id, branch_head, expected_head
            )

        self.operation_key = operation_key or mint_operation_key()
        commit_message = message_with_marker(cs.commit_message, self.operation_key)
        if _FULL_SHA_RE.fullmatch(start_ref):
            logger.debug("start_ref %s is a full SHA — frozen base", start_ref)
        await self.ensure_branch(cs.branch, start_ref)

        # One head fetch serves both guards: drift detection against
        # expected_head (before anything is committed) and the expected
        # parent OID reconciliation needs if the response is lost.
        branch_head = await self._branch_head(cs.branch)
        if expected_head is not None and branch_head != expected_head:
            raise BranchDriftError(cs.branch, expected_head, branch_head)

        # (b) intent rows before dispatch (ADR-0005 + R11): the publication
        # intent (stable operation key + expected parent) and the action
        # journal commit TOGETHER, then — and only then — dispatch.
        async with self._session_factory() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                flow_run_id, "commit", correlation_id=cs.branch
            )
            intent_row = await record_intent(
                session,
                run_id=flow_run_id,
                provider=provider,
                repo=repo_identity,
                target_ref=cs.branch,
                idempotency_scope=scope,
                operation_key=self.operation_key,
                commit_cycle=commit_cycle,
                content_digest=content_digest,
                expected_parent_oid=branch_head,
                expected_head=expected_head,
            )
            await mark_dispatched(session, intent_row.id)
            await session.commit()

        commit_intent = _CommitIntent(
            branch=cs.branch,
            commit_message=commit_message,
            operation_key=self.operation_key,
            expected_parent=branch_head,
            expected_head=expected_head,
        )
        return await self._dispatch(
            flow_run_id, action_id, str(intent_row.id), commit_intent, cs, commit_message
        )

    async def _dispatch(
        self,
        flow_run_id: str,
        action_id: int,
        intent_id: str,
        commit_intent: _CommitIntent,
        cs: ChangeSet,
        commit_message: str,
    ) -> WriteResult:
        """POST the commit and journal the outcome on the intent + action rows."""
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
            return await self._resolve_unknown(action_id, intent_id, commit_intent)
        except GitLabAPIError as exc:
            await self._fail(action_id, intent_id, exc, commit_intent.expected_head)
            raise

        sha = commit.get("id")
        if not isinstance(sha, str) or not sha:
            # A committed response without a usable sha cannot be correlated
            # with later CI evidence — resolve through the probe instead of
            # propagating a None into the candidate history.
            return await self._resolve_unknown(action_id, intent_id, commit_intent)
        meta = self._meta({"sha": sha}, commit_intent.expected_head)
        # Exact-SHA correlation: this is the sha all later CI evidence must match.
        await self._succeed(action_id, intent_id, sha, meta, adopted=False)
        return WriteResult(WriteOutcome.COMMITTED, sha)

    async def _recover_open_intent(
        self,
        intent: PublicationIntent,
        cs: ChangeSet,
        flow_run_id: str,
        branch_head: str | None,
        expected_head: str | None,
    ) -> WriteResult:
        """Reconcile a PREVIOUS attempt's open intent before any new dispatch.

        The decision table (docs/research/remote-effect-reconciliation.md):
        exactly one marker+parent match ⇒ ADOPT (this process re-enters on
        the landed commit, zero new writes); zero matches + head intact ⇒
        safe re-dispatch WITH THE SAME KEY; zero matches + head moved ⇒
        ``duplicated`` (drift — never force); ≥2 matches or a failed probe
        read ⇒ conservative unknown. Never a blind POST as reconciliation.
        """
        self.operation_key = intent.operation_key
        try:
            commits = await self._gitlab.list_commits(self._project_id, intent.target_ref)
            hits = commit_matches(
                commits,
                operation_key=intent.operation_key,
                expected_parent_oid=intent.expected_parent_oid,
            )
            verdict = classify_probe(
                ProbeObservation(
                    marker_hits=tuple(hits),
                    head_oid=branch_head,
                    expected_parent_oid=intent.expected_parent_oid,
                )
            )
        except GitLabAPIError:
            logger.exception(
                "Recovery probe read failed for branch %r — leaving intent %s open "
                "for the scanner (no dispatch, never blind)",
                intent.target_ref,
                intent.id[:8],
            )
            await self._defer_probe(intent)
            action_id = await self._journal_action(flow_run_id, cs.branch)
            await self._complete(
                action_id,
                "unknown_outcome",
                self._meta({"probe": "unavailable"}, expected_head),
            )
            return WriteResult(WriteOutcome.UNKNOWN, None)

        if verdict is ProbeVerdict.ADOPT:
            sha = hits[0]
            logger.warning(
                "Adopting previous attempt's commit %s on %r (marker %s, parent intact)",
                sha[:8],
                intent.target_ref,
                intent.operation_key,
            )
            action_id = await self._journal_action(flow_run_id, cs.branch)
            await self._succeed(
                action_id,
                intent.id,
                sha,
                self._meta({"sha": sha, "reconciled": True, "adopted": True}, expected_head),
                adopted=True,
            )
            return WriteResult(WriteOutcome.COMMITTED, sha)

        if verdict is ProbeVerdict.UNKNOWN:
            action_id = await self._journal_action(flow_run_id, cs.branch)
            await self._complete(
                action_id, "unknown_outcome", self._meta({"matches": hits}, expected_head)
            )
            async with self._session_factory() as session:
                await complete_intent(
                    session, intent.id, "unknown", remote_result={"matches": list(hits)}
                )
                await session.commit()
            return WriteResult(WriteOutcome.UNKNOWN, None)

        if verdict is ProbeVerdict.DUPLICATED:
            # The ref moved away from this intent (someone else's push or a
            # later repair cycle owns the head) — drift, never force.
            async with self._session_factory() as session:
                await complete_intent(
                    session, intent.id, "duplicated", remote_result={"head": branch_head}
                )
                await session.commit()
            raise BranchDriftError(
                intent.target_ref,
                intent.expected_head or intent.expected_parent_oid or "",
                branch_head or "",
            )

        # REDISPATCH: nothing landed and the head still equals the intent's
        # expected parent — the same key goes out again, bounded.
        if expected_head is not None and intent.expected_parent_oid != expected_head:
            raise BranchDriftError(intent.target_ref, expected_head, branch_head or "")
        if int(intent.attempt_count) >= int(intent.max_attempts):
            logger.warning(
                "Intent %s exhausted %d attempts unresolved — unknown, no re-dispatch",
                intent.id[:8],
                intent.attempt_count,
            )
            action_id = await self._journal_action(flow_run_id, cs.branch)
            await self._complete(
                action_id,
                "unknown_outcome",
                self._meta({"attempts": int(intent.attempt_count)}, expected_head),
            )
            async with self._session_factory() as session:
                await complete_intent(session, intent.id, "unknown")
                await session.commit()
            return WriteResult(WriteOutcome.UNKNOWN, None)

        commit_message = message_with_marker(cs.commit_message, intent.operation_key)
        async with self._session_factory() as session:
            action_id = await self._journal_action_in_session(session, flow_run_id, cs.branch)
            await mark_dispatched(session, intent.id)
            await session.commit()
        commit_intent = _CommitIntent(
            branch=cs.branch,
            commit_message=commit_message,
            operation_key=intent.operation_key,
            expected_parent=intent.expected_parent_oid,
            expected_head=expected_head,
        )
        logger.warning(
            "No effect found for intent %s (marker %s) and head intact — "
            "re-dispatching with the SAME key (attempt %d)",
            intent.id[:8],
            intent.operation_key,
            int(intent.attempt_count),
        )
        return await self._dispatch(
            flow_run_id, action_id, intent.id, commit_intent, cs, commit_message
        )

    async def _open_intent(
        self,
        flow_run_id: str,
        provider: str,
        repo: str,
        branch: str,
        scope: str,
    ) -> PublicationIntent | None:
        """The previous attempt's open intent for this exact identity."""
        async with self._session_factory() as session:
            return await find_open_intent(
                session,
                run_id=flow_run_id,
                provider=provider,
                repo=repo,
                target_ref=branch,
                operation="commit",
                idempotency_scope=scope,
            )

    async def _journal_action(self, flow_run_id: str, branch: str) -> int:
        """Journal a fresh ``requested`` action row in its own transaction."""
        async with self._session_factory() as session:
            action_id = await self._journal_action_in_session(session, flow_run_id, branch)
            await session.commit()
            return action_id

    @staticmethod
    async def _journal_action_in_session(session: Any, flow_run_id: str, branch: str) -> int:
        controller = Controller(session)
        return await controller.record_action(flow_run_id, "commit", correlation_id=branch)

    async def _defer_probe(self, intent: PublicationIntent) -> None:
        """Push the intent's next probe into the future (transport failure)."""
        async with self._session_factory() as session:
            row = await session.get(PublicationIntent, intent.id)
            if row is not None:
                row.next_probe_at = datetime.now(timezone.utc) + timedelta(
                    seconds=_PROBE_RETRY_SECONDS
                )
                await session.commit()

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

    async def _resolve_unknown(
        self, action_id: int, intent_id: str, intent: _CommitIntent
    ) -> WriteResult:
        """Reconcile a lost create-commit response by marker + parent.

        A commit proves this attempt landed only when its message contains
        this intent's exact ``(forge-op:...)`` marker AND its parent is the
        branch head captured at intent time — the human message alone
        repeats across repair cycles. Exactly one such commit resolves as
        ``adopted`` (the response was lost, the probe proves the effect);
        zero or several stay unknown — the run must block, not retry
        (ADR-0005), and the intent row records ``unknown``.
        """
        marker = op_marker(intent.operation_key)
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
            await self._succeed(
                action_id,
                intent_id,
                sha,
                self._meta({"sha": sha, "reconciled": True}, intent.expected_head),
                adopted=True,
            )
            return WriteResult(WriteOutcome.COMMITTED, sha)

        remote_result: dict[str, Any] | None = None
        if intent.expected_head is not None:
            remote_result = {"expected_head": intent.expected_head}
        await self._complete(action_id, "unknown_outcome", remote_result)
        async with self._session_factory() as session:
            await complete_intent(session, intent_id, "unknown", remote_result=remote_result)
            await session.commit()
        return WriteResult(WriteOutcome.UNKNOWN, None)

    @staticmethod
    def _meta(result: dict[str, Any], expected_head: str | None) -> dict[str, Any]:
        """Outcome metadata; carries the pinned head when one was enforced."""
        if expected_head is not None:
            result["expected_head"] = expected_head
        return result

    async def _succeed(
        self,
        action_id: int,
        intent_id: str,
        sha: str,
        meta: dict[str, Any],
        *,
        adopted: bool,
    ) -> None:
        """Journal the success on BOTH rows in one transaction.

        The intent records ``committed`` when this dispatch's own response
        arrived and ``adopted`` when a probe found a previous attempt's
        effect — the run advances identically on either (ADR-0005/R11
        invariant 4), the distinction stays auditable.
        """
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.complete_action(action_id, "succeeded", meta)  # type: ignore[arg-type]
            await complete_intent(
                session,
                intent_id,
                "adopted" if adopted else "committed",
                provider_object_id=sha,
                remote_result=meta,
            )
            await session.commit()

    async def _fail(
        self,
        action_id: int,
        intent_id: str,
        exc: GitLabAPIError,
        expected_head: str | None,
    ) -> None:
        """A deterministic provider rejection: failed on both rows."""
        meta = self._meta({"error": str(exc)}, expected_head)
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.complete_action(action_id, "failed", meta)  # type: ignore[arg-type]
            await complete_intent(session, intent_id, "failed", remote_result=meta)
            await session.commit()

    async def _complete(self, action_id: int, status: str, remote_result: dict | None = None):
        async with self._session_factory() as session:
            controller = Controller(session)
            await controller.complete_action(action_id, status, remote_result)  # type: ignore[arg-type]
            await session.commit()
