"""Validate the canonical frozen specification without importing the engine."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Any

from pydantic import ConfigDict, Field

from .canonical import canonical_json, digest
from .policy import ExecutionPolicy, PolicyValue

MAX_SPEC_BYTES = 1024 * 1024


class FrozenWriteScope(PolicyValue):
    """Transport view of the domain PathPolicy's exact serialized knobs."""

    name: Annotated[str, Field(min_length=1)]
    denied_paths: tuple[str, ...]
    denied_prefixes: tuple[str, ...]
    sensitive_paths: tuple[str, ...]
    denied_globs: tuple[str, ...]
    permitted_globs: tuple[str, ...]
    allowed_globs: tuple[str, ...]
    deny_lockfiles: bool
    require_special_approval: bool


class FrozenLimits(PolicyValue):
    usd: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    calls: Annotated[int, Field(ge=0)]
    tokens: Annotated[int, Field(ge=0)]
    wallclock_seconds: Annotated[int, Field(gt=0)]


class FrozenRunSpec(PolicyValue):
    """A consumer projection; the raw complete document remains digest-bound."""

    model_config = ConfigDict(extra="ignore")
    driver: Annotated[str, Field(min_length=1, max_length=64)]
    model: Annotated[str, Field(min_length=1, max_length=1024)]
    limits: FrozenLimits
    write_policy: FrozenWriteScope
    execution_policy: ExecutionPolicy


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in frozen specification")
        result[key] = value
    return result


def _finite_only(value: str) -> None:
    raise ValueError("non-finite number in frozen specification")


def frozen_specification(
    spec_json: str,
    *,
    spec_digest: str,
    plan_digest: str,
    base_sha: str,
    revision: int,
) -> FrozenRunSpec:
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
    return FrozenRunSpec.model_validate_json(raw)
