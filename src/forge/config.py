from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Bounded automatic revival: how many times a run killed by a TRANSIENT
    # failure (dispatch 5xx/network/timeout, rate limit, runner startup) is
    # re-dispatched on the same branch before it parks blocked with the
    # reason. 0 disables auto-revive; the operator's /retry always stays
    # available. Fatal failures never auto-revive.
    FORGE_RUN_AUTO_REVIVE_LIMIT: int = 2

    # ADR-0015 pluggable implementer backend: "builtin" (LLM -> ChangeSet ->
    # Commits API) or "ci_harness" (optionally "ci_harness:claude-code") —
    # a coding harness executing as a job in the target project's CI.
    FORGE_IMPLEMENTER_BACKEND: str = "builtin"

    # ADR-0015 harness budgets: run-level wall-clock timeout for a harness
    # job (enforced durably by the controller; GitLab's own maximum_timeout
    # is the first line of defence) and the model passed to the harness job
    # as FORGE_HARNESS_MODEL.
    FORGE_HARNESS_TIMEOUT_SECONDS: int = 1800
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

    # Security findings (v0.7): when true, a /security verdict of "false
    # positive" on a GITHUB finding also dismisses the remote alert via the
    # alert PATCH APIs (research §4.2 enums, justification as the audit
    # comment). Default false — forge records the verdict in its own triage
    # store and leaves the provider alert untouched.
    FORGE_SECURITY_REMOTE_DISMISS: bool = False

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
        },
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
    def mcp_servers(self) -> dict[str, Any]:
        return self._data["mcp_servers"]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()  # type: ignore[call-arg]


def get_forge_config(path: str | Path = "forge.yml") -> ForgeConfig:
    """Return a ForgeConfig loaded from *path*."""
    return ForgeConfig(path)
