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
- Integration verification binds to a FROZEN candidate set (MRP-05):
  :func:`freeze_candidate_set` snapshots per-repo candidate oids into a
  :class:`~forge.adaptive.models.CandidateSet`, and
  :func:`identity_changed` says when an old result no longer confirms a
  new set.

Pure stdlib + the pydantic contract models; no provider I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from forge.adaptive.models import CandidateSet, CandidateSetMember

__all__ = [
    "SagaState",
    "WorkItemRef",
    "WorkPackage",
    "baseline_members",
    "bound_phases",
    "compile_dependencies",
    "freeze_candidate_set",
    "identity_changed",
    "lane_assignment",
    "recovery_targets",
    "saga_outcomes",
]


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
) -> CandidateSet:
    """Freeze per-repo candidates into the unit of system verification (MRP-05).

    *per_repo* maps ``repository_id -> {"base_oid", "candidate_oid",
    "role", "image_digest"}``. Construction IS the validation: the
    pydantic model enforces 40-hex git oids, the closed
    changed/baseline role vocabulary, the 64-hex contract digest and
    unique repositories — this function adds nothing looser. Members are
    sorted by repository so the frozen identity is dict-order-proof.
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
    )


def identity_changed(a: CandidateSet, b: CandidateSet) -> bool:
    """Whether two candidate sets are DIFFERENT verification identities (MRP-05).

    True when any member's ``candidate_oid`` differs or a repository was
    added or removed. An integration result binds to the exact set it
    verified: changing ONE candidate invalidates that binding — the old
    green result confirms nothing about the new set and the integration
    must re-run. (A moved base or new digests with the same candidate
    content do NOT change identity: the candidates are what was tested.)
    """
    a_by_repo = {member.repository_id: member for member in a.members}
    b_by_repo = {member.repository_id: member for member in b.members}
    if a_by_repo.keys() != b_by_repo.keys():
        return True
    return any(
        a_by_repo[repository_id].candidate_oid != b_by_repo[repository_id].candidate_oid
        for repository_id in a_by_repo
    )


def baseline_members(candidate_set: CandidateSet) -> list[str]:
    """Repositories riding along unchanged (role ``baseline``, MRP-05).

    Their current digests are part of the frozen set so integration
    verification runs against the SAME world the candidates were cut
    from — a baseline that silently moved is a different system under
    test, which is exactly what :func:`identity_changed` would catch.
    """
    return [member.repository_id for member in candidate_set.members if member.role == "baseline"]
