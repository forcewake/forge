"""Bounded impact slices and retrieval coverage over the system manifest.

DSC-08 (review 05868e9 backlog). Loading every repository into one context
is expensive AND still misses implicit contracts — discovery should follow
the change impact, not repository size. This module computes that follow:
a depth-bounded walk of the manifest's dependency edges around the changed
services, with everything the bound excludes STORED for review rather than
silently dropped, plus the coverage record that says whether retrieval
actually gathered the slice. Small independent discovery specialists are
planned here too: separable investigations fan out, while one root planner
stays in charge of resolving the contradictions between their reports.
"""

from __future__ import annotations

from collections import deque

from forge.adaptive.system_manifest import SystemManifest

#: Discriminator tag for impact-slice records (the manifest models keep
#: theirs as pydantic Literals; the slice is a plain dict, so the tag is
#: just carried here).
SLICE_SCHEMA = "forge.impact.slice/1"


def impact_slice(
    manifest: SystemManifest,
    changed_services: list[str],
    *,
    max_depth: int = 3,
) -> dict:
    """Breadth-first impact walk over the dependency edges, both directions.

    Why both directions: a CONSUMER of my event is impacted by my change,
    and a PROVIDER I depend on is context for it — an undirected walk finds
    both. Services at depth 1 (direct producers/consumers) are REQUIRED
    evidence; deeper services are OPTIONAL exploration. The walk stops at
    ``max_depth``; when a frontier still remains there, ``depth_capped`` is
    True and ``omitted`` stores the immediate ring of services the cap
    excluded (the ring only, not the whole remainder — the slice itself
    must stay bounded). The changed services are seeds at depth 0 and are
    never listed as impact. Cycles in the manifest are simply visited once.

    Changed services absent from the manifest raise ``ValueError``: a slice
    seeded from a typo must fail visibly, not report "no impact".
    """
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")
    known = {service.service_id for service in manifest.services}
    unknown = sorted(set(changed_services) - known)
    if unknown:
        raise ValueError(f"changed services not registered in manifest {unknown}")

    neighbors: dict[str, set[str]] = {service_id: set() for service_id in known}
    for edge in manifest.edges:
        neighbors[edge.source].add(edge.target)
        neighbors[edge.target].add(edge.source)

    seeds = set(changed_services)
    depth: dict[str, int] = {service_id: 0 for service_id in seeds}
    queue: deque[str] = deque(sorted(seeds))
    while queue:
        current = queue.popleft()
        if depth[current] >= max_depth:
            continue
        for nxt in sorted(neighbors[current]):
            if nxt not in depth:
                depth[nxt] = depth[current] + 1
                queue.append(nxt)

    # The frontier that remains AT the cap: unvisited neighbors of the
    # deepest reached services. Omitted dependencies are stored for review,
    # never silently dropped.
    frontier: set[str] = set()
    for service_id, hop in depth.items():
        if hop == max_depth:
            frontier.update(n for n in neighbors[service_id] if n not in depth)

    return {
        "schema": SLICE_SCHEMA,
        "required": sorted(sid for sid, hop in depth.items() if hop == 1),
        "optional": sorted(sid for sid, hop in depth.items() if hop >= 2),
        "depth_capped": bool(frontier),
        "omitted": sorted(frontier),
    }


def retrieval_coverage(slice_: dict, gathered: set[str]) -> dict:
    """Record what the retrieval pass actually covered against the slice.

    Why a record: coverage is review evidence. A missing REQUIRED service
    must be visible before anyone draws conclusions from the context, and
    material gathered outside the slice does not count as coverage of it
    (it is noise, not evidence). An empty universe is fully covered —
    there was nothing to miss.
    """
    required = set(slice_.get("required") or [])
    optional = set(slice_.get("optional") or [])
    universe = required | optional
    covered = sorted(gathered & universe)
    missing_required = sorted(required - gathered)
    ratio = len(covered) / len(universe) if universe else 1.0
    return {
        "covered": covered,
        "missing_required": missing_required,
        "coverage_ratio": ratio,
    }


def plan_specialists(questions: list[str], *, max_specialists: int = 4) -> list[dict]:
    """Split separable investigations into small independent specialists.

    Why split at all: parallel discovery only pays when the investigations
    are INDEPENDENT. Questions that share a leading ``[repo]`` or
    ``[domain]`` tag (e.g. ``"[billing] where are invoices calculated?"``)
    belong to one scope; an untagged question gets its own specialist
    because nothing asserts it shares a scope with anything.

    One ROOT PLANNER stays in charge: it assigns these scopes and resolves
    the contradictions between specialist reports before anything reaches
    the work contract — parallel reports extend coverage, never read
    authority and never the approved goal. When grouping yields more
    specialists than ``max_specialists``, the tail merges into the root
    planner's own scope rather than spawning unbounded parallelism.
    """
    if max_specialists < 1:
        raise ValueError("max_specialists must be at least 1")

    groups: dict[str, list[str]] = {}
    order: list[str] = []
    adhoc = 0
    for question in questions:
        stripped = question.lstrip()
        tag: str | None = None
        if stripped.startswith("["):
            end = stripped.find("]")
            if end > 1:
                tag = stripped[1:end]
        if tag is not None:
            if tag not in groups:
                groups[tag] = []
                order.append(tag)
            groups[tag].append(question)
        else:
            adhoc += 1
            key = f"adhoc-{adhoc}"
            groups[key] = [question]
            order.append(key)

    def _scope(key: str) -> str:
        return key if not key.startswith("adhoc-") else "adhoc"

    specialists: list[dict] = []
    if len(order) <= max_specialists:
        for index, key in enumerate(order, start=1):
            specialists.append(
                {
                    "specialist_id": f"specialist-{index}",
                    "scope": _scope(key),
                    "questions": groups[key],
                }
            )
        return specialists

    kept = order[: max_specialists - 1]
    merged = order[max_specialists - 1 :]
    for index, key in enumerate(kept, start=1):
        specialists.append(
            {
                "specialist_id": f"specialist-{index}",
                "scope": _scope(key),
                "questions": groups[key],
            }
        )
    tail = [question for key in merged for question in groups[key]]
    specialists.append(
        {
            "specialist_id": "root",
            "scope": "root",
            "questions": tail,
        }
    )
    return specialists
