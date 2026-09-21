"""VER-08: the ten-service benchmark system with seeded hazards.

These tests pin the honesty mechanics of the benchmark itself: the
fixture is DETERMINISTIC (same seed, identical dict — a claim can name
its fixture), hazards are VISIBLE and documented where they landed, the
original system is never mutated by seeding, the acceptance probe fixes
the 10-read/1-write-first shape, and a claim carries the versioned
schema tag with ``measured`` forced on — a synthetic system cannot
dress its numbers up as something they are not.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from forge.adaptive.benchmark import (
    CLAIM_SCHEMA,
    HAZARD_KINDS,
    acceptance_probe,
    benchmark_claims,
    generate_system,
    seed_hazards,
)
from forge.adaptive.system_manifest import parse_manifest_yaml


class TestGenerateSystem:
    def test_default_is_ten_services_with_the_pinned_shape(self):
        system = generate_system()
        assert [service["id"] for service in system["services"]] == [
            f"svc-{i}" for i in range(1, 11)
        ]
        for i, service in enumerate(system["services"], start=1):
            assert service["repositories"] == [f"repo-{i}"]
            assert service["owner"] == f"team-{i % 3}"
            assert service["apis"]
            assert isinstance(service["events"], list)

    def test_edges_chain_consecutive_services_with_alternating_kinds(self):
        system = generate_system()
        edges = system["edges"]
        assert len(edges) == 9
        assert [(edge["source"], edge["target"]) for edge in edges] == [
            (f"svc-{i}", f"svc-{i + 1}") for i in range(1, 10)
        ]
        assert [edge["kind"] for edge in edges] == ["api", "event", "schema"] * 3
        assert all(edge["provenance"] == "declared" for edge in edges)

    def test_same_seed_twice_is_identical(self):
        assert generate_system() == generate_system(n_services=10, seed=7)
        assert generate_system(n_services=12, seed=99) == generate_system(12, 99)

    def test_n_services_is_honored(self):
        system = generate_system(n_services=4, seed=7)
        assert [service["id"] for service in system["services"]] == [
            "svc-1",
            "svc-2",
            "svc-3",
            "svc-4",
        ]
        assert len(system["edges"]) == 3

    def test_a_system_with_no_services_is_refused(self):
        with pytest.raises(ValueError, match="at least 1"):
            generate_system(n_services=0)

    def test_the_output_parses_against_the_real_system_manifest(self):
        # "SystemManifest-shaped" is a claim about the administrative
        # YAML form, so prove it with the real parser.
        manifest = parse_manifest_yaml(yaml.safe_dump(generate_system()))
        assert len(manifest.services) == 10
        assert manifest.services[0].service_id == "svc-1"
        assert manifest.services[0].repositories == ["repo-1"]


class TestSeedHazards:
    def test_hazards_are_visible_in_a_documented_list(self):
        seeded = seed_hazards(generate_system(), list(HAZARD_KINDS))
        assert [hazard["kind"] for hazard in seeded["hazards"]] == list(HAZARD_KINDS)
        for hazard in seeded["hazards"]:
            assert hazard["where"], "each hazard documents WHERE it landed"
            assert hazard["detail"], "each hazard documents WHAT it is"

    def test_the_original_system_is_not_mutated(self):
        system = generate_system(seed=3)
        before = copy.deepcopy(system)
        seeded = seed_hazards(system, list(HAZARD_KINDS))
        assert system == before
        assert "hazards" not in system
        assert seeded is not system

    def test_shared_event_schema_puts_one_event_on_two_distant_services(self):
        seeded = seed_hazards(generate_system(), ["shared_event_schema"])
        first, last = seeded["services"][0], seeded["services"][-1]
        assert "order.expired.v2" in first["events"]
        assert "order.expired.v2" in last["events"]
        for middle in seeded["services"][1:-1]:
            assert "order.expired.v2" not in middle["events"]

    def test_hidden_db_shared_adds_database_edges_between_the_distant_pair(self):
        seeded = seed_hazards(generate_system(), ["hidden_db_shared"])
        database_edges = [edge for edge in seeded["edges"] if edge["kind"] == "database"]
        assert {(edge["source"], edge["target"]) for edge in database_edges} == {
            ("svc-1", "svc-10"),
            ("svc-10", "svc-1"),
        }

    def test_missing_migration_test_leaves_the_middle_service_unguarded(self):
        seeded = seed_hazards(generate_system(), ["missing_migration_test"])
        targets = [service for service in seeded["services"] if "migrations" in service]
        assert [service["id"] for service in targets] == ["svc-6"]
        assert targets[0]["migrations"]
        assert targets[0]["test_locations"] == []

    def test_an_unknown_hazard_kind_is_refused(self):
        with pytest.raises(ValueError, match="unknown hazard kinds"):
            seed_hazards(generate_system(), ["subtle_undocumented_bomb"])


class TestAcceptanceProbe:
    def test_the_probe_reports_the_acceptance_shape(self):
        system = generate_system()
        hazards = seed_hazards(system, list(HAZARD_KINDS))
        assert acceptance_probe(system, hazards) == {
            "services": 10,
            "edges": 9,
            "hazards_seeded": 3,
            "writable_first": 1,
        }

    def test_writable_first_is_one_the_ten_read_one_write_shape(self):
        probe = acceptance_probe(
            generate_system(),
            seed_hazards(generate_system(), ["shared_event_schema"]),
        )
        assert probe["services"] == 10
        assert probe["writable_first"] == 1
        assert probe["hazards_seeded"] == 1


class TestBenchmarkClaims:
    def test_claims_carry_the_versioned_schema_and_the_measured_flag(self):
        probe = acceptance_probe(
            generate_system(), seed_hazards(generate_system(), list(HAZARD_KINDS))
        )
        claim = benchmark_claims(probe)
        assert claim["schema"] == CLAIM_SCHEMA == "forge.benchmark.claim/1"
        assert claim["measured"] is True
        assert claim["services"] == 10
        assert claim["hazards_seeded"] == 3

    def test_a_result_cannot_rebrand_itself(self):
        claim = benchmark_claims({"schema": "forge.vendor.claim/9", "measured": False})
        assert claim["schema"] == "forge.benchmark.claim/1"
        assert claim["measured"] is True
