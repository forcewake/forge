from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from forge.repository.changeset import BUILTIN_WRITE_PROFILES


class Settings(BaseSettings):
    """Core settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # GitLab connection
    GITLAB_URL: str
    GITLAB_TOKEN: SecretStr
    GITLAB_WEBHOOK_SECRET: SecretStr
    FORGE_BOT_USERNAME: str = "forge-bot"

    # LiteLLM proxy
    LITELLM_URL: str = "http://litellm:4000"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/forge.db"

    # Redis (optional)
    REDIS_URL: str | None = None

    # Agents
    FORGE_AGENTS_DIR: str = "agents"

    # Behaviour
    FORGE_MENTION_PATTERN: str = "@forge"
    LOG_LEVEL: str = "INFO"

    # Human gates (ADR-0009): comma-separated GitLab usernames allowed to
    # approve a plan via `@forge /go <run-id>`. Empty — nobody may approve.
    FORGE_APPROVERS: str = ""

    # Dedicated bot identity forge acts with (comments, commits, MRs).
    # Never let forge speak with a human approver's credentials: its own
    # comments (which contain /go instructions) would then come back as
    # approver-authored webhooks and self-approve gates.
    FORGE_BOT_TOKEN: SecretStr | None = None

    # M1 run loop: MR target branch and max seconds a run may wait for CI
    # before the reconciler parks it as blocked(ci_timeout).
    FORGE_TARGET_BRANCH: str = "main"
    FORGE_CI_WAIT_SECONDS: int = 3600

    # ADR-0008 quality contract: comma-separated CI job names that must exist
    # AND succeed for a run to reach ready_for_human. Skipped/manual/missing
    # required jobs fail the contract even when the pipeline icon is green.
    # Empty — a successful pipeline status is enough.
    FORGE_REQUIRED_JOBS: str = ""

    # ADR-0004 budget semantics: max commit cycles per run — the initial
    # candidate plus (N - 1) code-repair commits. Infrastructure/config
    # failures never consume a cycle (they block instead of repairing).
    FORGE_MAX_COMMIT_CYCLES: int = 3

    # Tier-1 auto-revive: how many times a run killed by a TRANSIENT failure
    # (dispatch 5xx / network / timeout, rate limit, runner startup) may be
    # re-dispatched on the same branch by the reconciler before it parks
    # blocked for a human. 0 disables auto-revive. Fatal failures (config
    # errors, driver quality signals, exhausted cycles) never auto-retry.
    FORGE_RUN_AUTO_REVIVE_LIMIT: int = 2

    # Base of the auto-revive backoff ladder (60s → 120s → 240s …, capped in
    # forge.runs.revival) — the worker-free wait between a transient death and
    # its reconciler re-dispatch.
    FORGE_RUN_REVIVE_BACKOFF_SECONDS: int = 60

    # A12 effect-certainty window: how long a NEGATIVE publication-intent
    # recovery probe parks the intent in `probing` before its re-probe. A
    # provider may have accepted the first request and be applying it slowly,
    # so one negative read never justifies a re-dispatch. The window backs
    # off (×2) per still-negative round (bounded by the intents module's
    # MAX_SETTLE_ROUNDS); on CAS-less GitLab an exhausted window parks
    # unknown_outcome for an operator instead of ever re-dispatching, while
    # on GitHub/Azure the branch-wide CAS makes the released redispatch
    # inherently duplicate-safe.
    FORGE_PUBLISH_SETTLE_SECONDS: int = 30

    # ADR-0015 pluggable implementer backend: "builtin" (LLM -> ChangeSet ->
    # Commits API) or "ci_harness" (optionally "ci_harness:claude-code") —
    # a coding harness executing as a job in the target project's CI.
    FORGE_IMPLEMENTER_BACKEND: str = "builtin"

    # ADR-0015 harness budgets: run-level wall-clock timeout for a harness
    # job (enforced durably by the controller; GitLab's own maximum_timeout
    # is the first line of defence) and the model passed to the harness job
    # as FORGE_HARNESS_MODEL.
    FORGE_HARNESS_TIMEOUT_SECONDS: int = 1800
    # R17 liveness: bounded discovery for a dispatch whose response was lost
    # (legacy empty-202). After this many discovery ticks that never found
    # the Actions run, the run parks blocked ("dispatch never observed")
    # instead of retrying until the harness timeout on a stalled API.
    FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS: int = 20
    FORGE_HARNESS_MODEL: str = "glm-5.3-flash[1m]"

    # ADR-0023 §1: comma-separated harness driver ids — the env form of the
    # ordered preference list for lab/CI usage (the YAML form is
    # ``implement.harnesses`` in forge.yml). Empty — the configured backend
    # as a one-element list (byte-compatible with pre-ADR-0023 projects).
    FORGE_HARNESS_PREFERENCE: str = ""

    # ADR-0023 Decision 3: dispatch-time fallback down the frozen chain, OFF
    # by default. When true, ONLY an infrastructure-classified harness
    # failure BEFORE any candidate exists re-dispatches the chain's next
    # entry (journaled in action_log); everything else fails visibly.
    FORGE_HARNESS_FALLBACK: bool = False

    # ADR-0018 (F15): how long the pending plan decision — created when the
    # plan note is posted — stays consumable. After the deadline a `/go` is
    # refused: the plan is stale and must be re-planned and re-approved.
    FORGE_DECISION_TTL_SECONDS: int = 7 * 86400

    # Webhook payload capture (debug/diagnostics; path relative to CWD)
    FORGE_CAPTURE_DIR: str | None = None

    # Management read API (F30): when set, GET /runs and GET /runs/{id}
    # require `Authorization: Bearer <FORGE_API_READ_TOKEN>`. None (default)
    # leaves them open — dev only; set a token for anything network-exposed.
    # The legacy /flows/{id} endpoint was removed entirely (it leaked Redis
    # flow state without auth); /runs is the single durable read model.
    FORGE_API_READ_TOKEN: SecretStr | None = None

    # MCP server (optional). Fail closed: the MCP app is only mounted when
    # FORGE_MCP_ENABLED is true AND FORGE_MCP_KEY is set — an unauthenticated
    # endpoint is never exposed. Set FORGE_MCP_ENABLED=false only when the
    # deployment fronts /mcp with its own authentication.
    FORGE_MCP_ENABLED: bool = True
    FORGE_MCP_KEY: SecretStr | None = None
    # ADR-0021 §4: scoped MCP principals — JSON object token → scope list
    # (closed set: forge:read, forge:runs:write, forge:approvals:write,
    # forge:admin; "*" grants the full set). FORGE_MCP_KEY remains the
    # all-scope master. Malformed JSON or unknown scopes fail startup.
    FORGE_MCP_SCOPED_TOKENS: SecretStr | None = None
    # R19: optional repository-target allowlist for scoped MCP principals —
    # JSON object token (or its audit label "tok-<hash>") → list of fnmatch
    # patterns matched against the caller-supplied project path
    # ({"tok-abc...": ["group/app-*"]}; an empty list denies every target).
    # Unset (default) leaves scoped tokens unrestricted, so existing configs
    # keep working. FORGE_MCP_KEY master is unrestricted by design.
    FORGE_MCP_TOKEN_REPOS: SecretStr | None = None
    # Host headers the MCP endpoint accepts when mounted behind a proxy
    # (comma-separated; e.g. "forge.example.com"). Empty keeps the SDK's
    # localhost-only DNS-rebinding protection — a deployment MUST list its
    # public host or /mcp answers 421 to every proxied request.
    FORGE_MCP_ALLOWED_HOSTS: str = ""
    FORGE_GITHUB_TOKEN: SecretStr | None = None

    # Agno telemetry (disabled for self-hosted)
    AGNO_TELEMETRY: bool = False

    # --- GitHub source adapter (ADR-0019 first slice) -----------------------
    # Fail closed: the GitHub webhook ingress is only mounted when enabled AND
    # the webhook secret is set — an unsigned delivery is never accepted.
    FORGE_GITHUB_ENABLED: bool = False
    FORGE_GITHUB_API_URL: str = "https://api.github.com"
    # GitHub App identity (ADR-0019 §3: the private key stays server-side —
    # never in a harness job, never in repo config).
    FORGE_GITHUB_APP_ID: str = ""
    FORGE_GITHUB_PRIVATE_KEY: SecretStr | None = None
    FORGE_GITHUB_INSTALLATION_ID: str = ""
    FORGE_GITHUB_BOT_LOGIN: str = "forcewake-forge[bot]"
    # R02: bounded wait for PR checks on the candidate sha before a
    # waiting_ci run is declared verification_timeout.
    FORGE_VERIFICATION_TIMEOUT_SECONDS: int = 1800
    # A just-opened PR's checks need a few seconds to register; before this
    # grace elapses the verifier keeps waiting instead of concluding
    # "no CI configured" (live-found race on the dogfood cycle).
    FORGE_VERIFICATION_GRACE_SECONDS: int = 120
    # A01 positive-proof policy waiver: comma-separated provider check
    # conclusions/results a deployment explicitly ACCEPTS on a REQUIRED
    # check (e.g. "skipped,neutral"). Default empty — a skipped/neutral/
    # unknown conclusion on a required check never verifies the run. The
    # waiver can never apply to failure-class or cancelled/timed_out
    # conclusions (those blame the change or the infrastructure).
    FORGE_VERIFICATION_WAIVE_CONCLUSIONS: str = ""
    # HMAC-SHA256 secret for X-Hub-Signature-256 validation. None/empty →
    # ingress disabled (503), matching the fail-closed MCP pattern above.
    FORGE_GITHUB_WEBHOOK_SECRET: SecretStr | None = None

    # Stage E3b (ADR-0020): the harness workflow filename human-applied to
    # the TARGET repo (e.g. "forge-harness.github.yml"). Empty (default) →
    # builtin in-worker execution as before. When set, an approved /go
    # dispatches the Actions harness instead of running the builtin
    # proposer; the candidate returns as an Actions artifact for the same
    # trusted publisher.
    FORGE_GITHUB_HARNESS_WORKFLOW: str = ""

    # ADR-0020 §4 label trigger: an ``issues.labeled`` delivery whose label
    # name matches (case-insensitive) starts the same run command as
    # /implement — the actor is the labeler, so admission still applies.
    FORGE_TRIGGER_LABEL: str = "forge"

    # Connection-scoped approvers (ADR-0018 §3): comma-separated GitHub
    # logins allowed to /implement and /go on the GitHub path. Empty — fall
    # back to FORGE_APPROVERS (backward compat). The lists never merge, so a
    # GitLab username in FORGE_APPROVERS can never approve a GitHub run.
    FORGE_GITHUB_APPROVERS: str = ""

    # Security findings (v0.7): when true, the privileged remote-dismissal
    # action may dismiss the PROVIDER alert (research §4.2 enums) for a
    # finding whose AUTHORITATIVE forge status is already `false_positive`
    # — intent-journaled, with the justification as the audit comment.
    # Default false. A model suggestion alone can never close a remote
    # alert: the status must have been confirmed first (by an authorized
    # triager or the auto-accept opt-in below), and every dismissal is
    # journaled intent-first (R25).
    FORGE_SECURITY_REMOTE_DISMISS: bool = False

    # R25 triage governance: when true, the /security pass promotes its own
    # accepted AI suggestions into the authoritative `status` WITHOUT a
    # human confirm. Default OFF — the AI verdict is a SUGGESTION
    # (`suggested_verdict`) that an authorized actor confirms; only an
    # explicit operator opt-in lets the machine confirm on their behalf.
    FORGE_SECURITY_AUTO_ACCEPT: bool = False

    # R25: comma-separated usernames authorized to confirm/reject AI triage
    # suggestions (forge.findings.triage.confirm_finding_verdict). Empty —
    # nobody may confirm, mirroring FORGE_APPROVERS' fail-closed default.
    FORGE_SECURITY_TRIAGERS: str = ""

    # --- Azure DevOps source adapter (ADR-0024, AZ-2) ------------------------
    # Fail closed: the Azure DevOps webhook ingress is only mounted when
    # enabled AND the webhook Basic credentials are set — Azure service hooks
    # have NO HMAC signature (research §2.0); those credentials ARE the
    # authenticator, so an uncredentialed endpoint is never exposed.
    FORGE_AZDO_ENABLED: bool = False
    # Services https://dev.azure.com/{org} or Server https://{instance}/{collection}.
    FORGE_AZDO_ORG_URL: str = ""
    # Forge's own service-account identity (PAT, Basic ":"+PAT — ADR-0024 §2).
    FORGE_AZDO_PAT: SecretStr | None = None
    # Ingress validation pair — constant-time compared against every
    # delivery's Basic Authorization header. Either unset → ingress 503.
    FORGE_AZDO_WEBHOOK_USERNAME: str = ""
    FORGE_AZDO_WEBHOOK_PASSWORD: SecretStr | None = None

    # Connection-scoped approvers (ADR-0018 §3, mirrors FORGE_GITHUB_APPROVERS):
    # comma-separated Azure DevOps identities (uniqueName form) allowed to
    # /implement and /go on the azure_devops path. Empty — fall back to
    # FORGE_APPROVERS (backward compat). The lists never merge.
    FORGE_AZDO_APPROVERS: str = ""

    # Bot-loop guard: forge's own AzDO identity (uniqueName or displayName —
    # its plan comments re-trigger workitem.commented). Forge-authored
    # deliveries never act as triggers.
    FORGE_AZDO_BOT_NAME: str = "forge-bot"

    # ADR-0024 §6 execution adapter: the Azure Pipelines lane definition id
    # (dispatch target). Empty/None → the builtin in-worker lane; when set,
    # an approved /go dispatches the lane pipeline and parks the run in
    # waiting_harness (the FORGE_GITHUB_HARNESS_WORKFLOW analog).
    FORGE_AZDO_LANE_PIPELINE_ID: int | None = None

    # Evidence policy (F23): before CI-log-derived text enters a comment,
    # the run row or a repair brief, values matching these deny patterns
    # (literal substrings, comma-separated) are replaced with a
    # [REDACTED:rule] placeholder and the item is capped at FORGE_EVIDENCE_
    # MAX_CHARS.
    FORGE_EVIDENCE_DENY_PATTERNS: str = "glpat-,ghs_,sk-,xai-"
    FORGE_EVIDENCE_MAX_CHARS: int = 8000

    # ADR-0018 §5 (R13) numeric run budgets: JSON object of named budget
    # profiles — name → {"max_calls", "max_tokens", "wallclock_s"} (each an
    # optional positive int; omit/null = unlimited on that axis). A run's
    # budget class (frozen in the RunSpec) names the profile resolved AT
    # FREEZE TIME into numeric ceilings stored IN the spec; unknown class
    # names fall back to the "standard" profile. Unset/empty — no numeric
    # ceilings anywhere (runs are unlimited; no budget rows are opened),
    # byte-compatible with pre-R13 behavior. Malformed JSON fails startup
    # (fail closed, like FORGE_MCP_SCOPED_TOKENS) — a silently unlimited
    # budget would defeat the point of configuring one. The forge.yml form
    # (``budget_profiles:``) wins when both are set.
    FORGE_BUDGET_PROFILES: str = ""

    # R31 capability manifest: the drivers THIS project onboarded — JSON
    # array of driver ids, or a versioned manifest object
    # {"version": 1, "drivers": [...]}. A driver outside this set is never
    # selected, whatever the preference or the planner proposes (the set CAPS
    # the chain; it never extends it). Unset/empty — every shipped driver is
    # available (byte-compatible with pre-R31 projects, where the shipped
    # set was the implicit manifest). Malformed JSON or an unknown manifest
    # version fails startup (fail closed, like FORGE_BUDGET_PROFILES). The
    # forge.yml form (``implement.available_drivers``) wins when both are
    # set. An R15 pin (FORGE_DRIVER_VERSIONS) never widens this set: a pin
    # presupposes the capability, it does not create it.
    FORGE_AVAILABLE_DRIVERS: str = ""

    # R15: per-driver CLI version pins for the Actions harness lane — JSON
    # object driver → version (claude-code | grok-build | opencode |
    # copilot); the literal "latest" keeps the unpinned npm dist-tag
    # install. The LANE consumes the same-named repo VARIABLE
    # (FORGE_DRIVER_VERSIONS, passed through by the workflow template) and
    # falls back to forge's known-good defaults (forge.harness_entry);
    # this field is the control-plane/lab form of the same map. Shape-only
    # validation here (parse_driver_versions) — the closed driver-id set
    # and the per-version charset are the lane's fail-closed concern
    # (importing them here would close the runs-package import cycle, cf.
    # the harness_preference accessor).
    FORGE_DRIVER_VERSIONS: str = ""

    # R18 write-policy profiles: JSON object of custom write profiles —
    # name → {"denied_paths": [glob], "allowed_paths": [glob],
    # "require_special_approval": bool}. Custom profiles extend the base
    # denies tighten-only (lockfiles stay denied; use the built-in
    # "dependency_update" for dependency work) and may never shadow a
    # built-in name. Unset/empty — only the four built-in profiles exist and
    # the default profile ("no_dependencies") is exactly the historical
    # hardcoded behavior. Malformed JSON fails startup (fail closed, like
    # FORGE_BUDGET_PROFILES). The forge.yml form (``write_profiles:``) wins
    # when both are set.
    FORGE_WRITE_PROFILES: str = ""

    # R18 Azure sensitive paths: JSON object project key → pipeline
    # entrypoint paths — {"azure_devops:42": ["ci/build.yml"]} — treated as
    # denied paths under EVERY write profile (a candidate never rewrites
    # its own execution lane). Azure's pipeline definition may be any file,
    # so onboarding lists the real entrypoints instead of forge assuming
    # "azure-pipelines.yml". Keys are "<provider>:<project_id>" (GitHub
    # also accepts "github:<owner/repo>"). Malformed JSON fails startup.
    # The forge.yml form (``pipeline_entrypoints:``) wins when both are set.
    FORGE_PIPELINE_ENTRYPOINTS: str = ""


def _positive_int_or_none(value: object, where: str) -> int | None:
    """A usable positive budget ceiling, or ``None`` (absent/null = unset).

    Any other value — garbage, bool, zero, negative — is a configuration
    error, never a silent unlimited: a mistyped ceiling must fail loudly
    instead of disarming the budget.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{where} must be a positive int or null, got {value!r}")
    return value


def validate_budget_profiles(raw: object) -> dict[str, dict[str, Any]]:
    """Validate a budget-profiles mapping (ADR-0018 §5, R13) — ``ValueError``
    on any defect, so a broken configuration fails startup instead of
    silently running unlimited."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("budget_profiles must be a mapping of name → limits")
    profiles: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        key = str(name).strip()
        if not key:
            raise ValueError("budget_profiles entries must have non-empty names")
        if not isinstance(entry, dict):
            raise ValueError(f"budget_profiles[{key!r}] must be a mapping of limit names to ints")
        profiles[key] = {
            axis: _positive_int_or_none(entry.get(axis), f"budget_profiles[{key!r}][{axis!r}]")
            for axis in ("max_calls", "max_tokens", "wallclock_s")
        }
    return profiles


def parse_budget_profiles(raw: str | None) -> dict[str, dict[str, Any]]:
    """The FORGE_BUDGET_PROFILES JSON form — ``ValueError`` when malformed.

    Fail closed like FORGE_MCP_SCOPED_TOKENS: a profile JSON that does not
    parse must abort startup, never degrade to unlimited runs.
    """
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_BUDGET_PROFILES is not valid JSON: {exc}") from exc
    return validate_budget_profiles(data)


#: The manifest version :func:`validate_available_drivers` accepts (R31).
#: A higher number means a shape this control plane does not know yet — it
#: refuses rather than guess what the new fields mean.
AVAILABLE_DRIVERS_MANIFEST_VERSION = 1


def validate_available_drivers(raw: object) -> list[str]:
    """Validate a capability manifest (R31) — the drivers the project
    onboarded. Accepts a plain array of driver ids or a versioned manifest
    object ``{"version": 1, "drivers": [...]}``; ``ValueError`` on any other
    shape, so a mistyped manifest fails startup instead of silently widening
    (or emptying) the project's driver set."""
    if raw is None:
        return []
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        version = raw.get("version", AVAILABLE_DRIVERS_MANIFEST_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("available_drivers.version must be an int")
        if version != AVAILABLE_DRIVERS_MANIFEST_VERSION:
            raise ValueError(
                f"available_drivers manifest version {version} is not supported "
                f"(this forge understands version {AVAILABLE_DRIVERS_MANIFEST_VERSION})"
            )
        if set(raw) - {"version", "drivers"}:
            raise ValueError("available_drivers manifest accepts only 'version' and 'drivers' keys")
        entries = raw.get("drivers")
        if not isinstance(entries, list):
            raise ValueError("available_drivers manifest needs a 'drivers' list")
    else:
        raise ValueError(
            "available_drivers must be a driver-id list or a "
            "{'version': 1, 'drivers': [...]} manifest"
        )
    seen: list[str] = []
    for entry in entries:
        driver = str(entry).strip()
        if not driver:
            raise ValueError("available_drivers entries must be non-empty driver ids")
        if driver not in seen:
            seen.append(driver)
    return seen


def parse_available_drivers(raw: str | None) -> list[str]:
    """The FORGE_AVAILABLE_DRIVERS JSON form (R31) — ``ValueError`` when
    malformed. Fail closed like FORGE_BUDGET_PROFILES: a manifest that does
    not parse must abort startup, never silently change the driver set."""
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_AVAILABLE_DRIVERS is not valid JSON: {exc}") from exc
    return validate_available_drivers(data)


def parse_driver_versions(raw: str | None) -> dict[str, str]:
    """The FORGE_DRIVER_VERSIONS JSON form (R15) — ``ValueError`` when
    malformed.

    Shape-only: a JSON object of driver id → non-empty version string (the
    literal ``latest`` is a legal value = unpinned install). The closed
    driver-id set and the per-version charset are enforced by the lane
    (``forge.harness_entry.resolve_driver_versions``), which cannot import
    this module — the lane runs stdlib-only.
    """
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_DRIVER_VERSIONS is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("FORGE_DRIVER_VERSIONS must be a JSON object of driver → version")
    versions: dict[str, str] = {}
    for name, version in data.items():
        key = str(name).strip()
        pin = str(version).strip()
        if not key or not pin:
            raise ValueError(
                "FORGE_DRIVER_VERSIONS entries must be non-empty driver → version strings"
            )
        versions[key] = pin
    return versions


def validate_write_profiles(raw: object) -> dict[str, dict[str, Any]]:
    """Validate a write-profiles mapping (R18) — ``ValueError`` on any
    defect, so a broken configuration fails startup instead of silently
    changing the write boundary. Custom profiles may not shadow a built-in
    name: a same-named override could silently RELAX the default policy."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("write_profiles must be a mapping of name → policy")
    profiles: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        key = str(name).strip()
        if not key:
            raise ValueError("write_profiles entries must have non-empty names")
        if key in BUILTIN_WRITE_PROFILES:
            raise ValueError(
                f"write_profiles[{key!r}] shadows a built-in profile — choose another name"
            )
        if not isinstance(entry, dict):
            raise ValueError(f"write_profiles[{key!r}] must be a mapping of policy keys")
        denied = entry.get("denied_paths") or []
        allowed = entry.get("allowed_paths") or []
        for field, value in (("denied_paths", denied), ("allowed_paths", allowed)):
            if not isinstance(value, list) or not all(
                isinstance(path, str) and path.strip() for path in value
            ):
                raise ValueError(
                    f"write_profiles[{key!r}][{field}] must be a list of non-empty path globs"
                )
        special = entry.get("require_special_approval", False)
        if not isinstance(special, bool):
            raise ValueError(f"write_profiles[{key!r}][require_special_approval] must be a bool")
        profiles[key] = {
            "denied_paths": [str(path) for path in denied],
            "allowed_paths": [str(path) for path in allowed],
            "require_special_approval": special,
        }
    return profiles


def parse_write_profiles(raw: str | None) -> dict[str, dict[str, Any]]:
    """The FORGE_WRITE_PROFILES JSON form (R18) — ``ValueError`` when
    malformed. Fail closed like FORGE_BUDGET_PROFILES: a profile JSON that
    does not parse must abort startup, never degrade the write boundary."""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_WRITE_PROFILES is not valid JSON: {exc}") from exc
    return validate_write_profiles(data)


def validate_pipeline_entrypoints(raw: object) -> dict[str, list[str]]:
    """Validate a pipeline-entrypoints mapping (R18) — project key → list of
    sensitive pipeline paths; ``ValueError`` on any defect."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("pipeline_entrypoints must be a mapping of project key → path list")
    entrypoints: dict[str, list[str]] = {}
    for key, paths in raw.items():
        name = str(key).strip()
        if not name:
            raise ValueError("pipeline_entrypoints entries must have non-empty project keys")
        if not isinstance(paths, list) or not all(
            isinstance(path, str) and path.strip() for path in paths
        ):
            raise ValueError(
                f"pipeline_entrypoints[{name!r}] must be a list of non-empty pipeline paths"
            )
        entrypoints[name] = [str(path) for path in paths]
    return entrypoints


def parse_pipeline_entrypoints(raw: str | None) -> dict[str, list[str]]:
    """The FORGE_PIPELINE_ENTRYPOINTS JSON form (R18) — ``ValueError`` when
    malformed (fail closed, like the other JSON settings)."""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_PIPELINE_ENTRYPOINTS is not valid JSON: {exc}") from exc
    return validate_pipeline_entrypoints(data)


class ForgeConfig:
    """Optional YAML-based configuration loaded from forge.yml.

    Provides agent model aliases, default behaviours, labels, and rate limits.
    Falls back to sensible defaults when the file is missing.
    """

    _DEFAULTS: dict[str, Any] = {
        "version": "1",
        "bot_username": "forge-bot",
        "webhook_path": "/webhook",
        "port": 8420,
        "models": {
            "fast": "fast",
            "default": "strong",
            "strong": "strong",
            "code": "code",
        },
        "defaults": {
            "auto_review": True,
            "auto_pipeline_debug": True,
            "auto_security_triage": False,
            "mention_trigger": "@forge",
            "cooldown_seconds": 120,
            "max_diff_lines": 2000,
            "skip_draft_mrs": True,
        },
        "labels": {
            "reviewed": "ai-reviewed",
            "needs_changes": "ai-needs-changes",
            "security_critical": "security-critical",
        },
        "rate_limits": {
            "per_project_per_hour": 30,
            "per_user_per_hour": 20,
            "global_per_minute": 10,
        },
        "token_budgets": {
            "total": 24000,
            "diff": 12000,
            "per_file_diff": 4000,
            "pipeline_logs": 3000,
            "description": 2000,
            "previous_reviews": 3000,
        },
        "redaction": {
            "extra_patterns": [],
            "entropy_threshold": 4.5,
        },
        # ADR-0023 §1: the ordered harness preference (tighten-only). Empty —
        # the configured backend as a one-element list (byte-compatible).
        "implement": {
            "harnesses": [],
            # R31: the capability manifest — the drivers this project
            # onboarded (forge.yml form of FORGE_AVAILABLE_DRIVERS, wins when
            # both are set). Empty — every shipped driver is available
            # (byte-compatible default).
            "available_drivers": [],
        },
        # ADR-0018 §5 (R13): named numeric budget profiles — the forge.yml
        # form of FORGE_BUDGET_PROFILES (wins when both are set). Name →
        # {"max_calls", "max_tokens", "wallclock_s"}; a run's budget class
        # names the profile frozen into its RunSpec.
        "budget_profiles": {},
        # R18: named write-policy profiles — the forge.yml form of
        # FORGE_WRITE_PROFILES (wins when both are set). Name →
        # {"denied_paths", "allowed_paths", "require_special_approval"};
        # built-in names may not be shadowed.
        "write_profiles": {},
        # R18: per-project sensitive pipeline entrypoints — the forge.yml
        # form of FORGE_PIPELINE_ENTRYPOINTS (wins when both are set).
        # Project key → list of pipeline paths denied under every profile.
        "pipeline_entrypoints": {},
        "mcp_servers": {},
    }

    def __init__(self, path: str | Path = "forge.yml") -> None:
        # Deep copy: _DEFAULTS holds nested dicts, and _deep_merge below
        # mutates them in place — a shallow dict() copy would leak overrides
        # into the class-level defaults (and thus into every later instance).
        self._data: dict[str, Any] = copy.deepcopy(self._DEFAULTS)
        config_path = Path(path)
        if config_path.exists():
            with open(config_path) as f:
                raw = yaml.safe_load(f)
            if raw and isinstance(raw, dict):
                forge_section = raw.get("forge", raw)
                self._deep_merge(self._data, forge_section)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        """Recursively merge *override* into *base* in place."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                ForgeConfig._deep_merge(base[key], value)
            else:
                base[key] = value

    # Convenience accessors
    @property
    def models(self) -> dict[str, str]:
        return self._data["models"]

    @property
    def defaults(self) -> dict[str, Any]:
        return self._data["defaults"]

    @property
    def labels(self) -> dict[str, str]:
        return self._data["labels"]

    @property
    def rate_limits(self) -> dict[str, int]:
        return self._data["rate_limits"]

    @property
    def token_budgets(self) -> dict[str, int]:
        return self._data["token_budgets"]

    @property
    def redaction(self) -> dict[str, Any]:
        return self._data["redaction"]

    @property
    def implement(self) -> dict[str, Any]:
        """The ``implement`` block: run scoping and the harness preference."""
        return self._data["implement"]

    @property
    def harness_preference(self) -> list[str]:
        """``implement.harnesses`` — the ordered driver list (ADR-0023 §1).

        Ids are validated at compile time (``validate_preference``): only
        shipped drivers, and the configured backend driver stays in the list
        (tighten-only, ADR-0015) — a contradictory list is refused, never
        silently repaired. The driver set itself lives in
        :mod:`forge.runs.harness_selection` (importing it here would close an
        import cycle through the runs package).
        """
        raw = self._data["implement"].get("harnesses") or []
        if not isinstance(raw, list):
            raise ValueError("implement.harnesses must be a list of driver ids")
        seen: list[str] = []
        for entry in raw:
            driver = str(entry).strip()
            if driver and driver not in seen:
                seen.append(driver)
        return seen

    @property
    def available_drivers(self) -> list[str]:
        """``implement.available_drivers`` — the R31 capability manifest: the
        drivers this project onboarded. Only these may ever be selected, so
        an unavailable driver is dropped from the chain before anything else
        (a proposal can reorder the chain, never extend it). Empty — every
        shipped driver is available (byte-compatible default; the closed id
        set itself lives in :mod:`forge.runs.harness_selection` — importing
        it here would close an import cycle through the runs package). The
        forge.yml form wins over the FORGE_AVAILABLE_DRIVERS JSON.
        """
        return validate_available_drivers(self._data["implement"].get("available_drivers"))

    @property
    def mcp_servers(self) -> dict[str, Any]:
        return self._data["mcp_servers"]

    @property
    def budget_profiles(self) -> dict[str, dict[str, Any]]:
        """``budget_profiles`` — the named numeric run budgets (ADR-0018 §5).

        Each entry maps a budget class (the harness selection's
        ``budget_class``) to its numeric ceilings: ``max_calls``,
        ``max_tokens`` and ``wallclock_s``, each an optional positive int
        (omit/``None`` = unlimited on that axis). Invalid entries are refused
        with ``ValueError`` — a contradictory budget configuration is never
        silently repaired into an unlimited run.
        """
        return validate_budget_profiles(self._data.get("budget_profiles"))

    @property
    def write_profiles(self) -> dict[str, dict[str, Any]]:
        """``write_profiles`` — the custom write-policy profiles (R18).

        Each entry maps a profile name to ``{"denied_paths", "allowed_paths",
        "require_special_approval"}``; built-in names may not be shadowed and
        invalid entries are refused with ``ValueError`` — a broken write
        policy must fail startup, never silently change the boundary.
        """
        return validate_write_profiles(self._data.get("write_profiles"))

    @property
    def pipeline_entrypoints(self) -> dict[str, list[str]]:
        """``pipeline_entrypoints`` — per-project sensitive pipeline paths
        (R18). Project key (``"<provider>:<project_id>"``) → the pipeline
        entrypoint paths denied under every write profile. Invalid entries
        are refused with ``ValueError``.
        """
        return validate_pipeline_entrypoints(self._data.get("pipeline_entrypoints"))

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()  # type: ignore[call-arg]


def get_forge_config(path: str | Path = "forge.yml") -> ForgeConfig:
    """Return a ForgeConfig loaded from *path*."""
    return ForgeConfig(path)
