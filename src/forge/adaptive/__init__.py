"""The adaptive contracts substrate (review 05868e9, PLN/DSC/MRP/VER epics).

Typed, validated schemas for the adaptive workflow the customer plan
describes — the SEPARATION the review centers on:

- :class:`WorkContract` — WHAT must result and what is authorized (the
  approved object; goal, non-goals, invariants, read/write scope, budget).
- :class:`PlanRevision` — HOW to get there (versioned steps, dependencies,
  evidence references — replaceable without re-approving the contract).
- :class:`ChangeProposal` — WHY the plan must change (new evidence, the
  minimal revision, material-change classification for the human gate).
- :class:`ControlCommand` — the durable human-control mailbox record
  (pause/resume/steer/answer with sequence, actor provenance, expected
  revision/epoch, idempotency).
- :class:`SnapshotSet` — the immutable read-only source set every plan
  revision binds to (repository → OID + config digest).
- :class:`CandidateSet` — the unit of system verification (per-repo
  candidate/baseline OIDs + contract/test/environment digests).

Every schema validates its ``schema`` discriminator, digests are hex
sha256-shaped, and the shipped examples under
``docs/roadmap/2026-09-21-adaptive/contracts/`` parse against these
models (the review package's own contract proposals, now executable).
"""

from forge.adaptive.models import (
    CandidateSet,
    CandidateSetMember,
    ChangeProposal,
    Checkpoint,
    ControlCommand,
    PathScope,
    PlanRevision,
    PlanStep,
    SnapshotSet,
    Snapshot,
    WorkContract,
)

__all__ = [
    "CandidateSet",
    "CandidateSetMember",
    "ChangeProposal",
    "Checkpoint",
    "ControlCommand",
    "PathScope",
    "PlanRevision",
    "PlanStep",
    "SnapshotSet",
    "Snapshot",
    "WorkContract",
]
