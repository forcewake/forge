"""Versioned compatibility fixtures for shipped document versions (R32-24, ADR-0029 §4).

The expand-contract rule needs a testable memory of every document
version that can still be read from durable storage: "add alongside,
migrate consumers visibly, remove only after verified-zero usage" is
only enforceable when the old shapes exist as FIXTURES with their
SUPPORTED read/recovery semantics written down. This module is that
registry for the composition boundaries' documents:

- ``run_spec`` v1 — the Stage B2 digest-only RunSpec (migration 006,
  2026-09-13, GitLab-only forge): digests + backend config, no provider
  in the subject, NOT executable. Recovery:
  ``blocked(spec_legacy: re-approval required)`` — the approver never
  saw executable content, so a legacy row is never re-interpreted as
  v3 (``forge.runs.spec.SpecLegacy``).
- ``run_spec`` v2 — the multi-provider digest-only document (migration
  012 era, ADR-0023 harness selection frozen into ``backend_config``):
  provider-qualified subject, still digest-only — the same SpecLegacy
  recovery.
- ``run_spec`` v3 — the executable document (R04/A02), parsed by
  :meth:`forge.runs.spec.ExecutableRunSpec.from_document`.
- ``checkpoint_metadata`` v1 — the pre-migration-026 per-work
  FILESYSTEM index (the best-effort durability contract's authority);
  recovery is the documented no-backfill rule: the index stays
  read-only authority for work uploaded under it, a deployment
  switching to the postgres contract re-uploads (checkpoints are
  content-addressed and idempotent).
- ``control_command`` v1 — a pre-NEXT-03 durable ``resume`` row: the
  payload is EMPTY (no ResumeSpec). Recovery is the labelled legacy
  fallback ``lane_driver._maybe_restore_wip`` documents — restore falls
  back to the ACTIVE checkpoint and the report SAYS so.
- ``control_command`` v2 — a NEXT-03 ``resume`` row whose payload is
  the exact ResumeSpec (``checkpoint_ref``/``checkpoint_sequence``/
  ``source_oid``).

R36-20 (issue #279) extends the inventory to the schemas the AUTHORITY
BOUNDARIES persist (ADR-0030):

- ``attempt_start`` v1 — the Q35-07 envelope evidence document: the
  attempt axis WAS the source OID (``attempt_base``) and the authority
  epoch rode BESIDE the envelope digest. Recovery: audit-only — no
  execution identity exists to recover and none is manufactured;
  authority-bearing comparisons against it are refused.
- ``attempt_start`` v2 — the R36-06 envelope: the derived
  ``execution_attempt_id``, the separated ``source_base_oid``, the
  epoch INSIDE the digest and the pinned ``continuation_ref_digest``.
- ``continuation_decision`` v1 — the Q35-02 decision document
  (mode/reason/decided_at/evidence_digest, no lineage). Recovery:
  digest-governed reuse still works; the lineage fields simply do not
  exist (pre-R36-02) — they are read as absent, never guessed.
- ``continuation_decision`` v2 — the R36-02/R36-03 document: decision
  lineage (originating attempt, native command id, intent verdict,
  discard authority), the pinned ``checkpoint_digest`` and the
  ``refusal_code`` observability key.
- ``checkpoint_lookup`` v1 — the RETIRED legacy opt-in lookup's lossy
  answer (``exact``/``absent`` only, authority
  ``legacy-http-opt-in`` — an outage collapsed to absent, exactly as
  the pre-R36-03 deployment behaved).
- ``checkpoint_lookup`` v2 — the typed five-state outcome
  (``exact``/``absent``/``unavailable``/``corrupt``/``unauthorized``)
  the configured authority answers; NOTHING collapses one into
  another.

:func:`load_compat_document` parses a payload under the SUPPORTED
semantics of its (kind, version) or raises
:class:`UnsupportedDocumentVersion` — a silent best-effort parse of an
unknown shape is forbidden. :func:`compat_inventory` lists the
kind×version surface so drift between shipped fixtures and the
composition matrix is detectable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "CompatAttemptStartDocument",
    "CompatCheckpointIndex",
    "CompatControlCommand",
    "CompatContinuationDocument",
    "CompatSpecDocument",
    "UnsupportedDocumentVersion",
    "compat_document",
    "compat_inventory",
    "load_compat_document",
]


class UnsupportedDocumentVersion(ValueError):
    """A document kind/version this code cannot read under a supported contract.

    Raised instead of a best-effort parse: an unknown document version
    is a boundary finding (the shipped/qualified drift this module
    exists to surface), never a silently degraded read.
    """


@dataclass(frozen=True)
class CompatSpecDocument:
    """The supported read of a legacy (v1/v2) RunSpec document.

    Digest-only: the digests and subject are readable, the document is
    NOT executable, and ``recovery`` names the honest action
    (``blocked(spec_legacy: re-approval required)`` — A02's policy).
    ``provider`` on a v1 document is ``gitlab`` BY CONSTRUCTION (forge
    started GitLab-only; ``FlowRun.provider``'s server default, and the
    v1 subject carried no provider key) — recorded provenance, not a
    guess.
    """

    kind: str
    schema_version: int
    provider: str
    provider_provenance: str
    project_id: int
    issue_iid: int | None
    plan_digest: str
    task_digest: str
    policy_digest: str
    source_base_oid: str
    executable: bool
    recovery: str


@dataclass(frozen=True)
class CompatCheckpointIndex:
    """The supported read of a pre-migration-026 checkpoint index document."""

    kind: str
    schema_version: int
    work_id: str
    #: Entries sorted by ``(sequence, checkpoint_id)`` — the promotion
    #: order R28-06 pins; the ACTIVE checkpoint is the last entry.
    checkpoints: tuple[dict, ...]
    recovery: str


@dataclass(frozen=True)
class CompatControlCommand:
    """The supported read of a durable control-command row (resume shape)."""

    kind: str
    schema_version: int
    command_id: str
    work_id: str
    kind_command: str
    actor_ref: str
    #: ``exact`` (v2: the payload carries the ResumeSpec reference) or
    #: ``legacy_active_fallback`` (v1: empty payload — the labelled
    #: fallback the restore records).
    selection: str
    checkpoint_ref: str
    checkpoint_sequence: int
    source_oid: str
    recovery: str


@dataclass(frozen=True)
class CompatAttemptStartDocument:
    """The supported read of a persisted ``attempt_start`` envelope document.

    v1 (Q35-07) is AUDIT-ONLY: ``execution_attempt_id`` is ``None``
    because the weaker identity never existed — never manufactured —
    and ``identity_strength`` says so. v2 (R36-06) carries the derived
    durable execution identity, the separated source base, the epoch
    INSIDE the envelope digest and the pinned continuation reference.
    """

    kind: str
    schema_version: int
    envelope_digest: str
    context_digest: str
    subject_key: str
    resume_mode: str
    #: v2 only — ``None`` on v1 (the missing fact, never a guess).
    execution_attempt_id: str | None
    source_base_oid: str
    authority_epoch: int | None
    attempt_ordinal: int | None
    continuation_ref_digest: str
    profile_source: str
    #: ``source_oid_only`` (v1) or ``execution_id_v2`` (v2).
    identity_strength: str
    recovery: str


@dataclass(frozen=True)
class CompatContinuationDocument:
    """The supported read of a persisted continuation decision document.

    v1 (Q35-02) carries the decision core (mode/reason/decided_at plus
    the objective evidence digest that governs reuse); v2 (R36-02/03)
    adds the decision lineage and the pinned ``checkpoint_digest``.
    """

    kind: str
    schema_version: int
    mode: str
    mode_selected: str
    reason: str
    decided_at: str
    evidence_digest: str
    uncertain: bool
    #: v2 only — ``None`` on v1 (pre-R36-02 documents carry no lineage;
    #: the fields are read as absent, never guessed).
    source_attempt: int | None
    native_command_id: str | None
    native_start_verdict: str | None
    discard_authorized_by: str | None
    #: v2 only (R36-03): the pinned exact-checkpoint content address.
    checkpoint_digest: str | None
    refusal_code: str | None
    recovery: str


_SPEC_LEGACY_RECOVERY = "blocked(spec_legacy: re-approval required)"

_CHECKPOINT_INDEX_RECOVERY = (
    "best-effort filesystem authority: read-only for work uploaded under it; "
    "a deployment switching to the postgres contract (migration 026) re-uploads "
    "(content-addressed, idempotent) — no backfill"
)

_CONTROL_COMMAND_V1_RECOVERY = (
    "legacy active-checkpoint fallback: no ResumeSpec in the payload, so the "
    "restore falls back to the ACTIVE checkpoint and the report says so "
    "(labelled legacy, never claimed exact)"
)

_CONTROL_COMMAND_V2_RECOVERY = (
    "exact binding: the payload's checkpoint_ref is the approved resume point — "
    "a newer upload landing later changes nothing"
)

_ATTEMPT_START_V1_RECOVERY = (
    "audit-only: the v1 envelope's identity was the source OID alone — no "
    "execution identity exists to recover and none is manufactured; "
    "authority-bearing comparisons against it are refused "
    "(assert_publication_identity), never guessed"
)

_ATTEMPT_START_V2_RECOVERY = (
    "full identity: the derived execution id, the separated source base, the "
    "cancellation epoch and the pinned continuation reference are all inside "
    "the envelope digest — digest equality is the same authorized EXECUTION"
)

_CONTINUATION_V1_RECOVERY = (
    "digest-governed reuse only: the objective evidence digest still governs "
    "reuse, but the document carries no decision lineage (pre-R36-02) — the "
    "lineage fields are read as absent, never guessed"
)

_CONTINUATION_V2_RECOVERY = (
    "lineage + pinned digest: the originating attempt, the native command "
    "identity, the intent verdict, the discard authority and the pinned "
    "checkpoint digest are all on the document — reuse keeps naming the "
    "event that originated the decision and the bytes it approved"
)

_CHECKPOINT_LOOKUP_V1_RECOVERY = (
    "legacy lossy answer: the opt-in chain answered exact/absent only — an "
    "outage or a refused credential collapsed to absent exactly as the "
    "pre-R36-03 deployment did (authority legacy-http-opt-in)"
)

_CHECKPOINT_LOOKUP_V2_RECOVERY = (
    "typed five-state outcome: exact carries the checkpoint's content "
    "address; absent/unavailable/corrupt/unauthorized carry their own "
    "operator-facing detail — NOTHING collapses one into another"
)


def _spec_v1_document() -> dict:
    """Stage B2 (migration 006, 2026-09-13): the digest-only GitLab RunSpec.

    Reconstructed from the commit-af72f98 ``_build_run_spec_document``:
    no provider key in the subject (forge was GitLab-only), no task
    text, no harness selection — the document the pending decision bound
    before R04 made specs executable.
    """
    return {
        "subject": {"project_id": 42, "issue_iid": 7},
        "source_base_oid": "a" * 40,
        "plan_digest": "b" * 64,
        "task_digest": "c" * 64,
        "policy_digest": "d" * 64,
        "backend_config": {
            "backend": "builtin",
            "model": "forge-standard",
            "target_branch": "main",
        },
        "budgets": {"commit_cycles": 3, "harness_timeout": 1800},
    }


def _spec_v2_document() -> dict:
    """The multi-provider digest-only document (pre-A02): provider-qualified.

    Reconstructed from the pre-R04 builders — GitLab's document grew
    ``allowed_paths`` (v0.7 monorepo scoping) and the frozen ADR-0023
    harness selection in ``backend_config``; the GitHub/AzDO twins froze
    a provider key in the subject. Still digest-only: no task text, no
    plan artifact, no model route, no verification contract — which is
    exactly why v3 refuses to execute one.
    """
    return {
        "subject": {
            "provider": "github",
            "repo_full_name": "acme/widgets",
            "project_id": 99183,
            "issue_iid": 12,
        },
        "source_base_oid": "e" * 40,
        "plan_digest": "1" * 64,
        "task_digest": "2" * 64,
        "policy_digest": "3" * 64,
        "backend_config": {
            "backend": "ci_harness",
            "model": "forge-standard",
            "target_branch": "main",
            "harness": "codex",
            "harness_fallbacks": ["claude"],
            "budget_class": "standard",
            "selection_reason": "default",
            "harness_workflow": "forge-lane.yml",
        },
        "budgets": {"commit_cycles": 3, "harness_timeout": 1800},
        "allowed_paths": ["src/**"],
    }


def _spec_v3_document() -> dict:
    """The executable v3 document — frozen through the real :meth:`freeze`."""
    from forge.runs.spec import ExecutableRunSpec

    return (
        ExecutableRunSpec.freeze(
            provider="gitlab",
            project_id=42,
            issue_iid=7,
            source_base_oid="f" * 40,
            task_title="Add a widget",
            task_description="Widgets make the app better.",
            plan_summary="Add the widget module plus tests.",
            plan_files_hint=["src/app/widgets.py"],
            plan_digest="4" * 64,
            model_route="forge-standard",
            policy_digest="5" * 64,
            required_jobs=["test"],
            backend="builtin",
            harness_model="forge-standard",
            target_branch="main",
            harness_driver="builtin",
            commit_cycles=3,
            harness_timeout=1800,
            profile_digest="6" * 64,
        )
    ).to_document()


def _checkpoint_index_v1_document() -> dict:
    """The pre-migration-026 per-work filesystem index (best-effort contract).

    The shape ``CheckpointStore._save_index`` writes: one JSON document
    per work, entries sorted by ``(sequence, checkpoint_id)``, the
    active checkpoint derived as the highest — never stored as a second
    authority.
    """
    return {
        "work_id": "run-77",
        "checkpoints": [
            {
                "checkpoint_id": "a" * 64,
                "sequence": 1,
                "files": 3,
                "uploaded_at": "2026-09-21T10:00:00+00:00",
            },
            {
                "checkpoint_id": "b" * 64,
                "sequence": 2,
                "files": 4,
                "uploaded_at": "2026-09-21T11:00:00+00:00",
            },
        ],
    }


def _control_command_v1_document() -> dict:
    """A pre-NEXT-03 durable ``resume`` row: the payload carries NOTHING.

    Reconstructed from the pre-e6846fc ``OperatorControlService.resume``
    — the command row was the resume decision, and the restore had to
    guess "whatever is latest", which NEXT-03 replaced with the exact
    binding. The recovery is the labelled fallback, kept honest.
    """
    return {
        "schema": "forge.proposal.control-command/1",
        "command_id": "cmd-legacyresume01",
        "work_id": "run-77",
        "sequence": 4,
        "kind": "resume",
        "actor_ref": "approver@acme",
        "actor_origin": "server_authenticated_human",
        "idempotency_key": "resume:run-77:1",
        "status": "applied",
        "payload": {},
    }


def _control_command_v2_document() -> dict:
    """A NEXT-03 ``resume`` row: the payload IS the exact ResumeSpec.

    The shape ``OperatorControlService._active_resume_spec`` freezes:
    the durable ``checkpoint_ref``, its ``checkpoint_sequence`` and the
    manifest's declared ``source_oid`` — read from the durable command
    ROW (never the pending queue) long after acknowledgement.
    """
    return {
        "schema": "forge.proposal.control-command/1",
        "command_id": "cmd-resume000002",
        "work_id": "run-77",
        "sequence": 5,
        "kind": "resume",
        "actor_ref": "approver@acme",
        "actor_origin": "server_authenticated_human",
        "idempotency_key": "resume:run-77:2",
        "status": "applied",
        "payload": {
            "checkpoint_ref": f"run-77@{'b' * 64}",
            "checkpoint_sequence": 2,
            "source_oid": "f" * 40,
        },
    }


def _attempt_start_v1_document() -> dict:
    """A Q35-07 envelope: identity WAS the source OID, epoch beside the digest.

    The shape ``compose_attempt_start`` persisted before the R36-06 v2
    extension — reconstructed from the v1 fields the compat adapter
    (:func:`forge.adaptive.composition_adoption.legacy_attempt_start_view`)
    still reads: ``attempt_base`` (the source OID on the attempt axis),
    ``authority_epoch`` stored BESIDE the envelope digest, no
    ``execution_attempt_id`` key at all.
    """
    return {
        "version": 1,
        "envelope_digest": "7" * 64,
        "context_digest": "8" * 64,
        "subject_key": "github:acme/widgets#99183",
        "resume_mode": "required",
        "attempt_base": "e" * 40,
        "authority_epoch": 3,
        "profile_source": "execution_profile",
        "legacy": True,
    }


def _attempt_start_v2_document() -> dict:
    """An R36-06 envelope: the separated identity axes, epoch in the digest.

    The shape today's ``compose_attempt_start`` persists on the run's
    evidence — the derived durable ``execution_attempt_id`` (hex64 over
    run id + attempt ordinal + source base), the source base on its own
    axis, the cancellation epoch INSIDE the digest and the pinned
    ``continuation_ref_digest`` a ``required`` resume approved.
    """
    return {
        "version": 2,
        "envelope_digest": "9" * 64,
        "context_digest": "a" * 64,
        "subject_key": "github:acme/widgets#99183",
        "resume_mode": "required",
        "attempt_base": "e" * 40,
        "execution_attempt_id": "b" * 64,
        "source_base_oid": "e" * 40,
        "attempt_ordinal": 3,
        "authority_epoch": 3,
        "continuation_ref_digest": "c" * 64,
        "profile_source": "execution_profile",
        "legacy": False,
    }


def _continuation_v1_document() -> dict:
    """A Q35-02 decision: the core decision, no lineage (pre-R36-02).

    The shape ``decide_continuation().as_document()`` wrote before the
    R36-02 lineage extension — mode/reason/decided_at plus the
    objective evidence digest that governs reuse; the lineage keys and
    ``checkpoint_digest`` did not exist yet.
    """
    return {
        "mode": "uncertain",
        "mode_selected": "uncertain",
        "reason": (
            "no proof whether a vendor session started and no committed checkpoint "
            "is held — the recoverable state is unknown"
        ),
        "decided_at": "2026-09-22T09:15:00+00:00",
        "evidence_digest": "d" * 64,
        "uncertain": True,
        "vendor_started": None,
        "checkpoint_committed": None,
        "candidate_published": False,
        "operator_discard_requested": False,
        "prior_mode_selected": None,
        "no_checkpoint_baseline": False,
    }


def _continuation_v2_document() -> dict:
    """An R36-02/R36-03 decision: lineage and the pinned checkpoint digest.

    Today's document — the originating attempt's durable generation,
    the native command/event identity, the persisted native-start
    intent verdict, the discard authority, the PINNED exact-checkpoint
    content address (R36-03) and the typed ``refusal_code`` an R36-02
    refusal annotation records beside the decision.
    """
    return {
        "mode": "required",
        "mode_selected": "required",
        "reason": (
            "a committed checkpoint exists — the exact WIP checkpoint is the "
            "authorized continuation (the lane's required restore)"
        ),
        "decided_at": "2026-09-23T14:02:00+00:00",
        "evidence_version": 2,
        "evidence_digest": "e" * 64,
        "uncertain": False,
        "vendor_started": True,
        "checkpoint_committed": True,
        "candidate_published": False,
        "operator_discard_requested": False,
        "prior_mode_selected": None,
        "source_attempt": 3,
        "native_command_id": "gh-delivery-9f2c1a",
        "native_start_verdict": "dispatched",
        "checkpoint_digest": "c" * 64,
        "discard_authorized_by": None,
        "no_checkpoint_baseline": False,
        "refusal_code": None,
    }


def _checkpoint_lookup_v1_document() -> dict:
    """The RETIRED legacy opt-in lookup's answer: exact/absent, nothing else.

    The observable shape ``revival._legacy_http_lookup`` produces — the
    lossy boolean it always was: an outage, a refused credential or
    "rotted bytes" ALL collapsed into absent, labeled with the
    ``legacy-http-opt-in`` authority so nobody mistakes it for the
    configured one.
    """
    return {
        "state": "absent",
        "checkpoint_id": None,
        "digest": None,
        "authority": "legacy-http-opt-in",
        "detail": ("the legacy opt-in lookup answered no checkpoint (collapsed, as it always did)"),
    }


def _checkpoint_lookup_v2_document() -> dict:
    """The typed five-state outcome the configured authority answers.

    The observable shape of
    :class:`forge.adaptive.checkpoint_repository.CheckpointLookupOutcome`
    (the ``checkpoint.lookup.outcome`` spelling): ``exact`` carries the
    checkpoint's content address; every other state carries its own
    operator-facing detail and the authority that answered.
    """
    return {
        "state": "unavailable",
        "checkpoint_id": None,
        "digest": None,
        "authority": "postgres",
        "detail": "the checkpoint authority could not be reached (database outage)",
    }


@dataclass(frozen=True)
class CompatFixture:
    """One registered fixture: a canned document + its supported semantics."""

    kind: str
    schema_version: int
    description: str
    factory: Callable[[], dict]


#: The registry — kind×version → the canned document factory. This IS the
#: shipped-versions inventory; removing an entry is the verified-zero-usage
#: checkpoint the composition matrix must be able to show first
#: (ADR-0029 §3).
_FIXTURES: dict[tuple[str, int], CompatFixture] = {
    ("run_spec", 1): CompatFixture(
        kind="run_spec",
        schema_version=1,
        description="Stage B2 digest-only RunSpec (migration 006) — GitLab-only, "
        "not executable; SpecLegacy recovery",
        factory=_spec_v1_document,
    ),
    ("run_spec", 2): CompatFixture(
        kind="run_spec",
        schema_version=2,
        description="multi-provider digest-only RunSpec (pre-A02) — provider-qualified "
        "subject, ADR-0023 harness selection; SpecLegacy recovery",
        factory=_spec_v2_document,
    ),
    ("run_spec", 3): CompatFixture(
        kind="run_spec",
        schema_version=3,
        description="executable RunSpec v3 (R04/A02) — parses via ExecutableRunSpec.from_document",
        factory=_spec_v3_document,
    ),
    ("checkpoint_metadata", 1): CompatFixture(
        kind="checkpoint_metadata",
        schema_version=1,
        description="pre-migration-026 per-work filesystem checkpoint index "
        "(best-effort durability contract)",
        factory=_checkpoint_index_v1_document,
    ),
    ("control_command", 1): CompatFixture(
        kind="control_command",
        schema_version=1,
        description="pre-NEXT-03 durable resume command — empty payload, labelled "
        "active-checkpoint fallback",
        factory=_control_command_v1_document,
    ),
    ("control_command", 2): CompatFixture(
        kind="control_command",
        schema_version=2,
        description="NEXT-03 resume command — payload carries the exact ResumeSpec",
        factory=_control_command_v2_document,
    ),
    ("attempt_start", 1): CompatFixture(
        kind="attempt_start",
        schema_version=1,
        description="Q35-07 envelope evidence — identity was the source OID alone "
        "(audit-only; no execution identity to recover)",
        factory=_attempt_start_v1_document,
    ),
    ("attempt_start", 2): CompatFixture(
        kind="attempt_start",
        schema_version=2,
        description="R36-06 envelope evidence — derived execution id, separated "
        "source base, epoch and pinned continuation ref inside the digest",
        factory=_attempt_start_v2_document,
    ),
    ("continuation_decision", 1): CompatFixture(
        kind="continuation_decision",
        schema_version=1,
        description="Q35-02 continuation decision — the decision core, no lineage "
        "(digest-governed reuse only)",
        factory=_continuation_v1_document,
    ),
    ("continuation_decision", 2): CompatFixture(
        kind="continuation_decision",
        schema_version=2,
        description="R36-02/03 continuation decision — lineage, the pinned "
        "checkpoint digest and the typed refusal observability",
        factory=_continuation_v2_document,
    ),
    ("checkpoint_lookup", 1): CompatFixture(
        kind="checkpoint_lookup",
        schema_version=1,
        description="retired legacy opt-in lookup outcome — exact/absent only, "
        "failures collapsed to absent (authority legacy-http-opt-in)",
        factory=_checkpoint_lookup_v1_document,
    ),
    ("checkpoint_lookup", 2): CompatFixture(
        kind="checkpoint_lookup",
        schema_version=2,
        description="typed five-state lookup outcome — exact carries the content "
        "address; unavailable/corrupt/unauthorized never collapse",
        factory=_checkpoint_lookup_v2_document,
    ),
}


def compat_inventory() -> list[tuple[str, int]]:
    """The shipped fixture surface: every (kind, version) this module supports.

    Sorted for stable output — the drift check compares a deployment's
    readable document versions against exactly this list.
    """
    return sorted(_FIXTURES)


def compat_document(kind: str, version: int) -> dict:
    """The canned fixture document for (kind, version) — a fresh dict per call."""
    fixture = _FIXTURES.get((kind, version))
    if fixture is None:
        raise UnsupportedDocumentVersion(
            f"no compat fixture for {kind!r} v{version} — known: {compat_inventory()}"
        )
    factory = fixture.factory
    document = factory()
    if not isinstance(document, dict):
        raise UnsupportedDocumentVersion(
            f"compat fixture for {kind!r} v{version} did not produce a document"
        )
    return document


def load_compat_document(kind: str, version: int, payload: object) -> object:
    """Parse *payload* under the SUPPORTED read/recovery semantics of its version.

    Returns the typed view (:class:`CompatSpecDocument`,
    :class:`CompatCheckpointIndex`, :class:`CompatControlCommand`,
    :class:`CompatAttemptStartDocument`,
    :class:`CompatContinuationDocument`, a live
    :class:`forge.adaptive.checkpoint_repository.CheckpointLookupOutcome`
    for ``checkpoint_lookup``, or a live
    :class:`forge.runs.spec.ExecutableRunSpec` for v3). Unknown
    kind or version → :class:`UnsupportedDocumentVersion` — never a
    silent best-effort parse. A KNOWN version with a corrupt payload
    raises the underlying parser's error (``SpecInvalid`` /
    ``ValueError``): recovery semantics exist, corruption does not.
    """
    if (kind, version) not in _FIXTURES:
        raise UnsupportedDocumentVersion(
            f"unsupported document {kind!r} v{version!r} — supported: {compat_inventory()}"
        )
    if not isinstance(payload, dict):
        raise UnsupportedDocumentVersion(
            f"document {kind!r} v{version} is not an object — refusing best-effort parse"
        )
    if kind == "run_spec":
        return _load_spec_document(version, payload)
    if kind == "checkpoint_metadata":
        return _load_checkpoint_index(payload)
    if kind == "attempt_start":
        return _load_attempt_start(version, payload)
    if kind == "continuation_decision":
        return _load_continuation_decision(version, payload)
    if kind == "checkpoint_lookup":
        return _load_checkpoint_lookup(version, payload)
    return _load_control_command(version, payload)


def _load_spec_document(version: int, payload: dict) -> object:
    if version < 3:
        # v1/v2: digest-only — the SUPPORTED read is the legacy view with
        # the SpecLegacy recovery, never a best-effort v3 interpretation
        # (the approver never saw executable content).
        subject = payload.get("subject")
        if not isinstance(subject, dict):
            raise UnsupportedDocumentVersion(
                f"run_spec v{version} document has no 'subject' object"
            )
        provider = str(subject.get("provider") or "")
        provenance = "subject.provider"
        if version == 1:
            # v1 subjects carried no provider key; forge was GitLab-only
            # (FlowRun.provider server default) — construction, not a guess.
            provider = "gitlab"
            provenance = "server_default (v1 subjects carry no provider; GitLab-only forge)"
        try:
            return CompatSpecDocument(
                kind="run_spec",
                schema_version=version,
                provider=provider,
                provider_provenance=provenance,
                project_id=int(subject.get("project_id") or 0),
                issue_iid=(
                    int(subject["issue_iid"]) if subject.get("issue_iid") is not None else None
                ),
                plan_digest=str(payload.get("plan_digest") or ""),
                task_digest=str(payload.get("task_digest") or ""),
                policy_digest=str(payload.get("policy_digest") or ""),
                source_base_oid=str(payload.get("source_base_oid") or ""),
                executable=False,
                recovery=_SPEC_LEGACY_RECOVERY,
            )
        except (ValueError, TypeError) as exc:
            raise UnsupportedDocumentVersion(
                f"run_spec v{version} document is unreadable: {exc}"
            ) from exc
    from forge.runs.spec import ExecutableRunSpec, SpecInvalid

    try:
        return ExecutableRunSpec.from_document(payload)
    except SpecInvalid as exc:
        # A v3 row that no longer parses is corruption, not a version
        # gap — surface the verifier's reason unchanged.
        raise ValueError(f"run_spec v3 document is corrupt: {exc}") from exc


def _load_checkpoint_index(payload: dict) -> CompatCheckpointIndex:
    entries = payload.get("checkpoints")
    if not isinstance(entries, list) or not isinstance(payload.get("work_id"), str):
        raise UnsupportedDocumentVersion(
            "checkpoint_metadata v1 document is malformed "
            "(needs 'work_id' and a 'checkpoints' list)"
        )
    try:
        parsed = [dict(entry) for entry in entries if isinstance(entry, dict)]
    except TypeError as exc:
        raise UnsupportedDocumentVersion(
            f"checkpoint_metadata v1 entries are unreadable: {exc}"
        ) from exc
    parsed.sort(
        key=lambda entry: (int(entry.get("sequence") or 0), str(entry.get("checkpoint_id") or ""))
    )
    return CompatCheckpointIndex(
        kind="checkpoint_metadata",
        schema_version=1,
        work_id=str(payload["work_id"]),
        checkpoints=tuple(parsed),
        recovery=_CHECKPOINT_INDEX_RECOVERY,
    )


def _load_control_command(version: int, payload: dict) -> CompatControlCommand:
    resume_payload = payload.get("payload")
    if not isinstance(resume_payload, dict):
        raise UnsupportedDocumentVersion(
            f"control_command v{version} document has no 'payload' object"
        )
    checkpoint_ref = str(resume_payload.get("checkpoint_ref") or "")
    sequence = int(resume_payload.get("checkpoint_sequence") or 0)
    source_oid = str(resume_payload.get("source_oid") or "")
    if version == 1:
        if resume_payload:
            raise UnsupportedDocumentVersion(
                "control_command v1 (pre-NEXT-03) carries an EMPTY payload — a "
                "ResumeSpec-bearing payload is v2"
            )
        selection = "legacy_active_fallback"
        recovery = _CONTROL_COMMAND_V1_RECOVERY
    else:
        if not checkpoint_ref or "@" not in checkpoint_ref:
            raise UnsupportedDocumentVersion(
                "control_command v2 (NEXT-03) requires a 'checkpoint_ref' "
                "'<work_id>@<checkpoint_id>' payload"
            )
        selection = "exact"
        recovery = _CONTROL_COMMAND_V2_RECOVERY
    return CompatControlCommand(
        kind="control_command",
        schema_version=version,
        command_id=str(payload.get("command_id") or ""),
        work_id=str(payload.get("work_id") or ""),
        kind_command=str(payload.get("kind") or ""),
        actor_ref=str(payload.get("actor_ref") or ""),
        selection=selection,
        checkpoint_ref=checkpoint_ref,
        checkpoint_sequence=sequence,
        source_oid=source_oid,
        recovery=recovery,
    )


_HEX64 = set("0123456789abcdef")


def _is_hex64(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in _HEX64 for char in text)


def _load_attempt_start(version: int, payload: dict) -> CompatAttemptStartDocument:
    """The supported read of a persisted ``attempt_start`` document.

    v1 is AUDIT-ONLY (``execution_attempt_id`` is ``None`` — the weaker
    identity never existed and none is manufactured); v2 requires the
    derived hex64 execution identity and the separated source base. A
    v1 document carrying an ``execution_attempt_id`` key is refused —
    that shape never shipped.
    """
    envelope_digest = str(payload.get("envelope_digest") or "")
    source_base = str(payload.get("source_base_oid") or payload.get("attempt_base") or "")
    if not _is_hex64(envelope_digest):
        raise UnsupportedDocumentVersion(
            f"attempt_start v{version} document has no hex64 'envelope_digest'"
        )
    if not source_base:
        raise UnsupportedDocumentVersion(
            f"attempt_start v{version} document names no source base "
            "(neither 'source_base_oid' nor the v1 'attempt_base')"
        )
    epoch = payload.get("authority_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise UnsupportedDocumentVersion(
            f"attempt_start v{version} document's 'authority_epoch' must be an int"
        )
    if version == 1:
        if payload.get("execution_attempt_id") is not None:
            raise UnsupportedDocumentVersion(
                "attempt_start v1 (Q35-07) carried no execution identity — a "
                "document with 'execution_attempt_id' is v2"
            )
        return CompatAttemptStartDocument(
            kind="attempt_start",
            schema_version=1,
            envelope_digest=envelope_digest,
            context_digest=str(payload.get("context_digest") or ""),
            subject_key=str(payload.get("subject_key") or ""),
            resume_mode=str(payload.get("resume_mode") or ""),
            execution_attempt_id=None,  # never manufactured
            source_base_oid=source_base,
            authority_epoch=epoch,
            attempt_ordinal=None,
            continuation_ref_digest="",
            profile_source=str(payload.get("profile_source") or ""),
            identity_strength="source_oid_only",
            recovery=_ATTEMPT_START_V1_RECOVERY,
        )
    execution_id = str(payload.get("execution_attempt_id") or "")
    if not _is_hex64(execution_id):
        raise UnsupportedDocumentVersion(
            "attempt_start v2 requires the hex64 derived 'execution_attempt_id' — "
            "a document without it is v1 (audit-only), never a guessed identity"
        )
    return CompatAttemptStartDocument(
        kind="attempt_start",
        schema_version=2,
        envelope_digest=envelope_digest,
        context_digest=str(payload.get("context_digest") or ""),
        subject_key=str(payload.get("subject_key") or ""),
        resume_mode=str(payload.get("resume_mode") or ""),
        execution_attempt_id=execution_id,
        source_base_oid=source_base,
        authority_epoch=epoch,
        attempt_ordinal=(
            int(payload["attempt_ordinal"]) if payload.get("attempt_ordinal") is not None else None
        ),
        continuation_ref_digest=str(payload.get("continuation_ref_digest") or ""),
        profile_source=str(payload.get("profile_source") or ""),
        identity_strength="execution_id_v2",
        recovery=_ATTEMPT_START_V2_RECOVERY,
    )


def _load_continuation_decision(version: int, payload: dict) -> CompatContinuationDocument:
    """The supported read of a persisted continuation decision document.

    The mode vocabulary is the live
    :class:`forge.adaptive.continuation.ContinuationMode` one (``fresh``/
    ``required``/``restart``/``uncertain``); an unknown mode is refused.
    v1 documents carry no lineage — those fields read as ``None``,
    never guessed.
    """
    from forge.adaptive.continuation import ContinuationMode

    mode_text = str(payload.get("mode") or "")
    try:
        mode = ContinuationMode(mode_text)
    except ValueError as exc:
        raise UnsupportedDocumentVersion(
            f"continuation_decision v{version} names unknown mode {mode_text!r}"
        ) from exc
    evidence_digest = str(payload.get("evidence_digest") or "")
    reason = str(payload.get("reason") or "")
    decided_at = str(payload.get("decided_at") or "")
    if not _is_hex64(evidence_digest) or not reason or not decided_at:
        raise UnsupportedDocumentVersion(
            f"continuation_decision v{version} document is malformed "
            "(needs hex64 'evidence_digest', 'reason', 'decided_at')"
        )

    def _opt(key: str) -> str | None:
        value = payload.get(key)
        return str(value) if value is not None else None

    source_attempt = payload.get("source_attempt")
    return CompatContinuationDocument(
        kind="continuation_decision",
        schema_version=version,
        mode=mode.value,
        mode_selected=mode.value,
        reason=reason,
        decided_at=decided_at,
        evidence_digest=evidence_digest,
        uncertain=mode is ContinuationMode.UNCERTAIN,
        source_attempt=(
            int(source_attempt) if version >= 2 and isinstance(source_attempt, int) else None
        ),
        native_command_id=_opt("native_command_id") if version >= 2 else None,
        native_start_verdict=_opt("native_start_verdict") if version >= 2 else None,
        discard_authorized_by=_opt("discard_authorized_by") if version >= 2 else None,
        checkpoint_digest=_opt("checkpoint_digest") if version >= 2 else None,
        refusal_code=_opt("refusal_code"),
        recovery=_CONTINUATION_V2_RECOVERY if version >= 2 else _CONTINUATION_V1_RECOVERY,
    )


def _load_checkpoint_lookup(version: int, payload: dict) -> object:
    """The supported read of a checkpoint lookup outcome document.

    v1 (the retired opt-in chain) answers ``exact``/``absent`` ONLY —
    any typed state beyond those two is v2 vocabulary and a v1 document
    claiming one is refused (the chain could not produce it). Returns
    the LIVE :class:`CheckpointLookupOutcome` so consumers branch on
    the production type, with the fixture's recovery semantics
    documented here rather than duplicated.
    """
    from forge.adaptive.checkpoint_repository import (
        LOOKUP_ABSENT,
        LOOKUP_EXACT,
        CheckpointLookupOutcome,
    )

    state = str(payload.get("state") or "")
    detail = str(payload.get("detail") or "")
    authority = str(payload.get("authority") or "")
    allowed = (LOOKUP_EXACT, LOOKUP_ABSENT) if version == 1 else None
    if state not in (LOOKUP_EXACT, LOOKUP_ABSENT, "unavailable", "corrupt", "unauthorized"):
        raise UnsupportedDocumentVersion(
            f"checkpoint_lookup v{version} names unknown state {state!r}"
        )
    if allowed is not None and state not in allowed:
        raise UnsupportedDocumentVersion(
            f"checkpoint_lookup v1 answered exact/absent only — {state!r} is v2 "
            "typed vocabulary the legacy chain could not produce"
        )
    if state == LOOKUP_EXACT:
        checkpoint_id = str(payload.get("checkpoint_id") or "")
        if not _is_hex64(checkpoint_id):
            raise UnsupportedDocumentVersion(
                "checkpoint_lookup 'exact' requires the checkpoint's hex64 "
                "content address ('checkpoint_id')"
            )
        return CheckpointLookupOutcome.exact(checkpoint_id, authority=authority)
    return CheckpointLookupOutcome.missing(state, authority=authority, detail=detail)
