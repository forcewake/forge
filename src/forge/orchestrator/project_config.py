"""Typed, honest reads of a project's ``.forge.yml`` (review finding A13).

The legacy ``load_project_config`` conflated four different facts — "the
config does not exist", "the config is valid", "the read failed", and "the
config is malformed" — because it caught ANY exception and fell back to
``ProjectConfig()``. A transient 401/403/5xx or a malformed RESTRICTED
config therefore silently produced an UNRESTRICTED run: the empty
``implement_paths`` of the default profile looked exactly like "the
project scoped nothing", and the run's path scope WIDENED on a read
failure.

This module ends that with the same shape the R14 blob-read contract
(:mod:`forge.gitlab.blob_reads`) gave base-content reads:

- :class:`ConfigReadResult` — the outcome of ONE config read, with
  ``status ∈ {confirmed_absent, valid, unreadable, invalid}``. Only a
  provider-confirmed 404 (or a content-empty file) is
  ``confirmed_absent`` — the ONE status that may earn the documented
  default profile. Transport/permission failures are ``unreadable``; a
  payload that parses but is not a usable mapping is ``invalid``.
  ``valid`` carries the parsed :class:`ProjectConfig` PLUS the read
  provenance (ref + sha256 of the config bytes) that the executable
  RunSpec freezes (A13 §4).
- :func:`read_project_config` — the typed reader the run start paths
  MUST use. Never raises: every failure is a status.
- :func:`load_project_config` — the LEGACY tolerant wrapper for the
  reactive review lanes (orchestrator / flow runner). RUN START PATHS
  MUST NOT USE IT — a config failure may never degrade a run's scope.

Policy (A13): permissions may narrow, never widen. ``unreadable`` /
``invalid`` block the run (``config_unreadable`` / ``config_invalid``)
with zero paid calls; :mod:`forge.runs.revival` retries the read and the
run re-enters planning once the config is readable again. Post-freeze
legs never re-read the config at all — they scope from the spec's frozen
``allowed_paths``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from forge.gitlab.blob_reads import BlobReadResult, decode_blob_content

if TYPE_CHECKING:
    from forge.gitlab.client import GitLabClient
    from forge.integrations.azure import AzureRepositoryReader
    from forge.integrations.github import GitHubRepositoryReader


logger = logging.getLogger(__name__)

_CACHE_TTL = 300  # 5 minutes
# config -> (config or None for confirmed-absent, content sha256, read ref, ts)
# D01: the key is the CANONICAL AUTHORITY IDENTITY — (provider-connection,
# repository, ref, path) — never a bare project_id: one project id spans
# several repositories (Azure repos of one project share it) and
# independent providers can collide on the numeric id, so a project-id
# cache crossed repository policies (review 44cdae D01, probe P01/P02).
_cache: dict[tuple[str, str, str, str, str], tuple[ProjectConfig | None, str, str, float]] = {}


def _authority_identity(
    client: object,
    project_id: int,
    ref: str,
) -> tuple[str, str, str, str, str]:
    """The canonical cache identity of one authority read (D01 → FND-01).

    The PUBLIC :class:`~forge.repository.identity.RepositoryIdentity`
    contract owns the identity — adapters implement ``identity()``; this
    helper NEVER probes private attributes (the 44cdae helper recognized
    GitHub-style ``_owner``/``_repo`` but the REAL Azure reader carries
    ``_project``/``_repo``, so two repositories of one project fell into
    the project-id fallback and could share one policy entry — the first
    remaining defect of the 05868e9 review).
    """
    from forge.repository.identity import RepositoryIdentity, repository_identity

    identity = repository_identity(client)
    if identity is None and callable(getattr(client, "identity", None)):
        # GitLab: project-scoped at call time — identity(project_id)
        try:
            candidate = client.identity(project_id)  # type: ignore[call-arg,attr-defined]
            if isinstance(candidate, RepositoryIdentity):
                identity = candidate
        except TypeError:
            identity = None
    if identity is not None:
        return identity.cache_key(ref, CONFIG_FILE)
    # Legacy adapter without the contract: fully-qualified type+repr+project
    # key — never a bare project id (cross-adapter collisions), and two
    # projects never share the entry.
    return (type(client).__name__, repr(client), f"legacy:{int(project_id)}", ref, CONFIG_FILE)


#: The config path every provider reads (the config authority).
CONFIG_FILE = ".forge.yml"

#: Blocked-run reasons a failed config read parks a run with (A13): the
#: machine-readable prefixes every consumer blocks with until the config
#: read recovers.
CONFIG_UNREADABLE = "config_unreadable"
CONFIG_INVALID = "config_invalid"

#: The spec provenance statuses (the honest subset frozen into the
#: executable RunSpec — a failed read freezes NOTHING: the run is blocked).
ConfigProvenanceStatus = Literal["valid", "confirmed_absent"]

ConfigReadStatus = Literal["confirmed_absent", "valid", "unreadable", "invalid"]

#: The ONLY statuses a config read may produce (validated in ``__post_init__``).
_STATUSES = ("confirmed_absent", "valid", "unreadable", "invalid")


class ProjectConfig(BaseModel):
    """Per-project configuration loaded from .forge.yml in the repository."""

    enabled_agents: list[str] | None = Field(
        default=None,
        description="If set, only these agents are allowed. None means all.",
    )
    disabled_agents: list[str] = Field(
        default_factory=list,
        description="Agents to disable for this project.",
    )
    review_rules: list[str] = Field(
        default_factory=list,
        description="Project-specific review instructions appended to prompts.",
    )
    skip_paths: list[str] = Field(
        default_factory=list,
        description="Glob patterns for files to exclude from review.",
    )
    implement_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Glob patterns limiting the paths an /implement run may touch "
            "(the `implement.paths` key, v0.7 monorepo path scoping). Empty — "
            "the whole repo is in scope."
        ),
    )
    mcp_servers: dict[str, list[str]] | None = Field(
        default=None,
        description="Per-agent MCP server overrides. Keys are agent names, values are lists of server names.",
    )


@dataclass(frozen=True)
class ConfigReadResult:
    """The outcome of ONE authoritative read of ``.forge.yml`` (A13).

    Honesty rules, enforced by construction:

    - ``config``/``content_sha256`` are set ONLY when ``status == "valid"`` —
      ``content_sha256`` is the plain lowercase hex sha256 of the config
      file's utf-8 bytes (the provenance the executable RunSpec freezes).
    - ``detail`` is a human-readable fragment for blocked-run evidence —
      empty for ``confirmed_absent``/``valid`` unless the absence carried
      one.
    - :attr:`blocked_reason` is the parked-run reason for a failed read
      (``None`` when the run may proceed).
    """

    status: ConfigReadStatus
    ref: str = ""
    config: ProjectConfig | None = None
    content_sha256: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"unknown config read status: {self.status!r}")
        if self.status == "valid":
            if not isinstance(self.config, ProjectConfig):
                raise ValueError("a valid config read must carry a ProjectConfig")
            if len(self.content_sha256) != 64 or any(
                c not in "0123456789abcdef" for c in self.content_sha256
            ):
                raise ValueError("a valid config read must carry a sha256 digest")
        elif self.config is not None or self.content_sha256:
            raise ValueError(f"status {self.status!r} must not carry config content")

    # -- constructors ------------------------------------------------------

    @classmethod
    def confirmed_absent(cls, ref: str = "", detail: str = "") -> ConfigReadResult:
        """Provider-confirmed absence — the ONLY status that proves the
        project carries no config and may therefore run the documented
        default profile."""
        return cls(status="confirmed_absent", ref=ref, detail=detail)

    @classmethod
    def valid(
        cls, config: ProjectConfig, *, ref: str = "", content_sha256: str
    ) -> ConfigReadResult:
        """A successfully parsed config, with its content provenance."""
        return cls(
            status="valid",
            ref=ref,
            config=config,
            content_sha256=content_sha256,
        )

    @classmethod
    def unreadable(cls, ref: str = "", detail: str = "") -> ConfigReadResult:
        """The read failed (401/403/429/5xx/transport/undecodable) — whether
        a config exists, and what it restricts, is UNKNOWN. Never degrades
        to the default profile."""
        return cls(status="unreadable", ref=ref, detail=detail)

    @classmethod
    def invalid(cls, ref: str = "", detail: str = "") -> ConfigReadResult:
        """The config exists but is unusable (bad YAML, not a mapping,
        schema-violating fields) — its restrictions are UNKNOWN."""
        return cls(status="invalid", ref=ref, detail=detail)

    # -- views -------------------------------------------------------------

    @property
    def needs_block(self) -> bool:
        """Whether a run start must park on this read (A13 policy)."""
        return self.status in ("unreadable", "invalid")

    @property
    def blocked_reason(self) -> str | None:
        """The ``blocked(...)`` reason for a failed read; ``None`` otherwise."""
        if self.status == "unreadable":
            return f"{CONFIG_UNREADABLE}: {self.detail or 'config read failed'}"
        if self.status == "invalid":
            return f"{CONFIG_INVALID}: {self.detail or 'config is unusable'}"
        return None

    @property
    def provenance_status(self) -> ConfigProvenanceStatus | Literal[""]:
        """The provenance status to freeze into the executable spec (A13 §4).

        Empty ONLY for a failed read — which never reaches the freeze: the
        run is parked instead. The empty case exists so the field is
        total over the result type.
        """
        if self.status in ("valid", "confirmed_absent"):
            return self.status  # type: ignore[return-value]
        return ""


def _parse_project_config(content: str) -> ProjectConfig:
    """Parse ``.forge.yml`` text into a :class:`ProjectConfig`.

    Raises :class:`ValueError` (bad YAML, not a mapping) or
    :class:`pydantic.ValidationError` (schema-violating fields) — the
    caller turns both into ``invalid``, never into defaults.
    """
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError("expected a top-level mapping")
    # Support both top-level and nested under "forge" key. D02: a PRESENT
    # but non-mapping ``forge`` key (e.g. ``forge: []``) is INVALID — the
    # old code raised AttributeError straight through the reader's typed
    # error handling (probe P04).
    forge_raw = data.get("forge", data)
    if not isinstance(forge_raw, dict):
        raise ValueError(f"'forge' must be a mapping, got {type(forge_raw).__name__}")
    forge_data = forge_raw
    # Parse per-agent MCP server overrides
    mcp_raw = forge_data.get("mcp_servers")
    mcp_servers = None
    if isinstance(mcp_raw, dict):
        mcp_servers = {}
        for agent_name, agent_cfg in mcp_raw.items():
            if isinstance(agent_cfg, dict):
                mcp_servers[agent_name] = agent_cfg.get("mcp_servers", [])
            elif isinstance(agent_cfg, list):
                mcp_servers[agent_name] = agent_cfg

    # v0.7 monorepo path scoping: `implement.paths` — the glob
    # allowlist every /implement run of this project is frozen with.
    # D02: the nested structure is validated BEFORE normalization — a
    # malformed shape is INVALID, never silently dropped into the empty
    # list (empty list == whole repository; a string ``paths: "src/**"``
    # used to widen a typo into UNRESTRICTED scope, probe P03).
    implement = forge_data.get("implement")
    if implement is not None and not isinstance(implement, dict):
        raise ValueError(f"'implement' must be a mapping, got {type(implement).__name__}")
    implement_paths: list[str] = []
    if isinstance(implement, dict):
        if "paths" in implement:
            raw_paths = implement["paths"]
            if raw_paths is None:
                raise ValueError("'implement.paths' must be a list of globs, got null")
            if not isinstance(raw_paths, list):
                raise ValueError(
                    "'implement.paths' must be a list of globs, got "
                    f"{type(raw_paths).__name__} — a malformed path scope is invalid, "
                    "never unrestricted"
                )
            for entry in raw_paths:
                if not isinstance(entry, str) or not entry.strip():
                    raise ValueError(
                        f"'implement.paths' entries must be non-empty strings, got {entry!r}"
                    )
            implement_paths = [p.strip() for p in raw_paths]

    return ProjectConfig(
        enabled_agents=forge_data.get("enabled_agents"),
        disabled_agents=forge_data.get("disabled_agents", []),
        review_rules=forge_data.get("review_rules", []),
        skip_paths=forge_data.get("skip_paths", []),
        implement_paths=implement_paths,
        mcp_servers=mcp_servers,
    )


async def _read_config_blob(
    client: GitLabClient | GitHubRepositoryReader | AzureRepositoryReader,
    project_id: int,
    ref: str,
) -> BlobReadResult:
    """One authoritative read of :data:`CONFIG_FILE` as a
    :class:`~forge.gitlab.blob_reads.BlobReadResult` (reusing the R14
    primitives — the config is fetched over the same provider surface).

    Prefers the client's own typed ``read_blob``; a client that only
    exposes ``get_file`` is read through it. Both paths classify
    conservatively: any exception at all is ``unavailable`` — only a
    provider-confirmed 404 (via ``read_blob``) may vouch for absence.
    """
    if hasattr(type(client), "read_blob"):
        try:
            return await client.read_blob(project_id, CONFIG_FILE, ref)
        except Exception as exc:  # noqa: BLE001 — typed conservatism IS the contract
            return BlobReadResult.unavailable(
                f"{CONFIG_FILE!r} at {ref!r}: config read failed: {exc}"
            )
    try:
        repo_file = await client.get_file(project_id, CONFIG_FILE, ref)
    except Exception as exc:  # noqa: BLE001 — typed conservatism IS the contract
        return BlobReadResult.unavailable(f"{CONFIG_FILE!r} at {ref!r}: config read failed: {exc}")
    return decode_blob_content(repo_file.content, repo_file.encoding, path=CONFIG_FILE, ref=ref)


async def read_project_config(
    client: GitLabClient | GitHubRepositoryReader | AzureRepositoryReader,
    project_id: int,
    ref: str = "HEAD",
) -> ConfigReadResult:
    """Load ``.forge.yml`` from a project's repo as a TYPED result (A13).

    The client duck-types the shared ``read_blob`` surface, so GitLab,
    GitHub and Azure DevOps (AZ-2 wiring of the AzureRepositoryReader,
    ADR-0024) all read project config unchanged. Valid configs and
    provider-confirmed absences are cached per project with a 5-minute
    TTL; ``unreadable``/``invalid`` results are NEVER cached — the
    reconciler's retry pass must see a fresh read.

    Never raises: every failure mode is one of the four statuses, so a
    caller cannot accidentally swallow a read failure into defaults.
    """
    now = time.monotonic()
    cache_key = _authority_identity(client, project_id, ref)
    cached = _cache.get(cache_key)
    if cached is not None:
        config, sha256, cached_ref, ts = cached
        if now - ts < _CACHE_TTL:
            if config is None:
                return ConfigReadResult.confirmed_absent(ref=cached_ref)
            return ConfigReadResult.valid(config, ref=cached_ref, content_sha256=sha256)

    blob = await _read_config_blob(client, project_id, ref)
    if blob.confirmed_absent:
        _cache[cache_key] = (None, "", ref, now)
        return ConfigReadResult.confirmed_absent(ref=ref)
    if not blob.usable:
        # forbidden / unavailable / incomplete — the R14 taxonomy: absence
        # NOT proven, so the default profile is NOT earned.
        return ConfigReadResult.unreadable(ref=ref, detail=blob.detail or blob.status)
    assert isinstance(blob.content, str)  # decode_blob_content only yields text
    if not blob.content.strip():
        # A content-empty file asserts nothing — the honest default
        # profile (identical semantics to absence, no phantom restrictions).
        _cache[cache_key] = (None, "", ref, now)
        return ConfigReadResult.confirmed_absent(ref=ref, detail="config file is empty")
    try:
        config = _parse_project_config(blob.content)
    except (yaml.YAMLError, ValidationError, ValueError) as exc:
        # The file EXISTS and restricts something — its restrictions are
        # unknown, so the run must not silently go unscoped.
        logger.warning("Invalid %s in project %d: %s", CONFIG_FILE, project_id, exc)
        return ConfigReadResult.invalid(ref=ref, detail=str(exc)[:300])

    logger.debug("Loaded %s for project %d", CONFIG_FILE, project_id)
    _cache[cache_key] = (config, blob.content_sha256, ref, now)
    return ConfigReadResult.valid(config, ref=ref, content_sha256=blob.content_sha256)


async def load_project_config(
    client: GitLabClient | GitHubRepositoryReader | AzureRepositoryReader,
    project_id: int,
    ref: str = "HEAD",
) -> ProjectConfig:
    """LEGACY tolerant view of :func:`read_project_config` — defaults on
    every failure.

    The reactive review lanes (orchestrator, flow runner) still consume
    this: a failed review-lane read costs review coverage, not run scope,
    so the failure is logged loudly and degraded — never raised into the
    webhook path. RUN START PATHS MUST NOT USE THIS: a config-read
    failure must park a run (``blocked(config_…)``), never widen its
    scope — use :func:`read_project_config`.
    """
    result = await read_project_config(client, project_id, ref)
    if result.config is not None:
        return result.config
    if result.needs_block:
        logger.warning(
            "Project config read for project %d failed (%s) — degrading to defaults "
            "on the legacy review lane",
            project_id,
            result.blocked_reason,
        )
    return ProjectConfig()


def clear_cache() -> None:
    """Clear the project config cache (useful for testing)."""
    _cache.clear()
