"""Test-owned symbol/call policies; import ownership comes only from TOML."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
IMPORT_CONTRACTS = {
    contract["id"]: contract
    for contract in CONFIG["tool"]["importlinter"]["contracts"]
    if contract.get("id")
}


@dataclass(frozen=True)
class ImportBoundary:
    name: str
    owner_modules: tuple[str, ...]
    allowed_dependents: tuple[str, ...]


def _import_boundaries() -> tuple[ImportBoundary, ...]:
    return tuple(
        ImportBoundary(
            name=contract["id"].removeprefix("authority-"),
            owner_modules=tuple(contract["protected_modules"]),
            allowed_dependents=tuple(
                module
                for module in contract["allowed_importers"]
                if module not in contract["protected_modules"]
            ),
        )
        for contract in IMPORT_CONTRACTS.values()
        if contract.get("id", "").startswith("authority-")
    )


BOUNDARIES = _import_boundaries()


def boundary_by_name(name: str) -> ImportBoundary:
    return next(boundary for boundary in BOUNDARIES if boundary.name == name)


LEGACY_LOOKUP_CHAIN_MODULES: tuple[str, ...] = (
    "forge.adaptive.checkpoint_migration",
    "forge.adaptive.checkpoint_repository",
    "forge.api_checkpoint_channel",
    "forge.runs.revival",
)

LEGACY_LOOKUP_CONFINED_FUNCTION: str = "_legacy_http_lookup"

RESOLVE_REPOSITORY_CALLERS: tuple[str, ...] = (
    "forge.adaptive.checkpoint_migration",
    "forge.adaptive.checkpoint_repository",
    "forge.adaptive.revisions",
    "forge.adaptive.wiring",
    "forge.api_checkpoint_channel",
)

ATTEMPT_START_CONSTRUCTORS: tuple[str, ...] = (
    "forge.adaptive.composition_adoption",
    "forge.runs.composition",
)

COMPOSED_DISPATCH_ENTRIES: tuple[str, ...] = ("forge.runs.github_service",)

CONTINUATION_MODE_CONSTRUCTION_MODULES: tuple[str, ...] = (
    "forge.adaptive.compat_fixtures",
    "forge.adaptive.continuation",
)

MODE_VOCABULARY_HOME: str = "forge.runs.github_service"

GC_SWEEP_ENTRYPOINTS: tuple[str, ...] = ("_asweep_locked", "_sweep_locked")

GC_UNLINK_EXEMPT_RECEIVERS: tuple[str, ...] = (
    "os.unlink(tmp_name)",
    "self._pins_path(...).unlink()",
    "CheckpointGcJournal.clear path.unlink()",
)

REDEMPTION_ENDPOINT_MODULE: str = "forge.api_lane_control"

REDEMPTION_GRANT_LOADERS: tuple[str, ...] = (
    "_authorize_operation_grant",
    "persist_operation_grant",
)

REDEMPTION_REGISTRY_LOOKUPS: tuple[str, ...] = ("resolve_dispatch_credential",)

GRANT_PERSISTED_SURFACES: tuple[str, ...] = (
    "operation_grant_for_plan",
    "operation_grants_for_attempt",
    "merge_operation_grant",
    "CredentialOperationGrant",
    "validate_operation_grant_document",
)

APPROVED_INPUT_DISPATCH_MODULES: tuple[str, ...] = ("forge.runs.service",)

BRIEF_DISPATCH_VARIABLES: tuple[str, ...] = (
    "LANE_PLAN_DIGEST_VARIABLE",
    "LANE_BRIEF_ENVELOPE_DIGEST_VARIABLE",
)

LEGACY_CARRIER_DERIVATION_MODULES: tuple[str, ...] = ("forge.adaptive.credential_broker",)

LEGACY_CARRIER_FUNCTIONS: tuple[str, ...] = ("credential_secret_name", "credential_secret_segment")

BUDGET_AMENDMENT_APPLICANTS: tuple[str, ...] = (
    "forge.durable.budgets",
    "forge.runs.service",
    "forge.runs.github_service",
)

DEDUP_CACHE_PROBE_MODULES: tuple[str, ...] = ("forge.gateway.durable_ingress",)

LEGACY_EVENT_DEDUP_CALLERS: tuple[str, ...] = ("forge.gateway.router",)

ROUND_ADMISSION_CALLERS: tuple[str, ...] = ("forge.runs.round_admission", "forge.runs.service")

COMMAND_OUTCOME_JOURNALING_MODULE: str = "forge.runs.service"

COMMAND_OUTCOME_PROJECTION_MODULE: str = "forge.adaptive.operator_view"

CONTINUATION_PROVIDER_GAPS = (
    "The lane-resume dispatch contract is wired on GitHub and GitLab CE. "
    "Azure remains the honest gap: it carries no resume-mode selection. "
    "All three lanes share retry refusal semantics through runs.revival.retry_rejection."
)
