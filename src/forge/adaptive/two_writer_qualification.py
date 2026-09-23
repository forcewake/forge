"""Qualify ONE two-writer WorkPackage against a complete CandidateSet (R32-22).

The 2026-09-23 e2e-qualification research (topic 6) gives the two-writer
slice of R32-22 three industry answers, and this module is the harness
that DRIVES the existing models through all of them — it owns no new
coordination machinery of its own:

- **The scenario** (:class:`TwoWriterScenario`) — one concrete
  producer/consumer change: repo A (producer, writable), repo B
  (consumer, writable, depends on A), repo C (a pinned baseline
  dependency, frozen at its PINNED digest — never the branch head), and
  repo D (a read-only neighbor that never receives writer credentials).
  :meth:`TwoWriterScenario.freeze` snapshots the COMPLETE tested world
  through :func:`forge.adaptive.workpackage.freeze_candidate_set` +
  :func:`forge.adaptive.verification_sets.freeze_verified_world`:
  changed identities, baseline identities, the contract bundle
  (producer's changed contract + consumer's expectation), the per-repo
  test bundle and the environment profile, all digested and persisted
  on the set.
- **Dependency-gated child start**
  (:func:`drive_dependency_gated_phases`) — drives the durable
  :class:`~forge.adaptive.workpackage.WorkPackageCoordinator` through
  phase 1 (the producer child) and phase 2 (the consumer child) and
  captures the proof the review demands: the consumer CANNOT start
  while the producer is unproven or failed (the typed
  :class:`~forge.adaptive.workpackage.PhaseAdvanceRefused` surfaces),
  and only a PROVEN producer outcome — recorded against the package's
  ACTIVE tested world — admits phase 2.
- **The kill-at-step-k matrix** (:func:`kill_at_step_matrix`,
  :func:`pivot_kill_matrix`, :func:`lost_response_adoption`) — the
  saga-testing discipline: inject coordinator death after EVERY
  publication step (prepare, fence-check, commit-intent, provider
  commit, verify, record) of EACH writer's publication, restart the
  real :class:`~forge.adaptive.publication_saga.SagaCoordinator` and
  reconcile. Every cell must show: no duplicate publication (the
  commit-intent marker is idempotent), no destructive rollback
  (branches only ever grow — histories are prefix-preserved), recovery
  idempotent under re-execution, and convergence to completed or
  parked-with-reason. Post-pivot ("PR merged") recovery is
  FORWARD-ONLY: the merged publication stands and any collision parks
  for a human instead of rolling back.
- **readiness = can-i-deploy** (:func:`readiness`) — promotion is a
  QUERY over the recorded :class:`~forge.adaptive.verification_sets.EvidenceLedger`,
  never a re-run: every dependency edge (A→B contract, B→C baseline, D
  observation) must have passing verification AT THE FROZEN DIGESTS;
  any edge missing, stale (superseded) or invalid (recorded against
  different frozen identities) is blocked BY NAME.
- **Credential fan-out** (:func:`verify_credential_scope`) — the MRP-03
  authorization check restated for staging: each writer's credentials
  scope to its ONE repository, and a read-only repository NEVER
  receives a writer credential name
  (:class:`CredentialScopeViolation`).

Everything is driven against fakes and the in-memory saga store: this
is the pure/DB-level qualification, not a provider-wired run (see
``docs/evaluation/2026-09-23-two-writer/README.md`` for what remains
for a live two-writer run).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from forge.adaptive.models import CandidateSet
from forge.adaptive.publication_saga import (
    InMemorySagaStore,
    PublicationSaga,
    SagaCoordinator,
    begin_saga,
    observe_human_merge,
    provider_committed,
    record_commit_intent,
    review_opened,
    verified,
)
from forge.adaptive.verification_sets import (
    EvidenceLedger,
    bound_tested_world_digest,
    record_evidence,
)
from forge.adaptive.workpackage import (
    PhaseAdvanceRefused,
    WorkItemRef,
    WorkPackage,
    WorkPackageCoordinator,
    freeze_candidate_set,
    identity_changed,
    lane_assignment,
)
from forge.durable import FlowRun

__all__ = [
    "REPORT_SCHEMA",
    "CONTRACT_BUNDLE_SCHEMA",
    "TEST_BUNDLE_SCHEMA",
    "ENVIRONMENT_PROFILE_SCHEMA",
    "PUBLICATION_STEPS",
    "BlockedEdge",
    "ContractBundle",
    "CredentialScopeViolation",
    "DependencyEdge",
    "KillCellResult",
    "PhaseGatingArm",
    "ProcessDied",
    "ReadinessVerdict",
    "RecordingChildRunFactory",
    "ScenarioRepo",
    "ScriptedRemote",
    "TwoWriterReport",
    "TwoWriterScenario",
    "default_scenario",
    "drive_dependency_gated_phases",
    "kill_at_step_matrix",
    "lost_response_adoption",
    "pivot_kill_matrix",
    "readiness",
    "run_two_writer_qualification",
    "verify_credential_scope",
]

#: The report's versioned schema discriminator — a breaking change to
#: what the report covers bumps the tag.
REPORT_SCHEMA = "forge.two-writer.qualification/1"

#: Digest domain tags for the scenario's own bundles (same versioning
#: discipline as every other forge schema tag).
CONTRACT_BUNDLE_SCHEMA = "forge.two-writer.contract-bundle/1"
TEST_BUNDLE_SCHEMA = "forge.two-writer.test-bundle/1"
ENVIRONMENT_PROFILE_SCHEMA = "forge.two-writer.environment-profile/1"

#: The per-repo publication ladder, in order — the steps the kill matrix
#: parametrizes over (the journaled step names
#: :mod:`forge.adaptive.publication_saga` records).
PUBLICATION_STEPS: tuple[str, ...] = (
    "prepare",
    "fence_check",
    "commit_intent",
    "provider_commit",
    "verify",
    "record",
)

#: Anything that yields sessions — the same alias shape the coordinator
#: and the runs services use (``async_sessionmaker`` duck-types here).
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


# ---------------------------------------------------------------------------
# Deterministic identity helpers (the test-suite seeds, shipped so the
# harness's own scenario and report digests are reproducible).
# ---------------------------------------------------------------------------


def _oid(seed: str) -> str:
    """A lowercase 40-hex git sha derived from *seed* (hex-only seeds)."""
    return (seed * 40)[:40]


def _digest64(seed: str) -> str:
    """A lowercase 64-hex sha256 shape derived from *seed* (hex-only seeds)."""
    return (seed * 64)[:64]


def _image(seed: str) -> str:
    """A sha256-prefixed image digest, as registries spell them."""
    return f"sha256:{_digest64(seed)}"


def _canonical_digest(payload: object) -> str:
    """sha256 over the canonical JSON encoding of *payload* (sorted keys —
    the same determinism rule :func:`forge.adaptive.workpackage` uses)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The scenario: one concrete producer/consumer change across four repos.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioRepo:
    """One repository's place in the two-writer scenario.

    ``kind`` is the scenario role: ``producer``/``consumer`` (the two
    writers), ``pinned_baseline`` (repo C — frozen at a PINNED digest)
    and ``neighbor`` (repo D — read-only observation context). For a
    pinned baseline, ``candidate_oid`` IS the pin and
    ``branch_head_oid`` records where the branch has since moved: the
    frozen set must ride the pin, never the head.
    """

    repository_id: str
    role: str  # "changed" | "baseline" (the CandidateSetMember vocabulary)
    kind: str  # "producer" | "consumer" | "pinned_baseline" | "neighbor"
    base_oid: str
    candidate_oid: str
    image_digest: str
    branch: str = "main"
    branch_head_oid: str = ""


@dataclass(frozen=True)
class ContractBundle:
    """The cross-repo contract bundle: what the producer changed and what
    the consumer expects of it (the pact both sides' verification
    replays). The digest is over the canonical JSON of both documents —
    a changed expectation is a different bundle, and therefore a
    different tested world."""

    producer_contract: Mapping[str, Any]
    consumer_expectation: Mapping[str, Any]

    def digest(self) -> str:
        return _canonical_digest(
            {
                "schema": CONTRACT_BUNDLE_SCHEMA,
                "producer_contract": dict(self.producer_contract),
                "consumer_expectation": dict(self.consumer_expectation),
            }
        )


@dataclass(frozen=True)
class TwoWriterScenario:
    """The frozen description of ONE two-writer change.

    Builds — through the EXISTING models, never around them — the
    WorkPackage (lanes/phases/dependencies), the contract bundle, the
    per-repo test bundle, the environment profile, and the complete
    frozen :class:`~forge.adaptive.models.CandidateSet`.
    """

    scenario_id: str
    objective: str
    repos: tuple[ScenarioRepo, ...]
    contract_bundle: ContractBundle
    test_bundles: Mapping[str, Mapping[str, Any]]
    environment_profile: Mapping[str, Any]
    environment_pins: Mapping[str, str]
    policy_refs: tuple[str, ...] = ()
    plan_revision: int = 1
    work_contract_digest: str = _digest64("c0ffee")
    publication_epoch: int = 7

    # -- repo lookups ------------------------------------------------------

    def _by_kind(self, kind: str) -> ScenarioRepo:
        found = [repo for repo in self.repos if repo.kind == kind]
        if len(found) != 1:
            raise ValueError(f"scenario {self.scenario_id!r} needs exactly one {kind!r} repo")
        return found[0]

    def producer(self) -> ScenarioRepo:
        return self._by_kind("producer")

    def consumer(self) -> ScenarioRepo:
        return self._by_kind("consumer")

    def pinned(self) -> ScenarioRepo:
        return self._by_kind("pinned_baseline")

    def neighbor(self) -> ScenarioRepo:
        return self._by_kind("neighbor")

    def writer_repos(self) -> tuple[ScenarioRepo, ScenarioRepo]:
        """The two writers, in publication order (producer first)."""
        return (self.producer(), self.consumer())

    # -- the WorkPackage (existing models) ----------------------------------

    #: Fixed item ids: the package's lanes, deterministic by construction.
    PRODUCER_ITEM = "producer"
    CONSUMER_ITEM = "consumer"
    PINNED_ITEM = "pinned-baseline"

    def package(self) -> WorkPackage:
        """The scenario's WorkPackage: two writer lanes (the consumer
        depends on the producer), repo C as a read-only REFERENCE item,
        repo D as read-only context — the 2-write/N-read shape."""
        producer, consumer = self.writer_repos()
        return WorkPackage(
            package_id=f"wp-{self.scenario_id}",
            objective=self.objective,
            items=(
                WorkItemRef(
                    item_id=self.PRODUCER_ITEM,
                    repository_id=producer.repository_id,
                    writable=True,
                ),
                WorkItemRef(
                    item_id=self.PINNED_ITEM,
                    repository_id=self.pinned().repository_id,
                    writable=False,
                ),
                WorkItemRef(
                    item_id=self.CONSUMER_ITEM,
                    repository_id=consumer.repository_id,
                    writable=True,
                    depends_on=(self.PRODUCER_ITEM,),
                ),
            ),
            read_only_repositories=(self.neighbor().repository_id,),
        )

    # -- the complete frozen world -------------------------------------------

    def test_bundle_digest(self) -> str:
        return _canonical_digest(
            {
                "schema": TEST_BUNDLE_SCHEMA,
                "bundles": {
                    repo.repository_id: dict(self.test_bundles[repo.repository_id])
                    for repo in self.repos
                },
            }
        )

    def environment_profile_digest(self) -> str:
        return _canonical_digest(
            {
                "schema": ENVIRONMENT_PROFILE_SCHEMA,
                "profile": dict(self.environment_profile),
                "pins": dict(self.environment_pins),
            }
        )

    def freeze(self) -> CandidateSet:
        """The COMPLETE CandidateSet, frozen through the existing models.

        Changed source identities (the two writers), baseline identities
        — repo C at its PINNED digest, never its branch head — the
        contract bundle, the test bundle and the environment profile,
        with the world and applicability digests PERSISTED at freeze
        time (``freeze_candidate_set`` routes through
        ``freeze_verified_world`` when pins/refs are given).
        """
        return freeze_candidate_set(
            work_id=self.scenario_id,
            plan_revision=self.plan_revision,
            contract_digest=self.work_contract_digest,
            per_repo={
                repo.repository_id: {
                    "base_oid": repo.base_oid,
                    "candidate_oid": repo.candidate_oid,
                    "role": repo.role,
                    "image_digest": repo.image_digest,
                }
                for repo in self.repos
            },
            contract_bundle_digest=self.contract_bundle.digest(),
            test_bundle_digest=self.test_bundle_digest(),
            environment_profile_digest=self.environment_profile_digest(),
            environment_pins=dict(self.environment_pins),
            policy_refs=tuple(self.policy_refs),
        )

    # -- the dependency edges (can-i-deploy's per-edge questions) ------------

    def edges(self) -> tuple[DependencyEdge, ...]:
        """The three verification edges promotion must query:

        - ``contract:producer->consumer`` — the consumer's expectation
          replayed against the producer's change (covers BOTH writers);
        - ``baseline:consumer->pinned`` — the consumer verified against
          repo C at the PINNED digest (covers the consumer and C);
        - ``observation:neighbor`` — the read-only neighbor observed at
          its frozen identity (covers D alone — a consumer-side change
          must NOT invalidate it, which is exactly the per-edge
          precision the ledger gives).
        """
        producer, consumer = self.writer_repos()
        pinned, neighbor = self.pinned(), self.neighbor()
        return (
            DependencyEdge(
                edge_id=f"contract:{producer.repository_id}->{consumer.repository_id}",
                kind="contract",
                repositories=(producer.repository_id, consumer.repository_id),
                description="consumer expectation replayed against the producer's change",
            ),
            DependencyEdge(
                edge_id=f"baseline:{consumer.repository_id}->{pinned.repository_id}",
                kind="baseline",
                repositories=(consumer.repository_id, pinned.repository_id),
                description="consumer verified against the pinned baseline digest",
            ),
            DependencyEdge(
                edge_id=f"observation:{neighbor.repository_id}",
                kind="observation",
                repositories=(neighbor.repository_id,),
                description="read-only neighbor observed at its frozen identity",
            ),
        )

    # -- mutations (the stale-evidence probes) --------------------------------

    def mutate_consumer_contract(self) -> TwoWriterScenario:
        """The consumer EDITED its expectation mid-package: a new consumer
        candidate (new oid + rebuilt image) and a changed contract
        bundle. The re-frozen set is a DIFFERENT verification identity —
        both the tested world and the applicability digest must flip."""
        consumer = self.consumer()
        mutated_consumer = replace(
            consumer,
            candidate_oid=_oid("20e5"),
            image_digest=_image("20f6"),
        )
        mutated_bundle = ContractBundle(
            producer_contract=dict(self.contract_bundle.producer_contract),
            consumer_expectation={
                **dict(self.contract_bundle.consumer_expectation),
                "schema_version": "v3",
                "required_fields": [
                    *list(self.contract_bundle.consumer_expectation.get("required_fields", [])),
                    "region",
                ],
            },
        )
        return replace(
            self,
            repos=tuple(
                mutated_consumer if repo.repository_id == consumer.repository_id else repo
                for repo in self.repos
            ),
            contract_bundle=mutated_bundle,
        )

    def mutate_pinned_baseline(self) -> TwoWriterScenario:
        """Repo C's pin MOVED (re-pinned to a new digest): a different
        system under test for every consumer-side edge."""
        pinned = self.pinned()
        moved = replace(pinned, base_oid=_oid("30e5"), candidate_oid=_oid("30e5"))
        return replace(
            self,
            repos=tuple(
                moved if repo.repository_id == pinned.repository_id else repo for repo in self.repos
            ),
        )


def default_scenario() -> TwoWriterScenario:
    """The concrete default: the producer widens the order-event schema
    (adds ``region``), the consumer adapts its projection and records
    the expectation, repo C stays pinned, repo D is only observed."""
    return TwoWriterScenario(
        scenario_id="tw-qualification-1",
        objective="Widen the order-event contract (producer) and adapt the consumer projection",
        repos=(
            ScenarioRepo(
                repository_id="repo-producer",
                role="changed",
                kind="producer",
                base_oid=_oid("10a1"),
                candidate_oid=_oid("10b2"),
                image_digest=_image("10c3"),
            ),
            ScenarioRepo(
                repository_id="repo-consumer",
                role="changed",
                kind="consumer",
                base_oid=_oid("20a1"),
                candidate_oid=_oid("20b2"),
                image_digest=_image("20c3"),
            ),
            ScenarioRepo(
                repository_id="repo-pinned",
                role="baseline",
                kind="pinned_baseline",
                base_oid=_oid("30a1"),
                candidate_oid=_oid("30a1"),  # the PIN
                image_digest=_image("30c3"),
                branch_head_oid=_oid("30d4"),  # the branch moved past the pin
            ),
            ScenarioRepo(
                repository_id="repo-neighbor",
                role="baseline",
                kind="neighbor",
                base_oid=_oid("40a1"),
                candidate_oid=_oid("40a1"),
                image_digest=_image("40c3"),
            ),
        ),
        contract_bundle=ContractBundle(
            producer_contract={
                "event": "order.created",
                "schema_version": "v2",
                "payload_fields": ["id", "total", "region"],
            },
            consumer_expectation={
                "event": "order.created",
                "schema_version": "v2",
                "required_fields": ["id", "total"],
            },
        ),
        test_bundles={
            "repo-producer": {"suite": "producer-contract-suite", "cases": 12},
            "repo-consumer": {"suite": "consumer-projection-suite", "cases": 18},
            "repo-pinned": {"suite": "pinned-baseline-suite", "cases": 7},
            "repo-neighbor": {"suite": "neighbor-observation-suite", "cases": 5},
        },
        environment_profile={
            "profile": "two-writer-integration-v1",
            "services": ["repo-producer", "repo-consumer", "repo-pinned", "postgres"],
        },
        environment_pins={"postgres": _image("90a1")},
        policy_refs=("compat/matrix@1",),
    )


# ---------------------------------------------------------------------------
# readiness: the can-i-deploy query over the recorded EvidenceLedger.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyEdge:
    """One verification edge of the two-writer change.

    ``repositories`` are the members whose FROZEN identities the edge's
    evidence must cover — the per-edge question, never the whole
    fleet.
    """

    edge_id: str
    kind: str  # "contract" | "baseline" | "observation"
    repositories: tuple[str, ...]
    description: str = ""

    __test__ = False  # the pytest-collection guard (domain noun, not a test)


@dataclass(frozen=True)
class BlockedEdge:
    """Why one edge is not promotable: ``missing`` (no evidence covers
    it), ``stale`` (covering evidence was superseded/invalidated) or
    ``invalid`` (covering evidence was recorded against DIFFERENT
    frozen identities)."""

    edge_id: str
    reason_kind: str
    detail: str


@dataclass(frozen=True)
class ReadinessVerdict:
    """The can-i-deploy answer: ``ready`` iff EVERY edge has passing
    recorded verification at the frozen digests; otherwise each blocked
    edge is named. A pure lookup — nothing was re-run to produce it."""

    ready: bool
    tested_world_digest: str
    blocked_edges: tuple[BlockedEdge, ...]
    satisfied_evidence_ids: tuple[str, ...] = ()

    __test__ = False


def readiness(
    candidate_set: CandidateSet,
    ledger: EvidenceLedger,
    edges: Sequence[DependencyEdge],
) -> ReadinessVerdict:
    """The promotion QUERY (can-i-deploy shape) — never a re-run.

    Every edge must be covered by a NON-superseded evidence record whose
    claimed applicability matches this set's FROZEN digests
    (:meth:`EvidenceLedger.applicable_to` — members at their exact
    candidate oids/image artifacts, the same test bundle, environment
    profile, pins and policies). An unfrozen set refuses loudly: a
    query over a world with no recorded binding is malformed.
    """
    world = bound_tested_world_digest(candidate_set)
    applicable = ledger.applicable_to(candidate_set)
    blocked: list[BlockedEdge] = []
    satisfied: list[str] = []
    for edge in edges:
        covering = [
            record
            for record in ledger.records
            if all(record.covers(repository_id) for repository_id in edge.repositories)
        ]
        if not covering:
            blocked.append(
                BlockedEdge(
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
                BlockedEdge(
                    edge.edge_id,
                    "stale",
                    f"covering evidence was invalidated and superseded ({reasons})",
                )
            )
        else:
            blocked.append(
                BlockedEdge(
                    edge.edge_id,
                    "invalid",
                    "covering evidence was recorded against different frozen identities"
                    f" (this world: {world[:12]}) — stale evidence is never silently reused",
                )
            )
    return ReadinessVerdict(
        ready=not blocked,
        tested_world_digest=world,
        blocked_edges=tuple(blocked),
        satisfied_evidence_ids=tuple(sorted(set(satisfied))),
    )


# ---------------------------------------------------------------------------
# Credential fan-out: writers scoped to ONE repo, read-only never writes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialScopeViolation:
    """One credential that reached a repository it must never touch:
    a WRITER credential staged into a read-only repository (repo D must
    never receive one), into another writer's lane, or into a
    repository that is no lane of this package at all."""

    credential: str
    repository_id: str
    owner_repository_id: str
    reason: str

    def __str__(self) -> str:  # the one-line audit record
        return self.reason


def verify_credential_scope(
    lane_assignments: Mapping[str, Mapping[str, Any]],
    staged_credentials: Mapping[str, Sequence[str]],
    *,
    credential_owner: Mapping[str, str] | None = None,
) -> list[CredentialScopeViolation]:
    """Check the credential fan-out against the lane assignment (MRP-03).

    *lane_assignments* is :func:`forge.adaptive.workpackage.lane_assignment`'s
    output (``item_id -> {"repository_id", "mode", "siblings_read"}``) —
    the package's full READ universe (every item's repository PLUS the
    context read-only repositories, repo D included) is derived from the
    ``siblings_read`` arrivals. *staged_credentials* maps each REPOSITORY
    to the credential names staged into its lane's environment. A
    WRITER credential may appear ONLY under its owning writer's
    repository; read-only repositories — and repositories outside the
    package entirely — may hold READ credentials only.

    Ownership: *credential_owner* (credential name -> writer repository)
    is authoritative when given (the dispatch table a real dispatcher
    resolves names from). Without it, ownership is DERIVED from the
    staging: a name staged under exactly one writer repository is that
    writer's, and a name staged under several writer repositories is a
    cross-lane leak under ANY ownership (one violation, holding the
    lexicographically first staging as the owner).

    Returns every violation (deterministically ordered); empty = scoped.
    """
    writer_repos: dict[str, str] = {}  # repository_id -> item_id
    known_repos: set[str] = set()
    for item_id, lane in sorted(lane_assignments.items()):
        repository_id = str(lane["repository_id"])
        known_repos.add(repository_id)
        for sibling in lane.get("siblings_read") or ():
            known_repos.add(str(sibling))
        if str(lane["mode"]) == "writable":
            writer_repos[repository_id] = item_id

    owner_of: dict[str, str] = dict(credential_owner or {})
    for repository_id in sorted(staged_credentials):
        if repository_id in writer_repos:
            for name in sorted(staged_credentials[repository_id]):
                owner_of.setdefault(name, repository_id)

    violations: list[CredentialScopeViolation] = []
    for repository_id in sorted(staged_credentials):
        for name in sorted(staged_credentials[repository_id]):
            owner = owner_of.get(name)
            if owner is None:
                continue  # a read credential — read-only lanes may hold those
            if owner == repository_id:
                continue  # the writer's own lane: correct
            if repository_id not in known_repos:
                reason = (
                    f"repository {repository_id!r} is no lane of this package yet received"
                    f" writer credential {name!r} (owned by {owner!r})"
                )
            elif repository_id not in writer_repos:
                reason = (
                    f"read-only repository {repository_id!r} received writer credential"
                    f" {name!r} (owned by {owner!r}): read-only lanes never write"
                )
            else:
                reason = (
                    f"writer credential {name!r} crossed lanes: owned by {owner!r},"
                    f" staged for {repository_id!r} — one writer, one repository"
                )
            violations.append(
                CredentialScopeViolation(
                    credential=name,
                    repository_id=repository_id,
                    owner_repository_id=owner,
                    reason=reason,
                )
            )
    return violations


# ---------------------------------------------------------------------------
# The harness seams: a marker-keyed scripted remote + a recording factory.
# ---------------------------------------------------------------------------


class ProcessDied(RuntimeError):
    """The kill matrix's death signal: the driving process 'died' at a
    chosen step boundary. The durable state left behind is whatever the
    last save wrote — exactly what a real crash leaves."""


class ScriptedRemote:
    """A deterministic, marker-keyed :class:`PublicationProvider` fake.

    ``commit`` is idempotent per ``(branch, marker)`` — the property the
    re-issue path depends on. Branch histories only ever APPEND, and
    the destructive counters (``force_pushes``/``branch_deletions``)
    can only move through DIRECT calls the publication protocol cannot
    even express — a structural plus behavioral proof that recovery
    never deletes or rewrites a branch. ``lose_commit_response`` injects
    the lost-response window (the effect lands, the answer dies).
    """

    def __init__(self) -> None:
        self._history: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._reviews: dict[tuple[str, str, str], str] = {}
        self._counter = 0
        self.commit_calls: dict[str, int] = {}
        self.review_calls: dict[str, int] = {}
        self.force_pushes: list[tuple[str, str]] = []
        self.branch_deletions: list[tuple[str, str]] = []
        self.lose_commit_response: set[str] = set()

    # -- test/world knobs ---------------------------------------------------

    def seed(self, repository_id: str, branch: str, base_oid: str) -> None:
        self._history[(repository_id, branch)] = [(base_oid, "base")]

    def human_commit(self, repository_id: str, branch: str) -> str:
        """A person pushed: appended, never overwritten."""
        self._counter += 1
        oid = f"human-{repository_id}-{self._counter}"
        self._branches(repository_id, branch).append((oid, f"human:{repository_id}"))
        return oid

    def history(self, repository_id: str, branch: str) -> tuple[str, ...]:
        """The branch's commit oids, oldest first (the prefix-check view)."""
        return tuple(oid for oid, _marker in self._branches(repository_id, branch))

    def commits_landed(self, repository_id: str, branch: str, marker: str) -> int:
        """How many DISTINCT commits carry the marker (duplicate-effect view)."""
        return sum(1 for _oid, stored in self._branches(repository_id, branch) if stored == marker)

    def destructive_operations(self) -> list[str]:
        return [f"force-push {repo}/{branch}" for repo, branch in self.force_pushes] + [
            f"delete {repo}/{branch}" for repo, branch in self.branch_deletions
        ]

    def _branches(self, repository_id: str, branch: str) -> list[tuple[str, str]]:
        return self._history.setdefault((repository_id, branch), [])

    # -- the PublicationProvider surface --------------------------------------

    async def remote_head(self, repository_id: str, branch: str) -> str:
        history = self._branches(repository_id, branch)
        return history[-1][0] if history else ""

    async def head_carries_marker(self, repository_id: str, branch: str, marker: str) -> bool:
        return any(stored == marker for _oid, stored in self._branches(repository_id, branch))

    async def commit(self, repository_id: str, branch: str, marker: str) -> str:
        self.commit_calls[repository_id] = self.commit_calls.get(repository_id, 0) + 1
        history = self._branches(repository_id, branch)
        for oid, stored in history:
            if stored == marker:
                return oid  # idempotent: this intent already landed
        self._counter += 1
        oid = f"oid-{repository_id}-{self._counter}"
        history.append((oid, marker))
        if repository_id in self.lose_commit_response:
            raise TimeoutError(f"{repository_id}: response lost after the effect landed")
        return oid

    async def open_review(self, repository_id: str, branch: str, marker: str) -> str:
        self.review_calls[repository_id] = self.review_calls.get(repository_id, 0) + 1
        key = (repository_id, branch, marker)
        if key not in self._reviews:
            self._reviews[key] = (
                f"https://example.test/{repository_id}/pull/{len(self._reviews) + 1}"
            )
        return self._reviews[key]


class RecordingChildRunFactory:
    """The coordinator's child-run factory seam as NXT-23 contracts it:
    idempotent per intent key, recording every call so the gating proof
    can pin WHO launched WHEN."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._children: dict[str, str] = {}

    async def __call__(
        self, item_id: str, repository_id: str, *, writable: bool, intent_key: str, brief: str
    ) -> str:
        self.calls.append(
            {
                "item_id": item_id,
                "repository_id": repository_id,
                "writable": writable,
                "intent_key": intent_key,
                "brief": brief,
            }
        )
        if intent_key not in self._children:
            self._children[intent_key] = f"child-{item_id}"
        return self._children[intent_key]

    def launches_of(self, item_id: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["item_id"] == item_id]


# ---------------------------------------------------------------------------
# Dependency-gated child start: phases advance only on PROVEN outcomes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseGatingArm:
    """One arm of the phase-gating proof (the happy or the failed arm).

    ``refusal_before_proof`` is the captured
    :class:`~forge.adaptive.workpackage.PhaseAdvanceRefused` raised
    while the producer had no PROVEN outcome — ``awaiting`` when the
    producer is silent, ``failed`` when it failed. The consumer's
    launch count BEFORE that proof must be zero; only the recorded
    proof admits phase 2.
    """

    parent_run_id: str
    producer_outcome: str
    refusal_before_proof: str
    refusal_awaiting: tuple[str, ...]
    refusal_failed: tuple[str, ...]
    wrong_world_outcome_status: str
    consumer_launches_before_proof: int
    consumer_launches_total: int
    producer_launched: bool
    final_state: str
    phases: tuple[tuple[str, ...], ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "parent_run_id": self.parent_run_id,
            "producer_outcome": self.producer_outcome,
            "refusal_before_proof": self.refusal_before_proof,
            "refusal_awaiting": list(self.refusal_awaiting),
            "refusal_failed": list(self.refusal_failed),
            "wrong_world_outcome_status": self.wrong_world_outcome_status,
            "consumer_launches_before_proof": self.consumer_launches_before_proof,
            "consumer_launches_total": self.consumer_launches_total,
            "producer_launched": self.producer_launched,
            "final_state": self.final_state,
            "phases": [list(phase) for phase in self.phases],
        }


async def _seed_parent_run(
    session_factory: SessionFactory, parent_run_id: str, *, project_id: int
) -> None:
    async with session_factory() as session:
        session.add(FlowRun(id=parent_run_id, project_id=project_id, status="planning"))
        await session.commit()


async def drive_dependency_gated_phases(
    session_factory: SessionFactory,
    scenario: TwoWriterScenario,
    *,
    parent_run_id: str,
    producer_outcome: str = "succeeded",
) -> PhaseGatingArm:
    """Drive the coordinator through both phases and capture the gating.

    The happy arm additionally probes that an outcome recorded from a
    DIFFERENT tested world is rejected (it proves nothing about this
    package). Both arms pin: the consumer lane is never dispatched
    while the producer is unproven/failed, and — in the happy arm —
    dispatches exactly once the PROVEN outcome lands, completes the
    package after the consumer's own proven outcome.
    """
    package = scenario.package()
    world = scenario.freeze().tested_world_digest or ""
    await _seed_parent_run(session_factory, parent_run_id, project_id=1)
    seam = RecordingChildRunFactory()
    coordinator = WorkPackageCoordinator(session_factory, seam)
    state = await coordinator.start(
        package,
        parent_run_id=parent_run_id,
        task_brief=scenario.objective,
        tested_world_digest=world,
    )
    phases = tuple(tuple(phase) for phase in state["phases"])
    producer_launched = bool(
        (state["children"].get(scenario.PRODUCER_ITEM) or {}).get("child_run_id")
    )

    # The consumer CANNOT start: the producer has no proven outcome.
    awaiting: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    refusal = ""
    try:
        await coordinator.advance(parent_run_id, package)
        refusal = "NO REFUSAL — phase advanced without proof"
    except PhaseAdvanceRefused as exc:
        awaiting, failed, refusal = exc.awaiting, exc.failed, str(exc)
    consumer_before = len(seam.launches_of(scenario.CONSUMER_ITEM))

    # An outcome from a different tested world proves nothing (happy arm).
    wrong_world_status = ""
    if producer_outcome == "succeeded":
        wrong = await coordinator.record_outcome(
            parent_run_id,
            scenario.PRODUCER_ITEM,
            "succeeded",
            tested_world_digest=_digest64("dead"),
        )
        wrong_world_status = wrong.status

    applied = await coordinator.record_outcome(
        parent_run_id,
        scenario.PRODUCER_ITEM,
        producer_outcome,
        detail=f"producer outcome against frozen world {world[:12]}",
        tested_world_digest=world,
    )
    if not applied.applied:
        raise AssertionError(
            f"producer outcome was not applied: {applied.status} ({applied.reason})"
        )
    final_state = str(applied.record.get("state") or "")
    if producer_outcome == "succeeded":
        state = await coordinator.advance(parent_run_id, package)  # admits phase 2
        final_state = str(state.get("state") or "")
        consumer_child = state["children"].get(scenario.CONSUMER_ITEM) or {}
        if consumer_child.get("intent_status") != "launched":
            raise AssertionError("the consumer never launched after the proven producer")
        consumer = await coordinator.record_outcome(
            parent_run_id,
            scenario.CONSUMER_ITEM,
            "succeeded",
            detail=f"consumer outcome against frozen world {world[:12]}",
            tested_world_digest=world,
        )
        if not consumer.applied:
            raise AssertionError(
                f"consumer outcome was not applied: {consumer.status} ({consumer.reason})"
            )
        final_state = str((await coordinator.advance(parent_run_id, package)).get("state") or "")
    else:
        # A failed producer: advancing must REFUSE with the failed item named.
        try:
            await coordinator.advance(parent_run_id, package)
            refusal = "ADVANCED DESPITE FAILED PRODUCER"
        except PhaseAdvanceRefused as exc:
            failed, refusal = exc.failed, str(exc)
    return PhaseGatingArm(
        parent_run_id=parent_run_id,
        producer_outcome=producer_outcome,
        refusal_before_proof=refusal,
        refusal_awaiting=awaiting,
        refusal_failed=failed,
        wrong_world_outcome_status=wrong_world_status,
        consumer_launches_before_proof=consumer_before,
        consumer_launches_total=len(seam.launches_of(scenario.CONSUMER_ITEM)),
        producer_launched=producer_launched,
        final_state=final_state,
        phases=phases,
    )


# ---------------------------------------------------------------------------
# The kill-at-step-k matrix.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KillCellResult:
    """One matrix cell's reconciled outcome, with the invariant verdict.

    ``commits_landed``/``reviews_opened`` count DISTINCT effects (a
    re-issued intent that re-calls the provider but lands the same
    marker-keyed commit is still ONE publication).
    ``histories_preserved`` says every branch's pre-restart history is
    a PREFIX of its post-restart history — the no-destructive-rollback
    proof. ``second_recovery_idempotent`` says a THIRD process over the
    same durable state spends nothing and changes nothing.
    """

    variant: str  # "crash" | "pivot" | "lost_response"
    repository_id: str
    step: str
    saga_id: str
    saga_status: str
    repo_statuses: Mapping[str, str]
    commits_landed: Mapping[str, int]
    reviews_opened: Mapping[str, int]
    destructive_operations: tuple[str, ...]
    histories_preserved: bool
    adopted: Mapping[str, bool]
    second_recovery_idempotent: bool
    parked_reasons: Mapping[str, str]
    first_pass_statuses: Mapping[str, str]
    steps_digest: str
    invariants_hold: bool

    def to_document(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "repository_id": self.repository_id,
            "step": self.step,
            "saga_id": self.saga_id,
            "saga_status": self.saga_status,
            "repo_statuses": dict(sorted(self.repo_statuses.items())),
            "commits_landed": dict(sorted(self.commits_landed.items())),
            "reviews_opened": dict(sorted(self.reviews_opened.items())),
            "destructive_operations": list(self.destructive_operations),
            "histories_preserved": self.histories_preserved,
            "adopted": dict(sorted(self.adopted.items())),
            "second_recovery_idempotent": self.second_recovery_idempotent,
            "parked_reasons": dict(sorted(self.parked_reasons.items())),
            "first_pass_statuses": dict(sorted(self.first_pass_statuses.items())),
            "steps_digest": self.steps_digest,
            "invariants_hold": self.invariants_hold,
        }


async def _walk_publication(
    remote: ScriptedRemote,
    store: InMemorySagaStore,
    saga: PublicationSaga,
    repository_id: str,
    *,
    die_after: str | None,
) -> PublicationSaga:
    """Walk ONE repo's publication ladder one durable boundary at a time.

    Mirrors :class:`SagaCoordinator`'s own write-ahead ordering using the
    module's public pure transitions (journal the fence, persist the
    intent BEFORE the provider call, book the effect, verify, record);
    ``die_after`` abandons the process at that boundary
    (:class:`ProcessDied`), leaving exactly the durable state a real
    crash at that point would leave. The ``fence_check`` boundary saves
    a state the coordinator's own batching never persists on its own
    (fence-check + intent share one save there) — recovering from it is
    precisely the defensive coverage the matrix exists to give.
    """
    repo = saga.repo(repository_id)
    if repo is None:
        raise KeyError(f"unknown repository {repository_id!r}")
    for step in PUBLICATION_STEPS:
        if step == "prepare":
            # Journaled by begin_saga and persisted by the caller's
            # initial save: the boundary is the death point itself.
            if die_after == step:
                raise ProcessDied(f"died after {step} of {repository_id}")
            continue
        if step == "fence_check":
            saga = replace(
                saga,
                steps=saga.steps
                + (
                    {
                        "repo": repository_id,
                        "step": "fence_check",
                        "epoch": str(saga.publication_epoch),
                    },
                ),
            )
            await store.save(saga)
        elif step == "commit_intent":
            saga = record_commit_intent(saga, repository_id)
            await store.save(saga)  # WRITE-AHEAD: intent durable BEFORE the call
        elif step == "provider_commit":
            head = await remote.commit(repository_id, repo.branch, saga.commit_marker)
            saga = provider_committed(saga, repository_id, head)
            await store.save(saga)
        elif step == "verify":
            head_now = await remote.remote_head(repository_id, repo.branch)
            ours = await remote.head_carries_marker(repository_id, repo.branch, saga.commit_marker)
            moved = saga.repo(repository_id)
            if moved is None or head_now != moved.head_oid or not ours:
                raise AssertionError(
                    f"walk of {repository_id} could not verify its own clean commit"
                )
            saga = verified(saga, repository_id)
            await store.save(saga)
        elif step == "record":
            review_url = await remote.open_review(repository_id, repo.branch, saga.commit_marker)
            saga = review_opened(saga, repository_id, review_url)
            await store.save(saga)
        if die_after == step:
            raise ProcessDied(f"died after {step} of {repository_id}")
    return saga


def _begun_saga(
    scenario: TwoWriterScenario, candidate_digest: str, saga_id: str
) -> PublicationSaga:
    """The two-writer saga (producer first, consumer second), deterministic id."""
    saga = begin_saga(
        scenario.scenario_id,
        publication_epoch=scenario.publication_epoch,
        candidate_digest=candidate_digest,
        repo_plans={
            repo.repository_id: (repo.branch, repo.base_oid) for repo in scenario.writer_repos()
        },
    )
    return replace(saga, saga_id=saga_id)


async def _run_kill_cell(
    scenario: TwoWriterScenario,
    candidate_digest: str,
    *,
    target_repository_id: str,
    step: str,
    variant: str,
    pivot: bool = False,
) -> KillCellResult:
    """One matrix cell: walk, die at the boundary, restart, reconcile.

    The restart is the REAL :class:`SagaCoordinator` recovering the
    persisted saga from the store — the harness never fakes recovery.
    Invariants are computed (not assumed): distinct-effect counts,
    destructive-op and prefix checks, a second recovery, convergence to
    completed or parked-with-reason.
    """
    producer, consumer = scenario.writer_repos()
    remote = ScriptedRemote()
    for repo in scenario.writer_repos():
        remote.seed(repo.repository_id, repo.branch, repo.base_oid)
    store = InMemorySagaStore()
    saga_id = f"saga-{scenario.scenario_id}-{variant}-{target_repository_id}-{step}"
    saga = _begun_saga(scenario, candidate_digest, saga_id)
    await store.save(saga)  # run()'s first save: the begun intents durable

    first_pass_statuses: dict[str, str] = {}
    try:
        for repo in scenario.writer_repos():
            if repo.repository_id == target_repository_id:
                await _walk_publication(remote, store, saga, repo.repository_id, die_after=step)
            else:
                await _walk_publication(remote, store, saga, repo.repository_id, die_after=None)
            if pivot and repo.repository_id == producer.repository_id:
                # The PIVOT: a human merged the first publication's PR. The
                # harness records the OBSERVATION (the bot never merges);
                # recovery after this point is forward-only by design.
                walked = await store.load(saga_id)
                assert walked is not None
                merged = observe_human_merge(walked, producer.repository_id)
                await store.save(merged)
            saga = await store.load(saga_id) or saga
    except ProcessDied:
        pass  # the crash: everything durable is what the store last saved

    if pivot:
        # While the coordinator was dead, a person pushed to the SECOND
        # writer's branch — the collision that must park, never roll back.
        remote.human_commit(consumer.repository_id, consumer.branch)

    histories_before = {
        repo.repository_id: remote.history(repo.repository_id, repo.branch)
        for repo in scenario.writer_repos()
    }
    coordinator = SagaCoordinator(store, remote)
    recovered = await coordinator.recover(
        saga_id, current_publication_epoch=scenario.publication_epoch
    )
    if recovered is None:
        raise AssertionError(f"cell {saga_id}: the saga vanished from the store")

    # The idempotent-reconciliation probe: a THIRD process must spend
    # nothing and change nothing over the reconciled state.
    commits_after_first = dict(remote.commit_calls)
    reviews_after_first = dict(remote.review_calls)
    statuses_after_first = {repo.repository_id: str(repo.status) for repo in recovered.repos}
    again = await coordinator.recover(saga_id, current_publication_epoch=scenario.publication_epoch)
    if again is None:
        raise AssertionError(f"cell {saga_id}: the saga vanished from the store on re-recovery")
    idempotent = (
        dict(remote.commit_calls) == commits_after_first
        and dict(remote.review_calls) == reviews_after_first
        and {repo.repository_id: str(repo.status) for repo in again.repos} == statuses_after_first
    )

    histories_preserved = all(
        remote.history(repo.repository_id, repo.branch)[: len(histories_before[repo.repository_id])]
        == histories_before[repo.repository_id]
        for repo in scenario.writer_repos()
    )
    commits_landed = {
        repo.repository_id: remote.commits_landed(
            repo.repository_id, repo.branch, recovered.commit_marker
        )
        for repo in scenario.writer_repos()
    }
    destructive = remote.destructive_operations()
    parked_reasons = {
        repo.repository_id: repo.note
        for repo in recovered.repos
        if repo.status == "parked_human" and repo.note
    }
    invariants = (
        all(count <= 1 for count in commits_landed.values())
        and all(count <= 1 for count in remote.review_calls.values())
        and not destructive
        and histories_preserved
        and idempotent
        and recovered.status in {"complete", "parked"}
    )
    return KillCellResult(
        variant=variant,
        repository_id=target_repository_id,
        step=step,
        saga_id=saga_id,
        saga_status=str(recovered.status),
        repo_statuses=statuses_after_first,
        commits_landed=commits_landed,
        reviews_opened=dict(remote.review_calls),
        destructive_operations=tuple(destructive),
        histories_preserved=histories_preserved,
        adopted={repo.repository_id: repo.adopted for repo in recovered.repos},
        second_recovery_idempotent=idempotent,
        parked_reasons=parked_reasons,
        first_pass_statuses=first_pass_statuses,
        steps_digest=recovered.steps_digest,
        invariants_hold=invariants,
    )


async def kill_at_step_matrix(scenario: TwoWriterScenario) -> tuple[KillCellResult, ...]:
    """The full plain crash matrix: every publication step × BOTH writers.

    Death lands after step *k* of the named writer's publication — for
    the producer that is between the two publications; for the consumer
    it is mid-second-publication. Every cell must reconcile through the
    real coordinator to a completed saga with no duplicate effects, no
    destructive rollback and idempotent re-recovery.
    """
    candidate_digest = scenario.freeze().tested_world_digest or ""
    cells: list[KillCellResult] = []
    for repo in scenario.writer_repos():
        for step in PUBLICATION_STEPS:
            cells.append(
                await _run_kill_cell(
                    scenario,
                    candidate_digest,
                    target_repository_id=repo.repository_id,
                    step=step,
                    variant="crash",
                )
            )
    return tuple(cells)


async def pivot_kill_matrix(scenario: TwoWriterScenario) -> tuple[KillCellResult, ...]:
    """The post-pivot matrix: deaths during the SECOND publication after
    a human MERGED the first PR, with a human edit colliding on the
    consumer's branch while the coordinator was dead.

    Recovery past the pivot is forward-only: the merged publication
    stands and is never rolled back; the collision either parks the
    consumer for a human decision (the open effect-window steps) or
    completes forward (the already-decided steps).
    """
    candidate_digest = scenario.freeze().tested_world_digest or ""
    consumer = scenario.consumer()
    cells = [
        await _run_kill_cell(
            scenario,
            candidate_digest,
            target_repository_id=consumer.repository_id,
            step=step,
            variant="pivot",
            pivot=True,
        )
        for step in PUBLICATION_STEPS
    ]
    return tuple(cells)


async def lost_response_adoption(scenario: TwoWriterScenario) -> KillCellResult:
    """The lost-response window: the provider commit LANDED but the
    response died with the process.

    The first pass books ``outcome_unknown`` for the producer while the
    consumer's publication stands (the honest PARTIAL publication,
    reported — never rolled back); the restarted coordinator reconciles
    by ADOPTING the marker-carrying remote effect, never by
    re-publishing.
    """
    producer, _consumer = scenario.writer_repos()
    remote = ScriptedRemote()
    for repo in scenario.writer_repos():
        remote.seed(repo.repository_id, repo.branch, repo.base_oid)
    store = InMemorySagaStore()
    saga_id = f"saga-{scenario.scenario_id}-lost-response"
    saga = _begun_saga(scenario, scenario.freeze().tested_world_digest or "", saga_id)
    await store.save(saga)
    remote.lose_commit_response = {producer.repository_id}
    coordinator = SagaCoordinator(store, remote)
    first_pass = await coordinator.run(saga, current_publication_epoch=scenario.publication_epoch)
    first_pass_statuses = {repo.repository_id: str(repo.status) for repo in first_pass.repos}

    remote.lose_commit_response = set()  # the provider heals
    histories_before = {
        repo.repository_id: remote.history(repo.repository_id, repo.branch)
        for repo in scenario.writer_repos()
    }
    recovered = await coordinator.recover(
        saga_id, current_publication_epoch=scenario.publication_epoch
    )
    if recovered is None:
        raise AssertionError("the lost-response saga vanished from the store")

    commits_after = dict(remote.commit_calls)
    reviews_after = dict(remote.review_calls)
    statuses_after = {repo.repository_id: str(repo.status) for repo in recovered.repos}
    again = await coordinator.recover(saga_id, current_publication_epoch=scenario.publication_epoch)
    if again is None:
        raise AssertionError("the lost-response saga vanished on re-recovery")
    idempotent = (
        dict(remote.commit_calls) == commits_after
        and dict(remote.review_calls) == reviews_after
        and {repo.repository_id: str(repo.status) for repo in again.repos} == statuses_after
    )
    commits_landed = {
        repo.repository_id: remote.commits_landed(
            repo.repository_id, repo.branch, recovered.commit_marker
        )
        for repo in scenario.writer_repos()
    }
    histories_preserved = all(
        remote.history(repo.repository_id, repo.branch)[: len(histories_before[repo.repository_id])]
        == histories_before[repo.repository_id]
        for repo in scenario.writer_repos()
    )
    invariants = (
        all(count <= 1 for count in commits_landed.values())
        and not remote.destructive_operations()
        and histories_preserved
        and idempotent
        and recovered.status in {"complete", "parked"}
    )
    return KillCellResult(
        variant="lost_response",
        repository_id=producer.repository_id,
        step="provider_commit",
        saga_id=saga_id,
        saga_status=str(recovered.status),
        repo_statuses=statuses_after,
        commits_landed=commits_landed,
        reviews_opened=dict(remote.review_calls),
        destructive_operations=tuple(remote.destructive_operations()),
        histories_preserved=histories_preserved,
        adopted={repo.repository_id: repo.adopted for repo in recovered.repos},
        second_recovery_idempotent=idempotent,
        parked_reasons={},
        first_pass_statuses=dict(first_pass_statuses),
        steps_digest=recovered.steps_digest,
        invariants_hold=invariants,
    )


# ---------------------------------------------------------------------------
# The versioned report.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TwoWriterReport:
    """The complete qualification record (``forge.two-writer.qualification/1``).

    Deterministic by construction (every id, digest and URL in it is
    derived from the scenario), so :attr:`report_digest` is a
    reproducible fingerprint of the whole qualification run.
    """

    scenario: TwoWriterScenario
    tested_world_digest: str
    applicability_digest: str
    mutated_world_digest: str
    mutated_applicability_digest: str
    identity_flipped: bool
    phase_gating_happy: PhaseGatingArm
    phase_gating_failed: PhaseGatingArm
    kill_matrix: tuple[KillCellResult, ...]
    pivot_matrix: tuple[KillCellResult, ...]
    lost_response: KillCellResult
    readiness_queries: tuple[dict[str, Any], ...]
    credential_scope: dict[str, Any]
    decision_points: tuple[dict[str, Any], ...]
    publication_reviews: dict[str, str]

    @property
    def schema(self) -> str:
        return REPORT_SCHEMA

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "scenario": {
                "scenario_id": self.scenario.scenario_id,
                "objective": self.scenario.objective,
                "plan_revision": self.scenario.plan_revision,
                "tested_world_digest": self.tested_world_digest,
                "applicability_digest": self.applicability_digest,
                "mutated_world_digest": self.mutated_world_digest,
                "mutated_applicability_digest": self.mutated_applicability_digest,
                "identity_flipped": self.identity_flipped,
                "members": {
                    repo.repository_id: {
                        "role": repo.role,
                        "candidate_oid": repo.candidate_oid,
                        "image_digest": repo.image_digest,
                    }
                    for repo in self.scenario.repos
                },
                "edges": [
                    {
                        "edge_id": edge.edge_id,
                        "kind": edge.kind,
                        "repositories": list(edge.repositories),
                        "description": edge.description,
                    }
                    for edge in self.scenario.edges()
                ],
            },
            "phase_gating": {
                "happy": self.phase_gating_happy.to_document(),
                "failed_producer": self.phase_gating_failed.to_document(),
            },
            "kill_matrix": [cell.to_document() for cell in self.kill_matrix],
            "pivot_matrix": [cell.to_document() for cell in self.pivot_matrix],
            "lost_response": self.lost_response.to_document(),
            "readiness": [dict(query) for query in self.readiness_queries],
            "credential_scope": dict(self.credential_scope),
            "human_decision_points": [dict(point) for point in self.decision_points],
            "publication_reviews": dict(sorted(self.publication_reviews.items())),
        }

    @property
    def report_digest(self) -> str:
        return _canonical_digest(self.to_document())


async def _clean_publication(
    scenario: TwoWriterScenario, candidate_digest: str
) -> tuple[PublicationSaga, ScriptedRemote]:
    """A clean two-writer publication run (the matrix's zero-kill
    baseline): both PRs open, nothing injected."""
    remote = ScriptedRemote()
    for repo in scenario.writer_repos():
        remote.seed(repo.repository_id, repo.branch, repo.base_oid)
    store = InMemorySagaStore()
    saga = _begun_saga(scenario, candidate_digest, f"saga-{scenario.scenario_id}-publication")
    await store.save(saga)
    finished = await SagaCoordinator(store, remote).run(
        saga, current_publication_epoch=scenario.publication_epoch
    )
    return finished, remote


async def run_two_writer_qualification(
    session_factory: SessionFactory, scenario: TwoWriterScenario | None = None
) -> TwoWriterReport:
    """Run the FULL two-writer qualification and assemble the report.

    Legs: the freeze, both phase-gating arms (durable, over the given
    session factory), the plain kill matrix (12 cells), the post-pivot
    matrix (6 cells), the lost-response adoption, the readiness queries
    (the can-i-deploy happy path plus the stale/missing refusals), the
    credential fan-out checks, and the human decision points a human
    owner accepts or rejects with the complete cross-repo evidence
    bundle (merge and deploy stay SEPARATE approvals — the bot never
    merges, the report never promotes).
    """
    scenario = scenario or default_scenario()
    frozen = scenario.freeze()
    world = frozen.tested_world_digest or ""
    edges = scenario.edges()

    happy = await drive_dependency_gated_phases(
        session_factory, scenario, parent_run_id=f"{scenario.scenario_id}-happy"
    )
    failed = await drive_dependency_gated_phases(
        session_factory,
        scenario,
        parent_run_id=f"{scenario.scenario_id}-failed",
        producer_outcome="failed",
    )
    kill_matrix = await kill_at_step_matrix(scenario)
    pivot_matrix = await pivot_kill_matrix(scenario)
    lost = await lost_response_adoption(scenario)

    # The readiness contract: full evidence -> ready; a mutated consumer
    # contract -> the invalidated edges BLOCKED BY NAME; no evidence ->
    # every edge missing. The neighbor observation edge must SURVIVE the
    # consumer mutation — the per-edge precision can-i-deploy buys.
    full_ledger = EvidenceLedger().record(
        *[record_evidence(f"ev-{edge.edge_id}", frozen, edge.repositories) for edge in edges]
    )
    mutated_scenario = scenario.mutate_consumer_contract()
    mutated = mutated_scenario.freeze()
    ready_verdict = readiness(frozen, full_ledger, edges)
    stale_verdict = readiness(mutated, full_ledger, edges)
    missing_verdict = readiness(frozen, EvidenceLedger(), edges)
    readiness_queries = (
        {
            "query": "full-ledger@frozen",
            "ready": ready_verdict.ready,
            "tested_world_digest": ready_verdict.tested_world_digest,
            "satisfied_evidence_ids": list(ready_verdict.satisfied_evidence_ids),
            "blocked_edges": [
                {"edge_id": blocked.edge_id, "kind": blocked.reason_kind, "detail": blocked.detail}
                for blocked in ready_verdict.blocked_edges
            ],
        },
        {
            "query": "full-ledger@mutated-consumer-contract",
            "ready": stale_verdict.ready,
            "tested_world_digest": stale_verdict.tested_world_digest,
            "blocked_edges": [
                {"edge_id": blocked.edge_id, "kind": blocked.reason_kind, "detail": blocked.detail}
                for blocked in stale_verdict.blocked_edges
            ],
        },
        {
            "query": "empty-ledger@frozen",
            "ready": missing_verdict.ready,
            "blocked_edges": [
                {"edge_id": blocked.edge_id, "kind": blocked.reason_kind, "detail": blocked.detail}
                for blocked in missing_verdict.blocked_edges
            ],
        },
    )

    # Credential fan-out: the scenario's own staging must be clean, and
    # the adversarial probes must be CAUGHT.
    assignments = lane_assignment(scenario.package())
    producer_repo = scenario.producer().repository_id
    consumer_repo = scenario.consumer().repository_id
    pinned_repo = scenario.pinned().repository_id
    neighbor_repo = scenario.neighbor().repository_id
    clean_staging = {
        producer_repo: [f"cred-write-{producer_repo}"],
        consumer_repo: [f"cred-write-{consumer_repo}"],
        pinned_repo: [f"cred-read-{pinned_repo}"],
        neighbor_repo: [f"cred-read-{neighbor_repo}"],
    }
    clean_violations = verify_credential_scope(assignments, clean_staging)
    probes = (
        (
            "neighbor-receives-writer-credential",
            {**clean_staging, neighbor_repo: [f"cred-write-{producer_repo}"]},
        ),
        (
            "writer-credential-crosses-lanes",
            {
                **clean_staging,
                consumer_repo: [f"cred-write-{producer_repo}", f"cred-write-{consumer_repo}"],
            },
        ),
        (
            "pinned-baseline-receives-writer-credential",
            {
                **clean_staging,
                pinned_repo: [f"cred-read-{pinned_repo}", f"cred-write-{consumer_repo}"],
            },
        ),
    )
    credential_scope = {
        "clean_staging_ok": not clean_violations,
        "clean_violations": [str(violation) for violation in clean_violations],
        "probes": [
            {
                "label": label,
                "caught": bool(verify_credential_scope(assignments, staging)),
                "violations": [
                    str(violation) for violation in verify_credential_scope(assignments, staging)
                ],
            }
            for label, staging in probes
        ],
    }

    # A clean publication supplies the standing PRs the human merge
    # decisions point at.
    publication, _remote = await _clean_publication(scenario, world)
    publication_reviews = {
        repo.repository_id: repo.review_url or "" for repo in publication.repos if repo.review_url
    }
    decision_points: list[dict[str, Any]] = [
        {
            "kind": "merge_pr",
            "subject": review_url,
            "repository_id": repository_id,
            "evidence": {
                "tested_world_digest": world,
                "candidate_digest": world,
                "readiness": "ready" if ready_verdict.ready else "blocked",
                "evidence_ids": list(ready_verdict.satisfied_evidence_ids),
            },
        }
        for repository_id, review_url in sorted(publication_reviews.items())
    ]
    decision_points.append(
        {
            "kind": "promote_deploy",
            "subject": scenario.scenario_id,
            "evidence": {
                "readiness": "ready" if ready_verdict.ready else "blocked",
                "tested_world_digest": world,
                "edges": [edge.edge_id for edge in edges],
                "evidence_ids": list(ready_verdict.satisfied_evidence_ids),
            },
        }
    )
    for cell in pivot_matrix:
        for repository_id, reason in sorted(cell.parked_reasons.items()):
            # One decision per PARKED cell (each names the death step that
            # produced the park), not one per repo-reason pair.
            decision_points.append(
                {
                    "kind": "resolve_parked",
                    "subject": f"{repository_id}:{scenario.consumer().branch}",
                    "evidence": {
                        "cell": f"{cell.variant}/{cell.step}",
                        "reason": reason,
                        "producer_standing": "human_merged",
                    },
                }
            )
            break

    return TwoWriterReport(
        scenario=scenario,
        tested_world_digest=world,
        applicability_digest=frozen.applicability_digest or "",
        mutated_world_digest=mutated.tested_world_digest or "",
        mutated_applicability_digest=mutated.applicability_digest or "",
        identity_flipped=identity_changed(frozen, mutated),
        phase_gating_happy=happy,
        phase_gating_failed=failed,
        kill_matrix=kill_matrix,
        pivot_matrix=pivot_matrix,
        lost_response=lost,
        readiness_queries=readiness_queries,
        credential_scope=credential_scope,
        decision_points=tuple(decision_points),
        publication_reviews=publication_reviews,
    )
