"""Formal composition boundaries (R32-24, ADR-0029) — the five names as types.

This module consolidates the composition facts every dispatch entry
hand-assembles today into three frozen, validate-on-construction values
plus the recorded-evidence matrix that says which boundary versions may
compose with which:

- :class:`RepositoryContext` — provider family + connection identity +
  repository identity, ONE immutable value; family/connection/run/
  attempt identities are never interchangeable, and cross-family mixing
  is a construction error, not a runtime surprise;
- :class:`AttemptStartSpec` — the frozen envelope a dispatch entry must
  construct BEFORE any external effect. Envelope v2 (R36-06, issue #265)
  separates the EXECUTION identity from the SOURCE revision: the attempt
  identity is :attr:`AttemptStartSpec.execution_attempt_id` — a durable
  id derived deterministically from (run id, durable attempt ordinal,
  source base OID) by :func:`derive_execution_attempt_id` — while the
  code revision the lane was minted for is
  :attr:`AttemptStartSpec.source_base_oid` (what ``attempt_id`` held in
  v1). The authority epoch rides INSIDE the envelope digest now, and a
  WIP-resuming dispatch pins the exact continuation identity
  (:attr:`AttemptStartSpec.continuation_ref_digest`). Two executions can
  start from the same commit, so the source OID alone is never an
  execution identity. Permissive defaults are forbidden — a missing field
  raises :class:`MissingEnvelopeFieldError` (a ``TypeError``) naming the
  field;
- :class:`ResumeSpec` — the documented NEXT-03 concept formalized
  (resume mode, exact checkpoint reference, generation, authority);
- :class:`CompositionMatrix` — directional verified/untested/unsupported
  edges between boundary versions; ``blocked_by`` is the can-i-deploy
  QUERY over recorded edges (never a re-run), and
  :func:`matrix_drift` lists shipped-but-unqualified combinations as
  findings;
- :func:`assert_attempt_start` — the pre-effect boundary check an
  adapter MUST call before dispatch;
- :func:`assert_publication_identity` — the R36-06 publication-entry
  check: a callback is authorized by its EXECUTION identity matching the
  persisted envelope's, never by a source-OID match alone.

Import boundary (ADR-0027 §3, applied at creation): this module is core
— it imports no ``forge.integrations.*`` and no ``forge.gateway.*``.

Production adoption (Q35-07): the GitHub dispatch entry
(:meth:`forge.runs.github_service.GitHubRunService._advance_harness`)
now constructs and asserts these types before any effectful call, via
the narrow provider seam :mod:`forge.adaptive.composition_adoption`
(ADR-0027's ladder — one provider path first; the other services adopt
only after this path passes the composed suite). R36-06 (#265) extended
that envelope to v2 at the same single entry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from forge.runs.spec import canonical_json_digest

__all__ = [
    "ATTEMPT_START_SCHEMA_VERSIONS",
    "ATTEMPT_START_V1",
    "ATTEMPT_START_V2",
    "EDGE_UNTESTED",
    "EDGE_UNSUPPORTED",
    "EDGE_VERIFIED",
    "EDGE_VERDICTS",
    "PROVIDER_FAMILIES",
    "RESUME_MODES",
    "AttemptStartSpec",
    "CompositionBoundaryError",
    "CompositionMatrix",
    "DriftFinding",
    "MatrixEdge",
    "MissingEnvelopeFieldError",
    "RepositoryContext",
    "ResumeSpec",
    "assert_attempt_start",
    "assert_publication_identity",
    "derive_execution_attempt_id",
    "matrix_drift",
]

#: The provider family vocabulary — the ``FlowRun.provider`` values
#: (migration 012): forge started GitLab-only, and the partial unique
#: index ``uq_active_run_per_issue`` keys on (provider, project_id,
#: issue_iid) precisely because the numeric subject namespaces are NOT
#: comparable across families. A repository context from outside this
#: tuple is refused at construction.
PROVIDER_FAMILIES = ("gitlab", "github", "azure_devops")

#: NEXT-03's three DISTINCT dispatch modes — ``fresh`` (no checkpoint
#: needed; a 404-shaped restore failure is normal), ``required`` (the
#: exact ResumeSpec MUST restore or the lane halts before any vendor
#: client exists) and ``restart`` (the WIP is intentionally discarded).
#: They are distinct dispatch shapes, not hints.
RESUME_MODES = ("fresh", "required", "restart")

#: The honest edge verdicts (R32-24): ``verified`` only ever comes from
#: :meth:`CompositionMatrix.register_verified` WITH an evidence
#: reference; ``untested`` is the default for unregistered pairs (no
#: evidence is a label, never a pass); ``unsupported`` is an explicitly
#: recorded refusal.
EDGE_VERIFIED = "verified"
EDGE_UNTESTED = "untested"
EDGE_UNSUPPORTED = "unsupported"
EDGE_VERDICTS = (EDGE_VERIFIED, EDGE_UNTESTED, EDGE_UNSUPPORTED)

#: Envelope schema v1 (Q35-07, superseded): the attempt identity WAS the
#: source OID (``attempt_id``) and the authority epoch rode BESIDE the
#: envelope digest. Historical documents stay readable through the
#: explicit compat adapter (R36-06) — their weaker identity is recorded
#: for audit, never upgraded and never used for authority-bearing
#: comparisons.
ATTEMPT_START_V1 = 1

#: Envelope schema v2 (R36-06, issue #265): distinct
#: :attr:`AttemptStartSpec.execution_attempt_id` (the durable derived
#: execution identity) and :attr:`AttemptStartSpec.source_base_oid` (the
#: code revision), with :attr:`AttemptStartSpec.authority_epoch` and the
#: pinned :attr:`AttemptStartSpec.continuation_ref_digest` INSIDE the
#: envelope digest.
ATTEMPT_START_V2 = 2

#: The envelope schema versions a document/spec may carry.
ATTEMPT_START_SCHEMA_VERSIONS = (ATTEMPT_START_V1, ATTEMPT_START_V2)

#: A git OID is 40/64 hex chars; a checkpoint sequence or an
#: applied-command watermark is a small decimal counter. A value that is
#: ALL digits and SHORTER than an OID can only be a counter smuggled
#: onto a revision/identity axis — the one mixup the axis guards refuse.
_MIN_OID_LENGTH = 40


class CompositionBoundaryError(ValueError):
    """A composition boundary was crossed (R32-24, ADR-0029).

    Raised by every construction validation and boundary check in this
    module; the message always names the exact field. Subclasses
    ``ValueError`` so generic fail-closed callers stay fail-closed.
    """


class MissingEnvelopeFieldError(CompositionBoundaryError, TypeError):
    """A required envelope field is missing or empty (ADR-0029 §1).

    A ``TypeError`` by design: the permissive-default alternative
    (guessing run id, attempt oid, lease identity…) is exactly the
    identity-interchange defect class this module exists to close, so a
    missing field fails construction loudly instead of defaulting. Also
    a :class:`CompositionBoundaryError`, so one except clause sees every
    refusal.
    """


def _required_text(value: object, field_name: str, owner: str = "boundary value") -> str:
    """A required non-empty string field — never a permissive default.

    *owner* names the refusing type in the message (``RepositoryContext``,
    ``AttemptStart envelope``, …) so the error says WHERE the field was
    missing, not just that something was.
    """
    if value is None:
        raise MissingEnvelopeFieldError(f"{owner} field {field_name!r} is required")
    if not isinstance(value, str) or not value.strip():
        raise MissingEnvelopeFieldError(
            f"{owner} field {field_name!r} must be a non-empty string, got {value!r}"
        )
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _is_counter_shaped(value: str) -> bool:
    """A bare SHORT all-digit string — a checkpoint sequence or an
    applied-command watermark, never a git OID or a derived identity."""
    return value.isdigit() and len(value) < _MIN_OID_LENGTH


def derive_execution_attempt_id(*, run_id: str, attempt_ordinal: int, source_base_oid: str) -> str:
    """The durable EXECUTION identity of one dispatch attempt (R36-06).

    sha256 over the canonical document ``{"attempt_ordinal": n,
    "run_id": …, "source_base_oid": …}`` — deterministic from durable
    state alone, so:

    - two attempts from the SAME source commit derive DIFFERENT ids
      whenever the durable attempt ordinal differs (a retry/revival that
      opens a new attempt bumps it);
    - a REPEAT delivery of the same persisted start intent (a
      reconciler re-drive, a restart between envelope construction and
      the native start) reconstructs the IDENTICAL id, because the same
      run rows derive the same value — never a timestamp, never a fresh
      uuid, never anything that survives only in process memory.

    *attempt_ordinal* is the DURABLE attempt counter the run rows carry
    (the GitHub adoption passes ``FlowRun.cancellation_generation`` —
    the counter the lane-control and checkpoint APIs already verify
    against). A short all-digit *source_base_oid* — a checkpoint
    sequence or command watermark — is refused here too: the derivation
    never launders a counter into an identity.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise MissingEnvelopeFieldError(
            f"execution identity derivation requires a non-empty run_id, got {run_id!r}"
        )
    if isinstance(attempt_ordinal, bool) or not isinstance(attempt_ordinal, int):
        raise CompositionBoundaryError(
            "execution identity derivation requires an int attempt_ordinal (the durable "
            f"attempt counter), got {type(attempt_ordinal).__name__} {attempt_ordinal!r}"
        )
    if attempt_ordinal < 0:
        raise CompositionBoundaryError(
            f"execution identity derivation requires attempt_ordinal >= 0, got {attempt_ordinal}"
        )
    oid = str(source_base_oid if source_base_oid is not None else "").strip()
    if not oid:
        raise MissingEnvelopeFieldError(
            "execution identity derivation requires a non-empty source_base_oid"
        )
    if _is_counter_shaped(oid):
        raise CompositionBoundaryError(
            f"source axis carries {oid!r} — a checkpoint sequence (or applied-command "
            "watermark) is not a source base OID, and the execution identity is never "
            "derived from a counter"
        )
    return canonical_json_digest(
        {
            "run_id": run_id,
            "attempt_ordinal": attempt_ordinal,
            "source_base_oid": oid,
        }
    )


def _identity_family(identity: str) -> str | None:
    """The family prefix of a family-qualified identity, if any.

    Identities may be bare (``conn-42``) or family-qualified
    (``github:owner/repo`` — the convention the gateways and findings
    ingestion already write). A qualified identity carries its family in
    the segment before the first ``:``; only KNOWN families count as a
    qualification (``docker:pg17`` is a bare identity that happens to
    contain a colon, not a cross-family claim).
    """
    if ":" not in identity:
        return None
    prefix = identity.split(":", 1)[0]
    return prefix if prefix in PROVIDER_FAMILIES else None


@dataclass(frozen=True)
class RepositoryContext:
    """Provider family + connection identity + repository identity (ADR-0029 §1).

    ONE immutable value: the three members answer three different
    questions (which provider family owns the subject, which connection
    authenticates, which repository inside that connection), and none of
    them may substitute for a run id or an attempt oid. Cross-family
    mixing — a family-qualified identity whose family disagrees with
    ``provider_family`` — is refused at construction, because that is
    precisely the moment the mistake is still cheap.
    """

    #: One of :data:`PROVIDER_FAMILIES` (the ``FlowRun.provider`` values).
    provider_family: str
    #: The connection identity — bare or family-qualified
    #: (``github:owner/repo`` / ``azure_devops:org:project``).
    connection_id: str
    #: The repository identity within that connection.
    repository_id: str

    def __post_init__(self) -> None:
        family = self.provider_family
        if not isinstance(family, str) or not family.strip():
            raise MissingEnvelopeFieldError(
                "RepositoryContext field 'provider_family' must be a non-empty string, "
                f"got {family!r}"
            )
        if family not in PROVIDER_FAMILIES:
            raise CompositionBoundaryError(
                f"RepositoryContext field 'provider_family' is not a known provider family "
                f"(one of {PROVIDER_FAMILIES}), got {family!r}"
            )
        object.__setattr__(
            self,
            "connection_id",
            _required_text(self.connection_id, "connection_id", "RepositoryContext"),
        )
        object.__setattr__(
            self,
            "repository_id",
            _required_text(self.repository_id, "repository_id", "RepositoryContext"),
        )
        for name in ("connection_id", "repository_id"):
            identity = getattr(self, name)
            qualified = _identity_family(identity)
            if qualified is not None and qualified != family:
                raise CompositionBoundaryError(
                    f"RepositoryContext field {name!r} is cross-family mixed: identity "
                    f"{identity!r} declares family {qualified!r} but the context is "
                    f"{family!r}"
                )

    def subject_key(self) -> str:
        """The comparable repository identity: ``family:connection:repository``.

        The one string two contexts must agree on for a dispatch to be
        about the same subject — :func:`assert_attempt_start` compares
        exactly this.
        """
        return f"{self.provider_family}:{self.connection_id}:{self.repository_id}"

    def to_document(self) -> dict:
        """The canonical JSON document (the digest target)."""
        return {
            "provider_family": self.provider_family,
            "connection_id": self.connection_id,
            "repository_id": self.repository_id,
        }

    def digest(self) -> str:
        """sha256 over the canonical document — deterministic by value."""
        return canonical_json_digest(self.to_document())


@dataclass(frozen=True)
class ResumeSpec:
    """The exact approved resume decision (NEXT-03, formalized — ADR-0029 §1).

    The concept the five docstrings name: a resume binds to ONE exact
    checkpoint — ``checkpoint_ref`` as the durable
    ``<work_id>@<checkpoint_id>`` reference — never "whatever is latest
    now". ``generation`` is the checkpoint sequence (R28-06: arrival
    order is not authority), ``authority`` is the durable control-command
    identity that decided the resume (the audit four-facts rule: who
    decided must survive its own acknowledgement), and ``source_oid`` is
    the manifest's declared base identity.
    """

    #: One of :data:`RESUME_MODES` — the three DISTINCT dispatch modes.
    resume_mode: str
    #: ``<work_id>@<checkpoint_id>`` — required for ``required``, forbidden
    #: for ``fresh``/``restart`` (the modes are distinct shapes, not hints).
    checkpoint_ref: str = ""
    #: The checkpoint sequence — the generation being restored; >= 1 when a
    #: reference is present, 0 only without one.
    generation: int = 0
    #: The declared base identity from the checkpoint manifest ("" when the
    #: manifest carried none — the spec degrades to the ref, as wiring does).
    source_oid: str = ""
    #: The durable authority behind the resume — the control-command id
    #: (``cmd-<hex>``) or the dispatch surface (``lane:<work_id>``).
    authority: str = ""

    def __post_init__(self) -> None:
        mode = self.resume_mode
        if not isinstance(mode, str) or not mode.strip():
            raise MissingEnvelopeFieldError(
                f"ResumeSpec field 'resume_mode' must be a non-empty string, got {mode!r}"
            )
        if mode not in RESUME_MODES:
            raise CompositionBoundaryError(
                f"ResumeSpec field 'resume_mode' is an unknown resume mode "
                f"(one of {RESUME_MODES}), got {mode!r}"
            )
        if not isinstance(self.generation, int) or isinstance(self.generation, bool):
            raise CompositionBoundaryError(
                f"ResumeSpec field 'generation' must be an int, got {self.generation!r}"
            )
        for name in ("checkpoint_ref", "source_oid", "authority"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise MissingEnvelopeFieldError(
                    f"ResumeSpec field {name!r} must be a string, got {value!r}"
                )
        if mode == "required":
            if not self.checkpoint_ref.strip():
                raise MissingEnvelopeFieldError(
                    "ResumeSpec field 'checkpoint_ref' is required for resume mode "
                    "'required' — the exact approved checkpoint, never a latest lookup"
                )
            self._check_ref(self.checkpoint_ref)
            if self.generation < 1:
                raise CompositionBoundaryError(
                    "ResumeSpec field 'generation' must be >= 1 when a checkpoint_ref is "
                    f"present, got {self.generation}"
                )
        else:
            if self.checkpoint_ref.strip():
                raise CompositionBoundaryError(
                    f"ResumeSpec field 'checkpoint_ref' must be empty for resume mode "
                    f"{mode!r} — {mode} discards or never had WIP, so an exact checkpoint "
                    "reference is a contradiction"
                )
            if self.generation != 0:
                raise CompositionBoundaryError(
                    f"ResumeSpec field 'generation' must be 0 for resume mode {mode!r} "
                    f"(no checkpoint), got {self.generation}"
                )
        if not self.authority.strip():
            raise MissingEnvelopeFieldError(
                "ResumeSpec field 'authority' is required — the durable control-command "
                "identity that decided this resume (never an anonymous restart)"
            )

    @staticmethod
    def _check_ref(ref: str) -> None:
        """``<work_id>@<checkpoint_id>`` with a hex64 checkpoint id."""
        if "@" not in ref:
            raise CompositionBoundaryError(
                f"ResumeSpec checkpoint_ref {ref!r} is not a durable "
                "'<work_id>@<checkpoint_id>' reference"
            )
        work, checkpoint_id = ref.split("@", 1)
        if not work.strip() or not _is_sha256(checkpoint_id):
            raise CompositionBoundaryError(
                f"ResumeSpec checkpoint_ref {ref!r} is malformed — the checkpoint id must "
                "be the hex64 content address"
            )

    def to_payload(self) -> dict:
        """The NEXT-03 control-command payload document."""
        payload: dict = {
            "resume_mode": self.resume_mode,
            "generation": self.generation,
            "authority": self.authority,
        }
        if self.checkpoint_ref:
            payload["checkpoint_ref"] = self.checkpoint_ref
        if self.source_oid:
            payload["source_oid"] = self.source_oid
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> ResumeSpec:
        """Parse a durable resume-command payload; refuses the pre-NEXT-03 shape.

        A payload without a ``resume_mode`` is the pre-NEXT-03 command
        (or not a resume spec at all): refused with the honest reason,
        never silently re-read as a fresh resume — the caller routes it
        to the LABELLED legacy fallback instead
        (:mod:`forge.adaptive.compat_fixtures` owns that document).
        """
        if not isinstance(payload, dict) or not payload.get("resume_mode"):
            raise CompositionBoundaryError(
                "resume payload carries no 'resume_mode' — a pre-NEXT-03 resume command "
                "(or not a ResumeSpec); parse it as the labelled legacy fallback instead"
            )
        try:
            return cls(
                resume_mode=str(payload["resume_mode"]),
                checkpoint_ref=str(payload.get("checkpoint_ref") or ""),
                generation=int(payload.get("generation") or 0),
                source_oid=str(payload.get("source_oid") or ""),
                authority=str(payload.get("authority") or ""),
            )
        except (ValueError, TypeError) as exc:
            # int() garbage etc. — still a boundary refusal, never a crash
            # past the parse.
            raise CompositionBoundaryError(f"resume payload is unreadable: {exc}") from exc


@dataclass(frozen=True)
class AttemptStartSpec:
    """The frozen pre-effect dispatch envelope (ADR-0028 §2 as a type; ADR-0029 §1).

    Every dispatch entry constructs this BEFORE any external effect: the
    intent-first identity (run, execution attempt, repository context), the
    approved execution contract (the A18 profile digest the lane must
    echo), the resume mode and the held ``execution_leases`` slot. There
    are NO defaults — a missing member is a :class:`MissingEnvelopeFieldError`
    naming the field, because guessing here is the identity-interchange
    defect class (ADR-0029's context).

    Envelope v2 (R36-06, issue #265) separates the two identities v1
    conflated. The fields split by what they identify:

    ========================  ===============================================
    field                     what it identifies
    ========================  ===============================================
    ``run_id``                the LOGICAL INTENT — which run this
                              dispatch executes
    ``execution_attempt_id``  WHICH EXECUTION of that intent — the durable
                              id :func:`derive_execution_attempt_id`
                              returns; two attempts from the same source
                              commit differ here
    ``source_base_oid``       the CODE REVISION the lane credential is
                              minted for (what ``attempt_id`` held in v1)
                              — a source axis, never a process identity
    ``authority_epoch``       the authority axis (the cancellation epoch
                              the lane credential is scoped by), INSIDE
                              the envelope digest
    ``continuation_ref_digest``
                              the exact pinned continuation identity when
                              the attempt resumes WIP (resume mode
                              ``required``); empty for ``fresh``/
                              ``restart``
    ``lease_id``              the held execution slot — the occupancy
                              fact of THIS attempt
    ========================  ===============================================

    INTENT vs REPEAT DELIVERY: a repeated delivery of the same persisted
    start intent (the reconciler's re-drive, a stranding recovery, a
    restart between envelope construction and the native start)
    reconstructs IDENTICAL authority-bearing members, because every one
    of them derives from durable rows resolved once — the execution id
    from (run id, attempt ordinal, source base), the epoch from the run
    row, the lease from the run's open slot, the continuation ref from
    the persisted decision. A serialization-order change or a process
    restart is therefore never misread as a new execution.
    """

    #: The run identity (``FlowRun.id``) — the logical intent.
    run_id: str
    #: The EXECUTION identity — the durable derived id, NEVER the source
    #: OID and never the run id. Hex64 under schema v2; empty ONLY on a
    #: v1 compat read (the weaker identity is recorded, never
    #: manufactured).
    execution_attempt_id: str
    #: The source base OID — the code revision this attempt executes
    #: from (what ``attempt_id`` held in v1). A revision is not a process
    #: identity: two executions may share it.
    source_base_oid: str
    #: The repository context this dispatch executes against.
    repository: RepositoryContext
    #: The frozen execution profile digest (A18) — sha256, always present.
    profile_digest: str
    #: One of :data:`RESUME_MODES`.
    resume_mode: str
    #: The held execution lease identity (migration 023) — ``<slot-key>#<row>``
    #: or the lease row id; never empty.
    lease_id: str
    #: The authority axis (the cancellation epoch the lane credential is
    #: scoped by) — a non-negative int, INSIDE the envelope digest (v2).
    authority_epoch: int
    #: The canonical digest of the exact continuation identity when this
    #: attempt resumes WIP (the pinned checkpoint content address,
    #: ``continuation.checkpoint_digest``); empty for ``fresh`` and
    #: ``restart`` — neither resumes WIP, so neither may pin a ref.
    continuation_ref_digest: str
    #: The envelope schema version (one of :data:`ATTEMPT_START_SCHEMA_VERSIONS`).
    schema_version: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "run_id", _required_text(self.run_id, "run_id", "AttemptStart envelope")
        )
        object.__setattr__(
            self,
            "source_base_oid",
            _required_text(self.source_base_oid, "source_base_oid", "AttemptStart envelope"),
        )
        if _is_counter_shaped(self.source_base_oid):
            raise CompositionBoundaryError(
                f"AttemptStart envelope field 'source_base_oid' carries "
                f"{self.source_base_oid!r} — a checkpoint sequence (or applied-command "
                "watermark) is not a source base OID; the sequence belongs only inside a "
                "ResumeSpec"
            )
        if self.schema_version not in ATTEMPT_START_SCHEMA_VERSIONS:
            raise CompositionBoundaryError(
                "AttemptStart envelope field 'schema_version' must be one of "
                f"{ATTEMPT_START_SCHEMA_VERSIONS}, got {self.schema_version!r}"
            )
        execution_id = str(
            self.execution_attempt_id if self.execution_attempt_id is not None else ""
        )
        if self.schema_version == ATTEMPT_START_V2:
            if not execution_id.strip():
                raise MissingEnvelopeFieldError(
                    "AttemptStart envelope field 'execution_attempt_id' is required under "
                    "schema version 2 — derive it with derive_execution_attempt_id()"
                )
            if not _is_sha256(execution_id):
                raise CompositionBoundaryError(
                    "AttemptStart envelope field 'execution_attempt_id' is not the hex64 "
                    "durable identity derive_execution_attempt_id() returns, got "
                    f"{execution_id[:16]!r}"
                )
        elif execution_id:
            # v1 compat read: the weaker identity is RECORDED, never
            # manufactured into the strong shape.
            raise CompositionBoundaryError(
                "AttemptStart envelope field 'execution_attempt_id' must be empty under "
                "schema version 1 — a v1 envelope carries no execution identity and never "
                "invents one (read it through the legacy adapter instead)"
            )
        object.__setattr__(self, "execution_attempt_id", execution_id)
        if not isinstance(self.repository, RepositoryContext):
            raise MissingEnvelopeFieldError(
                "AttemptStart envelope field 'repository' must be a RepositoryContext, "
                f"got {type(self.repository).__name__}"
            )
        digest = _required_text(self.profile_digest, "profile_digest", "AttemptStart envelope")
        if not _is_sha256(digest):
            raise CompositionBoundaryError(
                "AttemptStart envelope field 'profile_digest' is not a sha256 digest, "
                f"got {digest[:16]!r}"
            )
        mode = _required_text(self.resume_mode, "resume_mode", "AttemptStart envelope")
        if mode not in RESUME_MODES:
            raise CompositionBoundaryError(
                f"AttemptStart envelope field 'resume_mode' is an unknown resume mode "
                f"(one of {RESUME_MODES}), got {mode!r}"
            )
        object.__setattr__(
            self, "lease_id", _required_text(self.lease_id, "lease_id", "AttemptStart envelope")
        )
        if isinstance(self.authority_epoch, bool) or not isinstance(self.authority_epoch, int):
            raise CompositionBoundaryError(
                "AttemptStart envelope field 'authority_epoch' must be an int, got "
                f"{type(self.authority_epoch).__name__} {self.authority_epoch!r} — the "
                "authority axis is never a checkpoint sequence string or a bool"
            )
        if self.authority_epoch < 0:
            raise CompositionBoundaryError(
                "AttemptStart envelope field 'authority_epoch' must be >= 0, got "
                f"{self.authority_epoch}"
            )
        ref = str(self.continuation_ref_digest if self.continuation_ref_digest is not None else "")
        if ref and not _is_sha256(ref):
            raise CompositionBoundaryError(
                "AttemptStart envelope field 'continuation_ref_digest' must be the hex64 "
                f"content address of the exact continuation identity, got {ref[:16]!r}"
            )
        if mode == "required" and not ref:
            raise MissingEnvelopeFieldError(
                "AttemptStart envelope field 'continuation_ref_digest' is required for "
                "resume mode 'required' — the exact committed checkpoint this attempt "
                "resumes, never a latest lookup"
            )
        if mode != "required" and ref:
            raise CompositionBoundaryError(
                f"AttemptStart envelope field 'continuation_ref_digest' must be empty for "
                f"resume mode {mode!r} — {mode} never had or deliberately discards WIP, so "
                "a pinned continuation ref is a contradiction"
            )
        object.__setattr__(self, "continuation_ref_digest", ref)
        if self.run_id == self.execution_attempt_id or self.run_id == self.source_base_oid:
            raise CompositionBoundaryError(
                "AttemptStart envelope fields 'run_id' and the attempt/source identities "
                f"are identical ({self.run_id!r}) — run and attempt identities are never "
                "interchangeable"
            )
        if (
            self.schema_version == ATTEMPT_START_V2
            and self.execution_attempt_id == self.source_base_oid
        ):
            raise CompositionBoundaryError(
                "AttemptStart envelope fields 'execution_attempt_id' and 'source_base_oid' "
                f"are identical ({self.execution_attempt_id!r}) — an execution identity is "
                "never a code revision (R36-06: two executions may share one source)"
            )

    def to_document(self) -> dict:
        """The canonical JSON document (the envelope digest target).

        Versioned: schema v1 serializes the HISTORICAL shape —
        ``attempt_id`` carrying the source OID, authority epoch absent —
        so a compat-read envelope recomputes exactly the digest its era
        produced; schema v2 serializes every authority-bearing member,
        making digest equality the documented equivalence (see
        :meth:`envelope_digest`).
        """
        if self.schema_version == ATTEMPT_START_V1:
            return {
                "run_id": self.run_id,
                "attempt_id": self.source_base_oid,
                "repository": self.repository.to_document(),
                "profile_digest": self.profile_digest,
                "resume_mode": self.resume_mode,
                "lease_id": self.lease_id,
            }
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "execution_attempt_id": self.execution_attempt_id,
            "source_base_oid": self.source_base_oid,
            "repository": self.repository.to_document(),
            "profile_digest": self.profile_digest,
            "resume_mode": self.resume_mode,
            "lease_id": self.lease_id,
            "authority_epoch": self.authority_epoch,
            "continuation_ref_digest": self.continuation_ref_digest,
        }

    def envelope_digest(self) -> str:
        """sha256 over the canonical document — deterministic by value.

        DIGEST SEMANTICS (R36-06): equality means the documented
        equivalence — ALL authority-bearing members equal: same run,
        same execution identity, same source base, same repository, same
        profile, same resume mode, same lease, same authority epoch and
        same pinned continuation identity. Under v1 the epoch rode BESIDE
        the digest, so an unchanged digest did not imply unchanged
        authority; under v2 it does. A source-OID match alone never
        establishes digest equality — the execution identity is a member.
        """
        return canonical_json_digest(self.to_document())


def assert_attempt_start(spec: AttemptStartSpec, context: RepositoryContext) -> None:
    """The pre-effect boundary check every adapter MUST call before dispatch.

    One function, four refuses, each naming the exact field:

    - the repository identity matches — ``spec.repository.subject_key()``
      equals ``context.subject_key()`` (family, connection AND repository;
      a dispatch about another subject is the interchange defect itself);
    - the profile digest is present (the A18 approved-vs-executed pairing
      needs a comparable member on both sides);
    - the lease identity is present (no dispatch without a held slot —
      migration 023's discipline at the composition boundary);
    - the execution identity is coherent with its schema version
      (R36-06): a v2 envelope carries the hex64 durable execution id and
      never conflates it with the source OID; a v1 envelope carries NO
      execution id (the weaker identity is recorded, never manufactured).

    Raises :class:`CompositionBoundaryError` on violation; returns
    ``None`` when the envelope may cross the boundary. Construction
    already refuses most of these — the assert exists because the
    boundary is what an adapter can forget, and defense in depth at a
    pre-effect checkpoint is the one place it is free.
    """
    if not isinstance(spec, AttemptStartSpec):
        raise CompositionBoundaryError(
            f"assert_attempt_start field 'spec' must be an AttemptStartSpec, "
            f"got {type(spec).__name__}"
        )
    if not isinstance(context, RepositoryContext):
        raise CompositionBoundaryError(
            f"assert_attempt_start field 'context' must be a RepositoryContext, "
            f"got {type(context).__name__}"
        )
    if spec.repository.subject_key() != context.subject_key():
        raise CompositionBoundaryError(
            "AttemptStart envelope field 'repository' does not match the dispatch "
            f"context: spec {spec.repository.subject_key()!r} vs context "
            f"{context.subject_key()!r}"
        )
    if not spec.profile_digest.strip():
        raise CompositionBoundaryError(
            "AttemptStart envelope field 'profile_digest' is empty — the A18 approved-"
            "vs-executed pairing cannot happen without it"
        )
    if not spec.lease_id.strip():
        raise CompositionBoundaryError(
            "AttemptStart envelope field 'lease_id' is empty — no dispatch without a "
            "held execution lease (migration 023)"
        )
    if spec.schema_version == ATTEMPT_START_V2:
        if not _is_sha256(spec.execution_attempt_id):
            raise CompositionBoundaryError(
                "AttemptStart envelope field 'execution_attempt_id' is not the hex64 "
                "durable identity — a v2 dispatch never crosses the boundary with a "
                "missing or malformed execution identity (R36-06)"
            )
        if spec.execution_attempt_id == spec.source_base_oid:
            raise CompositionBoundaryError(
                "AttemptStart envelope fields 'execution_attempt_id' and "
                "'source_base_oid' are identical — an execution identity is never a "
                "code revision (R36-06)"
            )
    elif spec.execution_attempt_id:
        raise CompositionBoundaryError(
            "AttemptStart envelope field 'execution_attempt_id' must be empty on a v1 "
            "envelope — the legacy shape records the weaker identity instead of "
            "manufacturing the strong one"
        )


def assert_publication_identity(
    *,
    persisted: Mapping[str, Any] | None,
    presented_execution_attempt_id: str,
    presented_source_base_oid: str = "",
    presented_authority_epoch: int | None = None,
) -> None:
    """The publication-entry identity check (R36-06, issue #265).

    A callback/publication claiming to come from an attempt is
    authorized ONLY by its EXECUTION identity matching the persisted
    envelope's — a source-OID match alone NEVER authorizes, because two
    executions can start from the same commit. The wiring point:
    ``GitHubRunService._publish_harness_candidate`` (and the
    waiting-harness reconciler tick feeding it) calls this with the
    run's persisted ``attempt_start`` document and the identity the
    arriving outcome carries, BEFORE any publication effect — the
    ``attempt.identity_mismatch`` / ``authority.epoch_mismatch`` /
    legacy refusals below are the log-worthy verdicts.

    Refuses (each naming what was compared):

    - no presented execution identity — nothing to authorize with;
    - no persisted envelope at all — a pre-Q35-07 run whose identity
      cannot be verified fail-closed rather than guessed;
    - a v1 persisted envelope (``execution_attempt_id`` absent) — the
      weaker identity is readable for audit but refused for
      authority-bearing comparisons: an old envelope never claims the
      new guarantee;
    - an execution-identity mismatch — the stale callback from the
      preceding attempt, even when the source OIDs match (the message
      says exactly that when they do);
    - an authority-epoch mismatch (when the caller presents one).
    """
    presented = str(presented_execution_attempt_id or "").strip()
    if not presented:
        raise CompositionBoundaryError(
            "attempt.identity_mismatch: the callback presents no execution_attempt_id — "
            "publication cannot be authorized without an execution identity (R36-06)"
        )
    if not isinstance(persisted, Mapping):
        raise CompositionBoundaryError(
            "attempt.identity_mismatch: no persisted attempt_start envelope to verify "
            "the callback's execution identity against — a run without a composed "
            "envelope publishes nothing through this boundary (fail-closed, R36-06)"
        )
    current = str(persisted.get("execution_attempt_id") or "").strip()
    if not current:
        raise CompositionBoundaryError(
            "attempt.identity_mismatch: the persisted attempt_start envelope is version 1 "
            "— its weaker identity (source OID only) is readable for audit but refused "
            "for authority-bearing comparisons; it cannot authorize a callback without "
            "claiming a guarantee it never made (R36-06)"
        )
    if presented != current:
        note = ""
        presented_oid = str(presented_source_base_oid or "").strip()
        persisted_oid = str(persisted.get("source_base_oid") or persisted.get("attempt_base") or "")
        if presented_oid and presented_oid == persisted_oid:
            note = (
                f" — the source_base_oid matches ({persisted_oid[:12]}…), and a source-OID "
                "match alone never authorizes a callback: two executions can start from "
                "the same commit"
            )
        raise CompositionBoundaryError(
            f"attempt.identity_mismatch: the callback's execution identity "
            f"{presented[:12]}… is not the current attempt's {current[:12]}…{note} (R36-06)"
        )
    if presented_authority_epoch is not None:
        persisted_epoch = persisted.get("authority_epoch")
        if (
            isinstance(persisted_epoch, bool)
            or not isinstance(persisted_epoch, int)
            or presented_authority_epoch != persisted_epoch
        ):
            raise CompositionBoundaryError(
                f"authority.epoch_mismatch: the callback's authority epoch "
                f"{presented_authority_epoch!r} is not the envelope's "
                f"{persisted_epoch!r} — the lane credential this envelope scoped is "
                "retired (R36-06)"
            )


@dataclass(frozen=True)
class MatrixEdge:
    """One directional composition edge: ``from_key`` depends on ``to_key``.

    ``verdict`` is one of :data:`EDGE_VERDICTS`; ``evidence`` is the
    promotion/CI record id a ``verified`` edge MUST carry — the audit
    trail that makes a compatibility claim checkable instead of
    remembered.
    """

    from_key: str
    to_key: str
    verdict: str
    evidence: str = ""

    def __post_init__(self) -> None:
        for name in ("from_key", "to_key"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise MissingEnvelopeFieldError(
                    f"MatrixEdge field {name!r} must be a non-empty string"
                )
        if self.verdict not in EDGE_VERDICTS:
            raise CompositionBoundaryError(
                f"MatrixEdge field 'verdict' must be one of {EDGE_VERDICTS}, got {self.verdict!r}"
            )
        if self.verdict == EDGE_VERIFIED and not self.evidence.strip():
            raise CompositionBoundaryError(
                "a verified matrix edge requires an evidence reference "
                "(promotion/CI record id) — a compatibility claim without evidence is "
                "exactly the drift this matrix exists to prevent"
            )

    def names(self, key: str) -> bool:
        """Whether this edge touches *key* on either endpoint."""
        return key in (self.from_key, self.to_key)


class CompositionMatrix:
    """The recorded composition permission between boundary versions (R32-24).

    Directional edges over combination keys (``run_spec:v3``,
    ``checkpoint_channel:NEXT-03``, ``lane_driver:resume/required`` —
    the key vocabulary is the caller's). Three rules make it honest:

    - **verified needs evidence** — :meth:`register_verified` without a
      promotion/CI record id raises;
    - **unregistered is untested** — :meth:`edge` answers ``untested``
      for pairs nobody recorded (absence of evidence is a label, never
      a pass — the inventory-drift rule);
    - **a verified edge is never silently demoted** — overwriting one
      with ``untested``/``unsupported`` raises: qualification is lost
      only by moving to NEW version keys (expand-contract), never by an
      in-place flip.

    :meth:`blocked_by` is the can-i-deploy lookup — a QUERY over the
    recorded edges, never a re-run.
    """

    def __init__(self) -> None:
        self._edges: dict[tuple[str, str], MatrixEdge] = {}

    def register_untested(self, from_key: str, to_key: str) -> MatrixEdge:
        """Record a pair as honestly untested (the explicit form of the default)."""
        return self._register(MatrixEdge(from_key, to_key, EDGE_UNTESTED))

    def register_unsupported(self, from_key: str, to_key: str) -> MatrixEdge:
        """Record a pair as explicitly unsupported — a refusal, not a gap."""
        return self._register(MatrixEdge(from_key, to_key, EDGE_UNSUPPORTED))

    def register_verified(self, from_key: str, to_key: str, *, evidence: str) -> MatrixEdge:
        """Promote a pair to verified — ONLY with an evidence reference."""
        if not isinstance(evidence, str) or not evidence.strip():
            raise CompositionBoundaryError(
                "register_verified requires an 'evidence' reference (promotion/CI record "
                "id) — a verified edge without evidence cannot be recorded"
            )
        return self._register(MatrixEdge(from_key, to_key, EDGE_VERIFIED, evidence=evidence))

    def _register(self, edge: MatrixEdge) -> MatrixEdge:
        existing = self._edges.get((edge.from_key, edge.to_key))
        if (
            existing is not None
            and existing.verdict == EDGE_VERIFIED
            and (edge.verdict != EDGE_VERIFIED)
        ):
            raise CompositionBoundaryError(
                f"edge {edge.from_key!r} -> {edge.to_key!r} is verified "
                f"(evidence {existing.evidence!r}) and is never silently demoted to "
                f"{edge.verdict!r} — move to new version keys (expand-contract) "
                "instead of flipping qualification in place"
            )
        self._edges[(edge.from_key, edge.to_key)] = edge
        return edge

    def edge(self, from_key: str, to_key: str) -> str:
        """The verdict for the DIRECTIONAL pair — unregistered reads untested.

        Direction is load-bearing: ``edge(a, b)`` says nothing about
        ``edge(b, a)``; a consumer verified against a provider does not
        verify the provider against the consumer.
        """
        recorded = self._edges.get((from_key, to_key))
        return recorded.verdict if recorded is not None else EDGE_UNTESTED

    def edges(self) -> tuple[MatrixEdge, ...]:
        """Every recorded edge (audit view)."""
        return tuple(self._edges.values())

    def blocked_by(self, key: str) -> tuple[MatrixEdge, ...]:
        """The non-verified recorded edges touching *key* — can-i-deploy.

        The query, not a test run: every edge naming *key* on either
        endpoint whose verdict is not ``verified``. Read from the
        consumer side these are "your dependencies are unproven"; read
        from the provider side these are "these combinations have not
        verified against you" — the SAME edges found from both
        directions, so the gate stays consistent both ways (a consumer
        not verified against its provider is the provider's blocked-by
        finding too).
        """
        return tuple(
            edge
            for edge in self._edges.values()
            if edge.names(key) and edge.verdict != EDGE_VERIFIED
        )


@dataclass(frozen=True)
class DriftFinding:
    """One shipped-but-unqualified combination — inventory drift, as a finding.

    A capability that exists (shipped) but has no qualification evidence
    in scope reads as loss of control (the R32-24 rule) — it is listed,
    never silently passed.
    """

    combination: str
    detail: str
    edges: tuple[MatrixEdge, ...] = ()


def matrix_drift(
    shipped_combinations: Sequence[str], matrix: CompositionMatrix
) -> list[DriftFinding]:
    """Shipped combinations the matrix cannot qualify — the doctor check.

    For every shipped combination key: qualified means every RECORDED
    edge touching it is ``verified`` AND at least one edge names it (an
    unknown key has no qualification evidence in scope at all). Anything
    else is a finding — the explicit surface for combinations that
    shipped ahead of their evidence.
    """
    findings: list[DriftFinding] = []
    for combination in shipped_combinations:
        touching = [edge for edge in matrix.edges() if edge.names(combination)]
        if not touching:
            findings.append(
                DriftFinding(
                    combination=combination,
                    detail="shipped but absent from the composition matrix — no "
                    "qualification evidence in scope",
                )
            )
            continue
        unqualified = [edge for edge in touching if edge.verdict != EDGE_VERIFIED]
        if unqualified:
            worst = (
                "explicitly unsupported"
                if any(e.verdict == EDGE_UNSUPPORTED for e in unqualified)
                else "untested"
            )
            findings.append(
                DriftFinding(
                    combination=combination,
                    detail=f"shipped with {worst} composition edges",
                    edges=tuple(unqualified),
                )
            )
    return findings
