"""Authorize and validate the exact candidate immediately before repository effects."""

from __future__ import annotations

import logging

from forge.domain.candidate import (
    PolicyViolation as PolicyViolation,
    ValidatedCandidate as ValidatedCandidate,
    manifest_to_changeset as manifest_to_changeset,
    validate_candidate_bundle as validate_candidate_bundle,
)
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable import FlowRun, FlowStatus, RunSpec, StepRun, factory_branch, short_run_id
from forge.durable.claims import ExecutionClaim, current_claim
from forge.durable.controller import as_aware_utc
from forge.repository.changeset import FORGE_MATERIALIZE_MAX_FILE_CHARS
from forge.gitlab.blob_reads import AUTHORITATIVE_READ_FAILED, BlobReadResult
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.repository import (
    WriteOutcome,
    resolve_write_policy,
)
from forge.repository.writer import BranchDriftError, ChangesetWriter
from forge.runs.candidate import (
    CandidateBundle,
    CandidateError,
    attempt_base_for,
)

logger = logging.getLogger(__name__)

#: Stage-B-style fence: an async callable re-checked right before the write; ``False``
#: rejects the publication (ADR-0017 fenced-CAS semantics, reused here via a callable so the
#: publisher stays independent of the step run).
FenceCheck = Callable[[], Awaitable[bool]]


#: Fetches the AUTHORITATIVE full content of *paths* at *base_ref* (the provider-side half
#: of materialization).
BaseContentFetcher = Callable[[str, list[str]], Awaitable[dict[str, str]]]


@dataclass(frozen=True)
class PublishResult:
    """The publisher's verdict. ``ok=False`` carries a machine-readable ``reason`` for the run's blocking evidence; ``unknown_outcome=True`` means the commit MAY exist — the caller must fail, never retry. ``settling=True`` (with ``ok=False``,) means the recovery probe was negative and the intent parked in the effect-certainty window — NOT final: the caller must NOT park the run terminal, the window-end re-probe (the lane's recovery scanner) adopts a late-landing commit or resolves the honest unknown. ``superseded=True`` (with ``ok=True``) means the commit DID land but the publication grant was revoked meanwhile — the run's completion records superseded evidence and must never walk toward ``ready_for_human``."""

    ok: bool
    reason: str = ""
    commit_sha: str | None = None
    unknown_outcome: bool = False
    superseded: bool = False
    settling: bool = False


def publication_grant_valid(run: FlowRun, generation: int | None) -> bool:
    """Whether *run* still grants a publication to the *generation* holder."""
    if bool(run.cancel_requested) or run.status == FlowStatus.CANCELLED.value:
        return False
    if generation is not None and int(run.cancellation_generation) != generation:
        return False
    return True


async def _latest_spec_row(session: AsyncSession, run: FlowRun) -> RunSpec | None:
    """The run's freshest RunSpec row, or ``None`` when it has none."""
    return (
        (
            await session.execute(
                select(RunSpec).where(RunSpec.run_id == run.id).order_by(RunSpec.id.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _spec_digest_matches(session: AsyncSession, run: FlowRun) -> bool:
    """Whether the frozen RunSpec's digest still equals the run's spec digest."""
    if not run.spec_digest:
        return True
    row = await _latest_spec_row(session, run)
    if row is None:
        return True
    return str(row.digest) == run.spec_digest


async def spec_allowed_paths(session: AsyncSession, run: FlowRun) -> list[str]:
    """The ``allowed_paths`` globs frozen in the run's RunSpec document."""
    row = await _latest_spec_row(session, run)
    if row is None:
        return []
    raw = (row.document or {}).get("allowed_paths")
    if not isinstance(raw, list):
        return []
    return [str(glob) for glob in raw if str(glob).strip()]


def run_project_keys(run: FlowRun) -> tuple[str, ...]:
    """The config keys identifying *run*'s project ( pipeline hooks)."""
    provider = str(getattr(run, "provider", "") or "gitlab")
    keys = [f"{provider}:{run.project_id}"]
    repo = str(getattr(run, "github_repo_full_name", "") or "")
    if repo:
        keys.append(f"github:{repo}")
    return tuple(keys)


def sensitive_paths_for_run(
    run: FlowRun,
    pipeline_entrypoints: Mapping[str, Sequence[str]] | None,
) -> list[str]:
    """The configured sensitive pipeline paths for *run*'s project."""
    if not pipeline_entrypoints:
        return []
    paths: list[str] = []
    for key in run_project_keys(run):
        for path in pipeline_entrypoints.get(key, ()):
            text = str(path).strip()
            if text:
                paths.append(text)
    return list(dict.fromkeys(paths))


async def _fetch_base_contents(
    gitlab: GitLabClient,
    project_id: int,
    base_ref: str,
    paths: list[str],
) -> dict[str, str]:
    """Fetch COMPLETE content of *paths* at the attempt base snapshot."""
    contents: dict[str, str] = {}
    for path in dict.fromkeys(paths):
        result: BlobReadResult = await gitlab.read_blob(project_id, path, ref=base_ref)
        if result.confirmed_absent:
            continue  # provider-confirmed absence — the only honest "missing"
        if not result.usable:
            raise CandidateError(
                AUTHORITATIVE_READ_FAILED,
                f"{path}: base read at {base_ref[:8]} returned {result.status}: "
                f"{result.detail or 'no detail'}",
            )
        text = result.text()
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
    """The run row as it is NOW, in a fresh session ( reservation checks)."""
    async with session_factory() as session:
        return await session.get(FlowRun, run_id)


#: The step status a live owner's row must still show. Mirrors
#: ``forge.worker.steps.STEP_RUNNING`` (not imported: the worker package transitively
#: imports this module).
_STEP_RUNNING = "running"


async def _claim_staleness(
    session: AsyncSession,
    claim: ExecutionClaim,
    run_id: str,
) -> str | None:
    """Why the ambient claim no longer owns its step, or ``None`` while it does."""
    step = await session.get(StepRun, claim.step_id)
    if step is None:
        return "step row vanished"
    if step.status != _STEP_RUNNING:
        return f"step is {step.status!r}, not running"
    if step.lease_owner != claim.owner:
        return f"lease owner is {step.lease_owner!r}, not {claim.owner!r}"
    if int(step.fence_token) != claim.fence_token:
        return f"fence moved to {step.fence_token}"
    if step.lease_expires_at is None or as_aware_utc(step.lease_expires_at) <= datetime.now(
        timezone.utc
    ):
        return "lease expired"
    if step.flow_run_id is not None and step.flow_run_id != run_id:
        return f"step is bound to run {step.flow_run_id[:8]}"
    return None


async def _record_superseded_publication(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    commit_sha: str | None,
    attempt_base: str,
    reason: str,
    detail: str | None = None,
) -> None:
    """Record on the run that a publication leg must not be trusted."""
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            return
        evidence = dict(run.evidence or {})
        record: dict[str, Any] = {
            "reason": reason,
            "commit_sha": commit_sha,
            "attempt_base": attempt_base,
        }
        if detail:
            record["detail"] = detail
        evidence["superseded"] = record
        run.evidence = evidence
        await session.commit()
    logger.warning(
        "Run %s publication superseded (%s) — commit %s recorded as superseded evidence",
        run_id[:8],
        reason,
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
    write_profile: str | None = None,
    custom_write_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    sensitive_paths: Sequence[str] | None = None,
    operator_approved: bool | None = None,
) -> PublishResult:
    """Validate and publish *candidate* for *run* — THE boundary (ADR-0026)."""
    claim: ExecutionClaim | None = current_claim()
    claim_generation = claim.cancellation_generation if claim is not None else None

    spec_present = False
    if session_factory is not None:
        # Fresh trusted state: the run row as it is NOW, not as the caller saw it.
        async with session_factory() as session:
            fresh = await session.get(FlowRun, run.id)
            if fresh is None:
                return PublishResult(False, "run_not_found")
            spec_ok = await _spec_digest_matches(session, fresh)
            spec_paths = await spec_allowed_paths(session, fresh)
            spec_present = await _latest_spec_row(session, fresh) is not None
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

    # Publication grant (F13/ADR-0018 §4, generation-fenced per).
    if not publication_grant_valid(fresh, claim_generation):
        return PublishResult(False, "publication_revoked")
    # F14: the spec frozen at plan acceptance is the one being executed.
    if not spec_ok:
        return PublishResult(False, "spec_digest_mismatch")
    # Stage-B fence (ADR-0017), re-checked at the boundary via the callable.
    if fence_check is not None and not await fence_check():
        return PublishResult(False, "fence_invalid")

    # The effective write policy: profile + provider-sensitive paths. A broken configuration
    # fails the publication with a clear reason — it never degrades to a more permissive
    # policy.
    try:
        policy = resolve_write_policy(
            write_profile,
            custom_profiles=custom_write_profiles,
            extra_denied_paths=sensitive_paths or (),
        )
    except ValueError as exc:
        return PublishResult(False, f"write_policy_config_error: {exc}")
    if policy.require_special_approval and operator_approved is None:
        # No explicit assertion: the frozen RunSpec row IS the proof (an operator /go —
        # FORGE_APPROVERS-gated — froze the plan).
        operator_approved = session_factory is not None and spec_present
    if policy.require_special_approval and not operator_approved:
        return PublishResult(
            False,
            f"special_approval_required: write profile {policy.name!r} requires "
            f"approval from the operator approver set",
        )

    # Materialize against authoritative base contents (no truncation) and run the shared
    # policy — the one validation, every backend.
    try:
        base_contents = await fetch_base_contents(attempt_base, candidate.paths)
        validated = validate_candidate_bundle(
            candidate,
            base_contents=base_contents,
            branch=branch or factory_branch(fresh.issue_iid, fresh.id),
            commit_message=commit_message
            or f"forge: implement {fresh.issue_iid or 0} (run {short_run_id(fresh.id)})",
            allowed_paths=scope,
            policy=policy,
        )
    except CandidateError as exc:
        return PublishResult(False, f"{exc.reason}: {exc}")
    except PolicyViolation as exc:
        return PublishResult(False, "changeset_invalid: " + "; ".join(exc.violations))

    # RESERVATION POINT + PRE-DISPATCH ARBITRATION: the reads above can take arbitrarily
    # long — re-read the grant immediately before handing the candidate to the adapter.
    if session_factory is not None:
        async with session_factory() as session:
            recent = await session.get(FlowRun, fresh.id)
            if recent is None or not publication_grant_valid(recent, claim_generation):
                return PublishResult(False, "publication_revoked: grant revoked during reservation")
            staleness = (
                await _claim_staleness(session, claim, fresh.id) if claim is not None else None
            )
        if staleness is not None:
            await _record_superseded_publication(
                session_factory,
                fresh.id,
                commit_sha=None,
                attempt_base=attempt_base,
                reason="publication_claim_stale",
                detail=staleness,
            )
            return PublishResult(
                False,
                f"claim_superseded: execution claim no longer owns its step ({staleness}) — "
                "standing down before dispatch",
            )

    result = await native_publish(validated)
    if result.ok:
        logger.info(
            "Published candidate for run %s at %s (base %s, %d change(s), profile %s)",
            fresh.id[:8],
            (result.commit_sha or "?")[:8],
            attempt_base[:8],
            len(validated.changeset.changes),
            policy.name,
        )
        # best-effort completion: the remote commit already started (or landed) — never
        # rolled back, but a grant revoked DURING the commit turns the completion into
        # superseded evidence.
        if session_factory is not None:
            recent = await _reload_run(session_factory, fresh.id)
            if recent is None or not publication_grant_valid(recent, claim_generation):
                await _record_superseded_publication(
                    session_factory,
                    fresh.id,
                    commit_sha=result.commit_sha,
                    attempt_base=attempt_base,
                    reason="cancelled_during_publication",
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
    branch: str | None = None,
    commit_message: str | None = None,
    fence_check: FenceCheck | None = None,
    write_profile: str | None = None,
    custom_write_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    sensitive_paths: Sequence[str] | None = None,
    operator_approved: bool | None = None,
) -> PublishResult:
    """Validate and publish *bundle* for *run* via the GitLab transport."""

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
                # intent identity: the writer journals the durable publication intent
                # (stable operation key + expected parent) BEFORE the HTTP effect, and an
                # open intent from a crashed attempt is probed-and-adopted, never
                # duplicated.
                provider="gitlab",
                repo=str(run.project_id),
                commit_cycle=run.commit_cycle or 1,
            )
        except BranchDriftError as exc:
            return PublishResult(False, f"branch_drift: {exc}")
        except GitLabAPIError as exc:
            return PublishResult(False, f"commit_failed: {exc}")
        if result.outcome is WriteOutcome.SETTLING:
            # the recovery probe was negative — the intent parked in the effect-certainty
            # window.
            return PublishResult(False, "commit_settling_probe_pending", settling=True)
        if result.outcome is WriteOutcome.UNKNOWN or not result.commit_sha:
            return PublishResult(False, "commit_unknown_outcome", unknown_outcome=True)
        return PublishResult(True, commit_sha=result.commit_sha)

    return await publish_validated_candidate(
        run,
        bundle,
        session_factory=session_factory,
        fetch_base_contents=_fetch,
        native_publish=_write,
        branch=branch,
        commit_message=commit_message,
        fence_check=fence_check,
        write_profile=write_profile,
        custom_write_profiles=custom_write_profiles,
        sensitive_paths=sensitive_paths,
        operator_approved=operator_approved,
    )
