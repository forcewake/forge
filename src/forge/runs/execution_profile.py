"""The versioned execution profile (review finding A18).

The lane must run the SAME build/test contract as target CI, deterministically
— and say honestly when it does not. This module is the single record of what
that contract IS, derived from the target repository itself:

- the toolchain pins come from the target's OWN ``uv.lock`` (the R15 lane
  extraction of ruff/pytest versions, generalized: the locked versions of
  every tool the lane carries, plus a sha256 over the lock bytes — the
  integrity pin), and the install strategy records whether the lane can
  honor them (``uv sync --frozen``) or must fall back to the minimal pip
  toolchain;
- the honesty axis is :attr:`ExecutionProfile.ci_contract`: what the lane
  ACTUALLY runs (:data:`LANE_TEST_COMMANDS` — the allowlisted quality-gate
  surface) versus what target CI runs (detected from the repo's CI config).
  ``matching`` | ``subset`` | ``unknown`` — the A16 manifest consumes this;
- the capability axes record the lane posture that the approval implicitly
  grants: the gated credential names (R15 capability/credential pairs), the
  MCP sourcing policy (ADR-0022) and the network posture.

The whole record is frozen data; :attr:`ExecutionProfile.profile_digest` is
the sha256 over its canonical JSON. The digest is embedded in the executable
RunSpec at freeze time (the additive ``execution_profile`` section — the gate
approves the exact execution contract) and echoed into the candidate meta v2
by the lane (``profile_digest`` — what the run ACTUALLY executed under). The
two are derived from the same record shape, so a drift between approved and
executed is a comparable pair of digests, never a silent difference.

Pure stdlib at module scope (tomllib/hashlib/re/dataclasses): the Actions
lane's ``--emit-meta`` step derives its profile from the bare checkout with
this module — no forge database, no forge credentials, nothing beyond the
interpreter. (Importing it still initializes the ``forge.runs`` package —
forge's own hard dependencies, which the lane interpreter already carries
because the lane pip-installs forge itself.)

Derivation is BEST-EFFORT and typed-honest, never fatal: a repo whose lock
cannot be read freezes the ``unknown``-honest record (and its digest), and a
run is never parked on a profile read failure — the profile sharpens the
approval contract, it does not gate it (the A13 config read keeps that job).
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from forge.runs.spec import canonical_json_digest

__all__ = [
    "BOOTSTRAP_STATUS_FAILED",
    "BOOTSTRAP_STATUS_OK",
    "BOOTSTRAP_STATUS_VALUES",
    "FORGE_BOOTSTRAP_FAILED_MARKER",
    "LANE_CREDENTIAL_CAPABILITIES",
    "LANE_MCP_POLICY",
    "LANE_NETWORK_POLICY",
    "LANE_PYTHON_VERSION",
    "LANE_TEST_COMMANDS",
    "PROFILE_LOCK_FILE",
    "PROFILE_PYPROJECT_FILE",
    "TOOLCHAIN_PACKAGE_NAMES",
    "ExecutionProfile",
    "FileRead",
    "LocalRepoSource",
    "MaterializedFiles",
    "ProfileSource",
    "bootstrap_failed",
    "classify_bootstrap_failure",
    "derive_from_reader",
    "derive_from_repo",
]

#: Schema version of the profile record covered by the digest. A changed
#: record shape bumps this, deliberately invalidating every previous digest.
EXECUTION_PROFILE_SCHEMA_VERSION = 1

#: The lane interpreter pin — the template's ``setup-python`` major.minor
#: (``ci/templates/forge-harness.github.yml``). One pin, both files.
LANE_PYTHON_VERSION = "3.13"

#: The target's dependency-lock file the lane syncs (``--frozen``).
PROFILE_LOCK_FILE = "uv.lock"

#: The target's project manifest — the ``requires-python`` constraint source.
PROFILE_PYPROJECT_FILE = "pyproject.toml"

#: Tool packages whose LOCKED versions the lane checks run under — the R15
#: ``uv.lock`` extraction (ruff/pytest), generalized. Sorted: canonical order
#: for the digest, never the lock's own ordering.
TOOLCHAIN_PACKAGE_NAMES = ("mypy", "pytest", "ruff")

#: The canonical build/test commands the lane ACTUALLY runs — the
#: quality-gate surface its tool allowlists grant (forge.harness_entry /
#: the templates' installation contract). Canonical names: every CI
#: detection below maps onto this vocabulary so ``ci_contract`` compares
#: like with like.
LANE_TEST_COMMANDS = ("make", "mypy", "pytest", "ruff")

#: The credential capability names the lane template gates per selected
#: driver (R15): the full set the template MAY render; every non-selected
#: driver's names resolve to '' in the lane env. Recorded in the profile so
#: the approval names exactly the capabilities the lane could see.
LANE_CREDENTIAL_CAPABILITIES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "COPILOT_GITHUB_TOKEN",
    "FORGE_GROK_AUTH",
    "ZAI_API_KEY",
)

#: The network posture, stated not implied: the runner has open egress,
#: provisioning fetches PINNED toolchains only, and the agent's tool
#: allowlist grants no network tools beyond the selected harness CLI.
LANE_NETWORK_POLICY = (
    "open runner egress; provisioning installs pinned toolchains only; "
    "the agent allowlist grants no network tools beyond the harness CLI"
)

#: The MCP sourcing policy (ADR-0022): canonical mcpServers JSON from the
#: FORGE_HARNESS_MCP repo VARIABLE — strict isolation with no servers when
#: unset. The profile records the POLICY; the variable is per-repo config
#: the freeze does not read.
LANE_MCP_POLICY = (
    "mcpServers sourced from the FORGE_HARNESS_MCP repo variable "
    "(ADR-0022); unset means strict isolation with no servers"
)

#: ``uv sync --frozen`` materializes EXACTLY the lock (deterministic, A18).
INSTALL_UV_FROZEN = "uv-sync-frozen"
#: Lock-less repos: the documented minimal pinned pip toolchain.
INSTALL_PIP_MINIMAL = "pip-minimal"
#: The lock read failed — whether the lane can sync is UNKNOWN.
INSTALL_UNKNOWN = "unknown"
_INSTALL_STRATEGIES = (INSTALL_UV_FROZEN, INSTALL_PIP_MINIMAL, INSTALL_UNKNOWN)

#: ``locked`` — the provider returned the lock bytes (digest + pins ride).
LOCK_LOCKED = "locked"
#: ``absent`` — provider-confirmed: no lock exists (pip-minimal lane).
LOCK_ABSENT = "absent"
#: ``unknown`` — the read failed; absence is NOT proven (R14 honesty).
LOCK_UNKNOWN = "unknown"
_LOCK_STATUSES = (LOCK_LOCKED, LOCK_ABSENT, LOCK_UNKNOWN)

#: The lane ran the same build/test contract as target CI.
CI_MATCHING = "matching"
#: Target CI runs checks the lane cannot (an honest subset).
CI_SUBSET = "subset"
#: Target CI's commands could not be detected — nothing is claimed.
CI_UNKNOWN = "unknown"
_CI_CONTRACTS = (CI_MATCHING, CI_SUBSET, CI_UNKNOWN)

# -- bootstrap classification (A18: determinism + honest failure class) ----

#: ``.forge/bootstrap`` statuses the lane template writes: ``ok`` when the
#: environment bootstrap completed (locked sync OR the documented minimal
#: fallback), ``failed`` when the locked sync could not materialize.
BOOTSTRAP_STATUS_OK = "ok"
BOOTSTRAP_STATUS_FAILED = "failed"
BOOTSTRAP_STATUS_VALUES = (BOOTSTRAP_STATUS_OK, BOOTSTRAP_STATUS_FAILED)

#: The job-log marker the lane template echoes when the environment
#: bootstrap fails. A FAILED bootstrap is lane infrastructure/config —
#: the environment, never the code — so it must classify infrastructure
#: (blocked), never a code-repair candidate. The marker makes that
#: classification mechanical: it lands in the job log AND the candidate
#: meta, and :data:`forge.runs.backends._HARNESS_INFRASTRUCTURE_PATTERNS`
#: carries it so a red lane classifies infra on the log path too.
FORGE_BOOTSTRAP_FAILED_MARKER = "FORGE_BOOTSTRAP_FAILED"


def classify_bootstrap_failure(log_text: str) -> bool:
    """Whether a lane log marks a failed environment bootstrap.

    True only on the explicit marker — a clean log is never retroactively
    declared a bootstrap failure.
    """
    return FORGE_BOOTSTRAP_FAILED_MARKER in (log_text or "")


def bootstrap_failed(meta: dict) -> bool:
    """Whether a candidate meta records a failed environment bootstrap.

    Reads the meta's additive ``bootstrap`` field (A18); anything absent or
    unrecognized is NOT a bootstrap failure — unknown stays unknown.
    """
    return str(meta.get("bootstrap") or "").strip() == BOOTSTRAP_STATUS_FAILED


# -- reads -----------------------------------------------------------------


@dataclass(frozen=True)
class FileRead:
    """The outcome of ONE profile-relevant file read, R14-honest.

    ``found`` carries the decoded text and its lowercase-hex sha256;
    ``absent`` is a provider-confirmed absence; ``unknown`` is every other
    outcome (read failure, undecodable) — absence NOT proven.
    """

    status: str  # "found" | "absent" | "unknown"
    content: str = ""
    sha256: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("found", "absent", "unknown"):
            raise ValueError(f"unknown file read status: {self.status!r}")
        if self.status == "found":
            if not self.sha256:
                raise ValueError("a found file read must carry its content sha256")
        elif self.content:
            raise ValueError(f"status {self.status!r} must not carry content")

    @classmethod
    def found(cls, content: str, *, sha256: str = "") -> FileRead:
        return cls(
            status="found",
            content=content,
            sha256=sha256 or hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def absent(cls) -> FileRead:
        return cls(status="absent")

    @classmethod
    def unknown(cls) -> FileRead:
        return cls(status="unknown")


class ProfileSource(Protocol):
    """What :func:`derive_from_repo` needs from one view of the target repo.

    Two implementations ship: :class:`LocalRepoSource` (the lane's bare
    checkout) and :class:`MaterializedFiles` (pre-fetched bytes — the
    freeze-time reader path and tests).
    """

    def read_file(self, path: str) -> FileRead:
        """One file read, typed-honest (see :class:`FileRead`)."""

    def workflow_texts(self) -> tuple[str, ...]:
        """The target's CI configuration texts, best-effort.

        Whatever CI config this view can see (workflow files, CI YAML);
        empty when none is visible. Commands are DETECTED from these —
        a view that cannot enumerate (provider blob reads) returns only
        what it can name.
        """


@dataclass(frozen=True)
class MaterializedFiles:
    """A pre-fetched repo view: path → :class:`FileRead`, plus CI texts.

    The freeze-time shape: the services prefetch the profile files over the
    provider's blob-read surface and derivation stays a pure function.
    """

    files: dict[str, FileRead]
    workflows: tuple[str, ...] = ()

    def read_file(self, path: str) -> FileRead:
        return self.files.get(path, FileRead.absent())

    def workflow_texts(self) -> tuple[str, ...]:
        return self.workflows


#: The CI config paths the blob-read view can NAME (it cannot enumerate a
#: directory). The lane's local view additionally globs the workflows dir.
READER_CI_PATHS = (".gitlab-ci.yml", ".github/workflows/ci.yml")

#: What the local view scans for CI commands, beyond :data:`READER_CI_PATHS`.
_LOCAL_CI_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml")
_LOCAL_CI_FILES = ("azure-pipelines.yml",)


@dataclass(frozen=True)
class LocalRepoSource:
    """The lane's view: a bare checkout on disk (the Actions workspace)."""

    root: Path

    def read_file(self, path: str) -> FileRead:
        target = self.root / path
        try:
            if not target.exists():
                return FileRead.absent()
            if not target.is_file():
                return FileRead.unknown()  # exists but is no regular file
            return FileRead.found(target.read_text(errors="replace"))
        except OSError:
            return FileRead.unknown()

    def workflow_texts(self) -> tuple[str, ...]:
        texts: list[str] = []
        for path in (*READER_CI_PATHS, *_LOCAL_CI_FILES):
            read = self.read_file(path)
            if read.status == "found":
                texts.append(read.content)
        for pattern in _LOCAL_CI_GLOBS:
            try:
                texts.extend(
                    candidate.read_text(errors="replace")
                    for candidate in sorted(self.root.glob(pattern))
                    if candidate.is_file()
                )
            except OSError:
                continue
        return tuple(texts)


# -- detection ---------------------------------------------------------------

#: CI command detection: canonical name → pattern over CI config text.
#: Best-effort by design — a token we cannot detect is ``unknown``
#: honesty for the whole contract, never a fabricated "matching". The
#: table spans BOTH directions of the honesty axis: the first four are
#: what the lane runs (:data:`LANE_TEST_COMMANDS`); the rest are common
#: CI toolchains the Python lane CANNOT run — their detection is what
#: makes a ``subset`` verdict (and the repair-budget honesty it buys)
#: reachable at all.
_CI_COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pytest", re.compile(r"\b(?:uv\s+run\s+|python\s+-m\s+)?pytest\b")),
    ("ruff", re.compile(r"\b(?:uv\s+run\s+|python\s+-m\s+)?ruff\b")),
    ("mypy", re.compile(r"\b(?:uv\s+run\s+|python\s+-m\s+)?mypy\b")),
    ("make", re.compile(r"\bmake\s+(?:lint|test|check|typecheck|ci)\b")),
    ("cargo", re.compile(r"\bcargo\s+(?:test|clippy)\b")),
    ("go", re.compile(r"\bgo\s+test\b")),
    ("npm", re.compile(r"\bnpm\s+(?:test|run\s+(?:lint|test|check))\b")),
    ("tox", re.compile(r"\btox\b")),
)

#: ``requires-python`` from a pyproject ``[project]`` table (tomllib).
_REQUIRES_PYTHON_KEY = "requires-python"


def _detect_ci_commands(texts: tuple[str, ...]) -> tuple[str, ...]:
    """Canonical CI commands detected in *texts* (sorted, deduplicated)."""
    detected: set[str] = set()
    for text in texts:
        for name, pattern in _CI_COMMAND_PATTERNS:
            if pattern.search(text or ""):
                detected.add(name)
    return tuple(sorted(detected))


def _extract_requires_python(pyproject_text: str) -> str:
    """The target's ``project.requires-python`` constraint, or '' unknown.

    A malformed manifest degrades to unknown — the profile records the
    constraint when readable, never guesses one.
    """
    try:
        data = tomllib.loads(pyproject_text or "")
    except tomllib.TOMLDecodeError:
        return ""
    project = data.get("project")
    if not isinstance(project, dict):
        return ""
    raw = project.get(_REQUIRES_PYTHON_KEY)
    return str(raw).strip() if isinstance(raw, str) and raw.strip() else ""


def _extract_toolchain_pins(lock_text: str) -> dict[str, str]:
    """The locked versions of :data:`TOOLCHAIN_PACKAGE_NAMES` from a lock.

    The R15 lane extraction (grep ruff/pytest versions out of ``uv.lock``)
    generalized: the lock is parsed as the TOML it is, and the pinned
    version of every tool the lane carries is recorded. A lock that does
    not carry a tool records nothing for it (unknown stays unknown — the
    digest still pins the lock bytes themselves).
    """
    try:
        data = tomllib.loads(lock_text or "")
    except tomllib.TOMLDecodeError:
        return {}
    pins: dict[str, str] = {}
    packages = data.get("package")
    if not isinstance(packages, list):
        return pins
    for entry in packages:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip().lower()
        if name in TOOLCHAIN_PACKAGE_NAMES and name not in pins:
            version = str(entry.get("version") or "").strip()
            if version:
                pins[name] = version
    return pins


# -- the record ---------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionProfile:
    """The versioned execution contract of one target repo on the lane (A18).

    Frozen data only; everything is derived from the repo (or the honest
    ``unknown`` of a failed read) plus the lane's own posture constants, so
    the same repo state always produces the same record — and the same
    :attr:`profile_digest`.
    """

    schema_version: int
    # The lane interpreter pin + the target's own declared constraint.
    python_version: str
    requires_python: str
    # Dependency installation: whether the lane syncs the target's lock
    # (``uv sync --frozen`` — deterministic) or falls back to the minimal
    # pinned pip toolchain, and the lock read honesty behind that.
    install_strategy: str
    lock_status: str
    lock_sha256: str
    toolchain_pins: tuple[tuple[str, str], ...]  # sorted (name, version)
    # Honesty axis: what the lane ACTUALLY runs vs what target CI runs.
    lane_commands: tuple[str, ...]
    ci_commands: tuple[str, ...]
    ci_contract: str
    # The capability axes the approval implicitly grants (lane posture).
    network_policy: str
    mcp_policy: str
    credential_capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_PROFILE_SCHEMA_VERSION:
            raise ValueError(f"unknown execution profile schema version {self.schema_version!r}")
        if self.install_strategy not in _INSTALL_STRATEGIES:
            raise ValueError(f"unknown install strategy: {self.install_strategy!r}")
        if self.lock_status not in _LOCK_STATUSES:
            raise ValueError(f"unknown lock status: {self.lock_status!r}")
        if self.lock_sha256 and (
            len(self.lock_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.lock_sha256)
        ):
            raise ValueError("lock_sha256 must be a sha256 digest")
        if (self.lock_status == LOCK_LOCKED) != bool(self.lock_sha256):
            raise ValueError("a locked profile must carry the lock digest, and only that")
        if self.ci_contract not in _CI_CONTRACTS:
            raise ValueError(f"unknown ci_contract: {self.ci_contract!r}")

    # -- views ------------------------------------------------------------

    def to_document(self) -> dict:
        """The canonical digest target (sorted-key JSON over this dict)."""
        return {
            "schema_version": self.schema_version,
            "python_version": self.python_version,
            "requires_python": self.requires_python,
            "install": {
                "strategy": self.install_strategy,
                "lock_status": self.lock_status,
                **({"lock_sha256": self.lock_sha256} if self.lock_sha256 else {}),
            },
            "toolchain_pins": {name: version for name, version in self.toolchain_pins},
            "test_commands": {
                "lane": list(self.lane_commands),
                "ci": list(self.ci_commands),
                "contract": self.ci_contract,
            },
            "network_policy": self.network_policy,
            "mcp_policy": self.mcp_policy,
            "credential_capabilities": list(self.credential_capabilities),
        }

    @property
    def profile_digest(self) -> str:
        """sha256 over the canonical JSON of the whole record (A18)."""
        return canonical_json_digest(self.to_document())


def _lock_read(lock: FileRead) -> tuple[str, str, dict[str, str]]:
    """(lock_status, lock_sha256, pins) from one lock read.

    ``found`` → ``locked``: the bytes are pinned by the digest and the
    tool pins are extracted best-effort — a lock that yields no readable
    tool entries (or one forge cannot parse) still ran the lock, so the
    status stays honest-locked while the pin set records what it saw.
    """
    if lock.status == "found":
        return LOCK_LOCKED, lock.sha256, _extract_toolchain_pins(lock.content)
    if lock.status == "absent":
        return LOCK_ABSENT, "", {}
    return LOCK_UNKNOWN, "", {}


def derive_from_repo(source: ProfileSource) -> ExecutionProfile:
    """Derive the execution profile from one view of the target repo.

    Pure and deterministic over the repo state: the same bytes always
    produce the same record and the same digest. Every read is typed-honest
    (:class:`FileRead`) — a failed read degrades that one axis to
    ``unknown``, never fabricates a value and never raises.
    """
    pyproject = source.read_file(PROFILE_PYPROJECT_FILE)
    requires_python = (
        _extract_requires_python(pyproject.content) if pyproject.status == "found" else ""
    )
    lock_status, lock_sha256, pins = _lock_read(source.read_file(PROFILE_LOCK_FILE))
    install_strategy = {
        LOCK_LOCKED: INSTALL_UV_FROZEN,
        LOCK_ABSENT: INSTALL_PIP_MINIMAL,
        LOCK_UNKNOWN: INSTALL_UNKNOWN,
    }[lock_status]
    ci_commands = _detect_ci_commands(source.workflow_texts())
    if not ci_commands:
        ci_contract = CI_UNKNOWN
    elif set(ci_commands) <= set(LANE_TEST_COMMANDS):
        ci_contract = CI_MATCHING
    else:
        ci_contract = CI_SUBSET
    return ExecutionProfile(
        schema_version=EXECUTION_PROFILE_SCHEMA_VERSION,
        python_version=LANE_PYTHON_VERSION,
        requires_python=requires_python,
        install_strategy=install_strategy,
        lock_status=lock_status,
        lock_sha256=lock_sha256,
        toolchain_pins=tuple(sorted(pins.items())),
        lane_commands=LANE_TEST_COMMANDS,
        ci_commands=ci_commands,
        ci_contract=ci_contract,
        network_policy=LANE_NETWORK_POLICY,
        mcp_policy=LANE_MCP_POLICY,
        credential_capabilities=LANE_CREDENTIAL_CAPABILITIES,
    )


# -- the freeze-time reader path ----------------------------------------------


async def _read_via_reader(reader: Any, project_id: int, path: str, ref: str) -> FileRead:
    """One typed file read over the provider blob-read surface.

    The reader duck-types ``read_blob(project_id, path, ref) ->``
    :class:`~forge.gitlab.blob_reads.BlobReadResult` (GitLab, GitHub and
    Azure DevOps readers all do). Any exception is ``unknown`` — the
    profile derivation NEVER raises into the freeze (it sharpens the
    approval contract; it does not gate it).
    """
    blob_read = getattr(reader, "read_blob", None)
    if blob_read is None:
        return FileRead.unknown()
    try:
        result = await blob_read(project_id, path, ref)
    except Exception:  # noqa: BLE001 — typed conservatism IS the contract
        return FileRead.unknown()
    status = str(getattr(result, "status", "") or "")
    if status == "found":
        content = getattr(result, "content", None)
        if not isinstance(content, str):
            return FileRead.unknown()
        return FileRead.found(content, sha256=str(getattr(result, "content_sha256", "") or ""))
    if getattr(result, "confirmed_absent", False):
        return FileRead.absent()
    return FileRead.unknown()


async def derive_from_reader(reader: Any, *, project_id: int, ref: str) -> ExecutionProfile:
    """The freeze-time profile over a provider repository reader.

    Reads :data:`PROFILE_LOCK_FILE` + :data:`PROFILE_PYPROJECT_FILE` (and
    the nameable :data:`READER_CI_PATHS` CI configs) over the reader's
    ``read_blob`` surface and derives the record. Never raises: every
    failure mode is an ``unknown`` axis inside an otherwise honest profile.
    """
    files = {
        PROFILE_PYPROJECT_FILE: await _read_via_reader(
            reader, project_id, PROFILE_PYPROJECT_FILE, ref
        ),
        PROFILE_LOCK_FILE: await _read_via_reader(reader, project_id, PROFILE_LOCK_FILE, ref),
    }
    workflows: list[str] = []
    for path in READER_CI_PATHS:
        read = await _read_via_reader(reader, project_id, path, ref)
        if read.status == "found":
            workflows.append(read.content)
    return derive_from_repo(MaterializedFiles(files=files, workflows=tuple(workflows)))
