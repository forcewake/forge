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

NXT-29 — :data:`LANE_PROFILE_V2` is the hardened lane execution profile:
staged credentials (the provider key mounted only for the lane/coding
step and stripped from every other stage), data boundaries (workspace +
tmpfs writable, a read-only rootfs elsewhere), ``cap_drop ALL`` with no
added capabilities, and an env-carried egress allowlist hook
(:data:`FORGE_EGRESS_ALLOWLIST_ENV`). HONESTY BOUND, stated plainly: this
module ships that surface as DATA plus validators — the template declares
it, the validators refuse malformed or half-declared selections, and the
CONTAINER controls (mounts, capability drops, egress filtering) act at
the runner boundary. What R28-24 adds on top is the measurable slice a
process CAN verify about itself: :func:`validate_runtime` checks the
ACTUAL runtime a v2-declared lane starts in — the egress allowlist hook
present in the stage env, the root filesystem read-only per
``/proc/mounts``, and no credential name present that the declared stage
must not see — and the lane entry
(:mod:`forge.harness_entry`) calls it at startup, FAILING CLOSED with an
actionable error when v2 is declared but the runtime does not match:
never a silent downgrade to the unstaged posture. ``cap_drop`` is
deliberately NOT checked: capabilities are not observable from an
unprivileged process, so an unverifiable claim is not made. A lane that
declares v2 without runner-side mounts/egress enforcement therefore never
starts — the declared contract and the deployed control cannot drift
apart silently in either direction. NEXT-18 sharpens the network axis one
step further: :func:`verify_network_egress` goes BEYOND the declaration
check and dials a destination the declared allowlist denies — a
successful connection proves enforcement ABSENT (``not_enforced``); a
refused one is only CONSISTENT with enforcement (``verified``, with the
epistemic bound stated), because from inside a process we can prove the
absence of a control, never its presence.

NEXT-15 — the driver identity is DECOMPOSED into two axes. A lane is a
(RECIPE, HARNESS) tuple: :class:`RuntimeRecipe` is the language/toolchain
the lane runs ON (the SDK image, the version/restore/build/test
commands, the repo files whose absence fails the gate — ``dotnet-9`` /
``python-3-13`` / ``node-22``), :class:`HarnessProfile` is the agent
driver that runs IN it (``claude-code`` / ``codex`` / ``copilot`` — its
sdk and its credential names). ``dotnet-lane`` stops being a fused
driver identity and becomes a COMPATIBILITY ALIAS
(:data:`DRIVER_PROFILE_ALIASES`) for ``("dotnet-9", "claude-code")``:
the existing driver id still resolves, but the decomposition is the
authority — :func:`compile_driver_profile` validates the pair against
:data:`COMPILED_COMBINATIONS` (an unsupported combination is refused
before any model call), and :func:`RuntimeRecipe.gate` checks the
TOOLCHAIN prerequisites against the target repo independently of which
harness was selected (the .NET recipe's global.json pin gates a codex
lane exactly as it gates the claude lane — one runtime recipe, no
per-harness copies).
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from forge.runs.spec import canonical_json_digest

__all__ = [
    "BOOTSTRAP_STATUS_FAILED",
    "BOOTSTRAP_STATUS_OK",
    "BOOTSTRAP_STATUS_VALUES",
    "COMPILED_COMBINATIONS",
    "DENIED_PROBE_HOST",
    "DENIED_PROBE_PORT",
    "DENIED_PROBE_TIMEOUT_S",
    "DRIVER_PROFILE_ALIASES",
    "FORGE_BOOTSTRAP_FAILED_MARKER",
    "FORGE_EGRESS_ALLOWLIST_ENV",
    "FORGE_LANE_PROFILE_ENV",
    "FORGE_LANE_STAGE_ENV",
    "HARNESS_PROFILES",
    "LANE_CREDENTIAL_CAPABILITIES",
    "LANE_MCP_POLICY",
    "LANE_NETWORK_POLICY",
    "LANE_PROFILE_VALUES",
    "LANE_PROFILE_V1",
    "LANE_PROFILE_V2",
    "LANE_PYTHON_VERSION",
    "LANE_STAGES",
    "LANE_TEST_COMMANDS",
    "NETWORK_EGRESS_DECLARED_ONLY",
    "NETWORK_EGRESS_NOT_ENFORCED",
    "NETWORK_EGRESS_STATUS_VALUES",
    "NETWORK_EGRESS_VERIFIED",
    "NetworkProbeResult",
    "PROC_MOUNTS_PATH",
    "PROFILE_LOCK_FILE",
    "PROFILE_PYPROJECT_FILE",
    "RUNTIME_RECIPES",
    "TOOLCHAIN_PACKAGE_NAMES",
    "ExecutionProfile",
    "FileRead",
    "HarnessProfile",
    "LaneDriverProfile",
    "LaneExecutionProfile",
    "LocalRepoSource",
    "MaterializedFiles",
    "ProfileSource",
    "RecipeViolation",
    "RuntimeRecipe",
    "RuntimeViolation",
    "bootstrap_failed",
    "classify_bootstrap_failure",
    "compile_driver_profile",
    "credentials_at",
    "derive_from_reader",
    "derive_from_repo",
    "harness_profile",
    "lane_profile",
    "resolve_driver_profile",
    "runtime_recipe",
    "validate_lane_profile_declaration",
    "validate_runtime",
    "verify_network_egress",
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


# -- the hardened lane execution profile (NXT-29) ---------------------------

#: The env var a lane template/dispatch declares the execution profile
#: through. Pipeline variables beat YAML ones, so a template default of
#: ``v1`` stays an OPT-IN: the dispatch leg (or a project variable) is
#: what selects ``v2``.
FORGE_LANE_PROFILE_ENV = "FORGE_LANE_PROFILE"

#: The env-carried egress allowlist hook (v2): comma-separated host
#: patterns the LANE/coding step may reach (``api.z.ai,...``). Empty or
#: unset means DENY-ALL for the agent step — bootstrap still needs its
#: pinned package registries, which is the bootstrap stage's own
#: allowance, never the agent's. Documented contract: the runner reads
#: this variable and enforces the filter at its network boundary
#: (deployment work — this module carries the contract, not the
#: enforcement).
FORGE_EGRESS_ALLOWLIST_ENV = "FORGE_EGRESS_ALLOWLIST"

#: The profile id vocabulary — anything else fails closed.
_PROFILE_V1_ID = "v1"
_PROFILE_V2_ID = "v2"
_PROFILE_IDS = (_PROFILE_V1_ID, _PROFILE_V2_ID)

#: The execution stages a hardened lane job runs, in order (NXT-29's
#: phase separation): bootstrap installs pinned toolchains; discovery
#: is read-only; coding is the writable agent step; verification is the
#: trusted test executor. Credential staging is per stage.
LANE_STAGES = ("bootstrap", "discovery", "coding", "verification")

#: v1's single undifferentiated "stage": the whole job shares one env
#: (the unstaged posture, stated as data).
_JOB_STAGE = "job"
_CREDENTIAL_STAGES = (*LANE_STAGES, _JOB_STAGE)

#: Provider credential names the v2 CODING stage may mount — the union
#: over the shipped SDK lanes' provider surfaces (the same names
#: ``forge.runs.harness_selection`` gates per driver). At run time only
#: the SELECTED lane's subset is mounted: the dispatch names exactly one
#: driver, and no stage ever receives another provider's key. Publisher
#: and write tokens appear NOWHERE in the vocabulary by construction.
_V2_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CODEX_API_KEY",
    "FORGE_CODEX_AUTH",
    "OPENAI_API_KEY",
    "OPENCODE_PROVIDER_API_KEY",
    "ZAI_API_KEY",
)

#: The read-only repository token bootstrap needs for ``git fetch`` — a
#: scoped read credential, not a provider key and never a write token.
_V2_BOOTSTRAP_KEYS = ("FORGE_BOT_READ_TOKEN",)


@dataclass(frozen=True)
class LaneExecutionProfile:
    """One lane execution profile's enforcement surface, as DATA (NXT-29).

    ``credential_staging`` maps stage → the env-var names that stage may
    see (empty tuple = no credentials at that stage); the stages come
    from :data:`LANE_STAGES`. ``writable_roots`` / ``read_only_roots`` /
    ``tmpfs_roots`` describe the container mount posture (read-only
    rootfs first, workspace over-mounted writable, tmpfs for scratch).
    ``cap_drop_all`` + ``cap_add`` are the Linux capability posture.
    ``egress_policy`` is the documented sentence and
    ``egress_allowlist_env`` the env var the runner reads.

    This record is a DECLARED CONTRACT, not a deployed control: the
    runner must enforce mounts/caps/egress for any of it to be true
    (see the module docstring's honesty bound).
    """

    profile_id: str
    credential_staging: tuple[tuple[str, tuple[str, ...]], ...]
    writable_roots: tuple[str, ...]
    read_only_roots: tuple[str, ...]
    tmpfs_roots: tuple[str, ...]
    cap_drop_all: bool
    cap_add: tuple[str, ...]
    egress_policy: str
    egress_allowlist_env: str

    def __post_init__(self) -> None:
        if self.profile_id not in _PROFILE_IDS:
            raise ValueError(
                f"unknown lane profile {self.profile_id!r}; vocabulary is {_PROFILE_IDS}"
            )
        stages = tuple(stage for stage, _names in self.credential_staging)
        unknown = set(stages) - set(_CREDENTIAL_STAGES)
        if unknown:
            raise ValueError(f"credential stages outside the vocabulary: {sorted(unknown)}")
        if len(stages) != len(set(stages)):
            raise ValueError("one credential-staging row per stage, no duplicates")

    def to_document(self) -> dict:
        """The audit shape (NOT digested — distinct from
        :attr:`ExecutionProfile.profile_digest`, which pins the
        build/test contract, not the isolation posture)."""
        return {
            "profile_id": self.profile_id,
            "credential_staging": {stage: list(names) for stage, names in self.credential_staging},
            "data_boundaries": {
                "writable": list(self.writable_roots),
                "read_only": list(self.read_only_roots),
                "tmpfs": list(self.tmpfs_roots),
            },
            "capabilities": {
                "drop_all": self.cap_drop_all,
                "add": list(self.cap_add),
            },
            "egress": {
                "policy": self.egress_policy,
                "allowlist_env": self.egress_allowlist_env,
            },
        }


#: v1 — the CURRENT documented posture, stated as data so the contrast
#: with v2 is auditable: credentials ride the job env unstaged (one
#: undifferentiated ``job`` "stage"), the runner filesystem is as the
#: executor shipped it, no capabilities are dropped, egress is open
#: (:data:`LANE_NETWORK_POLICY`).
LANE_PROFILE_V1 = LaneExecutionProfile(
    profile_id=_PROFILE_V1_ID,
    credential_staging=((_JOB_STAGE, LANE_CREDENTIAL_CAPABILITIES),),
    writable_roots=("/",),
    read_only_roots=(),
    tmpfs_roots=(),
    cap_drop_all=False,
    cap_add=(),
    egress_policy=LANE_NETWORK_POLICY,
    egress_allowlist_env=FORGE_EGRESS_ALLOWLIST_ENV,
)

#: v2 — the hardened profile (NXT-29): staged credentials, workspace +
#: tmpfs data boundaries, cap_drop ALL, deny-by-default egress with the
#: env-carried allowlist hook. DECLARED SURFACE ONLY — runner-side
#: enforcement is deployment work; selecting v2 without it records
#: intent, it does not isolate.
LANE_PROFILE_V2 = LaneExecutionProfile(
    profile_id=_PROFILE_V2_ID,
    credential_staging=(
        # Bootstrap fetches the pinned toolchains and the attempt base:
        # the scoped READ token, never a provider key.
        ("bootstrap", _V2_BOOTSTRAP_KEYS),
        # Discovery is read-only navigation — NO model credentials.
        ("discovery", ()),
        # The agent step: the SELECTED provider's key only, mounted for
        # this step and stripped from every later one.
        ("coding", _V2_PROVIDER_KEYS),
        # The trusted test executor runs tests, not models — NO
        # provider credentials reach verification.
        ("verification", ()),
    ),
    writable_roots=("/workspace",),
    read_only_roots=("/",),
    tmpfs_roots=("/tmp",),
    cap_drop_all=True,
    cap_add=(),
    egress_policy=(
        "deny by default: the lane/coding step may reach only the hosts in "
        f"{FORGE_EGRESS_ALLOWLIST_ENV} (comma-separated; empty = deny-all for "
        "the agent step) plus the bootstrap stage's pinned package "
        "registries; runner-side enforcement required — this profile is the "
        "declared contract, not a deployed control"
    ),
    egress_allowlist_env=FORGE_EGRESS_ALLOWLIST_ENV,
)

#: The profile id vocabulary, derived from the shipped records.
LANE_PROFILE_VALUES = (LANE_PROFILE_V1.profile_id, LANE_PROFILE_V2.profile_id)

_LANE_PROFILES: dict[str, LaneExecutionProfile] = {
    LANE_PROFILE_V1.profile_id: LANE_PROFILE_V1,
    LANE_PROFILE_V2.profile_id: LANE_PROFILE_V2,
}


def lane_profile(profile_id: str) -> LaneExecutionProfile:
    """The execution profile for *profile_id*; unknown ids raise (fail closed).

    Denial is observable — there is no silent fallback to any profile,
    and never a fallback to an UNRESTRICTED one.
    """
    try:
        return _LANE_PROFILES[str(profile_id or "").strip()]
    except KeyError:
        raise ValueError(
            f"unknown lane profile {profile_id!r}; vocabulary is {LANE_PROFILE_VALUES}"
        ) from None


def credentials_at(profile: LaneExecutionProfile, stage: str) -> tuple[str, ...]:
    """The credential names *stage* may see under *profile* (fail closed).

    v2 answers per its staging rows. v1 is the unstaged posture: every
    phase sees the ambient set. A v2 stage the profile does not name
    sees NOTHING — an unnamed stage is an unconfigured one, never a
    permissive one.
    """
    for named_stage, names in profile.credential_staging:
        if named_stage == stage:
            return names
    if profile.profile_id == LANE_PROFILE_V1.profile_id:
        return LANE_CREDENTIAL_CAPABILITIES
    return ()


def validate_lane_profile_declaration(variables: Mapping[str, Any]) -> tuple[bool, str]:
    """Validate a template/dispatch ``FORGE_LANE_PROFILE`` declaration.

    *variables* is the job's declared variables mapping (the template's
    YAML ``variables:`` block, or the dispatch env). Rules, fail closed
    and observable:

    - the profile key (:data:`FORGE_LANE_PROFILE_ENV`) must be present —
      an undeclared profile is refused, never defaulted;
    - its value must be in :data:`LANE_PROFILE_VALUES`;
    - declaring ``v2`` requires the egress allowlist hook
      (:data:`FORGE_EGRESS_ALLOWLIST_ENV`) to be declared in the same
      mapping — a v2 selection without its documented hook is a
      half-declaration and is refused (the hook's VALUE may be empty:
      empty is deny-all, which is a valid hardened state).
    """
    if FORGE_LANE_PROFILE_ENV not in variables:
        return False, f"{FORGE_LANE_PROFILE_ENV} is not declared"
    declared = str(variables[FORGE_LANE_PROFILE_ENV] or "").strip()
    if declared not in LANE_PROFILE_VALUES:
        return False, (
            f"{FORGE_LANE_PROFILE_ENV}={declared!r} is outside the vocabulary {LANE_PROFILE_VALUES}"
        )
    if declared == LANE_PROFILE_V2.profile_id and FORGE_EGRESS_ALLOWLIST_ENV not in variables:
        return False, (
            f"{LANE_PROFILE_V2.profile_id} requires the {FORGE_EGRESS_ALLOWLIST_ENV} hook "
            "to be declared in the same variables block (empty value = "
            "deny-all, which is valid; absent is a half-declaration)"
        )
    return True, "ok"


# -- runner-side runtime enforcement (R28-24) ---------------------------------

#: The lane stage identity the runner stamps into the stage env (v2's
#: phase separation). Absent means the coding stage — the writable agent
#: step the lane entry runs.
FORGE_LANE_STAGE_ENV = "FORGE_LANE_STAGE"

#: Where the read-only-rootfs posture is read from: the container's own
#: mount table. Linux-only by design — a runtime that cannot show its
#: mounts cannot prove the v2 boundary.
PROC_MOUNTS_PATH = "/proc/mounts"


@dataclass(frozen=True)
class RuntimeViolation:
    """One measurable non-compliance :func:`validate_runtime` found.

    ``check`` names the axis (``egress_allowlist_declared`` /
    ``root_filesystem_read_only`` / ``credential_staging`` /
    ``stage_identity``); ``detail`` is the actionable sentence — what was
    observed and which side (declared profile vs actual runtime) is
    wrong. The lane entry renders these into its fail-closed refusal.
    """

    check: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover — trivial rendering
        return f"[{self.check}] {self.detail}"


def _root_mount_options(proc_mounts: str) -> str | None:
    """The mount option set of the ``/`` entry in a ``/proc/mounts`` text.

    The LAST matching entry wins (later mounts over-mount earlier ones —
    the overlay that actually backs the rootfs). ``None`` when no entry
    mounts ``/`` at all.
    """
    options: str | None = None
    for line in (proc_mounts or "").splitlines():
        fields = line.split()
        # device mountpoint fstype options...
        if len(fields) >= 4 and fields[1] == "/":
            options = fields[3]
    return options


def validate_runtime(
    profile: LaneExecutionProfile,
    *,
    stage: str = "coding",
    env: Mapping[str, str] | None = None,
    proc_mounts: str | None = None,
) -> tuple[RuntimeViolation, ...]:
    """Check the ACTUAL runtime against *profile*'s declared surface (R28-24).

    The enforcement slice a process can honestly measure about itself,
    evaluated where the lane starts:

    - **v1 declares no isolation** — the unstaged posture IS the declared
      posture, so a v1 runtime is trivially compliant: no checks, no
      violations. (This is not a downgrade path: v2 is the profile that
      makes claims.)
    - **v2 must be able to show its controls.** Three axes, fail-closed:

      1. *egress_allowlist_declared* — :data:`FORGE_EGRESS_ALLOWLIST_ENV`
         present in the stage env (empty = deny-all, which is valid; a
         v2 without its documented hook is a half-deployment).
      2. *root_filesystem_read_only* — the root mount is ``ro`` per
         ``/proc/mounts`` (parameterizable for tests). An unreadable or
         root-``rw`` runtime does not match the declared read-only
         rootfs: unverifiable IS non-compliant here, because the
         alternative is trusting the profile name.
      3. *credential_staging* — no credential name the declared STAGE
         must not see is present (non-empty) in the env: the union of
         every OTHER stage's names minus this stage's allowance. A
         bootstrap read token surviving into the coding step, or a
         provider key reaching verification, is a staging leak. Absence
         of an ALLOWED name is NOT a violation — a missing key is the
         driver's own startup error, not an isolation breach (fail-closed
         on isolation, never on availability).

    ``cap_drop`` is deliberately not checked: Linux capabilities are not
    observable from inside an unprivileged process, and an unverifiable
    check would be decoration, not enforcement. Returns the violations;
    empty means the measurable surface matches the declaration.
    """
    if profile.profile_id == LANE_PROFILE_V1.profile_id:
        return ()
    if stage not in _CREDENTIAL_STAGES:
        return (
            RuntimeViolation(
                check="stage_identity",
                detail=(
                    f"lane stage {stage!r} is outside the vocabulary "
                    f"{_CREDENTIAL_STAGES} — the runtime cannot be scoped to a "
                    "declared stage"
                ),
            ),
        )
    source = os.environ if env is None else env
    violations: list[RuntimeViolation] = []

    # 1. The egress hook must ride the stage env the runner staged.
    if profile.egress_allowlist_env not in source:
        violations.append(
            RuntimeViolation(
                check="egress_allowlist_declared",
                detail=(
                    f"{profile.egress_allowlist_env} is not present in the "
                    f"{stage} stage env — v2 declares deny-by-default egress "
                    "enforced through this hook (empty value = deny-all is "
                    "valid; absent is a half-deployment)"
                ),
            )
        )

    # 2. The root filesystem must actually be read-only.
    mounts_text: str | None = proc_mounts
    if mounts_text is None:
        try:
            mounts_text = Path(PROC_MOUNTS_PATH).read_text(errors="replace")
        except OSError as exc:
            mounts_text = None
            violations.append(
                RuntimeViolation(
                    check="root_filesystem_read_only",
                    detail=(
                        f"{PROC_MOUNTS_PATH} unreadable ({exc}) — the read-only "
                        "rootfs the profile declares cannot be verified, and an "
                        "unverifiable boundary is treated as absent"
                    ),
                )
            )
    if mounts_text is not None:
        options = _root_mount_options(mounts_text)
        if options is None:
            violations.append(
                RuntimeViolation(
                    check="root_filesystem_read_only",
                    detail="no / mount in /proc/mounts — the rootfs posture cannot be verified",
                )
            )
        elif "ro" not in options.split(","):
            violations.append(
                RuntimeViolation(
                    check="root_filesystem_read_only",
                    detail=(
                        f"/ is mounted rw (options {options!r}) — the profile "
                        "declares a read-only rootfs with only "
                        f"{profile.writable_roots} writable"
                    ),
                )
            )

    # 3. Credential staging: nothing this stage must not see may be present.
    allowed_here = set(credentials_at(profile, stage))
    staged_elsewhere = {
        name for other, names in profile.credential_staging if other != stage for name in names
    }
    leaked = sorted(
        name for name in staged_elsewhere - allowed_here if str(source.get(name) or "").strip()
    )
    if leaked:
        violations.append(
            RuntimeViolation(
                check="credential_staging",
                detail=(
                    f"credential(s) {leaked} are present in the {stage} stage env "
                    f"but belong to other stages — the profile stages them away "
                    "from this step"
                ),
            )
        )
    return tuple(violations)


# -- the network-egress probe (NEXT-18) ---------------------------------------
#
# The review's demand: "having an env-var allowlist doesn't prove network
# restriction." ``validate_runtime`` checks the DECLARATION (the hook rides
# the stage env); the probe below adds the measurable slice beyond it: an
# actual connection attempt to a destination the declared policy DENIES.
# The epistemics are asymmetric and the probe says so: a SUCCESSFUL
# connection to a denied destination proves enforcement ABSENT
# (``not_enforced`` — declared but not enforced); a refused/timed-out
# connection is CONSISTENT WITH enforcement but cannot prove it (a down
# network, a dead DNS resolver or a firewall hiccup look identical from
# inside) — that answer is ``verified`` with its honesty bound in the
# detail, never a claim that the filter's completeness was tested. We can
# prove absence of enforcement, not presence.

#: The probe destination: a real, harmless public host the declared
#: allowlist never grants (model/package endpoints live elsewhere). The
#: probe connects (or fails to) and immediately closes — no bytes of
#: payload are sent either way.
DENIED_PROBE_HOST = "example.com"
DENIED_PROBE_PORT = 443
#: Short by design: the probe must never stall a lane entry.
DENIED_PROBE_TIMEOUT_S = 2.0

#: The probe's three honest answers.
NETWORK_EGRESS_VERIFIED = "verified"
NETWORK_EGRESS_DECLARED_ONLY = "declared_only"
NETWORK_EGRESS_NOT_ENFORCED = "not_enforced"
NETWORK_EGRESS_STATUS_VALUES = (
    NETWORK_EGRESS_VERIFIED,
    NETWORK_EGRESS_DECLARED_ONLY,
    NETWORK_EGRESS_NOT_ENFORCED,
)


@dataclass(frozen=True)
class NetworkProbeResult:
    """What one :func:`verify_network_egress` probe established (NEXT-18).

    ``status`` is the honest verdict: ``verified`` (the denied
    destination was unreachable — consistent with enforcement, an
    absence-of-violation answer, never proof of the filter itself),
    ``declared_only`` (the control exists as declaration only: the hook
    never reached the env, or the probe destination is allowlisted so
    the probe cannot falsify anything), or ``not_enforced`` (the denied
    destination ANSWERED — the allowlist is declared but nothing
    enforces it). ``connection_outcome`` carries the raw observation
    (``unreachable`` | ``connected`` | ``not_probed``) beside it.
    """

    status: str
    detail: str
    probed: str = ""
    connection_outcome: str = ""

    def __post_init__(self) -> None:
        if self.status not in NETWORK_EGRESS_STATUS_VALUES:
            raise ValueError(
                f"unknown network probe status {self.status!r}; vocabulary is"
                f" {NETWORK_EGRESS_STATUS_VALUES}"
            )

    def __str__(self) -> str:  # pragma: no cover — trivial rendering
        return f"[{self.status}] {self.detail}"


def _socket_connector(host: str, port: int, timeout_s: float) -> None:
    """The default probe leg: one TCP connect, closed immediately.

    Returns when the connection was ESTABLISHED (the caller reads that
    as enforcement absent); raises :class:`OSError` (refused, timeout,
    DNS failure, unreachable network) when it was not — every failure
    spelling is the same honest "could not get out" observation.
    """
    import socket

    with socket.create_connection((host, port), timeout=timeout_s):
        pass  # connected — nothing sent, immediately closed


def verify_network_egress(
    allowlist: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    denied_host: str = DENIED_PROBE_HOST,
    denied_port: int = DENIED_PROBE_PORT,
    timeout_s: float = DENIED_PROBE_TIMEOUT_S,
    connector: Any = None,
) -> NetworkProbeResult:
    """Probe whether the declared egress allowlist is actually enforced.

    *allowlist* is the declared policy (the patterns the dispatch/lane
    profile declared — typically parsed from
    :data:`FORGE_EGRESS_ALLOWLIST_ENV`). The probe's two legs, in order:

    1. **Declaration** — the env hook must be present in the stage env
       (*env*, default ``os.environ``): absent means the control never
       reached the runtime, so nothing downstream can be tested →
       ``declared_only``.
    2. **Falsification** — the probe destination must be DENIED by the
       declared policy (an allowlisted destination is permitted, so
       reaching it proves nothing → ``declared_only``, probe skipped).
       A denied destination is then actually dialed (short timeout):
       connected → ``not_enforced`` (the declared policy is not being
       enforced — this is the one thing the probe can PROVE); refused
       or timed out → ``verified`` with the honesty bound said plainly
       in the detail: a denied probe proves the control may exist, it
       cannot prove that it does.

    *connector* overrides the dial (``(host, port, timeout_s) -> None``,
    raising OSError when unreachable) so tests prove the three outcomes
    without a network.
    """
    from fnmatch import fnmatchcase

    source = os.environ if env is None else env
    raw = source.get(FORGE_EGRESS_ALLOWLIST_ENV)
    if raw is None:
        return NetworkProbeResult(
            status=NETWORK_EGRESS_DECLARED_ONLY,
            detail=(
                f"{FORGE_EGRESS_ALLOWLIST_ENV} is not present in the stage env —"
                " the allowlist is declared but the enforcement hook never"
                " reached the runtime; there is nothing to probe"
            ),
            probed=f"{denied_host}:{denied_port}",
            connection_outcome="not_probed",
        )

    patterns = [str(pattern).strip() for pattern in allowlist if str(pattern).strip()]
    destination = f"{denied_host}:{denied_port}"
    if any(fnmatchcase(denied_host, pattern) for pattern in patterns):
        return NetworkProbeResult(
            status=NETWORK_EGRESS_DECLARED_ONLY,
            detail=(
                f"the probe destination {destination} matches the declared"
                f" allowlist ({patterns}) — a permitted destination cannot"
                " falsify enforcement; the control remains declared_only"
            ),
            probed=destination,
            connection_outcome="not_probed",
        )

    dial = connector if connector is not None else _socket_connector
    try:
        dial(denied_host, denied_port, timeout_s)
    except OSError as exc:
        return NetworkProbeResult(
            status=NETWORK_EGRESS_VERIFIED,
            detail=(
                f"connection to the denied destination {destination} was"
                f" refused ({exc.__class__.__name__}) — consistent with"
                " deny-by-default enforcement. HONESTY BOUND: a denied probe"
                " can prove enforcement ABSENT (see not_enforced), never"
                " prove it present; this answer says the control may exist"
            ),
            probed=destination,
            connection_outcome="unreachable",
        )
    return NetworkProbeResult(
        status=NETWORK_EGRESS_NOT_ENFORCED,
        detail=(
            f"a connection to the denied destination {destination} SUCCEEDED"
            f" — the allowlist is declared ({patterns or 'deny-all'}) but"
            " nothing enforces it: declared, not enforced"
        ),
        probed=destination,
        connection_outcome="connected",
    )


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
class ObservedExecution:
    """What the lane ACTUALLY did (B12) — the honest twin of the DECLARED
    :class:`ExecutionProfile`.

    The declared profile describes ALLOWANCE and EXPECTATION (allowed
    commands, pinned toolchain, CI contract); this record carries the
    observed facts of ONE execution: the driver invoked, its exit status,
    whether usage numbers arrived, and whether any candidate changed. A
    command being ALLOWED is not evidence it RAN — the two vocabularies
    never merge ("allowed but unexecuted pytest is not executed"; "CI
    mentioning make is not the lane running make").
    """

    schema_version: int = 1
    driver: str = ""
    exit_status: str = "unknown"
    usage_completeness: str = "unknown"
    candidate_changed: bool | None = None
    # C10: the trusted wrapper's command receipts — (argv_head, exit, report).
    commands: tuple[tuple[str, int, str], ...] = ()
    # D08: the receipts' PROVENANCE — who wrote them. ``wrapper_observed``
    # rows come from a trusted channel; ``self_reported`` rows lived in the
    # agent-writable workspace (telemetry, never gate evidence). Absent =
    # unknown, treated self_reported.
    receipts_producer: str = ""
    observed_at: str = ""


def observed_execution(
    *,
    driver: str = "",
    exit_status: str = "unknown",
    usage_completeness: str = "unknown",
    candidate_changed: bool | None = None,
    commands: Sequence[tuple[str, int, str]] = (),
    receipts_producer: str = "",
    now: Any = None,
) -> ObservedExecution:
    """Build the observed record (ISO timestamp default: wall clock).

    *commands* are the trusted wrapper's receipts: ``(argv_head, exit_code,
    report_file)`` — e.g. ``("pytest -q", 0, "")`` proves the test command
    ran green, which the declared profile can never claim (C10).
    """
    from datetime import datetime, timezone

    stamp = now if now is not None else datetime.now(timezone.utc)
    return ObservedExecution(
        driver=str(driver or ""),
        exit_status=str(exit_status or "unknown"),
        usage_completeness=str(usage_completeness or "unknown"),
        candidate_changed=candidate_changed,
        commands=tuple((str(c[0]), int(c[1]), str(c[2])) for c in commands),
        receipts_producer=str(receipts_producer or ""),
        observed_at=stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp),
    )


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


# -- the recipe/harness decomposition (NEXT-15) --------------------------------
#
# The review's finding: "dotnet-lane conflates the .NET runtime recipe
# with the claude-code agent driver." Everything the .NET lane pins that
# is NOT about the agent — the SDK image digest, the global.json version
# resolution, the locked restore/build/test commands, the TRX report
# convention — is a RUNTIME RECIPE, reusable by any compatible harness;
# everything about the agent — which sdk drives it, which credentials it
# consumes — is a HARNESS PROFILE. The two axes compile into the
# (recipe, harness) pair the dispatch approves, with the legacy fused
# driver ids preserved as versioned aliases during migration.


@dataclass(frozen=True)
class RuntimeRecipe:
    """The language/toolchain axis (NEXT-15): what the lane runs ON.

    ``image_pin`` is the deterministic runtime surface — a
    digest-pinned container image (``dotnet-9``) or a pinned interpreter
    /runtime major (``python-3-13``, ``node-22``). ``version_check`` is
    the command that resolves the pinned version THROUGH the target's
    own pin file (``dotnet --version`` resolves global.json and fails
    loudly on a mismatch). ``build_commands`` are the argvs the recipe's
    verification tail runs (restore/build/test, locked where the
    toolchain supports it). ``required_files`` are the repo files whose
    ABSENCE fails :meth:`gate` before the agent burns a token.
    ``report_prefix`` is the test-report naming convention the recipe's
    aggregator keys on (the .NET TRX ``LogFilePrefix``; ``""`` when the
    recipe has no report convention).
    """

    recipe_id: str
    image_pin: str
    version_check: tuple[str, ...]
    build_commands: tuple[tuple[str, ...], ...]
    required_files: tuple[str, ...]
    report_prefix: str = ""

    def __post_init__(self) -> None:
        if not self.recipe_id or not self.image_pin:
            raise ValueError("a recipe carries a non-empty id and image pin")
        if not self.version_check or not self.build_commands:
            raise ValueError(
                "a recipe carries its version check and its build commands — "
                "an empty recipe is an unexecutable one"
            )

    def gate(self, source: ProfileSource) -> tuple[RecipeViolation, ...]:
        """Check the TOOLCHAIN prerequisites against one repo view.

        The recipe axis's own gate, INDEPENDENT of the selected harness
        (NEXT-15): every :attr:`required_files` entry must be FOUND in
        the target — an absent pin file is a violation naming the
        remedy, an unreadable one is a violation too (unverifiable IS
        non-compliant here, the same doctrine :func:`validate_runtime`
        applies to the v2 rootfs). What is deliberately NOT checked: the
        file's CONTENT (global.json's version resolution is the
        ``version_check`` command's job at run time, in the runner — a
        control-plane read never executes target build scripts).
        """
        violations: list[RecipeViolation] = []
        for path in self.required_files:
            read = source.read_file(path)
            if read.status == "absent":
                violations.append(
                    RecipeViolation(
                        recipe_id=self.recipe_id,
                        check="required_file",
                        path=path,
                        detail=(
                            f"{path} is required by the {self.recipe_id} recipe "
                            f"({self.image_pin}) — commit it so the pinned "
                            "toolchain can resolve, before the agent runs"
                        ),
                    )
                )
            elif read.status == "unknown":
                violations.append(
                    RecipeViolation(
                        recipe_id=self.recipe_id,
                        check="required_file_unreadable",
                        path=path,
                        detail=(
                            f"{path} could not be read — the {self.recipe_id} "
                            "recipe's toolchain gate is unverifiable, and an "
                            "unverifiable prerequisite is treated as unmet"
                        ),
                    )
                )
        return tuple(violations)

    def to_document(self) -> dict:
        """The audit shape (recipe axis only — the harness rides beside it)."""
        return {
            "recipe_id": self.recipe_id,
            "image_pin": self.image_pin,
            "version_check": list(self.version_check),
            "build_commands": [list(argv) for argv in self.build_commands],
            "required_files": list(self.required_files),
            "report_prefix": self.report_prefix,
        }


@dataclass(frozen=True)
class RecipeViolation:
    """One toolchain prerequisite the recipe's :meth:`RuntimeRecipe.gate`
    found unmet (NEXT-15). ``check`` names the axis
    (``required_file`` / ``required_file_unreadable``); ``detail`` is the
    actionable sentence — what is missing and what to do about it."""

    recipe_id: str
    check: str
    path: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover — trivial rendering
        return f"[{self.recipe_id}/{self.check}] {self.detail}"


@dataclass(frozen=True)
class HarnessProfile:
    """The agent-driver axis (NEXT-15): what runs IN the lane.

    ``harness_id`` is the agent identity (claude-code / codex / copilot —
    NOT a lane driver id); ``sdk`` is the adaptive driver sdk that drives
    it; ``credential_names`` are the env names the harness consumes (the
    recipe stages them per its own profile — the NAMES, never values).
    """

    harness_id: str
    sdk: str
    credential_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.harness_id or not self.sdk:
            raise ValueError("a harness profile carries a non-empty id and sdk")

    def to_document(self) -> dict:
        return {
            "harness_id": self.harness_id,
            "sdk": self.sdk,
            "credential_names": list(self.credential_names),
        }


#: The shipped runtime recipes (NEXT-15). The .NET recipe IS the
#: dotnet-lane's own runtime half — image digest verified against MCR
#: 2026-09-23 (the template's pin), global.json version resolution, the
#: locked restore/build/test tail, and the NEXT-16 TRX ``LogFilePrefix``
#: convention. The python recipe is the interpreter the SDK lanes and the
#: Actions lane pin; the node recipe is the npm runtime the scripted
#: claude-code lanes install their CLI into.
RUNTIME_RECIPES: Mapping[str, RuntimeRecipe] = {
    "dotnet-9": RuntimeRecipe(
        recipe_id="dotnet-9",
        image_pin=(
            "mcr.microsoft.com/dotnet/sdk:9.0"
            "@sha256:01fabc4758d1d74e39eda700c8463dae6241a61481f973683692ddcb59a5eeb7"
        ),
        version_check=("dotnet", "--version"),  # resolves THROUGH global.json
        build_commands=(
            ("dotnet", "restore", "--locked-mode"),
            ("dotnet", "build", "--no-restore", "--locked-mode"),
            ("dotnet", "test", "--no-build"),
        ),
        required_files=("global.json",),
        report_prefix="forge_",  # NEXT-16: unique TRX prefixes, aggregate all
    ),
    "python-3-13": RuntimeRecipe(
        recipe_id="python-3-13",
        image_pin=LANE_PYTHON_VERSION,  # the interpreter pin (setup-python)
        version_check=("python3", "--version"),
        build_commands=(("uv", "sync", "--frozen"),),
        required_files=(),  # lock-less repos take the documented pip fallback
    ),
    "node-22": RuntimeRecipe(
        recipe_id="node-22",
        image_pin="22",  # the node runtime major the npm-distributed CLIs pin
        version_check=("node", "--version"),
        build_commands=(("npm", "ci"),),
        required_files=("package-lock.json",),
    ),
}


#: The shipped harness profiles (NEXT-15) — the agent half of every
#: compiled combination. ``claude-code`` is the scripted CLI agent (the
#: ANTHROPIC gateway recipe); ``codex`` and ``copilot`` name the vendor
#: agents their sdk lanes drive.
HARNESS_PROFILES: Mapping[str, HarnessProfile] = {
    "claude-code": HarnessProfile(
        harness_id="claude-code",
        sdk="claude-sdk",
        credential_names=("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    ),
    "codex": HarnessProfile(
        harness_id="codex",
        sdk="codex-app",
        credential_names=("OPENAI_API_KEY", "CODEX_API_KEY"),
    ),
    "copilot": HarnessProfile(
        harness_id="copilot",
        sdk="copilot-acp",
        credential_names=("COPILOT_GITHUB_TOKEN",),
    ),
}


#: The APPROVED (recipe, harness) combinations (NEXT-15): compiled at
#: onboarding; anything else is refused BEFORE any model call. The .NET
#: runtime is deliberately approved for every shipped harness — another
#: compatible agent reuses the recipe without a second .NET command
#: implementation (the review's acceptance criterion), while the node
#: runtime (a thin npm-install surface) is approved for the claude-code
#: agent only.
COMPILED_COMBINATIONS: tuple[tuple[str, str], ...] = (
    ("dotnet-9", "claude-code"),  # the shipped dotnet-lane (the alias below)
    ("dotnet-9", "codex"),  # the runtime reused, no second .NET recipe
    ("dotnet-9", "copilot"),
    ("python-3-13", "claude-code"),
    ("python-3-13", "codex"),
    ("python-3-13", "copilot"),
    ("node-22", "claude-code"),
)


#: The versioned compatibility aliases (NEXT-15): legacy fused driver ids
#: → their (recipe, harness) decomposition. The existing driver id still
#: resolves (migration does not change active runs), but the
#: decomposition is the authority: :func:`resolve_driver_profile` answers
#: the pair, never a fused identity.
DRIVER_PROFILE_ALIASES: Mapping[str, tuple[str, str]] = {
    "dotnet-lane": ("dotnet-9", "claude-code"),
}


@dataclass(frozen=True)
class LaneDriverProfile:
    """One compiled (recipe, harness) lane (NEXT-15) — the decomposition
    a dispatch approves. ``alias`` names the legacy fused driver id this
    profile decomposes (``""`` for a freshly composed pair)."""

    recipe: RuntimeRecipe
    harness: HarnessProfile
    alias: str = ""

    @property
    def driver_id(self) -> str:
        """The dispatch id: the legacy alias when one exists, else the
        composed ``<recipe>+<harness>`` spelling."""
        return self.alias or f"{self.recipe.recipe_id}+{self.harness.harness_id}"

    def to_document(self) -> dict:
        """The audit shape — both axes stated, the alias beside them."""
        return {
            "driver_id": self.driver_id,
            "recipe": self.recipe.to_document(),
            "harness": self.harness.to_document(),
            "alias": self.alias,
        }


def runtime_recipe(recipe_id: str) -> RuntimeRecipe:
    """The shipped recipe for *recipe_id*; unknown ids raise (fail closed).

    Denial is observable — there is no silent fallback to a default
    runtime, never to an unpinned one.
    """
    try:
        return RUNTIME_RECIPES[str(recipe_id or "").strip()]
    except KeyError:
        raise ValueError(
            f"unknown runtime recipe {recipe_id!r}; vocabulary is {tuple(sorted(RUNTIME_RECIPES))}"
        ) from None


def harness_profile(harness_id: str) -> HarnessProfile:
    """The shipped harness profile for *harness_id*; unknown ids raise."""
    try:
        return HARNESS_PROFILES[str(harness_id or "").strip()]
    except KeyError:
        raise ValueError(
            f"unknown harness profile {harness_id!r}; vocabulary is "
            f"{tuple(sorted(HARNESS_PROFILES))}"
        ) from None


def compile_driver_profile(
    recipe_id: str,
    harness_id: str,
    *,
    alias: str = "",
) -> LaneDriverProfile:
    """Compile ONE (recipe, harness) lane against the compatibility matrix.

    Both axes must be in their shipped vocabularies AND the pair in
    :data:`COMPILED_COMBINATIONS` — an unsupported combination is
    refused here, BEFORE any model call (a ``ValueError`` at compile
    time, not a surprise in a paid turn). *alias* records the legacy
    driver id a migrated configuration resolved from.
    """
    recipe = runtime_recipe(recipe_id)
    harness = harness_profile(harness_id)
    pair = (recipe.recipe_id, harness.harness_id)
    if pair not in COMPILED_COMBINATIONS:
        raise ValueError(
            f"the (recipe, harness) combination {pair!r} is not in the compiled "
            f"compatibility matrix {COMPILED_COMBINATIONS} — refused before any "
            "model call; onboard the combination explicitly instead of assuming it"
        )
    return LaneDriverProfile(recipe=recipe, harness=harness, alias=str(alias or ""))


def resolve_driver_profile(driver_id: str) -> LaneDriverProfile:
    """Resolve a dispatch driver id to its decomposed profile (NEXT-15).

    Two spellings resolve: a legacy ALIAS id (:data:`DRIVER_PROFILE_ALIASES`
    — ``dotnet-lane`` → ``("dotnet-9", "claude-code")``, the profile
    carrying the alias for the audit trail) and the composed
    ``<recipe>+<harness>`` spelling (``dotnet-9+codex`` — the
    decomposition IS the id). Anything else raises: a fused id with no
    alias entry has no authority to assume a recipe or a harness.
    """
    raw = str(driver_id or "").strip()
    if not raw:
        raise ValueError("driver_id must be non-empty")
    if raw in DRIVER_PROFILE_ALIASES:
        recipe_id, harness_id = DRIVER_PROFILE_ALIASES[raw]
        return compile_driver_profile(recipe_id, harness_id, alias=raw)
    if "+" in raw:
        recipe_id, _, harness_id = raw.partition("+")
        return compile_driver_profile(recipe_id, harness_id)
    raise ValueError(
        f"unknown driver id {raw!r} — the (recipe, harness) decomposition is the "
        f"authority; aliases: {tuple(sorted(DRIVER_PROFILE_ALIASES))}, composed "
        "spelling: <recipe>+<harness> (e.g. dotnet-9+claude-code)"
    )
