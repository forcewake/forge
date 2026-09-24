"""R36-19 (#278) — verify a complete two-service CandidateSet AS A SYSTEM.

**The PYTHON-shaped twin is the DETERMINISTIC REFERENCE** (label:
:data:`~forge.adaptive.reference.system_twin.TWIN_REFERENCE_LABEL`,
retained per R37-15/#296's rollout note): in-process, synthetic
services, deterministic by construction — since R37-19 (#300) it lives
in :mod:`forge.adaptive.reference.system_twin` (the labelled evaluation
package), re-exported below for import compatibility. The verified
executor (``verification_executor.py``, #296) is the step beyond it — a
separate subprocess under a scrubbed environment, BUILT wheel artifacts
and real-socket dependencies. Both stay: the twin pins the mechanism's
semantics cheaply; the executor proves them against real artifacts and
processes.

What THIS module owns is the RUNTIME side of system verification — the
contracts and decisions, not the scenario:

- **The tested-world identity** — every result binds to the persisted
  digests through the existing entries (:func:`freeze_tested_world`
  composition lives on the scenario; the binding, applicability joins
  and drift naming are decided here over
  ``bound_tested_world_digest`` / ``bound_applicability_digest`` /
  :func:`baseline_drift`).
- **:class:`VerifierEnvironmentDocument` /
  :class:`VerifierAuthorityError`** — the runner's own environment
  description PROVES it holds neither model credentials nor repository
  publication authority; a document carrying either is refused before
  anything executes.
- **Per-edge results** (:class:`EdgeResult`) — the producer→consumer
  contract edge, the consumer→baseline edge and the environment edge,
  EXECUTED against the reference twin's declared scenario (its sqlite
  schema-upgrade and double-delivery dependencies). A failed edge NAMES
  the failing member and blocks ``system_ready``; missing evidence
  blocks the readiness query; the ``verification.report_coverage``
  fragment records what was expected, verified, failed and missing.
- **Selective invalidation** (:func:`replay_against_changed_inputs`) —
  a previously passed record replayed against changed baseline/test
  bundle inputs invalidates ONLY the affected edges, by dependency
  identity, with the drift NAMED and the history retained for audit
  (superseded, never deleted).
- **Readiness as a query** (:func:`system_readiness`) — a pure lookup
  over executed evidence + applicability that NEVER re-runs anything,
  reporting verification readiness, merge permission and deployment
  permission as THREE DISTINCT booleans: a passed environment test
  authorizes no production migration, and approvals are human grants
  forge records but never derives.

Evidence links: the two-writer qualification
(``two_writer_qualification.py``, #269) drove the MODELS; the durable
saga (``saga_durable.py``, #277) made publication durable; the verdict
binding (``verification_binding.py``, #273) binds a verdict to the exact
candidate. The .NET/TRX scenarios stay in the ExpectedReports machinery
(:mod:`forge.adaptive.verification_binding`).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.adaptive.models import CandidateSet

# The reference twin (R37-19/#300): the scenario machinery moved to the
# labelled evaluation package; the import below is BOTH the compatibility
# re-export (the old import paths keep working — pinned by
# tests/test_reference_separation.py) and the runtime runner's composition
# of the scenario it executes.
from forge.adaptive.reference.system_twin import (
    BASELINE_API_V2,
    BASELINE_API_V3,
    SYNTHETIC_BASELINE_ROWS,
    SYNTHETIC_MESSAGES,
    SYNTHETIC_SEED_ROWS,  # noqa: F401 — compat re-export (a pre-move module attribute)
    TWIN_CONTRACT_BUNDLE_SCHEMA,
    TWIN_ENVIRONMENT_PROFILE_SCHEMA,
    TWIN_REFERENCE_LABEL,
    TWIN_TEST_BUNDLE_SCHEMA,
    CrashWindowOpened,
    DeliveryOutcome,
    DoubleDeliveryHarness,
    SchemaUpgradeOutcome,
    SystemEdge,
    TwinContract,
    TwinMigration,
    TwinScenario,
    TwinService,
    _baseline_row,
    _synthetic_message,
    _synthetic_seed_rows,  # noqa: F401 — compat re-export (tests import it from here)
    default_twin_scenario,
    execute_schema_upgrade,
)
from forge.adaptive.verification_sets import (
    DependencyChange,
    EnvironmentPinChange,
    EvidenceLedger,
    EvidenceRecord,
    MemberChange,
    TestBundleChange,
    VerificationLane,
    WorldInputDrift,
    async_failure_scenarios,
    baseline_drift,
    bound_applicability_digest,
    bound_tested_world_digest,
    contract_checks,
    environment_compose,
    member_identity,
    record_evidence,
    select_lane,
)

__all__ = [
    "SYSTEM_VERIFICATION_SCHEMA",
    "REPORT_COVERAGE_SCHEMA",
    "TWIN_CONTRACT_BUNDLE_SCHEMA",
    "TWIN_TEST_BUNDLE_SCHEMA",
    "TWIN_ENVIRONMENT_PROFILE_SCHEMA",
    "TWIN_REFERENCE_LABEL",
    "BASELINE_API_V2",
    "BASELINE_API_V3",
    "CrashWindowOpened",
    "DeliveryOutcome",
    "DoubleDeliveryHarness",
    "EdgeResult",
    "ProjectionOutcome",
    "SchemaUpgradeOutcome",
    "SelectiveInvalidation",
    "SystemBlockedEdge",
    "SystemEdge",
    "SystemReadiness",
    "SystemVerificationReport",
    "TwinContract",
    "TwinMigration",
    "TwinScenario",
    "TwinService",
    "VerifierAuthorityError",
    "VerifierEnvironmentDocument",
    "baseline_drift",
    "default_twin_scenario",
    "default_verifier_environment",
    "execute_schema_upgrade",
    "forbid_verifier_authority",
    "replay_against_changed_inputs",
    "run_system_verification",
    "system_readiness",
]

#: The report's versioned schema discriminator — a breaking change to
#: what the report covers bumps the tag.
SYSTEM_VERIFICATION_SCHEMA = "forge.system-verification/1"

#: The ``verification.report_coverage`` fragment's discriminator.
REPORT_COVERAGE_SCHEMA = "forge.verification.report-coverage/1"


# ---------------------------------------------------------------------------
# The verifier's own environment: authority it must NEVER hold.
# ---------------------------------------------------------------------------


class VerifierAuthorityError(ValueError):
    """The verification environment was handed authority it must never
    hold (R36-19): model credentials (the coding agent's own tools — an
    implementation grading its own homework) or repository publication
    authority (a verifier that can publish verifies nothing about what
    MERGING would do)."""


@dataclass(frozen=True)
class VerifierEnvironmentDocument:
    """The isolated environment the system verifier runs in.

    The document names the LANE (a separate trusted executor, never the
    coding agent's environment), the system under test (the twin) and —
    asserted EMPTY — the two authorities the verifier must not hold:
    model credentials and publication authority. The runner records it
    in the report so the absence is PROVEN, not claimed.
    """

    lane: VerificationLane
    twin: TwinScenario
    model_credentials: tuple[str, ...] = ()
    publication_authority: bool = False

    def to_document(self) -> dict[str, Any]:
        """The report's environment fragment: the lane, the services,
        and the two absence proofs."""
        return {
            "lane": {
                "lane_id": self.lane.lane_id,
                "profile": self.lane.profile,
                "runs_code": self.lane.runs_code,
            },
            "system_under_test": self.twin.scenario_id,
            "model_credentials": list(self.model_credentials),
            "publication_authority": self.publication_authority,
        }


def default_verifier_environment(twin: TwinScenario) -> VerifierEnvironmentDocument:
    """The clean verifier environment: the trusted-integration lane (the
    twin probes a real database and a real delivery path), no model
    credentials, no publication authority."""
    checks = ["twin-contract-replay", "twin-baseline-projection", "twin-db-upgrade"]
    return VerifierEnvironmentDocument(
        lane=select_lane(checks, has_db=True, has_broker=True),
        twin=twin,
    )


def forbid_verifier_authority(environment: VerifierEnvironmentDocument) -> None:
    """Refuse an environment holding authority the verifier must not
    have — BEFORE any execution (fail closed, named violations)."""
    violations: list[str] = []
    if environment.model_credentials:
        violations.append(f"model credentials present: {sorted(environment.model_credentials)}")
    if environment.publication_authority:
        violations.append("publication authority granted: the verifier must not publish")
    if violations:
        raise VerifierAuthorityError(
            "the verification environment holds authority it must never have — "
            + "; ".join(violations)
        )


# ---------------------------------------------------------------------------
# The edge executions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeResult:
    """One edge's executed outcome.

    A FAILED edge NAMES the failing member (``failed_member``) — never
    a bare "edge failed" — and blocks ``system_ready``. ``checks`` are
    the executed check descriptors with their per-check outcomes.
    """

    edge_id: str
    kind: str
    status: str  # "passed" | "failed"
    repositories: tuple[str, ...]
    failed_member: str | None
    detail: str
    checks: tuple[dict[str, Any], ...] = ()
    evidence_id: str = ""

    def as_document(self) -> dict[str, Any]:
        """The ``system_verification.edge_results`` fragment's one row."""
        return {
            "edge_id": self.edge_id,
            "kind": self.kind,
            "status": self.status,
            "repositories": list(self.repositories),
            "failed_member": self.failed_member,
            "detail": self.detail,
            "checks": [dict(check) for check in self.checks],
            "evidence_id": self.evidence_id,
        }


def _contract_spec(scenario: TwinScenario) -> dict[str, Any]:
    shared = scenario.shared_contract
    return {
        "messages": [
            {
                "topic": scenario.contract_topic,
                "payload_schema": json.dumps(
                    shared.as_document(), sort_keys=True, separators=(",", ":")
                ),
            }
        ]
    }


def _run_contract_edge(scenario: TwinScenario, edge: SystemEdge) -> EdgeResult:
    """Execute the producer→consumer contract edge: replay SYNTHETIC
    messages from the producer build's dialect against the consumer
    build's requirement — a real replay with synthetic data, not a
    digest equality check. The shared bundle's dialect is the referee
    that decides WHICH member a mismatch names."""
    producer, consumer = scenario.producer(), scenario.consumer()
    shared = scenario.shared_contract
    emits = producer.emits or shared
    expects = consumer.expects or shared
    descriptors = contract_checks(_contract_spec(scenario))
    checks: list[dict[str, Any]] = [
        {
            "check": "contract-descriptor",
            "id": descriptor["id"],
            "kind": descriptor["kind"],
            "must_verify": True,  # demanded by shape; satisfied BY EXECUTION below
            "status": "verified-by-execution",
        }
        for descriptor in descriptors
    ]
    evidence_id = f"ev-{edge.edge_id}"

    def _failed(member: TwinService, reason: str) -> EdgeResult:
        checks.append({"check": "message-replay", "status": "failed", "detail": reason})
        return EdgeResult(
            edge_id=edge.edge_id,
            kind=edge.kind,
            status="failed",
            repositories=edge.repositories,
            failed_member=member.repository_id,
            detail=(
                f"{reason} — failed member {member.repository_id}"
                f" (producer dialect {emits.schema_version},"
                f" consumer dialect {expects.schema_version},"
                f" bundle dialect {shared.schema_version})"
            ),
            checks=tuple(checks),
            evidence_id=evidence_id,
        )

    if emits.schema_version != shared.schema_version:
        return _failed(
            producer,
            f"the producer build emits dialect {emits.schema_version};"
            f" the contract bundle froze {shared.schema_version}",
        )
    if expects.schema_version != shared.schema_version:
        return _failed(
            consumer,
            f"the consumer build expects dialect {expects.schema_version};"
            f" the contract bundle froze {shared.schema_version}",
        )
    demanded_outside_contract = sorted(set(expects.required) - set(shared.fields))
    if demanded_outside_contract:
        return _failed(
            consumer,
            "the consumer requires fields the shared contract never had:"
            f" {demanded_outside_contract}",
        )
    messages = [
        _synthetic_message(index, emits.fields) for index in range(1, SYNTHETIC_MESSAGES + 1)
    ]
    missing: dict[str, set[str]] = {}
    for message in messages:
        absent = sorted(set(expects.required) - set(message))
        if absent:
            missing[str(message.get("id"))] = set(absent)
    if missing:
        first_id, first_missing = next(iter(sorted(missing.items())))
        return _failed(
            producer,
            "producer messages omit required fields the consumer demands"
            f" (e.g. {first_id} missing {sorted(first_missing)})"
            f" across {len(missing)}/{len(messages)} replayed messages",
        )
    # the consumer side validates every replayed message (real loop).
    accepted = all(set(expects.required) <= set(message) for message in messages)
    if not accepted:  # pragma: no cover - the explicit guard above already caught it
        return _failed(producer, "a replayed message failed consumer validation")
    checks.append(
        {
            "check": "message-replay",
            "status": "passed",
            "messages": len(messages),
            "producer_dialect": emits.schema_version,
            "consumer_dialect": expects.schema_version,
            "required_fields": list(expects.required),
        }
    )
    return EdgeResult(
        edge_id=edge.edge_id,
        kind=edge.kind,
        status="passed",
        repositories=edge.repositories,
        failed_member=None,
        detail=(
            f"{len(messages)} synthetic messages replayed from"
            f" {producer.repository_id} and accepted by {consumer.repository_id}"
            f" at dialect {shared.schema_version}"
        ),
        checks=tuple(checks),
        evidence_id=evidence_id,
    )


@dataclass(frozen=True)
class ProjectionOutcome:
    """The baseline-edge projection replay's computed outcome."""

    served_api: str
    required_api: str
    rows: int
    projected: int
    missing_columns: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "served_api": self.served_api,
            "required_api": self.required_api,
            "rows": self.rows,
            "projected": self.projected,
            "missing_columns": list(self.missing_columns),
        }


def _run_baseline_edge(scenario: TwinScenario, edge: SystemEdge) -> EdgeResult:
    """Execute the consumer→baseline edge: the PINNED baseline (at its
    pinned digest, never its branch head) serves synthetic rows in its
    API's shape, and the consumer build PROJECTS them through a real
    sqlite round trip. A pin serving an API the changed consumer cannot
    integrate with fails the edge NAMING the pinned member."""
    consumer, pinned = scenario.consumer(), scenario.pinned()
    required_api = consumer.requires_baseline_api or BASELINE_API_V3
    served_api = pinned.baseline_api or BASELINE_API_V3
    evidence_id = f"ev-{edge.edge_id}"
    checks: list[dict[str, Any]] = [
        {
            "check": "baseline-api",
            "required": required_api,
            "served_at_pin": served_api,
            "pinned_digest": pinned.image_digest,
            "branch_head_past_pin": pinned.branch_head_oid or "",
        }
    ]
    if served_api != required_api:
        checks[0]["status"] = "failed"
        return EdgeResult(
            edge_id=edge.edge_id,
            kind=edge.kind,
            status="failed",
            repositories=edge.repositories,
            failed_member=pinned.repository_id,
            detail=(
                f"the pinned baseline serves {served_api} at digest"
                f" {pinned.image_digest[:19]}… but the changed consumer integrates"
                f" with {required_api} — failed member {pinned.repository_id}"
                " (the pin is stale for this consumer; the branch head"
                f" {pinned.branch_head_oid[:7]} is not the pin)"
            ),
            checks=tuple(checks),
            evidence_id=evidence_id,
        )
    checks[0]["status"] = "passed"

    rows = [_baseline_row(index, served_api) for index in range(1, SYNTHETIC_BASELINE_ROWS + 1)]
    required_columns = ("id", "total", "region")
    missing = sorted({column for row in rows for column in required_columns if column not in row})
    conn = sqlite3.connect(":memory:")
    try:
        with conn:
            conn.execute(
                "CREATE TABLE projection (id TEXT PRIMARY KEY, total INTEGER,"
                " region TEXT, projected_from TEXT)"
            )
        projected = 0
        for row in rows:
            if any(column not in row for column in required_columns):
                continue
            with conn:
                conn.execute(
                    "INSERT INTO projection (id, total, region, projected_from)"
                    " VALUES (?, ?, ?, ?)",
                    (str(row["id"]), int(row["total"]), str(row["region"]), pinned.repository_id),
                )
            projected += 1
        projected_count = int(conn.execute("SELECT count(*) FROM projection").fetchone()[0])
    finally:
        conn.close()
    projection = ProjectionOutcome(
        served_api=served_api,
        required_api=required_api,
        rows=len(rows),
        projected=projected_count,
        missing_columns=tuple(missing),
    )
    if projected != len(rows):
        checks.append(
            {
                "check": "projection-replay",
                "status": "failed",
                **projection.as_document(),
            }
        )
        return EdgeResult(
            edge_id=edge.edge_id,
            kind=edge.kind,
            status="failed",
            repositories=edge.repositories,
            failed_member=pinned.repository_id,
            detail=(
                f"only {projected}/{len(rows)} baseline rows projected — rows at"
                f" {served_api} lack columns the consumer's projection requires"
                f" ({missing}); failed member {pinned.repository_id}"
            ),
            checks=tuple(checks),
            evidence_id=evidence_id,
        )
    checks.append({"check": "projection-replay", "status": "passed", **projection.as_document()})
    return EdgeResult(
        edge_id=edge.edge_id,
        kind=edge.kind,
        status="passed",
        repositories=edge.repositories,
        failed_member=None,
        detail=(
            f"{projected} synthetic rows served by {pinned.repository_id} at"
            f" {served_api} (pinned digest) projected by {consumer.repository_id}"
        ),
        checks=tuple(checks),
        evidence_id=evidence_id,
    )


def _run_environment_edge(scenario: TwinScenario, edge: SystemEdge) -> EdgeResult:
    """Execute the environment edge: the ``db_upgrade_plan``-driven
    schema upgrade from the PINNED baseline schema with seeded data,
    and the full :func:`async_failure_scenarios` catalog through the
    double-delivery harness — crash between commit and ack, pure
    duplicate redelivery, out-of-order arrival. Every message must land
    EXACTLY ONCE (one projected effect) however many deliveries it
    took."""
    evidence_id = f"ev-{edge.edge_id}"
    db_dependency, bus_dependency = scenario.dependencies[0], scenario.dependencies[1]
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="forge-twin-") as directory:
        upgrade = execute_schema_upgrade(Path(directory) / "orders.db")
        checks.append(
            {
                "check": "db-upgrade",
                "status": "passed" if upgrade.preserved else "failed",
                **upgrade.as_document(),
            }
        )
        if not (upgrade.preserved and upgrade.schema_advanced and upgrade.post_upgrade_write):
            return EdgeResult(
                edge_id=edge.edge_id,
                kind=edge.kind,
                status="failed",
                repositories=edge.repositories,
                failed_member=db_dependency,
                detail=f"{db_dependency}: {upgrade.detail} — failed member {db_dependency}",
                checks=tuple(checks),
                evidence_id=evidence_id,
            )

        harness = DoubleDeliveryHarness(Path(directory) / "delivery.db")
        try:
            outcomes: list[DeliveryOutcome] = []
            dialect = scenario.shared_contract.fields
            # 1. crash_between_commit_and_ack: every message loses its ack
            #    window once and is redelivered post-commit.
            for index in range(1, 6):
                outcomes.append(
                    harness.deliver(
                        _synthetic_message(index, dialect),
                        scenario="crash_between_commit_and_ack",
                        crash_between_commit_and_ack=True,
                    )
                )
            # 2. redelivery_duplicate: already-ACKED messages redelivered.
            for index in range(1, 4):
                outcomes.append(
                    harness.deliver(
                        _synthetic_message(index, dialect),
                        scenario="redelivery_duplicate",
                    )
                )
            # 3. out_of_order_events: a fresh batch delivered in reverse,
            #    with crash windows interleaved.
            for index in reversed(range(6, SYNTHETIC_MESSAGES + 1)):
                outcomes.append(
                    harness.deliver(
                        _synthetic_message(index, dialect),
                        scenario="out_of_order_events",
                        crash_between_commit_and_ack=(index % 2 == 0),
                    )
                )
        finally:
            harness.close()
    violations = [outcome for outcome in outcomes if not outcome.exactly_once]
    per_scenario = {
        name: len([outcome for outcome in outcomes if outcome.scenario == name])
        for name in (scenario["name"] for scenario in async_failure_scenarios())
    }
    checks.append(
        {
            "check": "double-delivery",
            "status": "failed" if violations else "passed",
            "messages": len(outcomes),
            "scenarios": dict(sorted(per_scenario.items())),
            "exactly_once_all": not violations,
        }
    )
    if violations:
        first = violations[0]
        return EdgeResult(
            edge_id=edge.edge_id,
            kind=edge.kind,
            status="failed",
            repositories=edge.repositories,
            failed_member=bus_dependency,
            detail=(
                f"{bus_dependency}: {len(violations)}/{len(outcomes)} messages were"
                f" not exactly-once under redelivery (first: {first.message_id}"
                f" {first.effects} effects over {first.deliveries} deliveries)"
                f" — failed member {bus_dependency}"
            ),
            checks=tuple(checks),
            evidence_id=evidence_id,
        )
    return EdgeResult(
        edge_id=edge.edge_id,
        kind=edge.kind,
        status="passed",
        repositories=edge.repositories,
        failed_member=None,
        detail=(
            f"schema upgrade {upgrade.baseline_schema}->{upgrade.target_schema}"
            f" preserved {upgrade.seed_rows} seeded rows; {len(outcomes)} messages"
            " exactly-once across crash-redelivery, duplicate and out-of-order"
            " scenarios"
        ),
        checks=tuple(checks),
        evidence_id=evidence_id,
    )


# ---------------------------------------------------------------------------
# The runner and its report.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemVerificationReport:
    """The complete system-verification record for ONE frozen world."""

    schema: str = SYSTEM_VERIFICATION_SCHEMA
    scenario_id: str = ""
    tested_world_digest: str = ""
    applicability_digest: str = ""
    edge_results: tuple[EdgeResult, ...] = ()
    system_ready: bool = False
    failed_members: tuple[str, ...] = ()
    report_coverage: dict[str, Any] = field(default_factory=dict)
    baseline_drift: tuple[WorldInputDrift, ...] = ()
    environment_document: dict[str, Any] = field(default_factory=dict)
    evidence_records: tuple[EvidenceRecord, ...] = ()

    def to_document(self) -> dict[str, Any]:
        """The persisted shape — ``system_verification.edge_results``,
        ``verification.report_coverage`` and ``verification.baseline_drift``
        are the observability fragments the issue names."""
        return {
            "schema": self.schema,
            "scenario_id": self.scenario_id,
            "tested_world_digest": self.tested_world_digest,
            "applicability_digest": self.applicability_digest,
            "system_ready": self.system_ready,
            "failed_members": list(self.failed_members),
            "edge_results": [result.as_document() for result in self.edge_results],
            "report_coverage": dict(self.report_coverage),
            "baseline_drift": [drift.as_document() for drift in self.baseline_drift],
            "environment": dict(self.environment_document),
            "evidence_ids": [record.evidence_id for record in self.evidence_records],
        }


def _report_coverage(edges: Sequence[SystemEdge], results: Sequence[EdgeResult]) -> dict[str, Any]:
    """The ``verification.report_coverage`` fragment: what was expected,
    what verified, what failed, what is missing — and whether the
    coverage is complete."""
    by_id = {result.edge_id: result for result in results}
    expected = sorted(edge.edge_id for edge in edges)
    verified = sorted(edge_id for edge_id, result in by_id.items() if result.status == "passed")
    failed = sorted(edge_id for edge_id, result in by_id.items() if result.status != "passed")
    missing = sorted(set(expected) - set(by_id))
    return {
        "schema": REPORT_COVERAGE_SCHEMA,
        "edges_expected": expected,
        "edges_verified": verified,
        "edges_failed": failed,
        "edges_missing": missing,
        "checks_executed": sum(len(result.checks) for result in results),
        "complete": not failed and not missing,
    }


def run_system_verification(
    candidate_set: CandidateSet,
    environment: VerifierEnvironmentDocument,
    *,
    prior_world: CandidateSet | None = None,
) -> SystemVerificationReport:
    """Run the INDEPENDENT system verification on one frozen world.

    Order of authority (fail closed, each before anything runs):

    1. the verifier's own environment holds NO model credentials and NO
    publication authority (:func:`forbid_verifier_authority`);
    2. the set carries a PERSISTED world binding (unfrozen refuses);
    3. the twin's services ARE the set's members (verifying a different
    world than the frozen one is malformed) and every composed service
    resolves to a recorded exact artifact (an unresolved artifact
    verifies nothing);
    4. then the edges execute — the focused contract replay FIRST, the
    baseline projection, the environment's upgrade + delivery legs —
    and every PASSED edge records evidence bound to the frozen world.

    A failed edge names its failing member and blocks ``system_ready``;
    a failed edge records NO evidence. *prior_world*, when given,
    reports the named :func:`baseline_drift` between it and this world
    (the ``verification.baseline_drift`` fragment).
    """
    forbid_verifier_authority(environment)
    world = bound_tested_world_digest(candidate_set)
    applicability = bound_applicability_digest(candidate_set)
    twin = environment.twin
    twin_members = {service.repository_id for service in twin.services}
    set_members = {member.repository_id for member in candidate_set.members}
    if twin_members != set_members:
        raise ValueError(
            f"the twin's services {sorted(twin_members)} must be exactly the frozen"
            f" set's members {sorted(set_members)}: verify THIS world, not another"
        )
    services = sorted([*(service.repository_id for service in twin.services), *twin.dependencies])
    composed = environment_compose(candidate_set, services=services)
    unresolved = sorted(
        str(service["service"]) for service in composed["services"] if service.get("unresolved")
    )
    if unresolved:
        raise ValueError(
            f"cannot verify a world with unresolved artifacts: {unresolved}"
            " — a mutable tag or missing artifact is not verification identity"
        )

    edges = twin.edges()
    results = tuple(_execute_edge(twin, edge) for edge in edges)
    failed_members = tuple(
        sorted({result.failed_member for result in results if result.failed_member})
    )
    records = tuple(
        record_evidence(result.evidence_id, candidate_set, covers=result.repositories)
        for result in results
        if result.status == "passed"
    )
    return SystemVerificationReport(
        scenario_id=twin.scenario_id,
        tested_world_digest=world,
        applicability_digest=applicability,
        edge_results=results,
        system_ready=not failed_members and all(result.status == "passed" for result in results),
        failed_members=failed_members,
        report_coverage=_report_coverage(edges, results),
        baseline_drift=baseline_drift(prior_world, candidate_set)
        if prior_world is not None
        else (),
        environment_document={
            **environment.to_document(),
            "composed_services": composed["services"],
        },
        evidence_records=records,
    )


def _execute_edge(twin: TwinScenario, edge: SystemEdge) -> EdgeResult:
    if edge.kind == "contract":
        return _run_contract_edge(twin, edge)
    if edge.kind == "baseline":
        return _run_baseline_edge(twin, edge)
    if edge.kind == "environment":
        return _run_environment_edge(twin, edge)
    raise ValueError(f"unknown edge kind: {edge.kind!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Selective invalidation: replay a passed ledger against changed inputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectiveInvalidation:
    """The replay outcome: the named drift, the dependency changes it
    became, the evidence it invalidated — and the evidence that still
    applies (history retained for audit, authority withdrawn only where
    the CLAIMED inputs actually moved)."""

    drift: tuple[WorldInputDrift, ...]
    changes: tuple[str, ...]
    invalidated_evidence_ids: tuple[str, ...]
    retained_evidence_ids: tuple[str, ...]
    applied_ledger: EvidenceLedger

    __test__ = False


def replay_against_changed_inputs(
    ledger: EvidenceLedger, previous: CandidateSet, current: CandidateSet
) -> SelectiveInvalidation:
    """Replay a previously-passed ledger against a changed world.

    Digest-based, never whole-plan: the drift between the two worlds is
    named (:func:`baseline_drift`), translated into the per-dependency
    change vocabulary (:class:`MemberChange` per moved member — image
    rebuilds under an UNCHANGED source SHA included — plus
    :class:`TestBundleChange` and :class:`EnvironmentPinChange`), and
    applied to the ledger. ONLY the records whose CLAIMED inputs moved
    are superseded; everything else stays applicable; the superseded
    records stay inspectable forever with the drift as their reason.
    (A policy-ref change has no event type: it is judged state-wise by
    :meth:`EvidenceLedger.applicable_to`, which compares the refs.)
    """
    drift = baseline_drift(previous, current)
    changes: list[DependencyChange] = []
    previous_members = {member.repository_id: member for member in previous.members}
    current_members = {member.repository_id: member for member in current.members}
    for repository_id in sorted(set(previous_members) | set(current_members)):
        before = previous_members.get(repository_id)
        after = current_members.get(repository_id)
        before_identity = member_identity(before) if before is not None else None
        after_identity = member_identity(after) if after is not None else None
        if before_identity != after_identity:
            changes.append(
                MemberChange(repository_id, previous=before_identity, current=after_identity)
            )
    if previous.test_bundle_digest != current.test_bundle_digest:
        changes.append(TestBundleChange(previous.test_bundle_digest, current.test_bundle_digest))
    previous_pins = dict(previous.environment_pins)
    current_pins = dict(current.environment_pins)
    for service in sorted(set(previous_pins) | set(current_pins)):
        if previous_pins.get(service) != current_pins.get(service):
            changes.append(
                EnvironmentPinChange(service, previous_pins.get(service), current_pins.get(service))
            )
    applied = ledger
    invalidated: set[str] = set()
    for change in changes:
        invalidated |= applied.invalidated_by(change)
        applied = applied.apply(change)
    return SelectiveInvalidation(
        drift=drift,
        changes=tuple(type(change).__name__ for change in changes),
        invalidated_evidence_ids=tuple(sorted(invalidated)),
        retained_evidence_ids=tuple(sorted(applied.applicable_to(current))),
        applied_ledger=applied,
    )


# ---------------------------------------------------------------------------
# Readiness: the pure query, never a re-run.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemBlockedEdge:
    """Why one edge is not satisfied: ``missing`` (no evidence covers
    it), ``failed`` (the last executed result failed — replayed from the
    report when the caller holds one), ``stale`` (covering evidence was
    superseded) or ``invalid`` (covering evidence was recorded against
    DIFFERENT frozen identities)."""

    edge_id: str
    reason_kind: str
    detail: str

    __test__ = False


@dataclass(frozen=True)
class SystemReadiness:
    """The readiness answer — THREE DISTINCT booleans, by design.

    ``verification_ready`` is the pure query over executed evidence.
    ``merge_permitted`` and ``deploy_permitted`` record EXPLICIT human
    grants forge never derives: a passed environment test authorizes no
    production migration, and readiness itself is a human-consumed
    input, not an authority. The document reports all three separately
    so a reader always sees which axis said what.
    """

    verification_ready: bool
    merge_permitted: bool
    deploy_permitted: bool
    tested_world_digest: str
    blocked_edges: tuple[SystemBlockedEdge, ...] = ()
    satisfied_evidence_ids: tuple[str, ...] = ()

    __test__ = False

    def to_document(self) -> dict[str, Any]:
        return {
            "verification_ready": self.verification_ready,
            "merge_permitted": self.merge_permitted,
            "deploy_permitted": self.deploy_permitted,
            "tested_world_digest": self.tested_world_digest,
            "blocked_edges": [
                {
                    "edge_id": blocked.edge_id,
                    "kind": blocked.reason_kind,
                    "detail": blocked.detail,
                }
                for blocked in self.blocked_edges
            ],
            "satisfied_evidence_ids": list(self.satisfied_evidence_ids),
        }


def system_readiness(
    ledger: EvidenceLedger,
    candidate_set: CandidateSet,
    edges: Sequence[SystemEdge],
    *,
    merge_approval: bool = False,
    deploy_approval: bool = False,
    failed_edge_ids: Sequence[str] = (),
) -> SystemReadiness:
    """The readiness QUERY — a pure lookup, never a re-run.

    Every edge must be covered by NON-superseded evidence whose claimed
    applicability matches the set's frozen world
    (:meth:`EvidenceLedger.applicable_to`). Edges named in
    *failed_edge_ids* (from the last executed report) block as
    ``failed``. An unfrozen set refuses: a query over a world with no
    recorded binding is malformed. The two permission booleans record
    the caller's EXPLICIT grants unchanged — verification readiness
    never flips either of them.
    """
    world = bound_tested_world_digest(candidate_set)
    applicable = ledger.applicable_to(candidate_set)
    failed = set(failed_edge_ids)
    blocked: list[SystemBlockedEdge] = []
    satisfied: list[str] = []
    for edge in edges:
        if edge.edge_id in failed:
            blocked.append(
                SystemBlockedEdge(
                    edge.edge_id,
                    "failed",
                    f"the last executed verification failed on this edge ({edge.description})",
                )
            )
            continue
        covering = [
            record
            for record in ledger.records
            if all(record.covers(repository_id) for repository_id in edge.repositories)
        ]
        if not covering:
            blocked.append(
                SystemBlockedEdge(
                    edge.edge_id,
                    "missing",
                    f"no recorded verification covers {list(edge.repositories)}"
                    f" ({edge.description})",
                )
            )
            continue
        live = [record for record in covering if record.evidence_id in applicable]
        if live:
            satisfied.extend(record.evidence_id for record in live)
            continue
        if all(record.superseded for record in covering):
            reasons = "; ".join(
                sorted(
                    {record.superseded_reason for record in covering if record.superseded_reason}
                )
            )
            blocked.append(
                SystemBlockedEdge(
                    edge.edge_id,
                    "stale",
                    f"covering evidence was invalidated and superseded ({reasons})",
                )
            )
        else:
            blocked.append(
                SystemBlockedEdge(
                    edge.edge_id,
                    "invalid",
                    "covering evidence was recorded against different frozen"
                    f" identities (this world: {world[:12]}) — stale evidence is"
                    " never silently reused",
                )
            )
    return SystemReadiness(
        verification_ready=not blocked,
        merge_permitted=merge_approval,
        deploy_permitted=deploy_approval,
        tested_world_digest=world,
        blocked_edges=tuple(blocked),
        satisfied_evidence_ids=tuple(sorted(set(satisfied))),
    )
