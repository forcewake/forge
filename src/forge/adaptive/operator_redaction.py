"""Recursive credential redaction for current operator projections."""

from __future__ import annotations

import re
from typing import Any

_REDACTED_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[-_]?key|credential|auth)$", re.IGNORECASE
)


_SECRET_VALUE_RE = re.compile(r"(Bearer\s+\S|sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|glpat-)")


CREDENTIAL_DOCUMENT_FIELDS: dict[str, frozenset[str]] = {
    "forge.project.credential-binding/1": frozenset(
        {
            "schema",
            "project_id",
            "provider",
            "credential_ref",
            "env_var",
            "bound_at",
            "bound_by",
            "revoked_at",
        }
    ),
    "forge.project.credential-binding/2": frozenset(
        {
            "schema",
            "subject",
            "provider",
            "credential_ref",
            "env_var",
            "bound_at",
            "bound_by",
            "revision",
            "project_id",
            "revoked_at",
        }
    ),
    "forge.project.dispatch-credential-proof/1": frozenset(
        {
            "schema",
            "project_id",
            "provider",
            "credential_ref",
            "env_var",
            "resolved_at",
            "bound_at",
            "bound_by",
        }
    ),
    "forge.project.dispatch-credential-proof/2": frozenset(
        {
            "schema",
            "subject",
            "provider",
            "credential_ref",
            "env_var",
            "binding_revision",
            "resolved_at",
            "bound_at",
            "bound_by",
            "resolver_identity",
            "resolved_version",
            "grant",
            "receipt",
            "attempt_generation",
        }
    ),
    "forge.credential.broker-receipt/1": frozenset(
        {
            "schema",
            "resolver_identity",
            "provider_route",
            "credential_ref",
            "env_var",
            "env_present",
            "resolved_version",
            "resolved_at",
        }
    ),
}


def _is_credential_document(value: Any) -> bool:
    """Only declared credential metadata may bypass credential-shaped key filtering."""
    if not isinstance(value, dict):
        return False
    declared = CREDENTIAL_DOCUMENT_FIELDS.get(str(value.get("schema") or ""))
    return declared is not None and set(value) <= set(declared)


def redact(value: Any) -> Any:
    """A known metadata schema never exempts string values from secret checks."""
    if isinstance(value, dict) and _is_credential_document(value):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if _REDACTED_KEY_RE.search(str(key)) and not _is_credential_document(item)
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        return "[redacted]"
    return value
