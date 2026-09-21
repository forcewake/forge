"""A ten-service benchmark system with seeded, VISIBLE hazards (VER-08).

Review 05868e9 backlog. Claims about a system-level workflow need a
system to run against, and the claims must say what was ACTUALLY run —
a synthetic benchmark dressed up as customer evidence is the failure
mode this module is built to avoid, in two ways:

- the system is GENERATED deterministically from a seed
  (:func:`generate_system`), so a claim can name the exact fixture and
  anyone can reproduce it byte-for-byte;
- hazards are SEEDED, not hidden (:func:`seed_hazards`). Each injected
  hazard is written into a visible ``hazards`` list documenting what was
  seeded and where. The benchmark measures whether the workflow FINDS a
  known hazard — a trap the agent was never told about would measure
  luck, not capability, and an unfindable one would measure nothing.

:func:`acceptance_probe` fixes the representative acceptance shape —
read the whole system, write ONE service first — and
:func:`benchmark_claims` stamps a probe result as a measured claim
under a versioned schema tag, because a claim without the "this was
actually executed" marker is marketing.

Pure stdlib, dict-shaped throughout: the output parses against the
administrative YAML form of
:class:`forge.adaptive.system_manifest.SystemManifest` (the CLEAN
system does; a hazarded one deliberately carries fields the registry
would refuse, because the hazards are the finding, not the
registration).
"""

from __future__ import annotations

import copy
import random

__all__ = [
    "CLAIM_SCHEMA",
    "HAZARD_KINDS",
    "acceptance_probe",
    "benchmark_claims",
    "generate_system",
    "seed_hazards",
]

#: The schema discriminator every benchmark claim carries (versioned:
#: bumping the tag is how a breaking change to what claims MEAN stays
#: distinguishable from claims already published under the old shape).
CLAIM_SCHEMA = "forge.benchmark.claim/1"

#: The closed hazard vocabulary — each kind injects exactly one hazard,
#: and an unknown kind is refused instead of guessed.
HAZARD_KINDS: tuple[str, ...] = (
    "shared_event_schema",
    "hidden_db_shared",
    "missing_migration_test",
)

#: The event two distant services both list when
#: ``shared_event_schema`` is seeded: one schema, two owners — the
#: cross-repository coordination trap the review cares about.
_SHARED_EVENT = "order.expired.v2"

#: Edge kinds the generator alternates between, so a generated system
#: exercises every declared-dependency kind the manifest models.
_EDGE_KINDS: tuple[str, ...] = ("api", "event", "schema")


def _database_edge(source: str, target: str) -> dict:
    """The one edge shape :func:`seed_hazards` injects for a shared datastore."""
    return {"source": source, "target": target, "kind": "database", "provenance": "declared"}


def generate_system(n_services: int = 10, seed: int = 7) -> dict:
    """Deterministically generate a SystemManifest-shaped system dict.

    Services ``svc-1..svc-n`` each own one repository (``repo-{i}``),
    are routed to ``team-{i % 3}``, and expose a seeded-random smattering
    of apis and events — the randomness only varies CONTENT, and
    ``random.Random(seed)`` makes it reproducible: the same (n, seed)
    always yields the identical dict, so a claim can name its fixture.

    Consecutive services are joined by edges alternating through
    api/event/schema with provenance ``declared`` — a chain, not a
    hairball, so a benchmark's dependency story is legible at a glance.
    """

    if n_services < 1:
        raise ValueError(
            "n_services must be at least 1 — a manifest with no services is not a system"
        )

    rng = random.Random(seed)
    services: list[dict] = []
    for i in range(1, n_services + 1):
        apis = [f"svc-{i}/api"]
        if rng.random() < 0.5:
            apis.append(f"svc-{i}/internal-api")
        events: list[str] = []
        if rng.random() < 0.5:
            events.append(f"svc-{i}.events.v1")
        services.append(
            {
                "id": f"svc-{i}",
                "repositories": [f"repo-{i}"],
                "apis": apis,
                "events": events,
                "owner": f"team-{i % 3}",
            }
        )

    edges = [
        {
            "source": f"svc-{i}",
            "target": f"svc-{i + 1}",
            "kind": _EDGE_KINDS[(i - 1) % len(_EDGE_KINDS)],
            "provenance": "declared",
        }
        for i in range(1, n_services)
    ]

    return {
        "manifest_id": f"benchmark-{n_services}-seed-{seed}",
        "services": services,
        "edges": edges,
    }


def seed_hazards(system: dict, kinds: list[str]) -> dict:
    """Inject the named hazards into a COPY of *system*; the original is untouched.

    Each kind in *kinds* adds exactly one hazard:

    - ``shared_event_schema`` — the first and last service both list the
      event ``order.expired.v2``: a distant pair sharing one schema,
      invisible to any single-repository read;
    - ``hidden_db_shared`` — a database edge between the first and last
      service in BOTH directions: the shared datastore no service's
      declared API surface mentions;
    - ``missing_migration_test`` — the middle service gets non-empty
      ``migrations`` with an EMPTY ``test_locations``: schema changes
      with nothing guarding them.

    The returned copy carries a ``hazards`` list documenting every
    seeded hazard and where it landed. Hazards are VISIBLE, not traps:
    the benchmark grades whether the workflow finds what the fixture
    says is there — an undocumented hazard would grade luck.
    """

    unknown = [kind for kind in kinds if kind not in HAZARD_KINDS]
    if unknown:
        raise ValueError(
            f"unknown hazard kinds {unknown}; the closed vocabulary is {list(HAZARD_KINDS)} "
            "— an unknown kind is refused, never improvised"
        )

    seeded = copy.deepcopy(system)
    services = seeded["services"]
    n = len(services)
    first, last = services[0], services[n - 1]
    hazards: list[dict] = []

    for kind in kinds:
        if kind == "shared_event_schema":
            for service in (first, last):
                if _SHARED_EVENT not in service["events"]:
                    service["events"].append(_SHARED_EVENT)
            hazards.append(
                {
                    "kind": kind,
                    "where": [first["id"], last["id"]],
                    "detail": f"both services list event {_SHARED_EVENT!r} — one schema, two owners",
                }
            )
        elif kind == "hidden_db_shared":
            seeded["edges"].append(_database_edge(first["id"], last["id"]))
            seeded["edges"].append(_database_edge(last["id"], first["id"]))
            hazards.append(
                {
                    "kind": kind,
                    "where": [first["id"], last["id"]],
                    "detail": "a database edge between distant services, absent from both declared API surfaces",
                }
            )
        else:  # missing_migration_test — the only remaining closed-vocabulary kind
            target = services[n // 2]
            target["migrations"] = [f"{target['repositories'][0]}/db/migrate"]
            target["test_locations"] = []
            hazards.append(
                {
                    "kind": kind,
                    "where": [target["id"]],
                    "detail": "migrations present but test_locations is empty — schema changes with no guard",
                }
            )

    seeded["hazards"] = hazards
    return seeded


def acceptance_probe(system: dict, hazards: dict) -> dict:
    """Summarize the representative acceptance shape for this fixture.

    ``writable_first`` is 1 by design: the acceptance scenario the
    review fixed is read the WHOLE system (n reads), then write ONE
    service first — the minimal-blast-radius first change. A benchmark
    that has to write everything at once is not measuring the same
    workflow the customer runs.
    """

    return {
        "services": len(system["services"]),
        "edges": len(system["edges"]),
        "hazards_seeded": len(hazards["hazards"]),
        "writable_first": 1,
    }


def benchmark_claims(result: dict) -> dict:
    """Stamp *result* as a measured claim under the versioned claim schema.

    ``schema`` and ``measured`` are forced AFTER the spread, so a result
    dict cannot rebrand itself: a synthetic system's claims must say
    what was actually run, and nothing downstream can flip the markers
    that make the claim honest.
    """

    return {**result, "schema": CLAIM_SCHEMA, "measured": True}
