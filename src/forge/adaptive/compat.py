"""Compatibility migration substrate for legacy and adaptive runs (FND-07/FND-08).

Why a compatibility layer at all: adaptive mode is an OPT-IN that ships
NEXT TO the legacy pipeline, not a replacement for it. The same broker
serves both for a long time, so the boundary between them stays explicit
and boring — adaptive runs only where flagged, adaptive mode is enabled
only when no legacy run is mid-flight, a downgrade happens only under
documented constraints and never rewrites approved bytes, and a release
carries honestly-labelled verification (FND-07). The migration that
carries v3 runs forward INVENTS NOTHING: new capabilities come from
explicit new approvals, never from a convenient default.

FND-08's registry half lives here too: :data:`INVARIANT_SUITES`
documents the named invariant suites this codebase maintains on the
production path — the release manifest validates that their files
exist, because a test registration marker alone never closes a finding
— and :func:`performed_vs_skipped` keeps skipped and unsupported
verification explicit instead of letting them blur into pass.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "INVARIANT_SUITES",
    "CompatibilityFlags",
    "downgrade_constraints",
    "drain_in_flight",
    "migration_invents_nothing",
    "performed_vs_skipped",
    "validate_release",
]

#: The named invariant suites maintained on the production path (FND-08).
#: Documentation, not verification: the release manifest is what checks
#: these files exist — this constant is the single place the suite names
#: and their node ids are written down, so the manifest and the docs
#: cannot drift apart silently.
INVARIANT_SUITES: dict[str, str] = {
    "final_boundary_fence": "tests/test_github_runs.py::TestFinalBoundaryFenceFND02",
    "repository_identity": "tests/test_project_config.py::TestRepositoryIdentityContractFND01",
    "mutation_guards": "tests/test_github_runs.py::TestMutationGuardsC11",
}

#: Statuses a legacy run may rest in WITHOUT blocking the adaptive
#: cutover. Note ``blocked`` counts as at-rest here: a blocked run is
#: parked on a human decision, not executing — it holds no in-flight
#: work that a mode switch could corrupt.
_TERMINAL_STATUSES = frozenset({"ready_for_human", "blocked", "failed", "cancelled"})

#: The documented downgrade constraints (FND-07): adaptive event history
#: is forward-readable, but DOWNGRADE — returning a deployment to the
#: legacy pipeline — is only allowed when every adaptive run already
#: ended, the evidence it wrote stays readable, and no v3 spec is
#: rewritten. A downgrade never rewrites old approved bytes.
_DOWNGRADE_CONSTRAINTS = [
    "adaptive runs must be terminal",
    "evidence rows stay readable",
    "no schema rewrite of v3 specs",
]


@dataclass(frozen=True)
class CompatibilityFlags:
    """The opt-in boundary: WHERE adaptive runs are allowed (FND-07).

    ``adaptive_enabled`` is the master switch and the two scope tuples
    are the per-connection / per-project opt-ins; :meth:`allows` needs
    the master switch ON plus at least one matching scope. Adaptive is
    never the default: a connection or project that was never flagged
    keeps the legacy pipeline even when the master switch is on
    everywhere else.
    """

    adaptive_enabled: bool = False
    connection_scopes: tuple[str, ...] = ()
    project_scopes: tuple[str, ...] = ()

    def allows(self, connection: str, project: str) -> bool:
        """Whether THIS connection/project pair may run adaptive.

        Both checks must pass: the master switch alone (with no scopes)
        allows nothing, and a matching scope with the master switch off
        allows nothing — the flag set is an AND of opt-ins, never an
        accidental OR.
        """
        if not self.adaptive_enabled:
            return False
        return connection in self.connection_scopes or project in self.project_scopes


def drain_in_flight(legacy_runs: list[dict]) -> dict:
    """Check the safe-drain precondition before enabling adaptive (FND-07).

    Each run is ``{"id": str, "status": str}``. Adaptive mode enables
    only when NO legacy run is mid-flight: every run must rest in a
    terminal status (:data:`_TERMINAL_STATUSES`). Mid-flight run ids are
    returned as ``must_wait`` — the operator's waitlist, so "not yet"
    comes with the exact list of what to wait for, and
    ``safe_to_enable`` is simply whether that list is empty (never an
    independent judgement that could disagree with it).
    """
    must_wait = [run["id"] for run in legacy_runs if run["status"] not in _TERMINAL_STATUSES]
    return {"safe_to_enable": not must_wait, "must_wait": must_wait}


def downgrade_constraints() -> list[str]:
    """The documented constraints a downgrade must satisfy (FND-07).

    Adaptive event history is forward-readable, but going BACK to the
    legacy pipeline is only allowed when every adaptive run is terminal,
    the evidence rows it wrote stay readable, and no v3 spec is
    rewritten. A downgrade never rewrites old approved bytes — the list
    is returned per call so callers embed the SAME documented contract,
    not a stale copy of it.
    """
    return list(_DOWNGRADE_CONSTRAINTS)


def validate_release(legacy_replay: bool, new_startup: bool) -> dict:
    """Label release verification as performed or not — honestly (FND-07).

    ``legacy_run_replay`` / ``new_run_startup`` are ``True`` only when
    that verification actually RAN AND PASSED; anything else is
    ``"not_run"``. There is deliberately no third flattering label: a
    boot-time success is never dressed up as live verification, and a
    skipped check stays visibly skipped instead of being folded into
    pass.
    """
    return {
        "legacy_run_replay": "pass" if legacy_replay else "not_run",
        "new_run_startup": "pass" if new_startup else "not_run",
    }


def migration_invents_nothing(v3_run: dict) -> list[str]:
    """Violations where a migration would INVENT capability (FND-07).

    A non-empty ``read_scope`` or ``revision_permission`` key on a v3
    run means the migration would hand the run system read scope or
    plan-change permission it never had. That is invention: adaptive
    capabilities come from explicit NEW approvals, not from a migration
    conveniently granting what the old record omitted. An empty list
    means the migration invents nothing.
    """
    violations: list[str] = []
    if v3_run.get("read_scope"):
        violations.append(
            "read_scope: migration must not invent system read scope; "
            "adaptive capabilities come from explicit new approvals"
        )
    if v3_run.get("revision_permission"):
        violations.append(
            "revision_permission: migration must not invent plan-change "
            "permission; adaptive capabilities come from explicit new approvals"
        )
    return violations


def performed_vs_skipped(results: dict) -> dict:
    """Bucket verification results; skipped stays skipped (FND-08).

    Input maps a check name to ``"pass"`` | ``"skip"`` |
    ``"unsupported"``; the output buckets the names into ``performed``
    / ``skipped`` / ``unsupported``. A skipped or unsupported check is
    NEVER converted to pass — the whole point of the registry is that
    what was not run stays visibly not run. An unknown outcome label
    raises: a typo'd status must fail loudly, not fall into the
    nearest bucket.
    """
    buckets: dict[str, list[str]] = {"performed": [], "skipped": [], "unsupported": []}
    outcome_to_bucket = {
        "pass": "performed",
        "skip": "skipped",
        "unsupported": "unsupported",
    }
    for name, outcome in results.items():
        bucket = outcome_to_bucket.get(outcome)
        if bucket is None:
            raise ValueError(f"unknown verification outcome {outcome!r} for {name!r}")
        buckets[bucket].append(name)
    return buckets
