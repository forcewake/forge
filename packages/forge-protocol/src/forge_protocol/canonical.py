"""Canonical JSON bytes and content identities for the lane protocol."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_json(value: BaseModel | dict[str, Any]) -> bytes:
    document = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: BaseModel | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def approved_input_digest(
    *, brief_text: str, plan_digest: str, spec_digest: str, base_sha: str, revision: int
) -> str:
    return digest(
        {
            "brief_text": brief_text,
            "plan_digest": plan_digest,
            "spec_digest": spec_digest,
            "base_sha": base_sha,
            "revision": revision,
        }
    )
