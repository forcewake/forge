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
   ``base_blob_digest``/``intended_digest`` verification runs here; the
   base reads are TYPED (R14): only a provider-confirmed ``not_found``
   may make a path absent, so a create is only publishable against
   confirmed absence, and a ``forbidden``/``unavailable``/``incomplete``
   read fails validation (``authoritative_read_failed``) BEFORE any
   remote effect;
4. validates the resulting ChangeSet against the write policy
   (:func:`validate_changeset` — denied paths, lockfiles, size caps,
   existence rules) and the RunSpec's frozen ``allowed_paths`` scope
   (v0.7 monorepo path scoping: any change outside the globs is rejected);
5. hands a :class:`ValidatedCandidate` — the capability token only the
   boundary can issue — to the native adapter for the journaled,
   reconcilable write (GitLab: :class:`ChangesetWriter` with
   ``start_ref = expected_head = attempt base``).

The grant is re-checked at the RESERVATION POINT — after the long
base-content reads, immediately before the adapter (R10) — together with the
A04 claim-ownership arbitration: a bound
:class:`~forge.durable.claims.ExecutionClaim` must still OWN its step row
(same owner, same fence token, live unexpired lease, bound to this run) for
the dispatch to happen. A stale claim stands the publish leg down with
superseded evidence and no native call — queue ownership that lapsed
(reaped, reassigned, expired) no longer implies effect ownership. A cancel
that lands during the already-started remote commit is NOT rolled back
(best-effort), but its completion records superseded evidence on the run and
``PublishResult.superseded`` is set instead of letting the caller walk a
cancelled run toward ``ready_for_human``.

The publisher never executes candidate content and never trusts the
harness's claims; a rejected candidate is reported, not repaired. The
negative conformance suite (``tests/test_publication_boundary.py``) is the
enforcement: every publish path must refuse a policy-violating candidate
with zero commit-API calls.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable import FlowRun, FlowStatus, RunSpec, StepRun, factory_branch, short_run_id
from forge.durable.claims import ExecutionClaim, current_claim
from forge.durable.controller import as_aware_utc
from forge.factory.implementer import FORGE_MATERIALIZE_MAX_FILE_CHARS
from forge.gitlab.blob_reads import AUTHORITATIVE_READ_FAILED, BlobReadResult
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.repository import (
    Change,
    ChangeSet,
    Operation,
    WriteOutcome,
    WritePolicy,
    normalize_repo_path,
    resolve_write_policy,
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
#: provider-side half of materialization). Under the R14 policy a path is
#: absent from the result ONLY on a provider-confirmed ``not_found`` — a
#: fetcher that drops paths on ANY failure forges existence facts; a base
#: file over the materialization cap raises :class:`CandidateError`
#: (``file_too_large``).
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
    policy: WritePolicy | None = None,
) -> ValidatedCandidate:
    """The pure publication-boundary check, shared by EVERY backend (ADR-0026).

    1. strict materialization of *bundle* against *base_contents* (the
       authoritative full texts at the attempt base): ``base_blob_digest``
       verified BEFORE anything is applied, ``intended_digest`` AFTER — the
       R08/R09 guarantees run on every path, builtin included;
    2. :func:`validate_changeset` — denied paths/prefixes/lockfiles, change
       count and size caps, existence rules, and the *allowed_paths* globs,
       under the write profile *policy* (R18; ``None`` = the default
       ``no_dependencies`` profile, the historical behavior);
    3. the R14 create rule: a create publishes only against a CONFIRMED
       absence. Callers feed *base_contents* from the strict typed reads,
       so presence in the mapping means the provider said the file exists —
       a create over it is a violation, not a silent overwrite;
    4. one action per path (R18): two bundle entries for one canonical path
       are a ``duplicate_path`` rejection before anything is applied.

    Raises :class:`CandidateError` (materialization/digest failures) or
    :class:`PolicyViolation` (write-policy violations). Returns the
    :class:`ValidatedCandidate` wrapper — the only thing a transport may
    write.
    """
    if bundle.is_empty:
        raise CandidateError("empty_candidate", "candidate contains no changes")
    _reject_duplicate_paths(bundle)
    entries = tuple(bundle.materialize(base_contents))
    changeset = manifest_to_changeset(
        entries,
        branch=branch,
        commit_message=commit_message,
        attempt_base_oid=bundle.attempt_base_oid,
    )
    violations = validate_changeset(
        changeset,
        base_contents,
        allowed_paths=list(allowed_paths),
        policy=policy,
    )
    violations.extend(
        f"change {entry.path!r} (create): file already exists in the base snapshot"
        for entry in entries
        if entry.operation == "create" and entry.path in base_contents
    )
    if violations:
        raise PolicyViolation(violations)
    return ValidatedCandidate(
        bundle=bundle,
        manifest=entries,
        changeset=changeset,
        allowed_paths=tuple(allowed_paths),
    )


def _reject_duplicate_paths(bundle: CandidateBundle) -> None:
    """Refuse a bundle with two entries for one canonical path (R18).

    The bundle doctrine is exactly ONE representation per file (R08); a
    repeated path — even under a different spelling (``./a.py`` vs ``a.py``)
    — is an ambiguous candidate and is rejected before any base read.
    """
    seen: set[str] = set()
    for path in bundle.paths:
        canonical = normalize_repo_path(path)
        if canonical and canonical in seen:
            raise CandidateError(
                "duplicate_path",
                f"{path}: duplicate path ({canonical!r}) — one entry per path per candidate",
            )
        seen.add(canonical)


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
    """Whether the frozen RunSpec's digest still equals the run's spec digest.

    Legacy runs without a spec row/digest pass (nothing to compare); a real
    mismatch fails closed — the plan the approver saw is not the plan bound
    to this run.
    """
    if not run.spec_digest:
        return True
    row = await _latest_spec_row(session, run)
    if row is None:
        return True
    return str(row.digest) == run.spec_digest


async def spec_allowed_paths(session: AsyncSession, run: FlowRun) -> list[str]:
    """The ``allowed_paths`` globs frozen in the run's RunSpec document.

    Monorepo path scoping (v0.7, complex-projects.md §1): empty/missing —
    the run is unscoped and every path is in scope.
    """
    row = await _latest_spec_row(session, run)
    if row is None:
        return []
    raw = (row.document or {}).get("allowed_paths")
    if not isinstance(raw, list):
        return []
    return [str(glob) for glob in raw if str(glob).strip()]


def run_project_keys(run: FlowRun) -> tuple[str, ...]:
    """The config keys identifying *run*'s project (R18 pipeline hooks).

    ``"<provider>:<project_id>"`` always; a GitHub run additionally answers
    to ``"github:<owner/repo>"`` — the human-readable subject identity.
    """
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
    """The configured sensitive pipeline paths for *run*'s project (R18).

    Looks *run*'s :func:`run_project_keys` up in the parsed
    ``pipeline_entrypoints`` config (forge.yml / FORGE_PIPELINE_ENTRYPOINTS)
    and returns the union of the matches, de-duplicated in order. Azure's
    pipeline definition may be any file, so onboarding names the real
    entrypoints instead of forge assuming ``azure-pipelines.yml``.
    """
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
    """Fetch COMPLETE content of *paths* at the attempt base snapshot.

    Authoritative full-content reads (no truncation, ever). R14 strict
    semantics: a path is absent from the result ONLY on a provider-confirmed
    ``not_found`` — create is then provably safe, and update/delete
    existence is honestly reported by validation. ``forbidden``,
    ``unavailable`` and ``incomplete`` raise :class:`CandidateError`
    (``authoritative_read_failed``) so publication fails BEFORE any remote
    effect instead of letting an unreadable base forge existence facts. A
    file over the :data:`FORGE_MATERIALIZE_MAX_FILE_CHARS` cap still
    raises ``file_too_large``.
    """
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
    """The run row as it is NOW, in a fresh session (R10 reservation checks)."""
    async with session_factory() as session:
        return await session.get(FlowRun, run_id)


#: The step status a live owner's row must still show. Mirrors
#: ``forge.worker.steps.STEP_RUNNING`` (not imported: the worker package
#: transitively imports this module).
_STEP_RUNNING = "running"


async def _claim_staleness(
    session: AsyncSession,
    claim: ExecutionClaim,
    run_id: str,
) -> str | None:
    """Why the ambient claim no longer owns its step, or ``None`` while it does (A04).

    The pre-dispatch arbitration the review demands: queue ownership must
    imply effect ownership at EVERY effect, the publication dispatch
    included. The step row is re-read (same fresh session as the reservation
    re-check) and must still show the claim's owner and fence token on a
    live, unexpired lease, bound to the run being published — a reaped,
    reassigned, completed or lease-expired row means another execution owns
    the work now, and THIS one must stand down before the native call. A
    command step binds its run only at execution time, so a NULL
    ``flow_run_id`` stays acceptable.
    """
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
    """Record on the run that a publication leg must not be trusted.

    R10 best-effort completion: an already-started remote commit is not
    rolled back, and a stale claim never dispatches at all — either way the
    run's evidence names WHY (``reason``, with the failed dispatch's
    ``detail``) so no reader mistakes the leg for the published candidate,
    and the run is never walked toward ``ready_for_human`` on its back.
    """
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
    reservation out even before the flag flips (R10). A04: the claim must
    also still OWN its step at the reservation point — owner, fence token,
    live lease and run binding are re-read immediately before the dispatch;
    a stale claim stands the leg down with superseded evidence and no
    native call.

    Write-policy profile knobs (R18, all defaulting to today's behavior):

    - *write_profile* names the profile (a builtin or a custom profile from
      *custom_write_profiles* — the parsed forge.yml / FORGE_WRITE_PROFILES
      mapping); ``None`` = the default ``no_dependencies`` profile;
    - *sensitive_paths* carries the project's onboarding-provided pipeline
      entrypoints (see :func:`sensitive_paths_for_run`); they are denied
      under EVERY profile;
    - *operator_approved* explicitly asserts the operator-approver gate for
      profiles with ``require_special_approval`` (``ci_change``). When
      ``None``, a session-backed run proves the gate with its frozen RunSpec
      row (an operator /go froze it); transport callers without a session
      must assert it explicitly — absent proof refuses the publication.
    """
    claim: ExecutionClaim | None = current_claim()
    claim_generation = claim.cancellation_generation if claim is not None else None

    spec_present = False
    if session_factory is not None:
        # Fresh trusted state: the run row as it is NOW, not as the caller
        # saw it.
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

    # Publication grant (F13/ADR-0018 §4, generation-fenced per R10).
    if not publication_grant_valid(fresh, claim_generation):
        return PublishResult(False, "publication_revoked")
    # F14: the spec frozen at plan acceptance is the one being executed.
    if not spec_ok:
        return PublishResult(False, "spec_digest_mismatch")
    # Stage-B fence (ADR-0017), re-checked at the boundary via the callable.
    if fence_check is not None and not await fence_check():
        return PublishResult(False, "fence_invalid")

    # The effective write policy (R18): profile + provider-sensitive paths.
    # A broken configuration fails the publication with a clear reason —
    # it never degrades to a more permissive policy.
    try:
        policy = resolve_write_policy(
            write_profile,
            custom_profiles=custom_write_profiles,
            extra_denied_paths=sensitive_paths or (),
        )
    except ValueError as exc:
        return PublishResult(False, f"write_policy_config_error: {exc}")
    if policy.require_special_approval and operator_approved is None:
        # No explicit assertion: the frozen RunSpec row IS the proof (an
        # operator /go — FORGE_APPROVERS-gated — froze the plan).
        operator_approved = session_factory is not None and spec_present
    if policy.require_special_approval and not operator_approved:
        return PublishResult(
            False,
            f"special_approval_required: write profile {policy.name!r} requires "
            f"approval from the operator approver set",
        )

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
            policy=policy,
        )
    except CandidateError as exc:
        return PublishResult(False, f"{exc.reason}: {exc}")
    except PolicyViolation as exc:
        return PublishResult(False, "changeset_invalid: " + "; ".join(exc.violations))

    # R10 RESERVATION POINT + A04 PRE-DISPATCH ARBITRATION: the reads above
    # can take arbitrarily long — re-read the grant immediately before
    # handing the candidate to the adapter. A cancel during the reads forbids
    # this NEW reservation. A bound ExecutionClaim must ALSO still own its
    # step row (owner + fence + live lease + run binding): a stale claim
    # stands down with superseded evidence and no native call — the reclaimed
    # work belongs to the new owner now.
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
    commit_message: str | None = None,
    fence_check: FenceCheck | None = None,
    write_profile: str | None = None,
    custom_write_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    sensitive_paths: Sequence[str] | None = None,
    operator_approved: bool | None = None,
) -> PublishResult:
    """Validate and publish *bundle* for *run* via the GitLab transport.

    The GitLab-native binding of :func:`publish_validated_candidate`
    (ADR-0026): the run-state, materialization and policy legs are shared;
    only the base-content reads and the journaled
    :class:`ChangesetWriter` apply are GitLab's. The writer is pinned to
    the frozen attempt base (``start_ref = expected_head``) — in the
    proposal-only model nothing else ever pushed, so the guarded apply
    proves the branch did not move. The write-policy profile knobs (R18)
    pass straight through to the shared entry.
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
                # R11 intent identity: the writer journals the durable
                # publication intent (stable operation key + expected
                # parent) BEFORE the HTTP effect, and an open intent from a
                # crashed attempt is probed-and-adopted, never duplicated.
                provider="gitlab",
                repo=str(run.project_id),
                commit_cycle=run.commit_cycle or 1,
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
        write_profile=write_profile,
        custom_write_profiles=custom_write_profiles,
        sensitive_paths=sensitive_paths,
        operator_approved=operator_approved,
    )
