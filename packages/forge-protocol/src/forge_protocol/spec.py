"""Validate the canonical frozen specification without importing the engine."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .canonical import canonical_json, digest
from .policy import ExecutionPolicy

MAX_SPEC_BYTES = 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in frozen specification")
        result[key] = value
    return result


def _finite_only(value: str) -> None:
    raise ValueError("non-finite number in frozen specification")


def frozen_execution_policy(
    spec_json: str,
    *,
    spec_digest: str,
    plan_digest: str,
    base_sha: str,
    revision: int,
) -> ExecutionPolicy:
    """Refuse alternate bytes, unknown schemas and mismatched approved artifacts."""
    raw = spec_json.encode("utf-8")
    if len(raw) > MAX_SPEC_BYTES:
        raise ValueError("frozen specification exceeds the byte limit")
    if hashlib.sha256(raw).hexdigest() != spec_digest:
        raise ValueError("frozen specification digest mismatch")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_finite_only)
        canonical = canonical_json(document) if isinstance(document, dict) else None
    except RecursionError:
        raise ValueError("frozen specification nesting exceeds the decoder limit") from None
    if not isinstance(document, dict) or canonical != raw:
        raise ValueError("frozen specification must use canonical JSON bytes")
    if document.get("schema") != "forge.run-spec/2":
        raise ValueError("unsupported frozen specification schema")
    if document.get("base_sha") != base_sha or (
        type(document.get("revision")) is not int or document["revision"] != revision
    ):
        raise ValueError("frozen specification base or revision mismatch")
    plan = document.get("plan")
    if not isinstance(plan, dict) or digest(plan) != plan_digest:
        raise ValueError("frozen specification plan mismatch")
    if not isinstance(document.get("execution_policy"), dict):
        raise ValueError("frozen specification requires an explicit execution policy")
    return ExecutionPolicy.model_validate_json(canonical_json(document["execution_policy"]))
