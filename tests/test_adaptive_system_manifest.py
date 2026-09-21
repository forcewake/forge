"""DSC-07: the system manifest accepts real topology, refuses its corruption classes.

Cycles are legal TOPOLOGY (the service graph is not a work DAG), ``owner``
is routing-only metadata that must never smell like a credential, and
edges carry closed provenance/kind vocabularies. These tests pin each
guarantee at the boundary the review cares about: the YAML import, the
Backstage translation, and the pydantic models themselves.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from forge.adaptive.system_manifest import (
    DependencyEdge,
    ServiceEntry,
    SystemManifest,
    parse_manifest_yaml,
    translate_backstage,
)

MANIFEST_YAML = """\
manifest_id: local-stack
services:
  - id: orders
    repositories: [core/orders-api]
    apis: [Orders API]
    events: [order.created]
    owner: team-commerce
  - id: billing
    repositories: [core/billing-api]
    events: [invoice.issued]
edges:
  - source: orders
    target: billing
    kind: api
    provenance: declared
"""


def _service(service_id: str = "orders", **overrides) -> ServiceEntry:
    payload = {"service_id": service_id, "repositories": [f"core/{service_id}-api"]}
    payload.update(overrides)
    return ServiceEntry.model_validate(payload)


class TestYamlImport:
    def test_yaml_round_trip(self):
        first = parse_manifest_yaml(MANIFEST_YAML)
        assert first.manifest_id == "local-stack"
        orders = first.services[0]
        assert orders.service_id == "orders"
        assert orders.repositories == ["core/orders-api"]
        assert orders.apis == ["Orders API"]
        assert orders.events == ["order.created"]
        assert orders.owner == "team-commerce"
        assert first.edges[0].provenance == "declared"

        # What parsed once must parse again unchanged — the dumped model is
        # itself valid import YAML.
        redumped = yaml.safe_dump(first.model_dump())
        assert parse_manifest_yaml(redumped) == first

    def test_edges_are_optional(self):
        manifest = parse_manifest_yaml(
            "manifest_id: solo\nservices:\n  - id: orders\n    repositories: [core/orders-api]\n"
        )
        assert manifest.edges == []

    def test_unknown_top_level_key_is_refused(self):
        with pytest.raises(ValueError, match="invalid system manifest"):
            parse_manifest_yaml(MANIFEST_YAML + "owner_policy: allow-all\n")

    def test_unknown_service_key_is_refused(self):
        with pytest.raises(ValueError, match="invalid system manifest"):
            parse_manifest_yaml(
                "manifest_id: x\n"
                "services:\n"
                "  - id: orders\n"
                "    repositories: [core/orders-api]\n"
                "    credentials: [file:./env]\n"
            )

    def test_unknown_edge_key_is_refused(self):
        # Appending an indented key extends the LAST edge mapping — an
        # extra field the edge model must refuse.
        with pytest.raises(ValueError, match="invalid system manifest"):
            parse_manifest_yaml(MANIFEST_YAML + "    weight: 3\n")

    def test_yaml_syntax_error_raises_value_error(self):
        with pytest.raises(ValueError, match="does not parse"):
            parse_manifest_yaml("services: [unclosed")

    def test_non_mapping_document_is_refused(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            parse_manifest_yaml("- just\n- a\n- list\n")


class TestSystemManifest:
    def _manifest(self, edges: list[tuple[str, str]]) -> SystemManifest:
        service_ids = sorted({s for edge in edges for s in edge} | {"orders"})
        return SystemManifest.model_validate(
            {
                "manifest_id": "m",
                "services": [_service(sid) for sid in service_ids],
                "edges": [
                    DependencyEdge.model_validate(
                        {"source": s, "target": t, "kind": "api", "provenance": "declared"}
                    )
                    for s, t in edges
                ],
            }
        )

    def test_a_cycle_is_accepted_as_topology(self):
        # A->B->A: mutually dependent services are legal HERE. The service
        # graph and an execution DAG are different graphs; scheduling
        # constraints belong to the plan, not the registry.
        manifest = self._manifest([("orders", "billing"), ("billing", "orders")])
        assert [(e.source, e.target) for e in manifest.edges] == [
            ("orders", "billing"),
            ("billing", "orders"),
        ]

    def test_edge_to_unknown_service_is_refused(self):
        with pytest.raises(ValidationError, match="unknown"):
            SystemManifest.model_validate(
                {
                    "manifest_id": "m",
                    "services": [_service("orders")],
                    "edges": [
                        {
                            "source": "orders",
                            "target": "ghost",
                            "kind": "api",
                            "provenance": "declared",
                        }
                    ],
                }
            )

    def test_duplicate_service_ids_are_refused(self):
        with pytest.raises(ValidationError, match="twice"):
            SystemManifest.model_validate(
                {
                    "manifest_id": "m",
                    "services": [_service("orders"), _service("orders")],
                }
            )

    def test_empty_services_and_repositories_are_refused(self):
        with pytest.raises(ValidationError):
            SystemManifest.model_validate({"manifest_id": "m", "services": []})
        with pytest.raises(ValidationError):
            _service("orders", repositories=[])

    def test_models_are_frozen(self):
        manifest = parse_manifest_yaml(MANIFEST_YAML)
        with pytest.raises(ValidationError):
            manifest.services[0].owner = "other-team"


class TestVocabularies:
    EDGE_BASE = {
        "source": "orders",
        "target": "billing",
        "kind": "api",
        "provenance": "declared",
    }

    def test_provenance_vocabulary_is_closed(self):
        # declared (YAML), observed (discovery), inferred (heuristic) are
        # DISTINGUISHED — anything else is not an edge provenance.
        for provenance in ("declared", "observed", "inferred"):
            edge = DependencyEdge.model_validate({**self.EDGE_BASE, "provenance": provenance})
            assert edge.provenance == provenance
        with pytest.raises(ValidationError):
            DependencyEdge.model_validate({**self.EDGE_BASE, "provenance": "guessed"})

    def test_kind_vocabulary_is_closed(self):
        for kind in ("api", "event", "schema", "database", "shared_lib"):
            DependencyEdge.model_validate({**self.EDGE_BASE, "kind": kind})
        with pytest.raises(ValidationError):
            DependencyEdge.model_validate({**self.EDGE_BASE, "kind": "vibes"})


class TestOwnerIsRoutingOnly:
    def test_plain_team_name_and_empty_owner_are_fine(self):
        assert _service(owner="team-commerce").owner == "team-commerce"
        assert _service().owner == ""

    def test_owner_containing_token_is_refused(self):
        with pytest.raises(ValidationError, match="routing metadata only"):
            _service(owner="github-token")

    def test_owner_containing_assignment_is_refused(self):
        with pytest.raises(ValidationError, match="credential"):
            _service(owner="Authorization=Bearer abc123")


class TestBackstageTranslation:
    def _component(self, spec: dict | None = None, metadata_extra: dict | None = None) -> dict:
        metadata = {"name": "orders"}
        if metadata_extra:
            metadata.update(metadata_extra)
        return {
            "apiVersion": "backstage.io/v1alpha1",
            "kind": "Component",
            "metadata": metadata,
            "spec": {"type": "service", **(spec or {})},
        }

    def test_component_translates(self):
        entry = translate_backstage(
            self._component(
                spec={
                    "repositories": ["core/orders-api"],
                    "owner": "team-commerce",
                    "apis": ["orders-api"],
                    "events": ["order.created"],
                }
            )
        )
        assert entry is not None
        assert entry.service_id == "orders"
        assert entry.repositories == ["core/orders-api"]
        assert entry.owner == "team-commerce"
        assert entry.apis == ["orders-api"]
        assert entry.events == ["order.created"]

    def test_resource_returns_none(self):
        resource = {
            "apiVersion": "backstage.io/v1alpha1",
            "kind": "Resource",
            "metadata": {"name": "orders-db"},
            "spec": {"type": "database"},
        }
        assert translate_backstage(resource) is None

    def test_project_slug_annotation_is_a_repository_fallback(self):
        entry = translate_backstage(
            self._component(
                metadata_extra={"annotations": {"github.com/project-slug": "acme/orders"}}
            )
        )
        assert entry is not None
        assert entry.repositories == ["acme/orders"]

    def test_component_without_repositories_is_refused(self):
        with pytest.raises(ValueError, match="no repositories"):
            translate_backstage(self._component())
