"""Trusted publisher (ADR-0016 §2, ADR-0026): THE write boundary for candidates.

Every candidate — builtin ChangeSets and harness artifacts alike, on every
provider — crosses this boundary before anything reaches a commit API. The
application-service entry :func:`publish_validated_candidate` owns the
sequence (validate → fence → native adapter); the pure policy half is
:func:`validate_candidate_bundle`, shared by every backend:

1. checks the candidate's diff base against the run's frozen attempt base
   (cycle 1 → approved source base; repair → last verified candidate);
2. checks the publication grant (:func:`publication_grant_valid` — run not
   cancelled AND, when the executing :class:`~forge.durable.claims.ExecutionClaim`
   pinned one, the run's cancellation generation unchanged), the RunSpec
   digest (the spec frozen at plan acceptance is the one being executed) and
   a caller-supplied Stage-B-style fence callable;
3. materializes the bundle against AUTHORITATIVE full base contents
   (no truncation; strict hunk application with no fuzz) — the R08/R09
   ``base_blob_digest``/``intended_digest`` verification runs here;
4. validates the resulting ChangeSet against the write policy
   (:func:`validate_changeset` — denied paths, lockfiles, size caps,
   existence rules) and the RunSpec's frozen ``allowed_paths`` scope
   (v0.7 monorepo path scoping: any change outside the globs is rejected);
5. hands a :class:`ValidatedCandidate` — the capability token only the
   boundary can issue — to the native adapter for the journaled,
   reconcilable write (GitLab: :class:`ChangesetWriter` with
   ``start_ref = expected_head = attempt base``).

The grant is re-checked at the RESERVATION POINT — after the long
base-content reads, immediately before the adapter (R10): a cancel during
the reads forbids a NEW publication reservation. A cancel that lands during
the already-started remote commit is NOT rolled back (best-effort), but its
completion records superseded evidence on the run and
``PublishResult.superseded`` is set instead of letting the caller walk a
cancelled run toward ``ready_for_human``.

The publisher never executes candidate content and never trusts the
harness's claims; a rejected candidate is reported, not repaired. The
negative conformance suite (``tests/test_publication_boundary.py``) is the
enforcement: every publish path must refuse a policy-violating candidate
with zero commit-API calls.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable import FlowRun, FlowStatus, RunSpec, factory_branch, short_run_id
from forge.durable.claims import ExecutionClaim, current_claim
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

#: Fetches the AUTHORITATIVE full content of *paths* at *base_ref* (the
#: provider-side half of materialization). Missing paths are absent from the
#: result; a base file over the materialization cap raises
#: :class:`CandidateError` (``file_too_large``).
BaseContentFetcher = Callable[[str, list[str]], Awaitable[dict[str, str]]]


class PolicyViolation(Exception):
    """The publication boundary rejected a candidate on write policy.

    ``violations`` are the human-readable strings from
    :func:`validate_changeset` — reported to the run's evidence, never
    repaired and never narrowed to "just the first one".
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


@dataclass(frozen=True)
class ValidatedCandidate:
    """A candidate that crossed the publication boundary (ADR-0026).

    Constructed ONLY by :func:`validate_candidate_bundle` after strict
    materialization + policy validation passed; provider transports accept
    nothing else. Carrying this type IS the capability to publish — there
    is no unvalidated route into a commit API.
    """

    bundle: CandidateBundle
    #: The completed manifest (full contents) the digests were verified on.
    manifest: tuple[ChangeManifestEntry, ...]
    #: The native-adapter payload mapped 1:1 from the manifest.
    changeset: ChangeSet
    #: The frozen scope the paths were validated against (empty = unscoped).
    allowed_paths: tuple[str, ...]


def manifest_to_changeset(
    entries: Iterable[ChangeManifestEntry],
    *,
    branch: str,
    commit_message: str,
    attempt_base_oid: str = "",
) -> ChangeSet:
    """Map a completed manifest onto the write-path :class:`ChangeSet`."""
    return ChangeSet(
        branch=branch,
        commit_message=commit_message,
        changes=[
            Change(
                path=entry.path,
                operation=_OPERATION_MAP[entry.operation],
                content=entry.new_content,
            )
            for entry in entries
        ],
        attempt_base_oid=attempt_base_oid or None,
    )


def validate_candidate_bundle(
    bundle: CandidateBundle,
    *,
    base_contents: dict[str, str],
    branch: str,
    commit_message: str,
    allowed_paths: Sequence[str] = (),
) -> ValidatedCandidate:
    """The pure publication-boundary check, shared by EVERY backend (ADR-0026).

    1. strict materialization of *bundle* against *base_contents* (the
       authoritative full texts at the attempt base): ``base_blob_digest``
       verified BEFORE anything is applied, ``intended_digest`` AFTER — the
       R08/R09 guarantees run on every path, builtin included;
    2. :func:`validate_changeset` — denied paths/prefixes/lockfiles, change
       count and size caps, existence rules, and the *allowed_paths* globs.

    Raises :class:`CandidateError` (materialization/digest failures) or
    :class:`PolicyViolation` (write-policy violations). Returns the
    :class:`ValidatedCandidate` wrapper — the only thing a transport may
    write.
    """
    if bundle.is_empty:
        raise CandidateError("empty_candidate", "candidate contains no changes")
    entries = tuple(bundle.materialize(base_contents))
    changeset = manifest_to_changeset(
        entries,
        branch=branch,
        commit_message=commit_message,
        attempt_base_oid=bundle.attempt_base_oid,
    )
    violations = validate_changeset(changeset, base_contents, allowed_paths=list(allowed_paths))
    if violations:
        raise PolicyViolation(violations)
    return ValidatedCandidate(
        bundle=bundle,
        manifest=entries,
        changeset=changeset,
        allowed_paths=tuple(allowed_paths),
    )


@dataclass(frozen=True)
class PublishResult:
    """The publisher's verdict. ``ok=False`` carries a machine-readable
    ``reason`` for the run's blocking evidence; ``unknown_outcome=True``
    means the commit MAY exist — the caller must fail, never retry.
    ``superseded=True`` (with ``ok=True``) means the commit DID land but the
    publication grant was revoked meanwhile — the run's completion records
    superseded evidence and must never walk toward ``ready_for_human``
    (R10)."""

    ok: bool
    reason: str = ""
    commit_sha: str | None = None
    unknown_outcome: bool = False
    superseded: bool = False


def publication_grant_valid(run: FlowRun, generation: int | None) -> bool:
    """Whether *run* still grants a publication to the *generation* holder.

    The one grant predicate, consulted at the entry check and again at the
    reservation point (R10):

    - a run with ``cancel_requested`` set or already ``cancelled`` has NO
      grant (F13/ADR-0018 §4);
    - a *generation* that no longer equals the run's
      ``cancellation_generation`` has NO grant — the claim was minted before
      a cancel bumped the run (:meth:`forge.durable.controller.Controller.request_cancel`),
      so queue ownership no longer implies effect ownership;
    - ``generation=None`` (no claim binding — legacy/transport callers) only
      applies the flag-level checks above.
    """
    if bool(run.cancel_requested) or run.status == FlowStatus.CANCELLED.value:
        return False
    if generation is not None and int(run.cancellation_generation) != generation:
        return False
    return True


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
                select(RunSpec).where(RunSpec.run_id == run.id).order_by(RunSpec.id.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return True
    return str(row.digest) == run.spec_digest


async def spec_allowed_paths(session: AsyncSession, run: FlowRun) -> list[str]:
    """The ``allowed_paths`` globs frozen in the run's RunSpec document.

    Monorepo path scoping (v0.7, complex-projects.md §1): empty/missing —
    the run is unscoped and every path is in scope.
    """
    row = (
        (
            await session.execute(
                select(RunSpec).where(RunSpec.run_id == run.id).order_by(RunSpec.id.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return []
    raw = (row.document or {}).get("allowed_paths")
    if not isinstance(raw, list):
        return []
    return [str(glob) for glob in raw if str(glob).strip()]


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


async def _reload_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> FlowRun | None:
    """The run row as it is NOW, in a fresh session (R10 reservation checks)."""
    async with session_factory() as session:
        return await session.get(FlowRun, run_id)


async def _record_superseded_publication(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    commit_sha: str | None,
    attempt_base: str,
) -> None:
    """Record on the run that a commit landed AFTER its grant was revoked.

    R10 best-effort completion: an already-started remote commit is not
    rolled back, but the run's evidence names it as superseded so no reader
    mistakes it for the published candidate, and the run is never walked
    toward ``ready_for_human`` on its back.
    """
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            return
        evidence = dict(run.evidence or {})
        evidence["superseded"] = {
            "reason": "cancelled_during_publication",
            "commit_sha": commit_sha,
            "attempt_base": attempt_base,
        }
        run.evidence = evidence
        await session.commit()
    logger.warning(
        "Run %s publication grant revoked mid-commit — commit %s recorded as superseded",
        run_id[:8],
        (commit_sha or "?")[:8],
    )


async def publish_validated_candidate(
    run: FlowRun,
    candidate: CandidateBundle,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    fetch_base_contents: BaseContentFetcher,
    native_publish: Callable[[ValidatedCandidate], Awaitable["PublishResult"]],
    branch: str | None = None,
    commit_message: str | None = None,
    allowed_paths: Sequence[str] | None = None,
    fence_check: FenceCheck | None = None,
) -> PublishResult:
    """Validate and publish *candidate* for *run* — THE boundary (ADR-0026).

    The single application-service entry every provider×backend publishes
    through: run-state checks → strict materialization + write policy → the
    injected *native_publish* adapter (the journaled, reconcilable write).
    The adapter receives a :class:`ValidatedCandidate` or nothing at all —
    a rejected candidate never reaches it, so a rejection leaves the remote
    untouched by construction.

    *session_factory* enables the fresh-run leg (run existence, RunSpec
    digest, frozen ``allowed_paths`` from the spec when *allowed_paths* is
    None) and the R10 reservation guard: the publication grant is re-read
    AFTER the base-content reads and immediately before the adapter, so a
    cancel during the long reads forbids a NEW reservation. Transport
    callers without a session run the checks they own service-side. Never
    raises for candidate content — rejections are returned as a failed
    :class:`PublishResult` with a machine-readable reason; the caller
    decides what that means for the run.

    The executing step's :class:`~forge.durable.claims.ExecutionClaim` (if
    bound) pins the publication-grant generation it was minted under: a
    cancel that bumped the run's generation after the claim fences the
    reservation out even before the flag flips (R10).
    """
    claim: ExecutionClaim | None = current_claim()
    claim_generation = claim.cancellation_generation if claim is not None else None

    if session_factory is not None:
        # Fresh trusted state: the run row as it is NOW, not as the caller
        # saw it.
        async with session_factory() as session:
            fresh = await session.get(FlowRun, run.id)
            if fresh is None:
                return PublishResult(False, "run_not_found")
            spec_ok = await _spec_digest_matches(session, fresh)
            spec_paths = await spec_allowed_paths(session, fresh)
    else:
        fresh = run
        spec_ok = True
        spec_paths = []
    scope = list(allowed_paths) if allowed_paths is not None else spec_paths

    attempt_base = attempt_base_for(fresh)
    if candidate.attempt_base_oid != attempt_base:
        return PublishResult(
            False,
            "candidate_base_mismatch: candidate diff base "
            f"{candidate.attempt_base_oid[:8] or '<none>'}, expected {attempt_base[:8] or '<none>'}",
        )
    if candidate.is_empty:
        return PublishResult(False, "empty_candidate")

    # Publication grant (F13/ADR-0018 §4, generation-fenced per R10).
    if not publication_grant_valid(fresh, claim_generation):
        return PublishResult(False, "publication_revoked")
    # F14: the spec frozen at plan acceptance is the one being executed.
    if not spec_ok:
        return PublishResult(False, "spec_digest_mismatch")
    # Stage-B fence (ADR-0017), re-checked at the boundary via the callable.
    if fence_check is not None and not await fence_check():
        return PublishResult(False, "fence_invalid")

    # Materialize against authoritative base contents (no truncation) and
    # run the shared policy — the one validation, every backend.
    try:
        base_contents = await fetch_base_contents(attempt_base, candidate.paths)
        validated = validate_candidate_bundle(
            candidate,
            base_contents=base_contents,
            branch=branch or factory_branch(fresh.issue_iid, fresh.id),
            commit_message=commit_message
            or f"forge: implement {fresh.issue_iid or 0} (run {short_run_id(fresh.id)})",
            allowed_paths=scope,
        )
    except CandidateError as exc:
        return PublishResult(False, f"{exc.reason}: {exc}")
    except PolicyViolation as exc:
        return PublishResult(False, "changeset_invalid: " + "; ".join(exc.violations))

    # R10 RESERVATION POINT: the reads above can take arbitrarily long —
    # re-read the grant immediately before handing the candidate to the
    # adapter. A cancel during the reads forbids this NEW reservation.
    if session_factory is not None:
        recent = await _reload_run(session_factory, fresh.id)
        if recent is None or not publication_grant_valid(recent, claim_generation):
            return PublishResult(False, "publication_revoked: grant revoked during reservation")

    result = await native_publish(validated)
    if result.ok:
        logger.info(
            "Published candidate for run %s at %s (base %s, %d change(s))",
            fresh.id[:8],
            (result.commit_sha or "?")[:8],
            attempt_base[:8],
            len(validated.changeset.changes),
        )
        # R10 best-effort completion: the remote commit already started (or
        # landed) — never rolled back, but a grant revoked DURING the commit
        # turns the completion into superseded evidence.
        if session_factory is not None:
            recent = await _reload_run(session_factory, fresh.id)
            if recent is None or not publication_grant_valid(recent, claim_generation):
                await _record_superseded_publication(
                    session_factory,
                    fresh.id,
                    commit_sha=result.commit_sha,
                    attempt_base=attempt_base,
                )
                return PublishResult(
                    True,
                    commit_sha=result.commit_sha,
                    superseded=True,
                )
    return result


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
    """Validate and publish *bundle* for *run* via the GitLab transport.

    The GitLab-native binding of :func:`publish_validated_candidate`
    (ADR-0026): the run-state, materialization and policy legs are shared;
    only the base-content reads and the journaled
    :class:`ChangesetWriter` apply are GitLab's. The writer is pinned to
    the frozen attempt base (``start_ref = expected_head``) — in the
    proposal-only model nothing else ever pushed, so the guarded apply
    proves the branch did not move.
    """

    async def _fetch(base_ref: str, paths: list[str]) -> dict[str, str]:
        return await _fetch_base_contents(gitlab, run.project_id, base_ref, paths)

    async def _write(validated: ValidatedCandidate) -> PublishResult:
        attempt_base = validated.bundle.attempt_base_oid
        try:
            result = await writer.apply(
                run.id,
                validated.changeset,
                start_ref=attempt_base,
                expected_head=attempt_base,
            )
        except BranchDriftError as exc:
            return PublishResult(False, f"branch_drift: {exc}")
        except GitLabAPIError as exc:
            return PublishResult(False, f"commit_failed: {exc}")
        if result.outcome is WriteOutcome.UNKNOWN or not result.commit_sha:
            return PublishResult(False, "commit_unknown_outcome", unknown_outcome=True)
        return PublishResult(True, commit_sha=result.commit_sha)

    return await publish_validated_candidate(
        run,
        bundle,
        session_factory=session_factory,
        fetch_base_contents=_fetch,
        native_publish=_write,
        commit_message=commit_message,
        fence_check=fence_check,
    )
