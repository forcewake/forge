"""The DETERMINISTIC REFERENCE twin — the PYTHON-shaped two-service
scenario system verification executes (R36-19 #278 → R37-19 #300).

**This module is REFERENCE material** (label:
:data:`forge.adaptive.reference.REFERENCE_PACKAGE_LABEL`): the
in-process, synthetic-services, deterministic-by-construction scenario
the runtime verifier (:mod:`forge.adaptive.system_verification`) EXECUTES
against. It was extracted therefrom so that importing the runtime
contracts never turns the scenario's assumptions into deployed
guarantees — the twin pins the mechanism's semantics cheaply; the
verified executor (``verification_executor.py``, #296) proves them
against real artifacts and processes.

What lives here (the scenario + its two synthetic dependencies):

- the synthetic identity helpers (``_oid``/``_digest64``/``_image``) and
  the frozen volumes (``SYNTHETIC_*``);
- :class:`TwinContract` / :class:`TwinService` / :class:`TwinScenario`
  — the scenario's builds and dialects, frozen through the existing
  models (``freeze()`` → :func:`freeze_tested_world`), plus the
  negative-combination and stale-evidence mutations;
- :class:`SystemEdge` — the edge DECLARATIONS a scenario judges
  (the executed :class:`~forge.adaptive.system_verification.EdgeResult`
  vocabulary stays runtime);
- the REAL sqlite schema-upgrade path (:class:`TwinMigration`,
  :func:`execute_schema_upgrade` — real DDL, seeded data, preservation
  fingerprints);
- the REDIS-less double-delivery harness (:class:`DoubleDeliveryHarness`
  — a deterministic in-process broker + consumer with the crash window
  injected BETWEEN state persistence and acknowledgement);
- the synthetic replay data generators (``_synthetic_message`` /
  ``_baseline_row``) and the baseline API dialects
  (:data:`BASELINE_API_V2` / :data:`BASELINE_API_V3`).

Everything executes against stdlib sqlite in temporary databases — real
schema DDL, real INSERT/SELECT round trips, real transactional commit
boundaries — deterministic by construction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from forge.adaptive.models import CandidateSet, CandidateSetMember
from forge.adaptive.verification_sets import (
    async_failure_scenarios,
    db_upgrade_plan,
    freeze_tested_world,
)

__all__ = [
    "TWIN_CONTRACT_BUNDLE_SCHEMA",
    "TWIN_TEST_BUNDLE_SCHEMA",
    "TWIN_ENVIRONMENT_PROFILE_SCHEMA",
    "TWIN_REFERENCE_LABEL",
    "BASELINE_API_V2",
    "BASELINE_API_V3",
    "SYNTHETIC_MESSAGES",
    "SYNTHETIC_SEED_ROWS",
    "SYNTHETIC_BASELINE_ROWS",
    "CrashWindowOpened",
    "DeliveryOutcome",
    "DoubleDeliveryHarness",
    "SchemaUpgradeOutcome",
    "SystemEdge",
    "TwinContract",
    "TwinMigration",
    "TwinScenario",
    "TwinService",
    "default_twin_scenario",
    "execute_schema_upgrade",
]

#: The twin's retention label (R37-15/#296): the in-process twin stays
#: the DETERMINISTIC REFERENCE beside the verified executor — the
#: mechanism's cheap, reproducible semantics pin.
TWIN_REFERENCE_LABEL = "deterministic-reference-twin"

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


# ---------------------------------------------------------------------------
# The edge declarations a scenario judges (the executed-outcome records —
# EdgeResult and friends — stay runtime in system_verification).
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
