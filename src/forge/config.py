from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from forge.domain.paths import BUILTIN_WRITE_PROFILES


class Settings(BaseSettings):
    """Core settings loaded from environment / .env file."""

    # hide_input_in_errors: a validation error must never echo a token.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )
    # GitLab connection. Required while FORGE_GITLAB_ENABLED is true (the default); a
    # GitHub-only deployment sets FORGE_GITLAB_ENABLED=false.
    FORGE_GITLAB_ENABLED: bool = True
    GITLAB_URL: str = ""
    GITLAB_TOKEN: SecretStr = SecretStr("")
    GITLAB_WEBHOOK_SECRET: SecretStr = SecretStr("")
    FORGE_BOT_USERNAME: str = "forge-bot"

    @model_validator(mode="after")
    def _require_gitlab_settings_when_enabled(self) -> Settings:
        if self.FORGE_GITLAB_ENABLED:
            missing = [
                name
                for name, value in (
                    ("GITLAB_URL", self.GITLAB_URL),
                    ("GITLAB_TOKEN", self.GITLAB_TOKEN.get_secret_value()),
                    ("GITLAB_WEBHOOK_SECRET", self.GITLAB_WEBHOOK_SECRET.get_secret_value()),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"{', '.join(missing)} must be set while FORGE_GITLAB_ENABLED is true"
                )
        return self

    # LiteLLM proxy
    LITELLM_URL: str = "http://litellm:4000"
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/forge.db"
    # Redis (optional)
    REDIS_URL: str | None = None

    FORGE_MENTION_PATTERN: str = "@forge"
    LOG_LEVEL: str = "INFO"

    # Human gates (ADR-0009): comma-separated GitLab usernames allowed to approve a plan via
    # `@forge /go <run-id>`. Empty — nobody may approve.
    FORGE_APPROVERS: str = ""

    # Dedicated bot credentials prevent its own /go comments self-approving.
    FORGE_BOT_TOKEN: SecretStr | None = None

    # run loop: MR target branch and max seconds a run may wait for CI before the reconciler
    # parks it as blocked(ci_timeout).
    FORGE_TARGET_BRANCH: str = "main"
    FORGE_CI_WAIT_SECONDS: int = 3600

    # Required CI jobs must exist and succeed; skipped/manual/missing fails even on a green
    # pipeline. Empty accepts a successful pipeline status.
    FORGE_REQUIRED_JOBS: str = ""

    # ADR-0004 budget semantics: max commit cycles per run — the initial candidate plus (N -
    # 1) code-repair commits.
    FORGE_MAX_COMMIT_CYCLES: int = 3

    # Transient-only re-dispatch limit (5xx/network/timeout/rate-limit/startup). Zero
    # disables; config, quality and exhausted-cycle failures never retry.
    FORGE_RUN_AUTO_REVIVE_LIMIT: int = 2

    # Worker-free auto-revive backoff base; revival caps the doubling ladder.
    FORGE_RUN_REVIVE_BACKOFF_SECONDS: int = 60

    # Negative publication probes wait and double their settle window up to
    # MAX_SETTLE_ROUNDS: a slow accepted write must never be dispatched twice.
    FORGE_PUBLISH_SETTLE_SECONDS: int = 30

    # Implementation runs in target-project CI. The optional suffix selects the driver;
    # projects without an onboarded lane refuse before planning.
    FORGE_IMPLEMENTER_BACKEND: str = "ci_harness"

    # Controller wall-clock timeout (behind GitLab's maximum_timeout) and model passed to
    # the harness job as FORGE_HARNESS_MODEL.
    FORGE_HARNESS_TIMEOUT_SECONDS: int = 1800
    # Lost-response discovery ticks before blocked("dispatch never observed"); never poll a
    # stalled Actions API until the full harness timeout.
    FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS: int = 20
    FORGE_HARNESS_MODEL: str = "glm-5.3-flash[1m]"

    # ADR-0023 §1: comma-separated harness driver ids — the env form of the ordered
    # preference list for lab/CI usage (the YAML form is ``implement.harnesses`` in
    # forge.yml).
    FORGE_HARNESS_PREFERENCE: str = ""

    # ADR-0023 Decision 3: dispatch-time fallback down the frozen chain, OFF by default.
    FORGE_HARNESS_FALLBACK: bool = False

    # ADR-0018 (F15): how long the pending plan decision — created when the plan note is
    # posted — stays consumable.
    FORGE_DECISION_TTL_SECONDS: int = 7 * 86400

    # Webhook payload capture (debug/diagnostics; path relative to CWD)
    FORGE_CAPTURE_DIR: str | None = None

    # Management read API (F30): when set, GET /runs and GET /runs/{id} require
    # `Authorization: Bearer <FORGE_API_READ_TOKEN>`.
    FORGE_API_READ_TOKEN: SecretStr | None = None

    FORGE_GITHUB_TOKEN: SecretStr | None = None

    # GitHub source adapter (ADR-0019 first slice) Fail closed: the
    # GitHub webhook ingress is only mounted when enabled AND the webhook secret is set — an
    # unsigned delivery is never accepted.
    FORGE_GITHUB_ENABLED: bool = False
    FORGE_GITHUB_API_URL: str = "https://api.github.com"
    # GitHub App identity (ADR-0019 §3: the private key stays server-side — never in a
    # harness job, never in repo config).
    FORGE_GITHUB_APP_ID: str = ""
    FORGE_GITHUB_PRIVATE_KEY: SecretStr | None = None
    FORGE_GITHUB_INSTALLATION_ID: str = ""
    FORGE_GITHUB_BOT_LOGIN: str = "forcewake-forge[bot]"
    # bounded wait for PR checks on the candidate sha before a waiting_ci run is declared
    # verification_timeout.
    FORGE_MR_IO_TIMEOUT_SECONDS: float = 30.0

    FORGE_VERIFICATION_TIMEOUT_SECONDS: int = 1800
    # A just-opened PR's checks need a few seconds to register; before this grace elapses
    # the verifier keeps waiting instead of concluding "no CI configured" (live-found race
    # on the dogfood cycle).
    FORGE_VERIFICATION_GRACE_SECONDS: int = 120
    # positive-proof policy waiver: comma-separated provider check conclusions/results a
    # deployment explicitly ACCEPTS on a REQUIRED check (e.g. "skipped,neutral").
    FORGE_VERIFICATION_WAIVE_CONCLUSIONS: str = ""
    # HMAC-SHA256 secret for X-Hub-Signature-256 validation. None/empty → ingress disabled
    # (503), an unsigned delivery is never accepted.
    FORGE_GITHUB_WEBHOOK_SECRET: SecretStr | None = None

    # The workflow installed in this target repository. Admission verifies it exists before
    # any planner call; empty configuration refuses the run.
    FORGE_GITHUB_HARNESS_WORKFLOW: str = ""

    # Supported ingress authorization and lane control remain explicit.
    FORGE_GITHUB_APPROVERS: str = ""
    FORGE_LANE_CONTROL_SECRET: SecretStr | None = None

    # Lane v1 validates these when its authentication service is composed.
    FORGE_SECRET_KEY: SecretStr | None = None
    FORGE_PUBLIC_URL: str = ""
    FORGE_GITHUB_OIDC_ISSUER: str = "https://token.actions.githubusercontent.com"
    FORGE_GITHUB_OIDC_JWKS_URL: str = "https://token.actions.githubusercontent.com/.well-known/jwks"
    FORGE_GITLAB_OIDC_ISSUER: str = ""
    FORGE_GITLAB_OIDC_JWKS_URL: str = ""
    FORGE_LANE_SESSION_TTL_SECONDS: int = 300
    FORGE_LANE_OIDC_CLOCK_SKEW_SECONDS: int = 30
    FORGE_LANE_OIDC_JWKS_CACHE_SECONDS: int = 300
    FORGE_LANE_BLOB_DIR: str = "data/lane-v1/blobs"

    # ADR-0020 §4 label trigger: an ``issues.labeled`` delivery whose label name matches
    # (case-insensitive) starts the same run command as /implement — the actor is the
    # labeler, so admission still applies.
    FORGE_TRIGGER_LABEL: str = "forge"

    # Evidence policy (F23): before CI-log-derived text enters a comment, the run row or a
    # repair brief, values matching these deny patterns (literal substrings,
    # comma-separated) are replaced with a [REDACTED:rule] placeholder and the item is
    # capped at FORGE_EVIDENCE_ MAX_CHARS.
    FORGE_EVIDENCE_DENY_PATTERNS: str = "glpat-,ghs_,sk-,xai-"
    FORGE_EVIDENCE_MAX_CHARS: int = 8000

    # ADR-0018 §5 numeric run budgets: JSON object of named budget profiles — name →
    # {"max_calls", "max_tokens", "wallclock_s"} (each an optional positive int; omit/null =
    # unlimited on that axis).
    FORGE_BUDGET_PROFILES: str = ""

    # capability manifest: the drivers THIS project onboarded — JSON array of driver ids, or
    # a versioned manifest object {"version": 1, "drivers": [...]}. A driver outside this
    # set is never selected, whatever the preference or the planner proposes (the set CAPS
    # the chain; it never extends it).
    FORGE_AVAILABLE_DRIVERS: str = ""

    # per-driver CLI version pins for the Actions harness lane — JSON object driver →
    # version (claude-code | grok-build | opencode | copilot); the literal "latest" keeps
    # the unpinned npm dist-tag install.
    FORGE_DRIVER_VERSIONS: str = ""

    # write-policy profiles: JSON object of custom write profiles — name → {"denied_paths":
    # [glob], "allowed_paths": [glob], "require_special_approval": bool}. Custom profiles
    # extend the base denies tighten-only (lockfiles stay denied; use the built-in
    # "dependency_update" for dependency work) and may never shadow a built-in name.
    FORGE_WRITE_PROFILES: str = ""

    FORGE_PIPELINE_ENTRYPOINTS: str = ""


def _positive_int_or_none(value: object, where: str) -> int | None:
    """A usable positive budget ceiling, or ``None`` (absent/null = unset)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{where} must be a positive int or null, got {value!r}")
    return value


def validate_budget_profiles(raw: object) -> dict[str, dict[str, Any]]:
    """Validate a budget-profiles mapping (ADR-0018 §5,) — ``ValueError`` on any defect, so a broken configuration fails startup instead of silently running unlimited."""
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
    """The FORGE_BUDGET_PROFILES JSON form — ``ValueError`` when malformed."""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_BUDGET_PROFILES is not valid JSON: {exc}") from exc
    return validate_budget_profiles(data)


#: The manifest version :func:`validate_available_drivers` accepts.
AVAILABLE_DRIVERS_MANIFEST_VERSION = 1


def validate_available_drivers(raw: object) -> list[str]:
    """Validate a capability manifest — the drivers the project onboarded."""
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
    """The FORGE_AVAILABLE_DRIVERS JSON form — ``ValueError`` when malformed."""
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_AVAILABLE_DRIVERS is not valid JSON: {exc}") from exc
    return validate_available_drivers(data)


def parse_driver_versions(raw: str | None) -> dict[str, str]:
    """The FORGE_DRIVER_VERSIONS JSON form — ``ValueError`` when malformed."""
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
    """Validate a write-profiles mapping — ``ValueError`` on any defect, so a broken configuration fails startup instead of silently changing the write boundary."""
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
    """The FORGE_WRITE_PROFILES JSON form — ``ValueError`` when malformed."""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_WRITE_PROFILES is not valid JSON: {exc}") from exc
    return validate_write_profiles(data)


def validate_pipeline_entrypoints(raw: object) -> dict[str, list[str]]:
    """Validate a pipeline-entrypoints mapping — project key → list of sensitive pipeline paths; ``ValueError`` on any defect."""
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
    """The FORGE_PIPELINE_ENTRYPOINTS JSON form — ``ValueError`` when malformed (fail closed, like the other JSON settings)."""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_PIPELINE_ENTRYPOINTS is not valid JSON: {exc}") from exc
    return validate_pipeline_entrypoints(data)


class ForgeConfig:
    """Optional YAML-based configuration loaded from forge.yml."""

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
        # ADR-0023 §1: the ordered harness preference (tighten-only). Empty — the configured
        # backend as a one-element list (byte-compatible).
        "implement": {
            "harnesses": [],
            # the capability manifest — the drivers this project onboarded (forge.yml form
            # of FORGE_AVAILABLE_DRIVERS, wins when both are set).
            "available_drivers": [],
        },
        # ADR-0018 §5: named numeric budget profiles — the forge.yml form of
        # FORGE_BUDGET_PROFILES (wins when both are set).
        "budget_profiles": {},
        # named write-policy profiles — the forge.yml form of FORGE_WRITE_PROFILES (wins
        # when both are set).
        "write_profiles": {},
        # per-project sensitive pipeline entrypoints — the forge.yml form of
        # FORGE_PIPELINE_ENTRYPOINTS (wins when both are set).
        "pipeline_entrypoints": {},
        "mcp_servers": {},
    }

    def __init__(self, path: str | Path = "forge.yml") -> None:
        # Deep copy: _DEFAULTS holds nested dicts, and _deep_merge below mutates them in
        # place — a shallow dict copy would leak overrides into the class-level defaults
        # (and thus into every later instance).
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
        """``implement.harnesses`` — the ordered driver list (ADR-0023 §1)."""
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
        """``implement.available_drivers`` — the capability manifest: the drivers this project onboarded."""
        return validate_available_drivers(self._data["implement"].get("available_drivers"))

    @property
    def mcp_servers(self) -> dict[str, Any]:
        return self._data["mcp_servers"]

    @property
    def budget_profiles(self) -> dict[str, dict[str, Any]]:
        """``budget_profiles`` — the named numeric run budgets (ADR-0018 §5)."""
        return validate_budget_profiles(self._data.get("budget_profiles"))

    @property
    def write_profiles(self) -> dict[str, dict[str, Any]]:
        """``write_profiles`` — the custom write-policy profiles."""
        return validate_write_profiles(self._data.get("write_profiles"))

    @property
    def pipeline_entrypoints(self) -> dict[str, list[str]]:
        """``pipeline_entrypoints`` — per-project sensitive pipeline paths."""
        return validate_pipeline_entrypoints(self._data.get("pipeline_entrypoints"))

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()  # type: ignore[call-arg]


def get_forge_config(path: str | Path = "forge.yml") -> ForgeConfig:
    """Return a ForgeConfig loaded from *path*."""
    return ForgeConfig(path)
