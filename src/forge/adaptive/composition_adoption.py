"""Q35-07: the composition types adopted at ONE production dispatch entry.

ADR-0029 (R32-24) shipped :mod:`forge.runs.composition` with the honest
admission that no production caller used it. This module is the FIRST
adoption: the narrow, provider-specific adapter the GitHub dispatch entry
(:meth:`forge.runs.github_service.GitHubRunService._advance_harness`)
calls BEFORE any effectful provider call. It is a thin composition of the
existing types — no parallel DTO layer, no new controller:

- :func:`github_repository_context` — the dispatch's
  :class:`~forge.runs.composition.RepositoryContext` from the service's
  bound ``<owner>/<repo>`` connection and the webhook's numeric
  repository id;
- :func:`assert_axis_separation` — the Q35-07 coordinate-axis guard: the
  ATTEMPT axis (the attempt base OID) must never carry a checkpoint
  sequence, and the AUTHORITY axis (the cancellation epoch) must be the
  non-negative int the run row holds;
- :func:`compose_attempt_start` — the frozen
  :class:`~forge.runs.composition.AttemptStartSpec` envelope plus the
  persisted ``attempt_start`` evidence document (envelope digest +
  context digest), built ONLY from inputs that were resolved once and
  are durable: the frozen spec's execution-profile digest, the
  continuation-selected resume mode, the run row's attempt base and
  cancellation epoch, the reserved execution lease. No live setting
  enters the envelope, so a restart between approval and redispatch
  reconstructs the SAME envelope.

Coordinate axes, separated once and explicitly (the issue's core):

==========================  ==============================================
axis                        where it lives — and ONLY there
==========================  ==============================================
run identity                ``AttemptStartSpec.run_id``
attempt identity            ``AttemptStartSpec.attempt_id`` (the attempt
                            base OID the lane credential is minted for)
resume mode                 ``AttemptStartSpec.resume_mode`` (the
                            continuation decision's selected contract)
checkpoint sequence         ``ResumeSpec.generation`` — never an envelope
                            field, never a dispatch counter
control-command sequence    the control mailbox's own sequence — not an
                            envelope field at all
cancellation/authority      the run row's ``cancellation_generation``,
epoch                       recorded on the evidence document beside the
                            envelope digest, never inside it
==========================  ==============================================

The existing :class:`~forge.runs.composition.AttemptStartSpec` fields
suffice for the envelope (verified against the axis table above); nothing
in :mod:`forge.runs.composition` had to change for this adoption.

Import boundary (ADR-0027 §3): like :mod:`forge.runs.composition`, this
module is core — it imports no ``forge.integrations.*`` and no
``forge.gateway.*``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from forge.runs.composition import (
    AttemptStartSpec,
    CompositionBoundaryError,
    RepositoryContext,
    assert_attempt_start,
)

__all__ = [
    "ATTEMPT_START_EVIDENCE_KEY",
    "ATTEMPT_START_VERSION",
    "ComposedAttemptStart",
    "PROFILE_SOURCE_EXECUTION_PROFILE",
    "PROFILE_SOURCE_SPEC_DIGEST",
    "assert_axis_separation",
    "compose_attempt_start",
    "github_repository_context",
]

#: The run-evidence key the composed envelope digest persists under
#: (Q35-07's audit trail: a later redispatch — the reconciler's re-drive,
#: a stranding recovery — reconstructs the envelope and compares digests;
#: equal means the same authorized dispatch, different means a
#: legitimately new attempt).
ATTEMPT_START_EVIDENCE_KEY: str = "attempt_start"

#: Schema version of the persisted ``attempt_start`` document.
ATTEMPT_START_VERSION: int = 1

#: The A18 execution-profile digest is the envelope's profile axis (the
#: approved-vs-executed pairing).
PROFILE_SOURCE_EXECUTION_PROFILE = "execution_profile"

#: The explicit LEGACY profile source: a spec frozen before A18 carries
#: no execution-profile section, so the envelope pins the run's frozen
#: ``spec_digest`` instead — the strongest execution-contract identity
#: that run ever had. Observable in the persisted document
#: (``profile_source``), never silent.
PROFILE_SOURCE_SPEC_DIGEST = "spec_digest"

#: The provider family of the one adopted dispatch path (ADR-0027's
#: ladder: one provider first; GitLab/Azure adopt after this path passes
#: the composed suite).
GITHUB_FAMILY = "github"

#: A checkpoint sequence is a small decimal counter; a git OID is 40/64
#: hex chars. A value that is ALL digits and SHORTER than an OID can only
#: be a sequence number smuggled onto the attempt axis — the one mixup
#: this guard exists to refuse.
_MIN_OID_LENGTH = 40


def github_repository_context(repo_full_name: str, project_id: int) -> RepositoryContext:
    """The dispatch's repository context for the bound GitHub connection.

    ``connection_id`` is the family-qualified ``github:<owner>/<repo>``
    identity the gateways and findings ingestion already write (the
    convention :mod:`forge.runs.composition` documents); the numeric
    repository id is the webhook's ``project_id`` — the identity the
    partial unique index ``uq_active_run_per_issue`` already keys on.
    The two members are structurally distinct, so the SAME numeric id on
    two different connections can never collide: the comparable identity
    is the structured :meth:`~forge.runs.composition.RepositoryContext.subject_key`
    (and its canonical digest), never a delimiter concatenation of loose
    parts.
    """
    owner, _, name = str(repo_full_name or "").partition("/")
    if not owner.strip() or not name.strip():
        raise CompositionBoundaryError(
            "RepositoryContext field 'connection_id' cannot be derived from "
            f"repo_full_name {repo_full_name!r} — the GitHub dispatch binds exactly "
            "one '<owner>/<repo>' connection"
        )
    if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id < 1:
        raise CompositionBoundaryError(
            "RepositoryContext field 'repository_id' cannot be the numeric repository "
            f"id {project_id!r} — a dispatch without a repository identity is refused"
        )
    return RepositoryContext(
        provider_family=GITHUB_FAMILY,
        connection_id=f"{GITHUB_FAMILY}:{owner}/{name}",
        repository_id=str(project_id),
    )


def assert_axis_separation(*, attempt_oid: object, authority_epoch: object) -> None:
    """The Q35-07 coordinate-axis guard at the dispatch entry.

    Two refuses, each naming the axis:

    - a bare SHORT decimal integer on the ATTEMPT axis is a checkpoint
      sequence — the exact ``generation``-overloading defect the issue
      describes (a sequence like ``7`` is never an attempt base OID, and
      the checkpoint sequence belongs ONLY inside a
      :class:`~forge.runs.composition.ResumeSpec`);
    - the AUTHORITY axis (the cancellation epoch the lane credential is
      scoped by) must be a non-negative int — never a bool, never a
      string, never negative.

    The remaining separations are structural: the envelope has no
    generation member at all, the checkpoint sequence lives only inside
    ``ResumeSpec``, and the control-command sequence never reaches the
    dispatch envelope.
    """
    oid = str(attempt_oid if attempt_oid is not None else "").strip()
    if oid.isdigit() and len(oid) < _MIN_OID_LENGTH:
        raise CompositionBoundaryError(
            f"attempt axis carries {oid!r} — a checkpoint sequence is not an attempt "
            "identity: the attempt axis is the attempt base OID, the checkpoint "
            "sequence lives only inside a ResumeSpec (generation must not be "
            "overloaded across axes)"
        )
    if isinstance(authority_epoch, bool) or not isinstance(authority_epoch, int):
        raise CompositionBoundaryError(
            "authority axis (cancellation epoch) must be an int, got "
            f"{type(authority_epoch).__name__} {authority_epoch!r} — the epoch is "
            "never a checkpoint sequence string or a bool"
        )
    if authority_epoch < 0:
        raise CompositionBoundaryError(
            f"authority axis (cancellation epoch) must be >= 0, got {authority_epoch}"
        )


@dataclass(frozen=True)
class ComposedAttemptStart:
    """The composed dispatch boundary: envelope, context, evidence document.

    ``legacy`` marks the compat shape — the run's evidence carried no
    persisted ``attempt_start`` key (every run persisted before Q35-07),
    so this dispatch constructed the envelope on the fly; the caller
    MUST make that observable (the ``composition.legacy_attempts`` log
    marker), never silent. ``unchanged`` answers the redispatch
    verification: a prior digest equal to this envelope's means the
    recovered dispatch reconstructs the SAME authorization.
    """

    spec: AttemptStartSpec
    context: RepositoryContext
    document: dict[str, Any]
    legacy: bool
    unchanged: bool


def compose_attempt_start(
    *,
    run_id: str,
    repo_full_name: str,
    project_id: int,
    attempt_oid: str,
    authority_epoch: int,
    profile_digest: str,
    fallback_profile_digest: str,
    resume_mode: str,
    lease_id: str,
    prior_document: Mapping[str, Any] | None,
) -> ComposedAttemptStart:
    """Compose the pre-effect dispatch envelope from the durable inputs.

    Every input was resolved ONCE, upstream and durably: *profile_digest*
    is the frozen spec's A18 execution-profile digest (a pre-A18 spec
    falls back to *fallback_profile_digest* — the run's frozen spec
    digest — through the explicit, observable legacy adapter), and the
    rest come from the run row and the reserved execution lease. No live
    setting enters, so settings mutated after approval cannot reshape a
    redispatched envelope.

    Raises :class:`~forge.runs.composition.CompositionBoundaryError`
    (including :class:`~forge.runs.composition.MissingEnvelopeFieldError`)
    naming the exact field on any defect — the caller refuses the
    dispatch BEFORE the provider call.
    """
    context = github_repository_context(repo_full_name, project_id)
    assert_axis_separation(attempt_oid=attempt_oid, authority_epoch=authority_epoch)
    profile_source = PROFILE_SOURCE_EXECUTION_PROFILE
    resolved_profile = str(profile_digest or "").strip().lower()
    if not resolved_profile:
        resolved_profile = str(fallback_profile_digest or "").strip().lower()
        profile_source = PROFILE_SOURCE_SPEC_DIGEST
    spec = AttemptStartSpec(
        run_id=run_id,
        attempt_id=str(attempt_oid or "").strip(),
        repository=context,
        profile_digest=resolved_profile,
        resume_mode=resume_mode,
        lease_id=str(lease_id or "").strip(),
    )
    # The pre-effect boundary check every adapter MUST call (ADR-0029 §5)
    # — construction already refuses most of these; the assert is the
    # defense in depth at the one checkpoint where it is free.
    assert_attempt_start(spec, context)
    prior = prior_document if isinstance(prior_document, Mapping) else None
    legacy = prior is None
    envelope_digest = spec.envelope_digest()
    prior_digest = str(prior.get("envelope_digest") or "") if prior is not None else ""
    unchanged = bool(prior_digest) and prior_digest == envelope_digest
    document: dict[str, Any] = {
        "version": ATTEMPT_START_VERSION,
        "envelope_digest": envelope_digest,
        "context_digest": context.digest(),
        "subject_key": context.subject_key(),
        "resume_mode": spec.resume_mode,
        "attempt_base": spec.attempt_id,
        # The authority axis rides BESIDE the envelope, named for what it
        # is — the cancellation epoch the lane credential is scoped by.
        # It is deliberately not an envelope member (axis separation).
        "authority_epoch": authority_epoch,
        "profile_source": profile_source,
        "legacy": legacy,
    }
    if prior_digest and not unchanged:
        # A legitimately new attempt superseding a prior envelope — the
        # audit trail of WHICH authorization this dispatch replaced.
        document["supersedes"] = prior_digest
    return ComposedAttemptStart(
        spec=spec,
        context=context,
        document=document,
        legacy=legacy,
        unchanged=unchanged,
    )
