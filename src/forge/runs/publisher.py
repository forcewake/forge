"""Trusted publisher (ADR-0016 §2): the single write boundary for candidates.

Every backend candidate — builtin ChangeSets and harness artifacts alike —
crosses this boundary before anything reaches GitLab. The publisher:

1. checks the candidate's diff base against the run's frozen attempt base
   (cycle 1 → approved source base; repair → last verified candidate);
2. checks the publication grant (run not cancelled), the RunSpec digest
   (the spec frozen at plan acceptance is the one being executed) and a
   caller-supplied Stage-B-style fence callable;
3. materializes the bundle against AUTHORITATIVE full base contents
   (no truncation; strict hunk application with no fuzz);
4. validates the resulting ChangeSet against the write policy
   (:func:`validate_changeset` — denied paths, lockfiles, size caps,
   existence rules);
5. writes through the journaled, reconcilable :class:`ChangesetWriter`
   with ``start_ref = expected_head = attempt base`` — the factory branch
   already sits at the attempt base in the proposal-only model, so the
   guarded apply proves the branch did not move.

The publisher never executes candidate content and never trusts the
harness's claims; a rejected candidate is reported, not repaired.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable import FlowRun, FlowStatus, RunSpec, factory_branch, short_run_id
from forge.factory.implementer import FORGE_MATERIALIZE_MAX_FILE_CHARS
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.repository import (
    Change,
    ChangeSet,
    Operation,
    WriteOutcome,
    validate_changeset,
)
from forge.repository.writer import BranchDriftError, ChangesetWriter
from forge.runs.candidate import (
    CandidateBundle,
    CandidateError,
    ChangeManifestEntry,
    attempt_base_for,
)

logger = logging.getLogger(__name__)

#: Stage-B-style fence: an async callable re-checked right before the write;
#: ``False`` rejects the publication (ADR-0017 fenced-CAS semantics, reused
#: here via a callable so the publisher stays independent of the step run).
FenceCheck = Callable[[], Awaitable[bool]]

_OPERATION_MAP: dict[str, Operation] = {
    "create": Operation.CREATE,
    "modify": Operation.UPDATE,
    "delete": Operation.DELETE,
}


@dataclass(frozen=True)
class PublishResult:
    """The publisher's verdict. ``ok=False`` carries a machine-readable
    ``reason`` for the run's blocking evidence; ``unknown_outcome=True``
    means the commit MAY exist — the caller must fail, never retry."""

    ok: bool
    reason: str = ""
    commit_sha: str | None = None
    unknown_outcome: bool = False


async def _spec_digest_matches(session: AsyncSession, run: FlowRun) -> bool:
    """Whether the frozen RunSpec's digest still equals the run's spec digest.

    Legacy runs without a spec row/digest pass (nothing to compare); a real
    mismatch fails closed — the plan the approver saw is not the plan bound
    to this run.
    """
    if not run.spec_digest:
        return True
    row = (
        (
            await session.execute(
                select(RunSpec)
                .where(RunSpec.run_id == run.id)
                .order_by(RunSpec.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return True
    return str(row.digest) == run.spec_digest


async def _fetch_base_contents(
    gitlab: GitLabClient,
    project_id: int,
    base_ref: str,
    paths: list[str],
) -> dict[str, str]:
    """Fetch COMPLETE content of *paths* at the attempt base snapshot.

    Authoritative full-content reads (no truncation, ever); a file over the
    :data:`FORGE_MATERIALIZE_MAX_FILE_CHARS` cap raises
    :class:`CandidateError` instead. Paths missing from the snapshot are
    absent from the result — validation reports create/update/delete
    existence against it.
    """
    contents: dict[str, str] = {}
    for path in dict.fromkeys(paths):
        try:
            repo_file = await gitlab.get_file(project_id, path, ref=base_ref)
        except GitLabAPIError:
            continue  # not in the snapshot — validate_changeset reports it
        raw = repo_file.content
        if (repo_file.encoding or "") == "base64":
            text = base64.b64decode(raw).decode("utf-8", errors="replace")
        else:
            try:
                text = base64.b64decode(raw, validate=True).decode("utf-8", errors="replace")
            except Exception:
                text = raw
        if len(text) > FORGE_MATERIALIZE_MAX_FILE_CHARS:
            raise CandidateError(
                "file_too_large",
                f"{path}: base content is {len(text)} chars at {base_ref[:8]}, over the "
                f"materialization cap of {FORGE_MATERIALIZE_MAX_FILE_CHARS}",
            )
        contents[path] = text
    return contents


async def publish_candidate(
    *,
    gitlab: GitLabClient,
    session_factory: async_sessionmaker[AsyncSession],
    writer: ChangesetWriter,
    run: FlowRun,
    bundle: CandidateBundle,
    commit_message: str | None = None,
    fence_check: FenceCheck | None = None,
) -> PublishResult:
    """Validate and publish *bundle* for *run* (ADR-0016 §2).

    Returns a :class:`PublishResult`; the caller decides what a rejection
    means for the run (block / fail / supersede). Never raises for candidate
    content — rejections are reported.
    """
    # Fresh trusted state: the run row as it is NOW, not as the caller saw it.
    async with session_factory() as session:
        fresh = await session.get(FlowRun, run.id)
        if fresh is None:
            return PublishResult(False, "run_not_found")
        spec_ok = await _spec_digest_matches(session, fresh)

    attempt_base = attempt_base_for(fresh)
    if bundle.attempt_base_oid != attempt_base:
        return PublishResult(
            False,
            "candidate_base_mismatch: candidate diff base "
            f"{bundle.attempt_base_oid[:8] or '<none>'}, expected {attempt_base[:8] or '<none>'}",
        )
    if bundle.is_empty:
        return PublishResult(False, "empty_candidate")

    # Publication grant (F13/ADR-0018 §4): a cancelled run has none.
    if bool(fresh.cancel_requested) or fresh.status == FlowStatus.CANCELLED.value:
        return PublishResult(False, "publication_revoked")
    # F14: the spec frozen at plan acceptance is the one being executed.
    if not spec_ok:
        return PublishResult(False, "spec_digest_mismatch")
    # Stage-B fence (ADR-0017), re-checked at the boundary via the callable.
    if fence_check is not None and not await fence_check():
        return PublishResult(False, "fence_invalid")

    # Materialize against authoritative base contents (no truncation).
    try:
        base_contents = await _fetch_base_contents(
            gitlab, fresh.project_id, attempt_base, bundle.paths
        )
        entries: list[ChangeManifestEntry] = bundle.materialize(base_contents)
    except CandidateError as exc:
        return PublishResult(False, f"{exc.reason}: {exc}")

    changeset = ChangeSet(
        branch=factory_branch(fresh.issue_iid, fresh.id),
        commit_message=commit_message
        or f"forge: implement {fresh.issue_iid or 0} (run {short_run_id(fresh.id)})",
        changes=[
            Change(
                path=entry.path,
                operation=_OPERATION_MAP[entry.operation],
                content=entry.new_content,
            )
            for entry in entries
        ],
        attempt_base_oid=attempt_base,
    )
    violations = validate_changeset(changeset, base_contents)
    if violations:
        return PublishResult(False, "changeset_invalid: " + "; ".join(violations))

    # Journaled, reconcilable write pinned to the frozen attempt base: in the
    # proposal-only model nothing else ever pushed, so the live branch head
    # IS the attempt base — the guarded apply proves it stayed that way.
    try:
        result = await writer.apply(
            fresh.id,
            changeset,
            start_ref=attempt_base,
            expected_head=attempt_base,
        )
    except BranchDriftError as exc:
        return PublishResult(False, f"branch_drift: {exc}")
    except GitLabAPIError as exc:
        return PublishResult(False, f"commit_failed: {exc}")

    if result.outcome is WriteOutcome.UNKNOWN or not result.commit_sha:
        return PublishResult(False, "commit_unknown_outcome", unknown_outcome=True)
    logger.info(
        "Published candidate for run %s at %s (base %s, %d change(s))",
        fresh.id[:8],
        result.commit_sha[:8],
        attempt_base[:8],
        len(changeset.changes),
    )
    return PublishResult(True, commit_sha=result.commit_sha)
