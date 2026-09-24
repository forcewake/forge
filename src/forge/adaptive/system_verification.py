"""R36-19 (#278) — verify a complete two-service CandidateSet AS A SYSTEM.

Two separately green PRs are not one compatible system change. The
two-writer qualification (``two_writer_qualification.py``, #269) drove
the MODELS and recorded verification evidence in-harness; the durable
saga (``saga_durable.py``, #277) made publication itself durable and
provider-realistic; the verdict binding (``verification_binding.py``,
#273) binds a verdict to the exact candidate. This module is the piece
the review still demanded: an INDEPENDENT EXECUTION that verifies the
coordinated change against the FROZEN TESTED WORLD — never in the
coding agent's privileged environment, and never with a shred of the
authority it exists to check:

- **:func:`freeze_tested_world` composition** — the twin freezes its
  world through the existing entry (members at their candidate oids AND
  exact image artifacts — a rebuilt image under an unchanged source SHA
  is a different world — plus the contract bundle, test bundle,
  environment profile, external pins and policy refs), so every result
  binds to the persisted digests.
- **:class:`VerifierEnvironmentDocument` /
  :class:`VerifierAuthorityError`** — the runner's own environment
  description PROVES it holds neither model credentials nor repository
  publication authority; a document carrying either is refused before
  anything executes.
- **The PYTHON-shaped twin** (:class:`TwinScenario`) — two services
  (producer + consumer), a shared message contract, a pinned baseline
  dependency and two synthetic dependencies: a REAL sqlite schema
  upgrade from a pinned baseline schema (seeded data preserved under the
  release canary's fingerprint discipline — counts plus sha256 per
  table, compared across the upgrade) and a REDIS-less idempotency
  check through a deterministic in-process double-delivery harness with
  the crash window injected BETWEEN state persistence and message
  acknowledgement. The .NET/TRX scenarios stay in the ExpectedReports
  machinery (:mod:`forge.adaptive.verification_binding`); the python
  twin proves the MECHANISM here.
- **Per-edge results** (:class:`EdgeResult`) — the producer→consumer
  contract edge, the consumer→baseline edge and the environment edge.
  A failed edge NAMES the failing member and blocks ``system_ready``;
  missing evidence blocks the readiness query; the
  ``verification.report_coverage`` fragment records what was expected,
  verified, failed and missing.
- **Selective invalidation** (:func:`replay_against_changed_inputs`) —
  a previously passed record replayed against changed baseline/test
  bundle inputs invalidates ONLY the affected edges, by dependency
  identity, with the drift NAMED (:func:`baseline_drift` → the
  ``verification.baseline_drift`` fragment) and the history retained
  for audit (superseded, never deleted).
- **Readiness as a query** (:func:`system_readiness`) — a pure lookup
  over executed evidence + applicability that NEVER re-runs anything,
  reporting verification readiness, merge permission and deployment
  permission as THREE DISTINCT booleans: a passed environment test
  authorizes no production migration, and approvals are human grants
  forge records but never derives.

Everything executes against stdlib sqlite in temporary databases — real
schema DDL, real INSERT/SELECT round trips, real transactional commit
boundaries — deterministic by construction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from forge.adaptive.models import CandidateSet, CandidateSetMember
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
    db_upgrade_plan,
    environment_compose,
    freeze_tested_world,
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

#: Digest domain tags for the twin's own bundles (the same versioning
#: discipline every forge schema tag carries).
TWIN_CONTRACT_BUNDLE_SCHEMA = "forge.twin.contract-bundle/1"
TWIN_TEST_BUNDLE_SCHEMA = "forge.twin.test-bundle/1"
TWIN_ENVIRONMENT_PROFILE_SCHEMA = "forge.twin.environment-profile/1"

#: The pinned baseline dependency's API dialects. ``api/v3`` adds the
#: ``region`` column ``api/v2`` rows never carried — the concrete shape
#: a consumer↔baseline incompatibility takes.
BASELINE_API_V2 = "api/v2"
BASELINE_API_V3 = "api/v3"

#: The synthetic volumes (fixed so every report is reproducible).
SYNTHETIC_MESSAGES = 12  # contract-edge message replay
SYNTHETIC_SEED_ROWS = 25  # db upgrade: rows seeded at the N-1 schema
SYNTHETIC_BASELINE_ROWS = 15  # baseline-edge projection replay


# ---------------------------------------------------------------------------
# Deterministic identity helpers (hex-only seeds, the harness convention).
# ---------------------------------------------------------------------------


def _oid(seed: str) -> str:
    """A lowercase 40-hex git sha shape derived from *seed*."""
    return (seed * 40)[:40]


def _digest64(seed: str) -> str:
    """A lowercase 64-hex sha256 shape derived from *seed*."""
    return (seed * 64)[:64]


def _image(seed: str) -> str:
    """A sha256-prefixed image digest, as registries spell them."""
    return f"sha256:{_digest64(seed)}"


def _canonical_digest(payload: object) -> str:
    """sha256 over the canonical JSON of *payload* (sorted keys — the
    determinism rule the whole adaptive package uses)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
# The PYTHON-shaped twin: two services, one shared contract, a pinned
# baseline and two synthetic dependencies (db upgrade + delivery).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TwinContract:
    """One dialect of the shared producer→consumer message contract.

    ``fields`` is what a PRODUCER build emits at this version;
    ``required`` is what a CONSUMER build demands. The shared bundle's
    dialect is the referee: a build speaking another version is the
    member the failed edge names.
    """

    schema_version: str
    fields: tuple[str, ...]
    required: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fields": list(self.fields),
            "required": list(self.required),
        }


#: The current shared dialect (v2): the producer widens the order event
#: with ``region`` and the consumer's projection requires it.
SHARED_CONTRACT_V2 = TwinContract(
    schema_version="v2",
    fields=("id", "total", "region"),
    required=("id", "total", "region"),
)

#: The PREVIOUS dialect (v1): no ``region`` — the shape the OLD builds
#: speak, for the old/new producer-consumer negative combinations.
SHARED_CONTRACT_V1 = TwinContract(
    schema_version="v1",
    fields=("id", "total"),
    required=("id", "total"),
)


@dataclass(frozen=True)
class TwinService:
    """One service of the twin, at its EXACT build identity.

    ``kind`` implies the member role (``producer``/``consumer`` are the
    changed writers; ``pinned`` rides as a baseline at its PINNED
    digest). The behavior fields describe what the artifact at
    ``image_digest`` does (the dialect it emits/expects, the baseline
    API it serves or requires) — the scenario declares its builds, the
    verifier EXECUTES the declaration against real sqlite and delivery
    machinery.
    """

    repository_id: str
    kind: str  # "producer" | "consumer" | "pinned"
    base_oid: str
    candidate_oid: str
    image_digest: str
    emits: TwinContract | None = None  # the producer build's dialect
    expects: TwinContract | None = None  # the consumer build's dialect
    requires_baseline_api: str | None = None  # the consumer's integration demand
    baseline_api: str | None = None  # the pinned build's served API
    branch_head_oid: str = ""  # where the branch moved PAST the pin


@dataclass(frozen=True)
class TwinScenario:
    """The complete PYTHON-shaped twin scenario, frozen through the
    existing models (never around them)."""

    scenario_id: str
    objective: str
    services: tuple[TwinService, ...]
    shared_contract: TwinContract
    contract_topic: str = "orders.events.order-created"
    dependencies: tuple[str, ...] = ("orders-db", "orders-bus")
    environment_pins: tuple[tuple[str, str], ...] = (
        ("orders-db", _image("db01")),
        ("orders-bus", _image("b502")),
    )
    policy_refs: tuple[str, ...] = ("compat/orders-matrix@1",)
    test_bundle_extra_cases: tuple[str, ...] = ()
    plan_revision: int = 1
    work_contract_digest: str = _digest64("7e1d")

    # -- service lookups ---------------------------------------------------

    def _by_kind(self, kind: str) -> TwinService:
        found = [service for service in self.services if service.kind == kind]
        if len(found) != 1:
            raise ValueError(f"twin {self.scenario_id!r} needs exactly one {kind!r} service")
        return found[0]

    def producer(self) -> TwinService:
        return self._by_kind("producer")

    def consumer(self) -> TwinService:
        return self._by_kind("consumer")

    def pinned(self) -> TwinService:
        return self._by_kind("pinned")

    # -- the complete frozen world ------------------------------------------

    def contract_bundle_digest(self) -> str:
        return _canonical_digest(
            {
                "schema": TWIN_CONTRACT_BUNDLE_SCHEMA,
                "topic": self.contract_topic,
                "shared": self.shared_contract.as_document(),
                "producer_emits": (self.producer().emits or self.shared_contract).as_document(),
                "consumer_expects": (self.consumer().expects or self.shared_contract).as_document(),
            }
        )

    def test_bundle_digest(self) -> str:
        return _canonical_digest(
            {
                "schema": TWIN_TEST_BUNDLE_SCHEMA,
                "bundles": {
                    "contract-replay": {
                        "cases": SYNTHETIC_MESSAGES,
                        "extra_cases": list(self.test_bundle_extra_cases),
                    },
                    "baseline-projection": {"cases": SYNTHETIC_BASELINE_ROWS},
                    "db-upgrade": {"cases": SYNTHETIC_SEED_ROWS},
                    "delivery": {"scenarios": [s["name"] for s in async_failure_scenarios()]},
                },
            }
        )

    def environment_profile_digest(self) -> str:
        return _canonical_digest(
            {
                "schema": TWIN_ENVIRONMENT_PROFILE_SCHEMA,
                "profile": "python-twin-integration-v1",
                "services": [
                    *(service.repository_id for service in self.services),
                    *self.dependencies,
                ],
            }
        )

    def freeze(self) -> CandidateSet:
        """The COMPLETE frozen world: every member (changed and pinned)
        at its candidate oid AND exact image artifact, the contract
        bundle, the test bundle, the environment profile, the external
        pins and the policy refs — digests PERSISTED at freeze time
        through :func:`freeze_tested_world`."""
        members = tuple(
            CandidateSetMember(
                repository_id=service.repository_id,
                base_oid=service.base_oid,
                candidate_oid=service.candidate_oid,
                image_digest=service.image_digest,
                role="changed" if service.kind in ("producer", "consumer") else "baseline",
            )
            for service in sorted(self.services, key=lambda service: service.repository_id)
        )
        return freeze_tested_world(
            CandidateSet(
                work_id=self.scenario_id,
                plan_revision=self.plan_revision,
                work_contract_digest=self.work_contract_digest,
                members=members,
            ),
            contract_bundle_digest=self.contract_bundle_digest(),
            test_bundle_digest=self.test_bundle_digest(),
            environment_profile_digest=self.environment_profile_digest(),
            environment_pins=dict(self.environment_pins),
            policy_refs=self.policy_refs,
        )

    # -- the verification edges ----------------------------------------------

    def edges(self) -> tuple[SystemEdge, ...]:
        """The three edges system verification judges, per member name:

        - ``contract:producer->consumer`` — the shared contract replayed
          with synthetic messages against BOTH changed builds (covers
          the two writers only);
        - ``baseline:consumer->pinned`` — the consumer's projection
          replayed against the pinned baseline at its PINNED digest
          (covers the consumer and the pinned member);
        - ``environment:integration`` — the composed environment itself:
          the schema upgrade from the pinned baseline schema and the
          delivery/idempotency behavior (covers EVERY member — the
          compose binds them all, so any member's move touches it).
        """
        producer, consumer, pinned = self.producer(), self.consumer(), self.pinned()
        return (
            SystemEdge(
                edge_id=f"contract:{producer.repository_id}->{consumer.repository_id}",
                kind="contract",
                repositories=(producer.repository_id, consumer.repository_id),
                description="shared contract replayed with synthetic messages",
            ),
            SystemEdge(
                edge_id=f"baseline:{consumer.repository_id}->{pinned.repository_id}",
                kind="baseline",
                repositories=(consumer.repository_id, pinned.repository_id),
                description="consumer projection replayed against the pinned baseline digest",
            ),
            SystemEdge(
                edge_id="environment:integration",
                kind="environment",
                repositories=tuple(sorted(service.repository_id for service in self.services)),
                description="db schema upgrade from the pinned baseline + delivery idempotency",
            ),
        )

    # -- mutations (the stale-evidence and negative-combo probes) -------------

    def _with_service(self, service: TwinService) -> TwinScenario:
        return replace(
            self,
            services=tuple(
                service if s.repository_id == service.repository_id else s for s in self.services
            ),
        )

    def with_old_producer(self) -> TwinScenario:
        """The OLD producer build (the base artifact, dialect v1) against
        the NEW consumer — separately green branches, incompatible as a
        system. The contract edge must FAIL naming the producer."""
        producer = self.producer()
        return self._with_service(
            replace(
                producer,
                candidate_oid=producer.base_oid,
                image_digest=_image("01d9"),
                emits=SHARED_CONTRACT_V1,
            )
        )

    def with_old_consumer(self) -> TwinScenario:
        """The NEW producer against the OLD consumer build (base
        artifact, dialect v1) — the mirror combination. The contract
        edge must FAIL naming the consumer."""
        consumer = self.consumer()
        return self._with_service(
            replace(
                consumer,
                candidate_oid=consumer.base_oid,
                image_digest=_image("01dc"),
                expects=SHARED_CONTRACT_V1,
            )
        )

    def with_rebuilt_image(self, repository_id: str, seed: str) -> TwinScenario:
        """The member's image was REBUILT under an UNCHANGED source SHA
        (candidate oid) — the review's exact arm: source SHAs matching
        while the artifact changed still invalidates every result that
        covered the old world."""
        target = next(s for s in self.services if s.repository_id == repository_id)
        return self._with_service(replace(target, image_digest=_image(seed)))

    def with_changed_test_bundle(self, extra_case: str) -> TwinScenario:
        """The test bundle the world is judged under changed (a new case
        in the contract-replay suite): the bundle digest moves even
        though no source SHA did."""
        return replace(
            self,
            scenario_id=f"{self.scenario_id}-rebundled",
            test_bundle_extra_cases=(*self.test_bundle_extra_cases, extra_case),
        )


def default_twin_scenario() -> TwinScenario:
    """The concrete default twin: the producer widens the order event
    with ``region`` (v2), the consumer's projection requires it, the
    ledger baseline stays PINNED at api/v3 (its branch has moved past
    the pin), and the two synthetic dependencies ride exact pins."""
    return TwinScenario(
        scenario_id="twin-orders-1",
        objective="Widen the order event with region (producer) and require it (consumer)",
        services=(
            TwinService(
                repository_id="orders-api",
                kind="producer",
                base_oid=_oid("a1b2"),
                candidate_oid=_oid("a3c4"),
                image_digest=_image("a5d6"),
                emits=SHARED_CONTRACT_V2,
            ),
            TwinService(
                repository_id="orders-projection",
                kind="consumer",
                base_oid=_oid("b1c2"),
                candidate_oid=_oid("b3d4"),
                image_digest=_image("b5e6"),
                expects=SHARED_CONTRACT_V2,
                requires_baseline_api=BASELINE_API_V3,
            ),
            TwinService(
                repository_id="ledger-baseline",
                kind="pinned",
                base_oid=_oid("c1d2"),
                candidate_oid=_oid("c1d2"),  # the PIN
                image_digest=_image("c3f4"),
                baseline_api=BASELINE_API_V3,
                branch_head_oid=_oid("c9e8"),  # the branch moved past the pin
            ),
        ),
        shared_contract=SHARED_CONTRACT_V2,
    )


# ---------------------------------------------------------------------------
# Synthetic dependency #1: the REAL sqlite schema-upgrade path.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TwinMigration:
    """One schema migration: a version stamp plus the statements that
    move the schema to it. The FIRST migration is the PINNED baseline
    schema — upgrades verify from THERE, never from an empty database
    (an empty schema proves nothing about the rows already in tables)."""

    version: str
    statements: tuple[str, ...]


#: The twin's schema ladder: v1 (the pinned baseline — no ``region``)
#: then v2 (adds ``region`` with a backfill default).
TWIN_MIGRATIONS: tuple[TwinMigration, ...] = (
    TwinMigration(
        version="1",
        statements=(
            "CREATE TABLE _schema_version (version TEXT PRIMARY KEY)",
            "CREATE TABLE orders (id TEXT PRIMARY KEY, total INTEGER NOT NULL)",
        ),
    ),
    TwinMigration(
        version="2",
        statements=("ALTER TABLE orders ADD COLUMN region TEXT NOT NULL DEFAULT 'eu'",),
    ),
)

#: (table, identity-expression) for the preservation fingerprint — the
#: release canary's discipline in-process: the sha256 over the ORDERED
#: per-row identity strings, compared across the upgrade. Only the
#: columns that must SURVIVE enter the identity; the added column does
#: not (a backfilled default is not data loss).
PRESERVATION_FINGERPRINTS: tuple[tuple[str, str], ...] = (("orders", "id || '|' || total"),)


def _apply_migration(conn: sqlite3.Connection, migration: TwinMigration) -> None:
    with conn:  # one transaction per migration: DDL + version stamp land together
        for statement in migration.statements:
            conn.execute(statement)
        conn.execute(
            "INSERT OR REPLACE INTO _schema_version (version) VALUES (?)", (migration.version,)
        )


def _preservation_fingerprint(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Per table: ``<table> <count> <sha256>`` over the ordered row
    identities — the canary's in-process fingerprint."""
    rows: list[str] = []
    for table, identity in PRESERVATION_FINGERPRINTS:
        count = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        identities = [
            str(row[0]) for row in conn.execute(f"SELECT {identity} AS x FROM {table} ORDER BY x")
        ]
        digest = hashlib.sha256("|".join(identities).encode("utf-8")).hexdigest()
        rows.append(f"{table} {count} {digest}")
    return tuple(rows)


def _synthetic_seed_rows(count: int) -> tuple[tuple[str, int], ...]:
    """Deterministic synthetic rows for the N-1 schema."""
    return tuple((f"ord-{index:03d}", 1000 + index * 7) for index in range(1, count + 1))


@dataclass(frozen=True)
class SchemaUpgradeOutcome:
    """The db-upgrade leg's computed outcome (nothing assumed)."""

    plan_steps: tuple[str, ...]
    baseline_schema: str
    target_schema: str
    seed_rows: int
    baseline_fingerprint: tuple[str, ...]
    target_fingerprint: tuple[str, ...]
    preserved: bool
    schema_advanced: bool
    post_upgrade_write: bool
    detail: str

    def as_document(self) -> dict[str, Any]:
        return {
            "plan_steps": list(self.plan_steps),
            "baseline_schema": self.baseline_schema,
            "target_schema": self.target_schema,
            "seed_rows": self.seed_rows,
            "baseline_fingerprint": list(self.baseline_fingerprint),
            "target_fingerprint": list(self.target_fingerprint),
            "preserved": self.preserved,
            "schema_advanced": self.schema_advanced,
            "post_upgrade_write": self.post_upgrade_write,
            "detail": self.detail,
        }


def execute_schema_upgrade(
    path: Path,
    migrations: Sequence[TwinMigration] = TWIN_MIGRATIONS,
    *,
    seed_rows: Sequence[tuple[str, int]] | None = None,
    baseline_schema: str = "1",
    target_schema: str = "2",
) -> SchemaUpgradeOutcome:
    """Execute a REAL upgrade between two schema versions on *path*.

    Driven by :func:`db_upgrade_plan` (a data-bearing baseline, the
    migrations, then verification of the surviving data): apply the
    BASELINE migration, seed synthetic rows at the N-1 schema,
    fingerprint them, apply the upgrade migrations, fingerprint again —
    preservation means counts AND per-table sha256 digests are EQUAL
    across the upgrade. The schema must genuinely ADVANCE (the version
    ladder records the target, the new column exists) and the new
    schema must accept writes — an upgrade that only "succeeds" on an
    empty database proves nothing, and neither does a no-op.
    """
    plan = db_upgrade_plan(baseline_schema, target_schema, synthetic_data=True)
    seeds = tuple(seed_rows) if seed_rows is not None else _synthetic_seed_rows(SYNTHETIC_SEED_ROWS)
    baseline = next((m for m in migrations if m.version == baseline_schema), migrations[0])
    ladder = tuple(m for m in migrations if baseline_schema < m.version <= target_schema)
    conn = sqlite3.connect(path)
    try:
        # step 1: snapshot_baseline — build the PINNED baseline schema and
        # seed it with synthetic data (never verify from an empty schema).
        _apply_migration(conn, baseline)
        with conn:
            conn.executemany("INSERT INTO orders (id, total) VALUES (?, ?)", seeds)
        seeded_fingerprint = _preservation_fingerprint(conn)

        # step 2: apply_migrations — the real upgrade ladder.
        for migration in ladder:
            _apply_migration(conn, migration)

        # step 3: verify_synthetic_data — counts AND digests equal.
        upgraded_fingerprint = _preservation_fingerprint(conn)
        versions = {str(row[0]) for row in conn.execute("SELECT version FROM _schema_version")}
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(orders)")}
        schema_advanced = target_schema in versions and "region" in columns
        # the new schema must accept NEW-shaped writes (region explicit).
        post_upgrade_write = True
        try:
            with conn:
                conn.execute("INSERT INTO orders (id, total, region) VALUES ('ord-post', 1, 'us')")
        except sqlite3.Error:
            post_upgrade_write = False
    finally:
        conn.close()
    preserved = seeded_fingerprint == upgraded_fingerprint
    if not preserved:
        detail = "the upgrade did NOT preserve the seeded rows (fingerprint changed)"
    elif not schema_advanced:
        detail = f"the schema did not advance to {target_schema}"
    elif not post_upgrade_write:
        detail = "the upgraded schema rejected a new-shaped write"
    else:
        detail = (
            f"upgrade {baseline_schema}->{target_schema} preserved every seeded row"
            f" ({len(seeds)} rows — counts and sha256 fingerprints equal)"
        )
    return SchemaUpgradeOutcome(
        plan_steps=tuple(plan["steps"]),
        baseline_schema=baseline_schema,
        target_schema=target_schema,
        seed_rows=len(seeds),
        baseline_fingerprint=seeded_fingerprint,
        target_fingerprint=upgraded_fingerprint,
        preserved=preserved,
        schema_advanced=schema_advanced,
        post_upgrade_write=post_upgrade_write,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Synthetic dependency #2: the REDIS-less double-delivery harness.
# ---------------------------------------------------------------------------


class CrashWindowOpened(RuntimeError):
    """The injected failure: the consumer's effect COMMITTED (state
    persistence durable) but the process died BEFORE the message
    acknowledgement was recorded — the broker will redeliver."""


@dataclass(frozen=True)
class DeliveryOutcome:
    """One message's delivery outcome through the harness."""

    message_id: str
    scenario: str  # the async_failure_scenarios() name being replayed
    deliveries: int
    effects: int
    acks: int

    @property
    def exactly_once(self) -> bool:
        """Exactly-once: ONE effect per message no matter how many
        deliveries it took (a JSON-schema match is not idempotency
        proof — the projected ROW count is)."""
        return self.effects == 1 and self.acks >= 1


class DoubleDeliveryHarness:
    """A deterministic in-process broker + consumer twin.

    The consumer keeps three sqlite tables — ``processed_messages``
    (the idempotency key), ``projected_orders`` (the effect) and
    ``message_acks`` (the acknowledgement) — in ONE database, so the
    crash window is a REAL transaction boundary: the effect insert and
    the idempotency key commit TOGETHER, and the ack is a SEPARATE
    later write the injected death can skip. Delivery with
    ``crash_between_commit_and_ack`` replays exactly the business
    scenario's failure: state persisted, acknowledgement lost,
    redelivery arrives — and the idempotency key must keep the effect
    at exactly one.
    """

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path)
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS processed_messages"
                " (message_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS projected_orders"
                " (message_id TEXT NOT NULL, order_id TEXT NOT NULL,"
                " total INTEGER NOT NULL, region TEXT,"
                " PRIMARY KEY (message_id, order_id))"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS message_acks"
                " (message_id TEXT PRIMARY KEY, acked_at TEXT NOT NULL)"
            )

    def close(self) -> None:
        self._conn.close()

    # -- the consumer handler ------------------------------------------------

    def _handle(self, message: Mapping[str, Any], *, die_before_ack: bool) -> str:
        message_id = str(message["id"])
        with self._conn:  # state persistence: effect + idempotency key, together
            seen = self._conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if seen is None:
                self._conn.execute(
                    "INSERT INTO processed_messages (message_id, processed_at) VALUES (?, 'now')",
                    (message_id,),
                )
                self._conn.execute(
                    "INSERT INTO projected_orders (message_id, order_id, total, region)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        message_id,
                        message_id,
                        int(message.get("total") or 0),
                        str(message.get("region") or ""),
                    ),
                )
        if seen is None and die_before_ack:
            # FAILURE INJECTED between state persistence and the ack: the
            # effect is durable, the broker will never see an ack.
            raise CrashWindowOpened(message_id)
        with self._conn:  # the acknowledgement — a separate durable write
            self._conn.execute(
                "INSERT OR IGNORE INTO message_acks (message_id, acked_at) VALUES (?, 'now')",
                (message_id,),
            )
        return "effect-applied" if seen is None else "duplicate-no-effect"

    # -- the broker ------------------------------------------------------------

    def deliver(
        self,
        message: Mapping[str, Any],
        *,
        scenario: str = "crash_between_commit_and_ack",
        crash_between_commit_and_ack: bool = False,
    ) -> DeliveryOutcome:
        """Deliver *message*, redelivering when the crash window ate the
        ack. Pure duplicates of an ACKED message are also supported
        (``redelivery_duplicate`` with no crash): the redelivery must
        still produce no second effect."""
        message_id = str(message["id"])
        deliveries = 0
        if crash_between_commit_and_ack:
            deliveries += 1
            try:
                self._handle(message, die_before_ack=True)
            except CrashWindowOpened:
                pass  # the broker observes no ack and redelivers
        deliveries += 1
        self._handle(message, die_before_ack=False)  # the (re)delivery
        if scenario == "redelivery_duplicate":
            deliveries += 1
            self._handle(message, die_before_ack=False)  # a pure duplicate
        effects = int(
            self._conn.execute(
                "SELECT count(*) FROM projected_orders WHERE message_id = ?", (message_id,)
            ).fetchone()[0]
        )
        acks = int(
            self._conn.execute(
                "SELECT count(*) FROM message_acks WHERE message_id = ?", (message_id,)
            ).fetchone()[0]
        )
        return DeliveryOutcome(
            message_id=message_id,
            scenario=scenario,
            deliveries=deliveries,
            effects=effects,
            acks=acks,
        )


def _synthetic_message(index: int, fields: Sequence[str]) -> dict[str, Any]:
    """One deterministic synthetic message in the given dialect."""
    values: dict[str, Any] = {
        "id": f"ord-{index:04d}",
        "total": 1000 + index * 7,
        "region": ("eu", "us", "apac")[index % 3],
        "schema_version": "v2",
    }
    return {field: values.get(field, f"{field}-{index:04d}") for field in fields}


def _baseline_row(index: int, api_version: str) -> dict[str, Any]:
    """One deterministic synthetic row served by the PINNED baseline at
    its API version — ``api/v3`` rows carry ``region``, ``api/v2`` rows
    never did."""
    row: dict[str, Any] = {"id": f"led-{index:04d}", "total": 500 + index * 3}
    if api_version == BASELINE_API_V3:
        row["region"] = ("eu", "us", "apac")[index % 3]
    return row


# ---------------------------------------------------------------------------
# The edge executions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemEdge:
    """One verification edge of the system change, in member names."""

    edge_id: str
    kind: str  # "contract" | "baseline" | "environment"
    repositories: tuple[str, ...]
    description: str = ""

    __test__ = False  # the pytest-collection guard (domain noun, not a test)


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
