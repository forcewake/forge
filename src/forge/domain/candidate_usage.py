"""Honest candidate usage receipts and deterministic receipt identities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

UsageCompleteness = Literal["exact", "aggregate", "unknown"]


@dataclass(frozen=True)
class HarnessUsage:
    """The F22-lite usage receipt parsed from ``candidate.meta.json``."""

    driver: str = ""
    model: str = ""
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    #: Anthropic ``cache_creation_input_tokens`` — a counter the OpenAI-compatible shapes
    #: never expose. Absent → unknown, never 0.
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    #: Informational only (``reasoning_tokens`` / ``thinking_tokens``): a breakdown inside
    #: the inclusive output on every shape forge sees — never added on top of anything.
    reasoning_tokens: int | None = None
    completeness: UsageCompleteness = "unknown"
    source: str = ""
    #: The lane's attempt identity (v2 meta ``attempt_id``, GitHub's own
    #: ``<run_id>:<run_attempt>``) — the second component of the receipt identity: a repair
    #: re-dispatch is a legitimately distinct receipt.
    attempt_id: str = ""
    #: The receipt's identity, sha256 over (run id, attempt id, normalized usage JSON)
    #: — :func:`usage_receipt_id`.
    receipt_id: str = ""
    #: The verbatim usage block as received (never normalized in place) — preserved on the
    #: ledger so spend stays reconstructable later.
    raw_json: str | None = None

    def __post_init__(self) -> None:
        if self.raw_json is not None:
            document = json.loads(self.raw_json)
            if not isinstance(document, dict):
                raise ValueError("raw usage must be a JSON object")
            object.__setattr__(self, "raw_json", _canonical_raw(document))

    def raw_document(self) -> dict[str, Any] | None:
        """Decode a fresh snapshot for persistence; callers cannot mutate the receipt."""
        if self.raw_json is None:
            return None
        document = json.loads(self.raw_json)
        if not isinstance(document, dict):
            raise ValueError("raw usage must be a JSON object")
        return document

    @classmethod
    def from_meta(
        cls, data: object, *, driver: str = "", model: str = "", attempt_id: str = ""
    ) -> HarnessUsage:
        """Build a receipt from the meta JSON's ``usage`` object (defensive)."""
        if not isinstance(data, dict):
            return cls(driver=driver, model=model, attempt_id=attempt_id)

        def _token(key: str) -> int | None:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        input_tokens = _token("input_tokens")
        cached = _token("cached_input_tokens")
        cache_write = _token("cache_write_tokens")
        output_tokens = _token("output_tokens")
        reasoning = _token("reasoning_tokens")
        completeness = data.get("completeness")
        if completeness not in ("exact", "aggregate"):
            # Receipts parsed from the event stream are sums of per-turn counters — the
            # honest default is "aggregate", never fabricated precision.
            completeness = "aggregate"
        if all(
            value is None for value in (input_tokens, cached, cache_write, output_tokens, reasoning)
        ):
            completeness = "unknown"
        if completeness != "unknown" and not any(
            (input_tokens, cached, cache_write, output_tokens, reasoning)
        ):
            input_tokens = cached = cache_write = output_tokens = reasoning = None
            completeness = "unknown"
        return cls(
            driver=str(data.get("driver") or driver or ""),
            model=str(data.get("model") or model or ""),
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            cache_write_tokens=cache_write,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning,
            completeness=completeness,  # type: ignore[arg-type]
            source=str(data.get("source") or ""),
            attempt_id=str(data.get("attempt_id") or attempt_id or ""),
            receipt_id=str(data.get("receipt_id") or ""),
            raw_json=_canonical_raw(data),
        )

    @property
    def anthropic_shaped(self) -> bool:
        """Whether the counters are DISJOINT (Anthropic-compatible semantics)."""
        return self.cache_write_tokens is not None or self.driver == "claude-code"

    @property
    def total_known_tokens(self) -> int | None:
        """The receipt's KNOWN token total, per the normalization table."""
        parts: tuple[int | None, ...]
        if self.anthropic_shaped:
            parts = (
                self.input_tokens,
                self.cached_input_tokens,
                self.cache_write_tokens,
                self.output_tokens,
            )
        else:
            parts = (self.input_tokens, self.output_tokens)
        known = [part for part in parts if isinstance(part, int)]
        if not known:
            return None
        return sum(known)


def usage_document(usage: HarnessUsage) -> dict[str, Any]:
    """The receipt's canonical normalized counters — the identity material."""
    return {
        "driver": usage.driver,
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "completeness": usage.completeness,
        "source": usage.source,
    }


def usage_receipt_id(run_id: str, attempt_id: str, usage: HarnessUsage) -> str:
    """The receipt's identity: sha256 over (run id, attempt id, usage JSON)."""
    document = json.dumps(usage_document(usage), sort_keys=True, separators=(",", ":"))
    material = f"{run_id}|{attempt_id}|{document}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _canonical_raw(document: dict[str, Any]) -> str:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
