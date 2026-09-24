"""Q35-07 + R36-06: the composition types adopted at ONE production dispatch entry.

ADR-0029 (R32-24) shipped :mod:`forge.runs.composition` with the honest
admission that no production caller used it. Q35-07 was the FIRST
adoption: the narrow, provider-specific adapter the GitHub dispatch
entry (:meth:`forge.runs.github_service.GitHubRunService._advance_harness`)
calls BEFORE any effectful provider call. R36-06 (issue #265) extended
the same envelope to v2 — no parallel DTO layer, no new controller:

- :func:`github_repository_context` — the dispatch's
  :class:`~forge.runs.composition.RepositoryContext` from the service's
  bound ``<owner>/<repo>`` connection and the webhook's numeric
  repository id;
- :func:`derive_execution_attempt_id` (re-exported from
  :mod:`forge.runs.composition`) — the durable EXECUTION identity,
  sha256 over (run id, durable attempt ordinal, source base OID);
- :func:`assert_axis_separation` — the coordinate-axis guard: the
  IDENTITY axis (``execution_attempt_id``), the SOURCE axis (the
  attempt base OID) and the AUTHORITY axis (the cancellation epoch)
  each refuse a checkpoint sequence / applied-command watermark, and
  the epoch must be the non-negative int the run row holds;
- :func:`compose_attempt_start` — the frozen v2
  :class:`~forge.runs.composition.AttemptStartSpec` envelope plus the
  persisted ``attempt_start`` evidence document (envelope digest +
  context digest), built ONLY from inputs that were resolved once and
  are durable: the frozen spec's execution-profile digest, the
  continuation-selected resume mode and its pinned checkpoint digest,
  the run row's attempt base, durable attempt ordinal and cancellation
  epoch, the reserved execution lease. No live setting enters the
  envelope, so a restart between approval and redispatch reconstructs
  the SAME envelope.

Coordinate axes, separated once and explicitly (R36-06's core):

==========================  ==============================================
axis                        where it lives — and ONLY there
==========================  ==============================================
run identity                ``AttemptStartSpec.run_id`` — the LOGICAL
                            INTENT
execution identity          ``AttemptStartSpec.execution_attempt_id`` —
                            the durable id derived from (run id, attempt
                            ordinal, source base); two executions from
                            one source commit differ here
source revision             ``AttemptStartSpec.source_base_oid`` (what
                            ``attempt_id`` held in v1) — a code
                            revision, never a process identity
authority epoch             ``AttemptStartSpec.authority_epoch`` —
                            INSIDE the envelope digest (v2); the run
                            row's ``cancellation_generation``
continuation identity       ``AttemptStartSpec.continuation_ref_digest``
                            — the pinned checkpoint content address
                            when resuming WIP (``required``); empty
                            otherwise
resume mode                 ``AttemptStartSpec.resume_mode`` (the
                            continuation decision's selected contract)
checkpoint sequence         ``ResumeSpec.generation`` — never an envelope
                            field, never a dispatch counter
control-command sequence /  the checkpoint's own applied-command
watermark                   watermark and the control mailbox's own
                            sequence — never envelope fields at all
==========================  ==============================================

INTENT vs REPEAT DELIVERY (R36-06 acceptance): the LOGICAL INTENT is
identified by (run id, durable attempt ordinal, source base OID, resume
mode, pinned continuation ref) — the durable rows a start decision
persisted. A REPEAT DELIVERY of that intent (the reconciler's re-drive,
a stranding recovery, a restart between envelope construction and the
native start) reconstructs IDENTICAL authority-bearing members —
execution id, epoch, lease and continuation ref all derive from the
same durable state, never from fresh defaults or a timestamp — so
digest equality still means "the same authorized execution", and a
serialization-order change or process restart is never misread as a
new attempt. A legitimately NEW attempt changes at least one durable
row (the ordinal bumps, the source base moves), and therefore derives a
different execution identity even from the same commit.

The v1 compat adapter (:func:`legacy_attempt_start_view`) reads
pre-R36-06 ``attempt_start`` documents: their weaker identity (source
OID only, no execution id) is RECORDED for audit and refused for
authority-bearing comparisons — historical ids are never manufactured.

Import boundary (ADR-0027 §3): like :mod:`forge.runs.composition`, this
module is core — it imports no ``forge.integrations.*`` and no
``forge.gateway.*``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from forge.runs.composition import (
    ATTEMPT_START_V1,
    ATTEMPT_START_V2,
    AttemptStartSpec,
    CompositionBoundaryError,
    RepositoryContext,
    assert_attempt_start,
    derive_execution_attempt_id,
)

__all__ = [
    "ATTEMPT_START_EVIDENCE_KEY",
    "ATTEMPT_START_VERSION",
    "ComposedAttemptStart",
    "PROFILE_SOURCE_EXECUTION_PROFILE",
    "PROFILE_SOURCE_SPEC_DIGEST",
    "assert_axis_separation",
    "compose_attempt_start",
    "derive_execution_attempt_id",
    "github_repository_context",
    "legacy_attempt_start_view",
]

#: The run-evidence key the composed envelope digest persists under
#: (Q35-07's audit trail: a later redispatch — the reconciler's re-drive,
#: a stranding recovery — reconstructs the envelope and compares digests;
#: under v2 equal means the same authorized EXECUTION — every
#: authority-bearing member, epoch included, is inside the digest —
#: different means a legitimately new attempt).
ATTEMPT_START_EVIDENCE_KEY: str = "attempt_start"

#: Schema version of the persisted ``attempt_start`` document. v2
#: (R36-06): distinct ``execution_attempt_id`` / ``source_base_oid``,
#: ``authority_epoch`` inside the envelope digest, the pinned
#: ``continuation_ref_digest``. v1 documents stay readable through
#: :func:`legacy_attempt_start_view`.
ATTEMPT_START_VERSION: int = ATTEMPT_START_V2

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
#: hex chars and the derived execution identity is hex64. A value that is
#: ALL digits and SHORTER than an OID can only be a sequence number (or
#: an applied-command watermark) smuggled onto a revision/identity axis
#: — the one mixup this guard exists to refuse.
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


def assert_axis_separation(
    *,
    attempt_oid: object,
    authority_epoch: object,
    execution_attempt_id: object = None,
) -> None:
    """The coordinate-axis guard at the dispatch entry (Q35-07, R36-06).

    Three refuses, each naming the axis:

    - a bare SHORT decimal integer on the SOURCE axis (*attempt_oid*) is
      a checkpoint sequence or an applied-command watermark — the exact
      ``generation``-overloading defect the issue describes (a sequence
      like ``7`` is never an attempt base OID, and the checkpoint
      sequence belongs ONLY inside a
      :class:`~forge.runs.composition.ResumeSpec`);
    - the same counter shape — or anything that is not the hex64
      :func:`derive_execution_attempt_id` returns — on the IDENTITY axis
      (*execution_attempt_id*, when supplied) is refused: an execution
      identity is a derived durable id, never a sequence, a watermark or
      a source OID;
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
    if execution_attempt_id is not None:
        identity = str(execution_attempt_id).strip()
        if not identity:
            raise CompositionBoundaryError(
                "identity axis (execution_attempt_id) is empty — the execution identity "
                "is the durable id derive_execution_attempt_id() returns, never blank"
            )
        if identity.isdigit() and len(identity) < _MIN_OID_LENGTH:
            raise CompositionBoundaryError(
                f"identity axis carries {identity!r} — a checkpoint sequence (or "
                "applied-command watermark) is not an execution identity: the "
                "execution identity is the derived durable id, and a counter must "
                "never be laundered into one"
            )
        if identity == oid:
            raise CompositionBoundaryError(
                "identity axis (execution_attempt_id) equals the source axis "
                f"({identity!r}) — an execution identity is never a code revision: "
                "two executions can start from the same commit (R36-06)"
            )
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise CompositionBoundaryError(
                "identity axis (execution_attempt_id) must be the hex64 durable id "
                "derive_execution_attempt_id() returns — never a source OID, a "
                "sequence or a watermark"
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


def legacy_attempt_start_view(document: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The R36-06 compat adapter: a v1 ``attempt_start`` document → audit view.

    A document persisted before R36-06 carries the WEAKER identity: the
    attempt axis WAS the source OID (``attempt_base``) and the authority
    epoch rode beside the envelope digest — no execution identity exists
    to recover, and none is manufactured. The view records exactly that:

    - ``envelope_version`` — the persisted document's version (1);
    - ``execution_attempt_id`` — always ``None`` (the missing fact);
    - ``source_base_oid`` — what the v1 ``attempt_base`` held;
    - ``authority_epoch`` — the epoch the v1 document stored beside;
    - ``identity_strength`` — ``"source_oid_only"``.

    Returns ``None`` for a v2 document (nothing legacy about it) and for
    a non-mapping. The view is READABLE FOR AUDIT; authority-bearing
    comparisons against it are refused
    (:func:`forge.runs.composition.assert_publication_identity`), so an
    old envelope never claims the v2 guarantee.
    """
    if not isinstance(document, Mapping):
        return None
    version = document.get("version")
    if version == ATTEMPT_START_V2 and str(document.get("execution_attempt_id") or ""):
        return None
    return {
        "envelope_version": int(version) if isinstance(version, int) else ATTEMPT_START_V1,
        "execution_attempt_id": None,  # never manufactured
        "source_base_oid": str(
            document.get("source_base_oid") or document.get("attempt_base") or ""
        ),
        "authority_epoch": document.get("authority_epoch"),
        "identity_strength": "source_oid_only",
    }


@dataclass(frozen=True)
class ComposedAttemptStart:
    """The composed dispatch boundary: envelope, context, evidence document.

    ``legacy`` marks the compat shape — the run's evidence carried no
    persisted ``attempt_start`` key at all (every run persisted before
    Q35-07), so this dispatch constructed the envelope on the fly; the
    caller MUST make that observable (the ``composition.legacy_attempts``
    log marker), never silent. ``unchanged`` answers the redispatch
    verification: a prior digest equal to this envelope's means the
    recovered dispatch reconstructs the SAME authorization — and under
    v2 that claim is authority-bearing (the epoch is inside the digest),
    so it is made ONLY against a v2 prior document.

    ``prior_identity`` (R36-06) is the v1 compat view when the prior
    document was persisted before the v2 extension — the weaker identity
    recorded for audit (``execution_attempt_id=None``) with
    ``unchanged`` refused, never guessed; ``None`` when there is no
    legacy prior. The caller logs ``composition.legacy_read`` for it.
    """

    spec: AttemptStartSpec
    context: RepositoryContext
    document: dict[str, Any]
    legacy: bool
    unchanged: bool
    prior_identity: dict[str, Any] | None = None


def compose_attempt_start(
    *,
    run_id: str,
    repo_full_name: str,
    project_id: int,
    attempt_oid: str,
    authority_epoch: int,
    attempt_ordinal: int,
    profile_digest: str,
    fallback_profile_digest: str,
    resume_mode: str,
    lease_id: str,
    continuation_ref_digest: str = "",
    prior_document: Mapping[str, Any] | None = None,
) -> ComposedAttemptStart:
    """Compose the pre-effect v2 dispatch envelope from the durable inputs.

    Every input was resolved ONCE, upstream and durably: *profile_digest*
    is the frozen spec's A18 execution-profile digest (a pre-A18 spec
    falls back to *fallback_profile_digest* — the run's frozen spec
    digest — through the explicit, observable legacy adapter), and the
    rest come from the run row and the reserved execution lease.
    *attempt_ordinal* is the DURABLE attempt counter the run rows carry
    (the GitHub entry passes ``FlowRun.cancellation_generation`` — the
    counter the lane-control and checkpoint APIs already verify against);
    it derives the :attr:`AttemptStartSpec.execution_attempt_id`
    together with *attempt_oid* (the source base) and *run_id* — never a
    timestamp. *continuation_ref_digest* is the pinned checkpoint content
    address when *resume_mode* is ``required`` (empty otherwise). No
    live setting enters, so settings mutated after approval cannot
    reshape a redispatched envelope.

    Raises :class:`~forge.runs.composition.CompositionBoundaryError`
    (including :class:`~forge.runs.composition.MissingEnvelopeFieldError`)
    naming the exact field on any defect — the caller refuses the
    dispatch BEFORE the provider call.
    """
    context = github_repository_context(repo_full_name, project_id)
    assert_axis_separation(attempt_oid=attempt_oid, authority_epoch=authority_epoch)
    execution_attempt_id = derive_execution_attempt_id(
        run_id=run_id, attempt_ordinal=attempt_ordinal, source_base_oid=attempt_oid
    )
    # The derived identity is re-checked on its own axis (defense in
    # depth: the derivation guarantees the shape; the guard proves the
    # separation the digest will claim).
    assert_axis_separation(
        attempt_oid=attempt_oid,
        authority_epoch=authority_epoch,
        execution_attempt_id=execution_attempt_id,
    )
    profile_source = PROFILE_SOURCE_EXECUTION_PROFILE
    resolved_profile = str(profile_digest or "").strip().lower()
    if not resolved_profile:
        resolved_profile = str(fallback_profile_digest or "").strip().lower()
        profile_source = PROFILE_SOURCE_SPEC_DIGEST
    spec = AttemptStartSpec(
        run_id=run_id,
        execution_attempt_id=execution_attempt_id,
        source_base_oid=str(attempt_oid or "").strip(),
        repository=context,
        profile_digest=resolved_profile,
        resume_mode=resume_mode,
        lease_id=str(lease_id or "").strip(),
        authority_epoch=authority_epoch,
        continuation_ref_digest=str(continuation_ref_digest or "").strip(),
        schema_version=ATTEMPT_START_V2,
    )
    # The pre-effect boundary check every adapter MUST call (ADR-0029 §5)
    # — construction already refuses most of these; the assert is the
    # defense in depth at the one checkpoint where it is free.
    assert_attempt_start(spec, context)
    prior = prior_document if isinstance(prior_document, Mapping) else None
    legacy = prior is None
    # R36-06: a v1 prior document is read through the compat adapter —
    # its weaker identity is recorded for audit, and it can never
    # support the authority-bearing "unchanged" claim.
    prior_identity = legacy_attempt_start_view(prior)
    envelope_digest = spec.envelope_digest()
    prior_digest = str(prior.get("envelope_digest") or "") if prior is not None else ""
    unchanged = bool(prior_digest) and prior_digest == envelope_digest and prior_identity is None
    document: dict[str, Any] = {
        "version": ATTEMPT_START_VERSION,
        "envelope_digest": envelope_digest,
        "context_digest": context.digest(),
        "subject_key": context.subject_key(),
        "resume_mode": spec.resume_mode,
        # The historical audit spelling of the source axis, kept stable
        # for pre-R36-06 readers of the evidence trail.
        "attempt_base": spec.source_base_oid,
        # R36-06: the separated identity axes — WHICH execution (the
        # derived durable id) from WHAT source revision, with the
        # authority epoch and the pinned continuation identity INSIDE
        # the envelope digest (all four are digest members under v2).
        "execution_attempt_id": spec.execution_attempt_id,
        "source_base_oid": spec.source_base_oid,
        "attempt_ordinal": attempt_ordinal,
        "authority_epoch": authority_epoch,
        "continuation_ref_digest": spec.continuation_ref_digest,
        "profile_source": profile_source,
        "legacy": legacy,
    }
    if prior_identity is not None:
        # The explicit legacy read: the audit trail says the prior
        # envelope carried only the weaker identity — observable, never
        # silent, and no new guarantee is claimed over it.
        document["legacy_read"] = prior_identity
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
        prior_identity=prior_identity,
    )
