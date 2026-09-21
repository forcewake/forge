"""Multi-repository work packages: lanes, phases, saga, frozen candidates (MRP epic).

The review's MRP stories separate four graphs that used to be conflated
into one "just publish everything" step:

- The SERVICE graph (who calls whom) may be cyclic — mutually dependent
  services form legal architectures. The EXECUTION DAG (what to build
  first) may not: a concrete change must decompose into compatible
  phases. :func:`compile_dependencies` (MRP-01/MRP-02) keeps those two
  graphs distinct by phasing items topologically and refusing cycles
  with an explicit "phase the CHANGE" error instead of silently
  reordering an architecture.
- One child execution lane owns ONE writable repository (MRP-03). The
  package is the parent object: :class:`WorkPackage` holds
  :class:`WorkItemRef` children, each naming a single repository, so the
  10-read/1-write first configuration falls out of
  ``writer_items``/``reader_repos`` rather than a bespoke flag.
- Coordinated publication is an explicit SAGA (MRP-04):
  :class:`SagaState` records what published and what failed — no pretend
  rollback by deleting branches that may already carry human edits;
  recovery is by publication intents the CALLER reconciles.
- Integration verification binds to a FROZEN candidate set (MRP-05,
  NXT-25): :func:`freeze_candidate_set` snapshots per-repo candidates,
  images and bundle digests into a
  :class:`~forge.adaptive.models.CandidateSet`;
  :func:`tested_world_digest` fingerprints the COMPLETE tested world
  (changed AND baseline members, exact image artifacts, contracts, test
  bundle, environment pins, policy refs) and :func:`identity_changed`
  says when an old result no longer confirms a new set — a digest
  change IS an identity change. :func:`applicability_digest` is the
  separate, narrower reuse fingerprint (the dependencies an evidence
  record claims to cover), so a plan-revision bump alone — historical
  provenance — never invalidates a world that did not change.

Pure stdlib + the pydantic contract models; no provider I/O here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from forge.adaptive.models import CandidateSet, CandidateSetMember

__all__ = [
    "APPLICABILITY_SCHEMA",
    "TESTED_WORLD_SCHEMA",
    "SagaState",
    "WorkItemRef",
    "WorkPackage",
    "applicability_digest",
    "baseline_members",
    "bound_phases",
    "compile_dependencies",
    "freeze_candidate_set",
    "identity_changed",
    "lane_assignment",
    "recovery_targets",
    "saga_outcomes",
    "tested_world_digest",
]

#: Domain-separation tags for the two digests (versioned: a breaking
#: change to what a digest covers bumps the tag, so old pinned digests
#: can never be confused with new ones). The tags also guarantee the
#: two digests can never collide for the same inputs — they answer
#: different questions and must stay incomparable.
TESTED_WORLD_SCHEMA = "forge.verify.tested-world/1"
APPLICABILITY_SCHEMA = "forge.verify.applicability/1"


@dataclass(frozen=True)
class WorkItemRef:
    """One child execution lane's pointer into its repository.

    A work item is deliberately NOT a diff or a plan step: it is the
    authorization-shaped reference (WHICH repository, writable or not,
    which sibling items it waits for). Everything content-shaped lives
    in the plan revision; everything verification-shaped lives in the
    candidate set.
    """

    item_id: str
    repository_id: str
    writable: bool = True
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkPackage:
    """The parent of repository work items — one change, many lanes.

    The package exists so cross-repository coordination has ONE object
    to authorize and audit. Its shape enforces the MRP-03 invariant
    structurally: each item names exactly one repository and at most one
    item may claim a given repository (a repository written from two
    lanes has no single owner for its publication intent).
    ``read_only_repositories`` are context — repositories the change
    reads for verification but never writes.
    """

    package_id: str
    objective: str
    items: tuple[WorkItemRef, ...] = ()
    read_only_repositories: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        """Return the package's structural violations (empty list = valid).

        Checked: no items at all (nothing to authorize); duplicate item
        ids (a dependency could resolve to either); a dependency on an
        item outside the package (the execution DAG would dangle); the
        same repository claimed by two items (no single writable owner).
        This returns rather than raises so a reviewer can see ALL
        violations at once — the caller decides to fail.
        """
        violations: list[str] = []
        if not self.items:
            violations.append(f"package {self.package_id} has no items: no lane to authorize")

        seen_ids: set[str] = set()
        for item in self.items:
            if item.item_id in seen_ids:
                violations.append(f"duplicate item id: {item.item_id}")
            seen_ids.add(item.item_id)

        known = {item.item_id for item in self.items}
        for item in self.items:
            for dep in item.depends_on:
                if dep not in known:
                    violations.append(f"item {item.item_id} depends on unknown item {dep}")

        seen_repos: set[str] = set()
        for item in self.items:
            if item.repository_id in seen_repos:
                violations.append(
                    f"repository {item.repository_id} is claimed by more than one item"
                    f" ({item.item_id}); one writable lane per repository"
                )
            seen_repos.add(item.repository_id)
        return violations

    def writer_items(self) -> list[WorkItemRef]:
        """The items that write — each one its own single repository."""
        return [item for item in self.items if item.writable]

    def reader_repos(self) -> set[str]:
        """Every repository this package holds READ-ONLY.

        Non-writable items' repositories plus the context repositories.
        Together with :meth:`writer_items` this is the 10-read/1-write
        first configuration — it falls out of the package shape rather
        than being bolted on as a mode flag.
        """
        return {item.repository_id for item in self.items if not item.writable} | set(
            self.read_only_repositories
        )


def compile_dependencies(items: list[WorkItemRef]) -> list[list[str]]:
    """Group item ids into topological PHASES (MRP-01/MRP-02).

    Phase *n* contains every item whose dependencies all landed in
    earlier phases, so dependencies always execute before dependents.
    Phases are breadth-first waves (not a linear topo sort) because the
    point is bounded parallelism: a phase is the maximal set of items
    that may run together, and :func:`bound_phases` then caps its width.

    A cycle raises ``ValueError("cyclic dependencies require phasing the
    CHANGE, not the architecture")``: mutually dependent services form a
    legal SERVICE graph, but this concrete change must decompose into
    phases — the fix is a plan revision that breaks the cycle at a
    compatible intermediate state, never a silent reorder. Dependencies
    naming items outside *items* are reported distinctly (they are a
    malformed package, not an architectural cycle).
    """
    by_id = {item.item_id: item for item in items}
    remaining = set(by_id)
    placed: set[str] = set()
    phases: list[list[str]] = []
    while remaining:
        ready = sorted(
            item_id
            for item_id in remaining
            if all(dep in placed for dep in by_id[item_id].depends_on)
        )
        if not ready:
            unknown = sorted(
                {
                    dep
                    for item_id in remaining
                    for dep in by_id[item_id].depends_on
                    if dep not in by_id
                }
            )
            if unknown:
                raise ValueError(f"dependencies name items outside the package: {unknown}")
            raise ValueError("cyclic dependencies require phasing the CHANGE, not the architecture")
        phases.append(ready)
        placed.update(ready)
        remaining.difference_update(ready)
    return phases


def bound_phases(phases: list[list[str]], *, max_parallel: int = 2) -> list[list[str]]:
    """Split any phase wider than *max_parallel* into consecutive sub-phases.

    A phase says "these items MAY run together"; the executor still has
    a finite lane budget. Sub-phases preserve the phase's order (items
    within a phase are independent, so any order is correct — this one
    is merely deterministic) and never REORDER across phases: a
    dependency still lands strictly before its dependent.
    """
    if max_parallel < 1:
        raise ValueError("max_parallel must be at least 1")
    bounded: list[list[str]] = []
    for phase in phases:
        for start in range(0, len(phase), max_parallel):
            bounded.append(phase[start : start + max_parallel])
    return bounded


def lane_assignment(package: WorkPackage) -> dict[str, dict]:
    """Map each item to its child-lane authorization (MRP-03).

    ``item_id -> {"repository_id": str, "mode": "writable"|"read_only",
    "siblings_read": [repo ids]}``. Each writer gets its ONE target
    repository; every sibling's repository (and each context
    read-only repository) is listed as read-arrivals — the related
    contracts and snapshots of siblings reach this lane read-only, which
    is what makes cross-lane evidence citable without making it
    writable. An invalid package refuses to assign (fail closed) —
    assigning lanes to an incoherent package would authorize it.
    """
    violations = package.validate()
    if violations:
        raise ValueError(f"refusing to assign lanes to an invalid package: {violations}")

    read_universe = {item.repository_id for item in package.items} | set(
        package.read_only_repositories
    )
    assignment: dict[str, dict] = {}
    for item in package.items:
        assignment[item.item_id] = {
            "repository_id": item.repository_id,
            "mode": "writable" if item.writable else "read_only",
            "siblings_read": sorted(read_universe - {item.repository_id}),
        }
    return assignment


@dataclass(frozen=True)
class SagaState:
    """The explicit record of a coordinated multi-lane publication (MRP-04).

    A frozen value object: every transition returns a NEW state, so the
    caller's audit trail is the sequence of states, not an in-place
    mutation nobody can replay.

    The hard rule: there is NO pretend rollback. A failed publication
    step does not delete already-published branches — a branch open for
    review may carry human edits, and deleting it destroys work while
    pretending to "compensate". Recovery is by PUBLICATION INTENTS: the
    caller reconciles each intent against the provider and drives it to
    a decision. The saga only RECORDS what happened.
    """

    package_id: str
    steps: tuple[str, ...]
    published: tuple[str, ...] = ()
    state: str = "pending"

    def mark_published(self, item_id: str) -> SagaState:
        """Record that *item_id* published; ``published`` only when ALL steps did.

        Appends to the published tuple; the state becomes
        ``partially_published`` after the first step and ``published``
        exactly when every step in :attr:`steps` is published — a
        coordinated publication is not done until its last lane is.
        """
        self._require_step(item_id)
        if item_id in self.published:
            raise ValueError(f"item {item_id} is already recorded as published")
        published = self.published + (item_id,)
        state = "published" if set(published) >= set(self.steps) else "partially_published"
        return replace(self, published=published, state=state)

    def mark_failed(self, item_id: str) -> SagaState:
        """Record that publishing *item_id* FAILED — and stop there.

        No compensating deletes, no fabricated "rolled back" state: a
        published branch may already carry human edits, so deleting it
        is destroying work, not recovery. The failed state tells the
        caller to reconcile the outstanding publication intents (see
        :func:`recovery_targets`); the saga only records.
        """
        self._require_step(item_id)
        return replace(self, state="failed")

    def _require_step(self, item_id: str) -> None:
        if item_id not in self.steps:
            raise ValueError(f"item {item_id} is not a step of saga for package {self.package_id}")


def recovery_targets(saga: SagaState) -> list[str]:
    """The steps not yet published, in publication order (MRP-04).

    After a failure these are exactly the publication intents that must
    be reconciled or driven forward — the unfinished half of the
    coordinated publication, not a rollback plan.
    """
    done = set(saga.published)
    return [step for step in saga.steps if step not in done]


def saga_outcomes(per_item: dict[str, bool]) -> str:
    """Fold per-step outcomes into one saga outcome (MRP-04).

    Any ``False`` → ``failed`` (one failed lane fails the coordinated
    publication); no falses and all ``True`` → ``published``; no steps
    at all → ``pending`` (nothing has happened yet, nothing has failed).
    """
    if not per_item:
        return "pending"
    if any(not outcome for outcome in per_item.values()):
        return "failed"
    return "published"


def freeze_candidate_set(
    work_id: str,
    plan_revision: int,
    contract_digest: str,
    per_repo: dict[str, dict],
    *,
    contract_bundle_digest: str | None = None,
    test_bundle_digest: str | None = None,
    environment_profile_digest: str | None = None,
) -> CandidateSet:
    """Freeze per-repo candidates into the unit of system verification (MRP-05).

    *per_repo* maps ``repository_id -> {"base_oid", "candidate_oid",
    "role", "image_digest"}``. Construction IS the validation that
    exists today: the pydantic model enforces the closed
    changed/baseline role vocabulary, the 64-hex contract digest and
    unique repositories — this function adds nothing looser. (Known
    models gap, NXT-25: member oids and image digests have NO format
    validators yet, so garbage spelled like an oid flows through; the
    digest below faithfully fingerprints whatever is there.) Members
    are sorted by repository so the frozen identity is
    dict-order-proof. The optional bundle digests (NXT-25) freeze the
    REST of the tested world at the same moment: the contract bundle,
    the test bundle and the environment profile the candidates will be
    verified against.
    """
    members = [
        CandidateSetMember(
            repository_id=repository_id,
            base_oid=spec["base_oid"],
            candidate_oid=spec["candidate_oid"],
            role=spec["role"],
            image_digest=spec["image_digest"],
        )
        for repository_id, spec in sorted(per_repo.items())
    ]
    return CandidateSet(
        work_id=work_id,
        plan_revision=plan_revision,
        work_contract_digest=contract_digest,
        members=members,
        contract_bundle_digest=contract_bundle_digest,
        test_bundle_digest=test_bundle_digest,
        environment_profile_digest=environment_profile_digest,
    )


def _canonical_digest(payload: object) -> str:
    """sha256 over the canonical JSON encoding of *payload*.

    ``sort_keys`` makes every mapping dict-order-proof and every list
    the caller must sort is sorted by construction, so two worlds that
    differ only in HOW they were spelled serialize to the same bytes —
    and therefore to the same digest. That determinism is the whole
    point: the digest is compared across runs and stored as identity.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _member_records(candidate_set: CandidateSet) -> list[dict]:
    """The per-member world records, sorted by repository.

    Each record carries WHAT was checked out (``candidate_oid``), the
    EXACT artifact built from it (``image_digest`` — the same source
    commit can rebuild into a different image) and the member's role.
    ``base_oid`` is deliberately absent: it is the diff the candidate
    was cut FROM — provenance of the change, not content of the tested
    world. (A baseline member rides along with candidate == base, so
    the baseline version is pinned by its ``candidate_oid``.)
    """
    return [
        {
            "repository_id": member.repository_id,
            "role": member.role,
            "candidate_oid": member.candidate_oid,
            "image_digest": member.image_digest,
        }
        for member in sorted(candidate_set.members, key=lambda member: member.repository_id)
    ]


def _policy_ref_list(policy_refs: Iterable[str] | None) -> list[str]:
    """Policy refs as a sorted, de-duplicated list — set-like, order-free."""
    return sorted(set(policy_refs or ()))


def tested_world_digest(
    candidate_set: CandidateSet,
    environment: Mapping[str, str] | None = None,
    policy_refs: Iterable[str] | None = None,
) -> str:
    """The canonical fingerprint of EVERYTHING that defined the run (NXT-25).

    Same source commits are NOT the same tested world: a rebuilt image,
    a different test bundle, a moved contract or environment profile,
    a drifted baseline dependency or a changed compatibility policy all
    produce a different world, and an old result confirms nothing about
    a world it did not run in. The digest therefore covers:

    - every member, changed AND baseline (repository, role, candidate
      oid, exact image artifact digest);
    - the work contract digest and the contract bundle digest;
    - the test bundle digest and the environment profile digest;
    - *environment* — external dependency pins (service/broker/… exact
      artifact digests) the model does not carry as fields;
    - *policy_refs* — the compatibility/policy references under which
      the run was judged.

    Deliberately EXCLUDED: ``work_id`` and ``plan_revision``. They are
    historical provenance — WHO asked, under which plan revision — not
    parts of the world. A revision bump alone must not invalidate a
    world that did not change (the review's counter-warning); the
    narrower reuse question is answered separately by
    :func:`applicability_digest`.
    """
    payload = {
        "schema": TESTED_WORLD_SCHEMA,
        "members": _member_records(candidate_set),
        "work_contract_digest": candidate_set.work_contract_digest,
        "contract_bundle_digest": candidate_set.contract_bundle_digest,
        "test_bundle_digest": candidate_set.test_bundle_digest,
        "environment_profile_digest": candidate_set.environment_profile_digest,
        "environment_pins": dict(environment or {}),
        "policy_refs": _policy_ref_list(policy_refs),
    }
    return _canonical_digest(payload)


def applicability_digest(
    candidate_set: CandidateSet,
    environment: Mapping[str, str] | None = None,
    policy_refs: Iterable[str] | None = None,
) -> str:
    """The fingerprint of what an evidence record claims to COVER (NXT-25).

    Distinct concept from :func:`tested_world_digest`, on purpose. The
    tested world is everything that defined one run; applicability is
    the INPUT list a green result vouches for when reused elsewhere:
    the dependencies (every member, changed and baseline, at their
    candidate oids and exact image artifacts), judged by that test
    bundle, under that environment profile and pins, under those
    policies. Reuse must still be bounded — the review forbids fixing
    over-broad invalidation with indefinite reuse by SHA alone, so
    test/environment/policy changes move this digest too.

    Excluded beside the provenance fields: the work-contract and
    contract-bundle digests. They defined THIS run's obligations; they
    are not dependencies the evidence speaks about, so evidence stays
    applicable when only the asking contract changed.
    """
    payload = {
        "schema": APPLICABILITY_SCHEMA,
        "members": _member_records(candidate_set),
        "test_bundle_digest": candidate_set.test_bundle_digest,
        "environment_profile_digest": candidate_set.environment_profile_digest,
        "environment_pins": dict(environment or {}),
        "policy_refs": _policy_ref_list(policy_refs),
    }
    return _canonical_digest(payload)


def identity_changed(
    a: CandidateSet,
    b: CandidateSet,
    *,
    environment: Mapping[str, str] | None = None,
    policy_refs: Iterable[str] | None = None,
) -> bool:
    """Whether two candidate sets are DIFFERENT verification identities (MRP-05).

    A thin consumer of :func:`tested_world_digest`: the two sets are
    the same verification identity exactly when their complete tested
    worlds — all members with their exact image artifacts, contracts,
    test bundle, environment pins and policy refs — share one digest.
    An integration result binds to the exact world it verified: change
    ANY of it (one candidate, one rebuilt image, one drifted baseline,
    one bundle digest) and the old green result confirms nothing about
    the new world — the integration must re-run. What still does NOT
    move identity: a re-declared base (diff provenance, not tested
    content) and a plan-revision or work-id bump alone (who asked, and
    under which revision, is not part of the world that was tested).
    """
    return tested_world_digest(a, environment, policy_refs) != tested_world_digest(
        b, environment, policy_refs
    )


def baseline_members(candidate_set: CandidateSet) -> list[str]:
    """Repositories riding along unchanged (role ``baseline``, MRP-05).

    Their current digests are part of the frozen set so integration
    verification runs against the SAME world the candidates were cut
    from — a baseline that silently moved is a different system under
    test, which is exactly what :func:`tested_world_digest` (and thus
    :func:`identity_changed`) now catches: baseline members enter the
    digest with their candidate oids and image artifacts like any
    other member.
    """
    return [member.repository_id for member in candidate_set.members if member.role == "baseline"]
