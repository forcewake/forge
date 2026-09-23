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
    "CompatCheckpointIndex",
    "CompatControlCommand",
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
    :class:`CompatCheckpointIndex`, :class:`CompatControlCommand`, or a
    live :class:`forge.runs.spec.ExecutableRunSpec` for v3). Unknown
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
