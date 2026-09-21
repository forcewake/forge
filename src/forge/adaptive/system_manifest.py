"""The system manifest: registered services and read-only dependency edges.

DSC-07 (review 05868e9 backlog). A customer with ten services needs system
awareness BEFORE any cross-repository write: which services exist, which
repositories each owns, and what flows between them. The manifest is small
administrative metadata that DESCRIBES the world — it never authorizes
anything. Three separations keep it honest:

- ``owner`` is routing metadata only (who to page, where to navigate). It
  may be empty, and it must never look like a credential — the validator
  refuses ``=``/``token`` shapes so a pasted secret fails loudly at the
  catalog boundary instead of riding into logs and contexts. Read
  authority comes from actual connection/project policy, never from the
  catalog.
- Dependency edges carry DISTINGUISHED provenance — ``declared`` (from
  the administrative YAML), ``observed`` (seen in discovery), ``inferred``
  (heuristic) — because a reader must know how much to trust an edge
  before following it.
- Cycles between services are legal TOPOLOGY. The service graph and an
  execution DAG are different graphs: mutually dependent services describe
  the world, they do not schedule it, so a cycle here is not an error.
"""

from __future__ import annotations

import typing
import warnings
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Every model redeclares ``schema`` with its own Literal tag — the
# discriminator pattern. Pydantic warns about the shadow; it is the design.
warnings.filterwarnings(
    "ignore", message='Field name "schema"', category=UserWarning, module=__name__
)


class _Strict(BaseModel):
    """Common rig: strict, frozen, no stray fields, a Literal schema tag."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    @field_validator("schema", check_fields=False)
    @classmethod
    def _schema_tag(cls, value: str) -> str:
        expected = typing.get_args(cls.model_fields["schema"].annotation)  # type: ignore[index]
        if expected and value not in expected:
            raise ValueError(f"schema must be one of {expected}, got {value!r}")
        return value


class ServiceEntry(_Strict):
    """One registered service: its repositories and what it exposes.

    ``repositories`` is the READ surface discovery may follow; ``owner``
    routes humans, it grants nothing (see the module docstring).
    """

    schema: Literal["forge.system.service/1"] = "forge.system.service/1"  # type: ignore[assignment]
    service_id: str = Field(min_length=1)
    repositories: list[str] = Field(min_length=1)
    apis: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    owner: str = ""

    @field_validator("owner")
    @classmethod
    def _owner_is_routing_only(cls, value: str) -> str:
        # A credential-shaped owner means someone pasted a secret into the
        # catalog. Refuse at the boundary: the field's whole job is to be
        # safe to copy into any context.
        if "=" in value or "token" in value.lower():
            raise ValueError(
                "owner is routing metadata only and must never look like a credential "
                f"(got {value!r}); authorize reads through connection policy, not the catalog"
            )
        return value


class DependencyEdge(_Strict):
    """A read-only dependency between two registered services.

    ``provenance`` says where the edge came from — declared in the admin
    YAML, observed during discovery, or inferred by a heuristic — so a
    consumer can weight trust accordingly.
    """

    schema: Literal["forge.system.edge/1"] = "forge.system.edge/1"  # type: ignore[assignment]
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    kind: Literal["api", "event", "schema", "database", "shared_lib"]
    provenance: Literal["declared", "observed", "inferred"]


class SystemManifest(_Strict):
    """The versioned service registry plus its dependency edges.

    Edges MAY form cycles: this is the service TOPOLOGY, not a work DAG,
    and mutual dependencies are legal (see the module docstring).
    """

    schema: Literal["forge.system.manifest/1"] = "forge.system.manifest/1"  # type: ignore[assignment]
    manifest_id: str = Field(min_length=1)
    services: list[ServiceEntry] = Field(min_length=1)
    edges: list[DependencyEdge] = Field(default_factory=list)

    @field_validator("services")
    @classmethod
    def _unique_service_ids(cls, value: list[ServiceEntry]) -> list[ServiceEntry]:
        ids = [service.service_id for service in value]
        if len(ids) != len(set(ids)):
            raise ValueError("a service_id appears twice in one manifest")
        return value

    @field_validator("edges")
    @classmethod
    def _endpoints_are_known(cls, value: list[DependencyEdge], info) -> list[DependencyEdge]:
        known = {service.service_id for service in (info.data.get("services") or [])}
        for edge in value:
            for endpoint in (edge.source, edge.target):
                if endpoint not in known:
                    raise ValueError(
                        f"edge {edge.source!r}->{edge.target!r} references unknown "
                        f"service {endpoint!r}"
                    )
        return value


def parse_manifest_yaml(text: str) -> SystemManifest:
    """Parse the administrative YAML form into a validated manifest.

    The YAML is deliberately boring (an operator writes it by hand)::

        manifest_id: local-stack
        services:
          - id: orders            # 'id' is the shorthand for service_id
            repositories: [core/orders-api]
            apis: [Orders API]
            events: [order.created]
            owner: team-commerce
        edges:
          - source: orders
            target: billing
            kind: api
            provenance: declared

    Unknown keys are refused at every level (``extra="forbid"``). A YAML
    syntax error or a validation failure raises ``ValueError`` with the
    underlying context — an import must fail loudly, never half-import.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"system manifest YAML does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("system manifest YAML must be a mapping with 'services'")

    payload: dict[str, Any] = dict(data)
    raw_services = payload.get("services")
    if raw_services is not None:
        if not isinstance(raw_services, list):
            raise ValueError("'services' must be a list")
        translated: list[dict[str, Any]] = []
        for entry in raw_services:
            if not isinstance(entry, dict):
                raise ValueError("each service entry must be a mapping")
            translated.append(
                {("service_id" if key == "id" else key): val for key, val in entry.items()}
            )
        payload["services"] = translated

    try:
        return SystemManifest.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"invalid system manifest: {exc}") from exc


def translate_backstage(entity: dict) -> ServiceEntry | None:
    """Translate a Backstage Software catalog entity into a ServiceEntry.

    Only ``kind: Component`` maps to a service; every other kind
    (Resource, API, Group, ...) returns ``None`` — the caller skips it
    rather than guessing. ``service_id`` comes from ``metadata.name``;
    repositories come from ``spec.repositories``, falling back to the
    conventional ``github.com/project-slug`` / ``gitlab.com/project-slug``
    annotations when the spec does not list them. A Component with no
    resolvable repositories is refused loudly: a service with nothing to
    read is a registration mistake, not a default.

    ``owner`` is taken from ``spec.owner`` when present. Backstage
    ``owner`` is metadata for NAVIGATION, never runtime authorization —
    importing a catalog entry grants no approval rights; reads are
    authorized by the connection/project policy alone.
    """
    if entity.get("kind") != "Component":
        return None
    metadata = entity.get("metadata") or {}
    spec = entity.get("spec") or {}
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ValueError("Backstage entity 'metadata' and 'spec' must be mappings")

    service_id = str(metadata.get("name") or "")
    repositories = [str(repo) for repo in (spec.get("repositories") or [])]
    if not repositories:
        annotations = metadata.get("annotations") or {}
        if isinstance(annotations, dict):
            for slug_key in ("github.com/project-slug", "gitlab.com/project-slug"):
                slug = annotations.get(slug_key)
                if slug:
                    repositories.append(str(slug))
    if not repositories:
        raise ValueError(
            f"Component {service_id!r} has no repositories (spec.repositories or a "
            "project-slug annotation); refusing to register a service with nothing to read"
        )

    def _names(key: str) -> list[str]:
        # Backstage commonly references catalog entities by ref
        # (":import:namespace/name" or plain names); keep them verbatim —
        # the manifest stores identifiers, not resolved objects.
        return [str(item) for item in (spec.get(key) or [])]

    return ServiceEntry.model_validate(
        {
            "service_id": service_id,
            "repositories": repositories,
            "apis": _names("apis"),
            "events": _names("events"),
            "resources": _names("resources"),
            "owner": str(spec.get("owner") or ""),
        }
    )
