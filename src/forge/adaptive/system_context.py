"""NEXT-21 — the read-many/write-one system context profile.

The review's premise: "the first multi-repo step is understanding
neighbors, not writing to them." The smallest valuable enterprise
capability is to DISCOVER across several authorized repositories while
MUTATING exactly one — this module is the declarative profile that says
which is which, and the enforcement that keeps the boundary honest:

- :class:`SystemContextProfile` — a frozen list of NEIGHBOR repositories
  (read-only) plus ONE writable target (the run's own repository). Each
  neighbor carries ``(provider, repository_id, ref, allowed_globs)``;
  the writable target is a :class:`WritableTarget` of the same shape.
  Nothing here discovers anything: the profile is EXPLICIT
  AUTHORIZATION, derived from the project's ``forge.yml`` ``neighbors:``
  section by :func:`from_project_config` and refused loudly when the
  section is malformed — an ignored authorization field is a silently
  broader scope, so there are none.
- :meth:`SystemContextProfile.authorize_write` — the write check: the
  writable target is the ONLY allowed write; a write-targeting tool call
  naming a NEIGHBOR is refused as ``read_only_neighbor``, and anything
  else as ``outside_context``. Reads never route through it — read
  scope and write scope are independent axes (NEXT-21).
- :meth:`SystemContextProfile.build_discovery_context` — the connection
  to the multi-repo discovery (R28-08 / NXT-08): the profile feeds
  :meth:`~forge.adaptive.discovery_stage.DiscoveryRunContext.from_readers`
  with the own repo FIRST and every neighbor after it, each with its own
  reader, ref and allowed_globs. The reader set must cover EXACTLY the
  authorized set: a missing reader refuses, and a reader for an
  UNAUTHORIZED repository refuses — no implicit discovery, so an
  unauthorized neighbor is absent from the context by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from forge.adaptive.artifact_store import ContentAddressedStore
    from forge.adaptive.discovery_stage import DiscoveryRunContext
    from forge.adaptive.research_planner import ResearchHarness
    from forge.config import ForgeConfig
    from forge.orchestrator.project_config import ProjectConfig

__all__ = [
    "NEIGHBOR_PROVIDER_VALUES",
    "OWN_REPO_KEY",
    "WriteDecision",
    "WritableTarget",
    "NeighborRepository",
    "SystemContextProfile",
    "from_project_config",
]

#: The closed provider vocabulary a neighbor may name — the three
#: provider gateways forge ships. Anything else is a config error, never
#: a silently guessed provider.
NEIGHBOR_PROVIDER_VALUES: tuple[str, ...] = ("gitlab", "github", "azure")

#: The discovery namespace the run's own repository occupies (the
#: research menu's documented key — a neighbor may not shadow it).
OWN_REPO_KEY = "own"

#: The fields one ``neighbors:`` entry may carry. Anything else refuses.
_NEIGHBOR_FIELDS = frozenset({"provider", "repository_id", "ref", "allowed_globs"})


@dataclass(frozen=True)
class WritableTarget:
    """The SINGLE writable repository of a run: the run's own repo.

    ``provider`` + ``repository_id`` identify it; ``ref`` is the ref the
    own repo's discovery snapshot freezes (the writable ref itself is
    the publisher's business — this record feeds discovery, not
    publication). ``allowed_globs`` are the own repo's READ-path
    constraints (R32-10: the v0.7 ``implement.paths`` monorepo scope) —
    they bound what discovery may read of the own repository exactly the
    way a neighbor's globs bound it, and carry no write authority at
    all (the write scope is the publisher's spec, never this field).
    """

    provider: str
    repository_id: str
    ref: str = "HEAD"
    allowed_globs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.provider not in NEIGHBOR_PROVIDER_VALUES:
            raise ValueError(
                f"provider {self.provider!r} is outside the vocabulary {NEIGHBOR_PROVIDER_VALUES}"
            )
        if not str(self.repository_id or "").strip():
            raise ValueError("repository_id must be a non-empty repository identity")
        if not str(self.ref or "").strip():
            raise ValueError("ref must be non-empty")
        for pattern in self.allowed_globs:
            if not str(pattern or "").strip():
                raise ValueError("allowed_globs entries must be non-empty patterns")

    @property
    def key(self) -> str:
        """The unambiguous composite identity: ``provider:repository_id``."""
        return f"{self.provider}:{self.repository_id}"


@dataclass(frozen=True)
class NeighborRepository:
    """One authorized READ-ONLY neighbor repository.

    ``ref`` should name an immutable snapshot (a commit sha) — the
    profile carries whatever the operator pinned and never resolves
    anything itself; ``allowed_globs`` bound what discovery may read
    (empty = the whole repository, the operator's explicit choice).
    """

    provider: str
    repository_id: str
    ref: str = "HEAD"
    allowed_globs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.provider not in NEIGHBOR_PROVIDER_VALUES:
            raise ValueError(
                f"neighbor provider {self.provider!r} is outside the vocabulary"
                f" {NEIGHBOR_PROVIDER_VALUES}"
            )
        if not str(self.repository_id or "").strip():
            raise ValueError("neighbor repository_id must be a non-empty repository identity")
        if not str(self.ref or "").strip():
            raise ValueError("neighbor ref must be non-empty")
        for pattern in self.allowed_globs:
            if not str(pattern).strip():
                raise ValueError("allowed_globs entries must be non-empty patterns")

    @property
    def key(self) -> str:
        """The unambiguous composite identity: ``provider:repository_id``.

        Equal repository ids across providers stay distinct (the
        "core/api" of github is not the "core/api" of gitlab).
        """
        return f"{self.provider}:{self.repository_id}"


@dataclass(frozen=True)
class WriteDecision:
    """The verdict of one :meth:`SystemContextProfile.authorize_write`.

    ``code`` is ``writable_target`` (allowed), ``read_only_neighbor``
    (refused: a neighbor is read-only by construction) or
    ``outside_context`` (refused: not part of the authorized context at
    all). Refusal is a typed answer, never an exception — a tool
    boundary renders it, it does not crash on it.
    """

    allowed: bool
    code: str
    reason: str


def _decision(code: str, reason: str) -> WriteDecision:
    return WriteDecision(allowed=code == "writable_target", code=code, reason=reason)


@dataclass(frozen=True)
class SystemContextProfile:
    """Read-many/write-one: N read-only neighbors + ONE writable target.

    Frozen and total: the neighbors are exactly the authorized set (no
    discovery, no defaults), and the writable target is the run's own
    repository. A neighbor may not BE the writable target (listing it as
    one is an authorization confusion — the own repo is in the context
    already, as the writable one), and neighbors may not repeat.
    """

    writable: WritableTarget
    neighbors: tuple[NeighborRepository, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.writable, WritableTarget):
            raise ValueError("the writable target must be a WritableTarget")
        seen: set[str] = {self.writable.key}
        for neighbor in self.neighbors:
            if not isinstance(neighbor, NeighborRepository):
                raise ValueError("every neighbor must be a NeighborRepository")
            if neighbor.key == self.writable.key:
                raise ValueError(
                    f"neighbor {neighbor.key} IS the writable target — the own"
                    " repository is in the context as the writable one, never"
                    " also as a neighbor"
                )
            if neighbor.key in seen:
                raise ValueError(f"neighbor {neighbor.key} is declared twice")
            seen.add(neighbor.key)

    # -- the write boundary -------------------------------------------------

    def authorize_write(self, provider: str, repository_id: str) -> WriteDecision:
        """Whether a write may target ``(provider, repository_id)``.

        The read-many/write-one rule, enforceable at any tool boundary:
        the writable target is the ONLY allowed write. A NEIGHBOR is
        refused as ``read_only_neighbor`` (the loudest refusal — the
        repository IS in the context, for reading), anything else as
        ``outside_context``. Reads never consult this method.
        """
        target = f"{provider}:{repository_id}"
        if target == self.writable.key:
            return _decision(
                "writable_target",
                f"{target} is the run's own repository — the single writable target",
            )
        if any(neighbor.key == target for neighbor in self.neighbors):
            return _decision(
                "read_only_neighbor",
                f"{target} is a read-only neighbor — writes target the run's own"
                f" repository {self.writable.key} only",
            )
        return _decision(
            "outside_context",
            f"{target} is not part of the authorized system context"
            f" (writable: {self.writable.key}; readable neighbors:"
            f" {[neighbor.key for neighbor in self.neighbors]})",
        )

    def reader_key_of(self, neighbor: NeighborRepository) -> str:
        """The key :meth:`build_discovery_context` expects for a neighbor."""
        return neighbor.key

    # -- the discovery connection (NEXT-21 ↔ NXT-08) ------------------------

    def build_discovery_context(
        self,
        *,
        run_id: str,
        project_id: int,
        session_factory: Any,
        neighbor_readers: Mapping[str, Any],
        own_reader: Any,
        store: ContentAddressedStore | None = None,
        research: ResearchHarness | None = None,
    ) -> DiscoveryRunContext:
        """Feed the profile into the multi-repo discovery context.

        ``neighbor_readers`` maps each neighbor's composite key
        (``provider:repository_id`` — :meth:`reader_key_of`) to its
        reader; ``own_reader`` reads the run's own repository. The
        reader set must cover EXACTLY the authorized neighbors: a
        missing reader refuses (an authorized neighbor nothing can read
        is a half-built context), and an EXTRA reader refuses — a
        repository the profile never authorized contributes nothing, by
        construction, so an unauthorized neighbor is absent from the
        built context. The own repo is the FIRST entry (the primary of
        ``from_readers``); every neighbor rides its own COMPOSITE
        namespace (``provider:repository_id``), so equal repository ids
        across providers stay unambiguous, each with its own ref and
        allowed_globs.
        """
        from forge.adaptive.discovery_stage import DiscoveryRunContext

        expected = {neighbor.key for neighbor in self.neighbors}
        supplied = set(neighbor_readers)
        missing = sorted(expected - supplied)
        if missing:
            raise ValueError(
                f"no reader for authorized neighbor(s) {missing} — every"
                " authorized neighbor needs its reader, or remove it from"
                " the profile"
            )
        extra = sorted(supplied - expected)
        if extra:
            raise ValueError(
                f"reader(s) for UNAUTHORIZED repository(s) {extra} — the"
                " system context authorizes exactly its declared neighbors,"
                " no implicit discovery"
            )

        readers: dict[str, Any] = {OWN_REPO_KEY: own_reader}
        repository_ids: dict[str, str] = {OWN_REPO_KEY: self.writable.repository_id}
        refs: dict[str, str] = {OWN_REPO_KEY: self.writable.ref}
        allowed_globs: dict[str, list[str]] = {}
        if self.writable.allowed_globs:
            # R32-10: the own repository's read-path constraints (the
            # monorepo path scope) survive composition — the multi-reader
            # map keeps them on the own entry exactly as the single-repo
            # ``from_reader`` path carried them.
            allowed_globs[OWN_REPO_KEY] = list(self.writable.allowed_globs)
        for neighbor in self.neighbors:
            readers[neighbor.key] = neighbor_readers[neighbor.key]
            repository_ids[neighbor.key] = neighbor.repository_id
            refs[neighbor.key] = neighbor.ref
            if neighbor.allowed_globs:
                allowed_globs[neighbor.key] = list(neighbor.allowed_globs)
        return DiscoveryRunContext.from_readers(
            readers,
            repository_ids,
            run_id=run_id,
            project_id=project_id,
            session_factory=session_factory,
            refs=refs,
            allowed_globs=allowed_globs or None,
            store=store,
            research=research,
        )


def from_project_config(
    config: ForgeConfig | ProjectConfig, own_repo: WritableTarget
) -> SystemContextProfile:
    """Derive the profile from the project's ``forge.yml`` (NEXT-21).

    The ``neighbors:`` section is EXPLICIT AUTHORIZATION — every entry
    names ``provider`` and ``repository_id`` (``ref`` defaults ``HEAD``,
    ``allowed_globs`` defaults to the whole repository), and the parse
    refuses loudly on anything ambiguous: a non-list section, a
    non-mapping entry, an unknown field, an unknown provider, an empty
    repository_id, or a non-list globs value. A missing section is the
    single-repo context (no neighbors — valid, not an error). No
    catalog, manifest or discovery output contributes authorization:
    imported metadata may hint, but only this section authorizes.

    *config* is the PROJECT configuration — either the server-side
    :class:`~forge.config.ForgeConfig` view of a ``forge.yml`` (its
    ``get("neighbors")``) or the typed
    :class:`~forge.orchestrator.project_config.ProjectConfig` the run
    start paths read from the repo's ``.forge.yml`` (its ``neighbors``
    field, carried raw for exactly this parse).
    """
    if hasattr(config, "get"):
        raw = config.get("neighbors")
    else:
        raw = getattr(config, "neighbors", None)
    if raw is None:
        return SystemContextProfile(writable=own_repo)
    if not isinstance(raw, list):
        raise ValueError("neighbors must be a list of neighbor repository entries")

    neighbors: list[NeighborRepository] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValueError("each neighbors entry must be a mapping")
        unknown = sorted(set(entry) - _NEIGHBOR_FIELDS)
        if unknown:
            raise ValueError(
                f"neighbors entry carries unknown field(s) {unknown} — an"
                " authorization record with ignored fields is a silently"
                " broader scope; allowed fields are"
                f" {sorted(_NEIGHBOR_FIELDS)}"
            )
        provider = str(entry.get("provider") or "").strip()
        if provider not in NEIGHBOR_PROVIDER_VALUES:
            raise ValueError(
                f"neighbor provider {provider!r} is outside the vocabulary"
                f" {NEIGHBOR_PROVIDER_VALUES}"
            )
        repository_id = str(entry.get("repository_id") or "").strip()
        if not repository_id:
            raise ValueError("a neighbors entry requires a non-empty repository_id")
        ref = str(entry.get("ref") or "HEAD").strip()
        globs_raw = entry.get("allowed_globs", [])
        if globs_raw is None:
            globs_raw = []
        if not isinstance(globs_raw, list):
            raise ValueError("allowed_globs must be a list of glob patterns")
        globs = tuple(str(pattern).strip() for pattern in globs_raw)
        neighbors.append(
            NeighborRepository(
                provider=provider,
                repository_id=repository_id,
                ref=ref,
                allowed_globs=globs,
            )
        )
    return SystemContextProfile(writable=own_repo, neighbors=tuple(neighbors))
