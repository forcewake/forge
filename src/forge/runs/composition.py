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
  construct BEFORE any external effect (run id, attempt oid, repository
  context, execution profile digest, resume mode, lease identity);
  permissive defaults are forbidden — a missing field raises
  :class:`MissingEnvelopeFieldError` (a ``TypeError``) naming the field;
- :class:`ResumeSpec` — the documented NEXT-03 concept formalized
  (resume mode, exact checkpoint reference, generation, authority);
- :class:`CompositionMatrix` — directional verified/untested/unsupported
  edges between boundary versions; ``blocked_by`` is the can-i-deploy
  QUERY over recorded edges (never a re-run), and
  :func:`matrix_drift` lists shipped-but-unqualified combinations as
  findings;
- :func:`assert_attempt_start` — the pre-effect boundary check an
  adapter MUST call before dispatch.

Import boundary (ADR-0027 §3, applied at creation): this module is core
— it imports no ``forge.integrations.*`` and no ``forge.gateway.*``.

No production caller yet (ADR-0029 §5): the services adopt these types
one dispatch entry at a time; until then the types ARE the contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from forge.runs.spec import canonical_json_digest

__all__ = [
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
    intent-first identity (run, attempt, repository context), the
    approved execution contract (the A18 profile digest the lane must
    echo), the resume mode and the held ``execution_leases`` slot. There
    are NO defaults — a missing member is a :class:`MissingEnvelopeFieldError`
    naming the field, because guessing here is the identity-interchange
    defect class (ADR-0029's context).
    """

    #: The run identity (``FlowRun.id``).
    run_id: str
    #: The attempt identity — the dispatch attempt oid, NEVER the run id.
    attempt_id: str
    #: The repository context this dispatch executes against.
    repository: RepositoryContext
    #: The frozen execution profile digest (A18) — sha256, always present.
    profile_digest: str
    #: One of :data:`RESUME_MODES`.
    resume_mode: str
    #: The held execution lease identity (migration 023) — ``<slot-key>#<row>``
    #: or the lease row id; never empty.
    lease_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "run_id", _required_text(self.run_id, "run_id", "AttemptStart envelope")
        )
        object.__setattr__(
            self,
            "attempt_id",
            _required_text(self.attempt_id, "attempt_id", "AttemptStart envelope"),
        )
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
        if self.run_id == self.attempt_id:
            raise CompositionBoundaryError(
                "AttemptStart envelope fields 'run_id' and 'attempt_id' are identical "
                f"({self.run_id!r}) — run and attempt identities are never interchangeable"
            )

    def to_document(self) -> dict:
        """The canonical JSON document (the envelope digest target)."""
        return {
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "repository": self.repository.to_document(),
            "profile_digest": self.profile_digest,
            "resume_mode": self.resume_mode,
            "lease_id": self.lease_id,
        }

    def envelope_digest(self) -> str:
        """sha256 over the canonical document — deterministic by value."""
        return canonical_json_digest(self.to_document())


def assert_attempt_start(spec: AttemptStartSpec, context: RepositoryContext) -> None:
    """The pre-effect boundary check every adapter MUST call before dispatch.

    One function, three refuses, each naming the exact field:

    - the repository identity matches — ``spec.repository.subject_key()``
      equals ``context.subject_key()`` (family, connection AND repository;
      a dispatch about another subject is the interchange defect itself);
    - the profile digest is present (the A18 approved-vs-executed pairing
      needs a comparable member on both sides);
    - the lease identity is present (no dispatch without a held slot —
      migration 023's discipline at the composition boundary).

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
