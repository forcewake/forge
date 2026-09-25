"""R36-17 — the CONNECTED delivery measurement path: identity-linked ledgers.

``delivery_metrics`` (R28-22/R32-20) reconciles ONE run's recorded facts;
this module is the join layer the review asked for next: a
:class:`MeasurementLinker` that chains records by IDENTITY — work id →
execution attempt id → model-call/receipt identity → provider route — over
the durable record shapes the repo already produces (the evidence
``attempts`` list, the ``.forge/usage.json``-shaped usage receipts riding
candidate meta, the ``llm_calls`` usage-ledger rows, and typed latency
spans), and emits a :class:`DeliveryLedger` for the cohort: every attempt
(accepted / rejected / cancelled / superseded / abandoned) with its
receipts, its timing spans and its known/unknown usage.

The honesty rules, all pinned by ``tests/test_delivery_measurement.py``:

- **Honest unknowns** — every total is either EXACT (every expected record
  present, unconflicted, counters monotone) or UNKNOWN with a
  ``known_cost_lower_bound`` and a ``coverage`` fraction (received receipts ÷
  expected-by-policy). An unknown EARLIER receipt stays unknown after a
  later known one: the fold never heals a gap by adding around it. Cache
  reads are never double-counted in inclusive input (OpenAI-shaped
  counters already include the cache; Anthropic-shaped counters are
  disjoint and summed), and reasoning counters carry their
  provider-convention notes.
- **Population separation** — latency spans are typed
  ``model | tool | queue | verification | human_wait | restore_collection``
  and scoped by ORIGIN (``planner`` vs ``harness`` vs ``operator``); every
  aggregate records its ``population_identity``. Output tokens divided by
  total-request latency can NEVER be labeled decode throughput:
  :data:`RATE_LABEL_SPACE` admits exactly one rate label and exactly the
  model-span populations as its denominator
  (:class:`RateLabelError` otherwise).
- **Programme vs accepted-unit cost** — ``programme_cost`` folds EVERY
  attempt of EVERY work including rejected spend; ``accepted_unit_cost``
  is the accepted work's OWN attempt chain (planner calls included, other
  works' attempts never). A rejected task's budget stays in the programme
  denominator.
- **Order invariance** — the linker sorts by identity and folds by content
  (never by arrival order), so reordering attempts, receipts, calls and
  spans changes no aggregate; :func:`replay_report` reproduces a
  byte-identical aggregation from the stored ledger document with no model
  calls (AT-12).
- **Conflicts surface, never average** — duplicate receipt delivery
  collapses by identity; two sources (or two claims) disagreeing over one
  identity become :class:`ReceiptConflict` / :class:`DuplicateCallIdentity`
  rows that degrade the exact total to unknown while the lower bound keeps
  its honest minimum; a same-source cumulative counter that DECREASES is a
  :class:`CounterReset`, not a negative delta.

:func:`trace_accepted_task` walks one work from its cost total down to
every INCLUDED receipt and every EXCLUDED/unknown record — the AT-12
trace. :func:`ledger_records_from_delivery_metrics` is the connected path
from ``delivery_metrics_for_run``'s reconciled output into the linker.
Everything here is pure: no database, no model calls, mapping-shaped
inputs only.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from forge.adaptive.delivery_metrics import (
    DeliveryMetrics,
    attempt_records_from_evidence as _attempt_records_from_evidence,
)

__all__ = [
    "ATTEMPT_OUTCOMES",
    "CounterReset",
    "DeliveryLedger",
    "DuplicateCallIdentity",
    "LATENCY_SPAN_TYPES",
    "MEASUREMENT_SCHEMA",
    "MeasurementLinker",
    "ProviderRoute",
    "RATE_LABEL_SPACE",
    "RateLabelError",
    "ReceiptConflict",
    "SPAN_ORIGINS",
    "UsageLedger",
    "WorkLedger",
    "attempt_records_from_evidence",
    "billing_comparison",
    "build_report",
    "ledger_records_from_delivery_metrics",
    "ledger_records_from_ingested_usage",
    "measured_rate",
    "replay_report",
    "trace_accepted_task",
]

#: The versioned stamp of every document this module emits (issue R36-17:
#: comparisons must not silently change meaning underneath the reader).
MEASUREMENT_SCHEMA = "forge.delivery.measurement/1"

#: The terminal outcome space a WORK (and, reused, an execution attempt)
#: can carry. Empty string means UNKNOWN — never coerced to a neighbor.
ATTEMPT_OUTCOMES = ("accepted", "rejected", "cancelled", "superseded", "abandoned")

#: The latency span types (R36-17 scope item 2). A span belongs to exactly
#: one; nothing is ever typed "total request" — that is a POPULATION, not
#: a span, and populations never mix types (see :func:`build_report`).
LATENCY_SPAN_TYPES = (
    "model",
    "tool",
    "queue",
    "verification",
    "human_wait",
    "restore_collection",
)

#: Where a span was measured: ``planner`` (forge-side episode breakdowns),
#: ``harness`` (the execution lane: driver turns, llm_calls durations, CI),
#: ``operator`` (human-side gate observations). Planner-only time never
#: mixes with harness-wide time — the AT-12 rule.
SPAN_ORIGINS = ("planner", "harness", "operator")

#: The default origin per span type (used when a span record carries none).
_DEFAULT_SPAN_ORIGIN: dict[str, str] = {
    "model": "harness",
    "tool": "harness",
    "queue": "harness",
    "verification": "harness",
    "human_wait": "operator",
    "restore_collection": "harness",
}

#: The ONLY sanctioned rate-label space (AT-12): the single throughput the
#: evidence supports is decode output over MODEL-span service seconds.
#: The value is the set of population keys allowed as denominator;
#: ``model:harness`` and ``model:planner`` are the only members — a mixed
#: population (model + queue + human_wait + …) has no single span-type key
#: and therefore no membership, and :func:`measured_rate` enforces exactly
#: that.
RATE_LABEL_SPACE: Mapping[str, frozenset[str]] = {
    "decode_output_tokens_per_second": frozenset({"model:harness", "model:planner"}),
}

#: The provider-convention notes every usage fold carries verbatim (the
#: "explicit definitions" the review demanded, recorded on the artifact).
USAGE_CONVENTIONS: tuple[str, ...] = (
    "input_tokens_inclusive: OpenAI-compatible shapes count cached tokens"
    " INSIDE input_tokens (cached is a breakdown, never added on top);"
    " Anthropic-compatible shapes carry DISJOINT counters and inclusive"
    " input is input + cache_read + cache_write",
    "cached_input_tokens / cache_write_tokens: separate sums, never folded"
    " into an OpenAI-shaped inclusive input",
    "reasoning_tokens: a breakdown inside the inclusive OUTPUT on every"
    " shape forge sees (OpenAI reasoning_tokens / Anthropic thinking) —"
    " recorded, never added on top of output",
    "unknown counters stay unknown: an absent vendor field contributes"
    " nothing to exact totals and is counted in the unknown columns, never"
    " zero-filled",
)


class RateLabelError(ValueError):
    """A rate was asked for under a label its population cannot support.

    The guarded construction: output tokens ÷ total-request latency (a
    population mixing model, queue, tool, verification and human-wait
    time) presented as "decode throughput". The label space admits
    exactly :data:`RATE_LABEL_SPACE`; everything else raises.
    """


# ----------------------------------------------------------------------
# Small parsing helpers (unknown stays unknown, never zero)
# ----------------------------------------------------------------------


def _nn_int(value: Any) -> int | None:
    """A non-negative int, or None — never a bool, never a zero-fill."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nn_float(value: Any) -> float | None:
    """A non-negative finite float, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or math.isnan(number) or math.isinf(number):
        return None
    return number


def _text(value: Any) -> str:
    return str(value or "").strip()


def _digest(material: str) -> str:
    """A short stable digest — the identity of content-addressed records."""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _round6(value: float | None) -> float | None:
    """Round for OUTPUT only — stored documents keep full precision so a
    replay re-aggregates byte-identically instead of re-rounding dust."""
    return round(value, 6) if value is not None else None


def _parse_dt(value: Any) -> datetime | None:
    """A best-effort ISO-8601 datetime for span math (None when absent)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# ----------------------------------------------------------------------
# The linked record rows
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderRoute:
    """The provider route one call/receipt was served through.

    ``provider`` is the route's owner (the driver id for harness receipts,
    the provider name for ``llm_calls`` rows); ``model`` the model id.
    Routes are the leaf of the join chain — usage aggregates name them so
    spend stays attributable after the fold.
    """

    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider

    def to_json(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}


@dataclass(frozen=True)
class ReceiptClaim:
    """One usage receipt's claim, keyed by its own identity.

    The shape is the ``.forge/usage.json`` / candidate-meta ``usage`` block
    (:class:`forge.runs.candidate.HarnessUsage`) plus the cost figure the
    evidence attempts carry. Identity is the receipt id when the record
    brings one (the sha256 :func:`usage_receipt_id` computes) and a content
    digest otherwise, so a re-delivered identical receipt collapses no
    matter how it arrived. R38-09 adds two additive fields ingested lane
    receipts carry: ``cost_basis`` (``provider-reported`` /
    ``estimated`` / ``billing-reconciliation`` — the SDK's own meter vs a
    versioned card vs a billing export) and ``segment`` (the attribution
    segment over route + route version + rate card; a change writes NEW
    rows, history is never rewritten).
    """

    receipt_id: str
    work_id: str
    attempt_id: str
    source: str
    route: ProviderRoute
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    completeness: str = "unknown"
    cost_basis: str = ""
    segment: str = ""

    @property
    def anthropic_shaped(self) -> bool:
        """Disjoint counters (the R23 research-table tells: the cache-write
        column only an Anthropic-compatible endpoint exposes, or a claude
        driver — the scripted ``claude-code`` batch lane AND the
        ``claude-sdk-lane`` interactive lane both always talk to one)."""
        return self.cache_write_tokens is not None or "claude" in self.route.provider.lower()

    @property
    def input_tokens_inclusive(self) -> int | None:
        """The receipt's INCLUSIVE input under its own shape's convention.

        Anthropic-shaped: ``input + cache_read + cache_write`` (disjoint
        counters sum). OpenAI-shaped: ``input`` alone — the cache rides
        inside it and is NEVER added on top (the double-count guard).
        """
        if self.anthropic_shaped:
            parts = (self.input_tokens, self.cached_input_tokens, self.cache_write_tokens)
            known = [part for part in parts if part is not None]
            return sum(known) if known else None
        return self.input_tokens

    @property
    def known_total_tokens(self) -> int:
        """The claim's known magnitude — the same-source cumulative key."""
        inclusive = self.input_tokens_inclusive
        total = inclusive if inclusive is not None else 0
        return total + (self.output_tokens if self.output_tokens is not None else 0)

    def to_json(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "source": self.source,
            "route": self.route.to_json(),
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "completeness": self.completeness,
            "cost_basis": self.cost_basis,
            "segment": self.segment,
        }


@dataclass(frozen=True)
class LatencySpan:
    """One measured time window, typed by population.

    ``span_type`` is one of :data:`LATENCY_SPAN_TYPES`, ``origin`` one of
    :data:`SPAN_ORIGINS`; a span without a measurable duration is UNKNOWN
    (``seconds is None``) and counts in its population's unknown column —
    never zero.
    """

    span_id: str
    work_id: str
    attempt_id: str
    span_type: str
    origin: str
    seconds: float | None

    @property
    def population(self) -> str:
        """The population key — span type scoped by origin, never mixed."""
        return f"{self.span_type}:{self.origin}"

    def to_json(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "span_type": self.span_type,
            "origin": self.origin,
            "seconds": self.seconds,
        }


@dataclass(frozen=True)
class UsageLedger:
    """A fold of receipts (and, uncovered, call telemetry) — bound-first.

    ``cost_usd`` / the token totals are EXACT only when every expected
    receipt contributed fully; otherwise ``None`` with the lower bounds
    and the counts that explain the gap. Zero telemetry is a lower bound
    of 0.0 plus the unknown notes — never an exact zero total.
    """

    receipts_received: int = 0
    receipts_expected: int = 0
    exact: bool = False
    cost_usd: float | None = None
    known_cost_lower_bound_usd: float = 0.0
    input_tokens_inclusive: int | None = None
    input_tokens_inclusive_lower_bound: int = 0
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    output_tokens_lower_bound: int = 0
    reasoning_tokens: int | None = None
    completeness: str = "unknown"
    call_derived: bool = False
    notes: tuple[str, ...] = ()

    @property
    def is_vacuous(self) -> bool:
        """Whether this block claims NOTHING at all (fold-skippable)."""
        return (
            self.receipts_expected == 0
            and self.receipts_received == 0
            and not self.call_derived
            and not self.notes
            and self.cost_usd is None
            and self.known_cost_lower_bound_usd == 0.0
            and self.input_tokens_inclusive is None
            and self.input_tokens_inclusive_lower_bound == 0
            and self.output_tokens is None
            and self.output_tokens_lower_bound == 0
        )

    @property
    def coverage(self) -> float | None:
        """Received receipts ÷ expected-by-policy (one per attempt).

        ``None`` when nothing was expected — a coverage fraction over an
        empty policy would be a fabricated 1.0.
        """
        if self.receipts_expected <= 0:
            return None
        return self.receipts_received / self.receipts_expected

    def to_json(self) -> dict[str, Any]:
        return {
            "receipts_received": self.receipts_received,
            "receipts_expected": self.receipts_expected,
            "exact": self.exact,
            "cost_usd": self.cost_usd,
            "known_cost_lower_bound_usd": self.known_cost_lower_bound_usd,
            "input_tokens_inclusive": self.input_tokens_inclusive,
            "input_tokens_inclusive_lower_bound": self.input_tokens_inclusive_lower_bound,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "output_tokens_lower_bound": self.output_tokens_lower_bound,
            "reasoning_tokens": self.reasoning_tokens,
            "completeness": self.completeness,
            "call_derived": self.call_derived,
            "coverage": self.coverage,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class AttemptLedger:
    """One execution attempt inside its work — the join chain's middle link."""

    attempt_id: str
    work_id: str
    outcome: str
    sources: tuple[str, ...] = ()
    routes: tuple[ProviderRoute, ...] = ()
    receipts: tuple[ReceiptClaim, ...] = ()
    call_ids: tuple[str, ...] = ()
    spans: tuple[LatencySpan, ...] = ()
    usage: UsageLedger = field(default_factory=UsageLedger)

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "work_id": self.work_id,
            "outcome": self.outcome,
            "sources": list(self.sources),
            "routes": [route.to_json() for route in self.routes],
            "receipts": [receipt.to_json() for receipt in self.receipts],
            "call_ids": list(self.call_ids),
            "spans": [span.to_json() for span in self.spans],
            "usage": self.usage.to_json(),
        }


@dataclass(frozen=True)
class WorkLedger:
    """One work's ledger: every attempt kept, its own-chain usage folded.

    ``own_usage`` is the accepted-unit basis — this work's attempts plus
    its work-level (planner) calls, never another work's spend. The
    model/harness version and acceptance contract ride along so two
    reports cannot silently compare across a changed quality bar.
    """

    work_id: str
    outcome: str
    attempts: tuple[AttemptLedger, ...] = ()
    planner_call_ids: tuple[str, ...] = ()
    planner_usage: UsageLedger = field(default_factory=UsageLedger)
    own_usage: UsageLedger = field(default_factory=UsageLedger)
    model_version: str = ""
    harness_version: str = ""
    acceptance_contract: str = ""
    orphan_receipt_ids: tuple[str, ...] = ()
    fully_linked: bool = False
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "outcome": self.outcome,
            "attempts": [attempt.to_json() for attempt in self.attempts],
            "planner_call_ids": list(self.planner_call_ids),
            "planner_usage": self.planner_usage.to_json(),
            "own_usage": self.own_usage.to_json(),
            "model_version": self.model_version,
            "harness_version": self.harness_version,
            "acceptance_contract": self.acceptance_contract,
            "orphan_receipt_ids": list(self.orphan_receipt_ids),
            "fully_linked": self.fully_linked,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ReceiptConflict:
    """Two claims disagreeing over ONE identity — surfaced, never averaged.

    ``identity_kind`` is ``receipt`` or ``call``; the exact side degrades
    to unknown while the lower bound keeps its honest minimum.
    """

    identity_kind: str
    identity: str
    attempt_id: str
    source_a: str
    source_b: str
    fields: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "identity_kind": self.identity_kind,
            "identity": self.identity,
            "attempt_id": self.attempt_id,
            "source_a": self.source_a,
            "source_b": self.source_b,
            "fields": list(self.fields),
        }


@dataclass(frozen=True)
class DuplicateCallIdentity:
    """One model-call id delivered under more than one attribution."""

    call_id: str
    attributions: tuple[str, ...] = ()
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "attributions": list(self.attributions),
            "note": self.note,
        }


@dataclass(frozen=True)
class CounterReset:
    """A same-source cumulative counter that DECREASED — a reset, not a delta."""

    attempt_id: str
    source: str
    field: str
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "source": self.source,
            "field": self.field,
            "note": self.note,
        }


@dataclass(frozen=True)
class DeliveryLedger:
    """The linker's output: works joined by identity, conflicts named.

    Every collection is sorted by identity at link time, so the document
    form (:meth:`to_document`) and every aggregate derived from it are
    byte-stable under any input ordering.
    """

    works: tuple[WorkLedger, ...] = ()
    conflicts: tuple[ReceiptConflict, ...] = ()
    duplicate_calls: tuple[DuplicateCallIdentity, ...] = ()
    counter_resets: tuple[CounterReset, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def schema(self) -> str:
        return MEASUREMENT_SCHEMA

    def work_by_id(self, work_id: str) -> WorkLedger | None:
        for work in self.works:
            if work.work_id == work_id:
                return work
        return None

    def to_document(self) -> dict[str, Any]:
        """The stored artifact: full precision, canonical-keyed, replayable."""
        return {
            "schema": MEASUREMENT_SCHEMA,
            "works": [work.to_json() for work in self.works],
            "conflicts": [row.to_json() for row in self.conflicts],
            "duplicate_calls": [row.to_json() for row in self.duplicate_calls],
            "counter_resets": [row.to_json() for row in self.counter_resets],
            "notes": list(self.notes),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> DeliveryLedger:
        """Rebuild the ledger from its stored document — lossless."""
        stamp = _text(document.get("schema")) or MEASUREMENT_SCHEMA
        if stamp != MEASUREMENT_SCHEMA:
            raise ValueError(
                f"ledger document carries schema {stamp!r}, expected {MEASUREMENT_SCHEMA!r}"
            )

        def _usage(block: Any) -> UsageLedger:
            if not isinstance(block, Mapping):
                return UsageLedger()
            return UsageLedger(
                receipts_received=int(block.get("receipts_received") or 0),
                receipts_expected=int(block.get("receipts_expected") or 0),
                exact=bool(block.get("exact")),
                cost_usd=_nn_float(block.get("cost_usd")),
                known_cost_lower_bound_usd=_nn_float(block.get("known_cost_lower_bound_usd"))
                or 0.0,
                input_tokens_inclusive=_nn_int(block.get("input_tokens_inclusive")),
                input_tokens_inclusive_lower_bound=_nn_int(
                    block.get("input_tokens_inclusive_lower_bound")
                )
                or 0,
                cached_input_tokens=_nn_int(block.get("cached_input_tokens")),
                cache_write_tokens=_nn_int(block.get("cache_write_tokens")),
                output_tokens=_nn_int(block.get("output_tokens")),
                output_tokens_lower_bound=_nn_int(block.get("output_tokens_lower_bound")) or 0,
                reasoning_tokens=_nn_int(block.get("reasoning_tokens")),
                completeness=_text(block.get("completeness")) or "unknown",
                call_derived=bool(block.get("call_derived")),
                notes=tuple(str(note) for note in block.get("notes") or ()),
            )

        def _route(block: Any) -> ProviderRoute:
            if not isinstance(block, Mapping):
                return ProviderRoute("", "")
            return ProviderRoute(
                provider=_text(block.get("provider")), model=_text(block.get("model"))
            )

        def _receipt(block: Mapping[str, Any]) -> ReceiptClaim:
            route = block.get("route")
            return ReceiptClaim(
                receipt_id=_text(block.get("receipt_id")),
                work_id=_text(block.get("work_id")),
                attempt_id=_text(block.get("attempt_id")),
                source=_text(block.get("source")),
                route=_route(route) if isinstance(route, Mapping) else ProviderRoute("", ""),
                input_tokens=_nn_int(block.get("input_tokens")),
                cached_input_tokens=_nn_int(block.get("cached_input_tokens")),
                cache_write_tokens=_nn_int(block.get("cache_write_tokens")),
                output_tokens=_nn_int(block.get("output_tokens")),
                reasoning_tokens=_nn_int(block.get("reasoning_tokens")),
                cost_usd=_nn_float(block.get("cost_usd")),
                completeness=_text(block.get("completeness")) or "unknown",
                cost_basis=_text(block.get("cost_basis")),
                segment=_text(block.get("segment")),
            )

        def _span(block: Mapping[str, Any]) -> LatencySpan:
            return LatencySpan(
                span_id=_text(block.get("span_id")),
                work_id=_text(block.get("work_id")),
                attempt_id=_text(block.get("attempt_id")),
                span_type=_text(block.get("span_type")),
                origin=_text(block.get("origin")),
                seconds=_nn_float(block.get("seconds")),
            )

        def _attempt(block: Mapping[str, Any]) -> AttemptLedger:
            routes = block.get("routes")
            receipts = block.get("receipts")
            spans = block.get("spans")
            return AttemptLedger(
                attempt_id=_text(block.get("attempt_id")),
                work_id=_text(block.get("work_id")),
                outcome=_text(block.get("outcome")),
                sources=tuple(_text(source) for source in block.get("sources") or ()),
                routes=tuple(_route(r) for r in routes if isinstance(r, Mapping))
                if isinstance(routes, list)
                else (),
                receipts=tuple(_receipt(r) for r in receipts if isinstance(r, Mapping))
                if isinstance(receipts, list)
                else (),
                call_ids=tuple(_text(call) for call in block.get("call_ids") or ()),
                spans=tuple(_span(s) for s in spans if isinstance(s, Mapping))
                if isinstance(spans, list)
                else (),
                usage=_usage(block.get("usage")),
            )

        def _work(block: Mapping[str, Any]) -> WorkLedger:
            attempts = block.get("attempts")
            return WorkLedger(
                work_id=_text(block.get("work_id")),
                outcome=_text(block.get("outcome")),
                attempts=tuple(_attempt(a) for a in attempts if isinstance(a, Mapping))
                if isinstance(attempts, list)
                else (),
                planner_call_ids=tuple(_text(call) for call in block.get("planner_call_ids") or ()),
                planner_usage=_usage(block.get("planner_usage")),
                own_usage=_usage(block.get("own_usage")),
                model_version=_text(block.get("model_version")),
                harness_version=_text(block.get("harness_version")),
                acceptance_contract=_text(block.get("acceptance_contract")),
                orphan_receipt_ids=tuple(
                    _text(receipt_id) for receipt_id in block.get("orphan_receipt_ids") or ()
                ),
                fully_linked=bool(block.get("fully_linked")),
                notes=tuple(str(note) for note in block.get("notes") or ()),
            )

        works_raw = document.get("works")
        conflicts_raw = document.get("conflicts")
        duplicates_raw = document.get("duplicate_calls")
        resets_raw = document.get("counter_resets")
        return cls(
            works=tuple(_work(w) for w in works_raw if isinstance(w, Mapping))
            if isinstance(works_raw, list)
            else (),
            conflicts=tuple(
                ReceiptConflict(
                    identity_kind=_text(row.get("identity_kind")),
                    identity=_text(row.get("identity")),
                    attempt_id=_text(row.get("attempt_id")),
                    source_a=_text(row.get("source_a")),
                    source_b=_text(row.get("source_b")),
                    fields=tuple(_text(name) for name in row.get("fields") or ()),
                )
                for row in conflicts_raw
                if isinstance(row, Mapping)
            )
            if isinstance(conflicts_raw, list)
            else (),
            duplicate_calls=tuple(
                DuplicateCallIdentity(
                    call_id=_text(row.get("call_id")),
                    attributions=tuple(_text(item) for item in row.get("attributions") or ()),
                    note=_text(row.get("note")),
                )
                for row in duplicates_raw
                if isinstance(row, Mapping)
            )
            if isinstance(duplicates_raw, list)
            else (),
            counter_resets=tuple(
                CounterReset(
                    attempt_id=_text(row.get("attempt_id")),
                    source=_text(row.get("source")),
                    field=_text(row.get("field")),
                    note=_text(row.get("note")),
                )
                for row in resets_raw
                if isinstance(row, Mapping)
            )
            if isinstance(resets_raw, list)
            else (),
            notes=tuple(str(note) for note in document.get("notes") or ()),
        )


# ----------------------------------------------------------------------
# The receipt fold — same-source collapse, resets, cross-source conflicts
# ----------------------------------------------------------------------

#: The receipt fields a cross-source disagreement is reported over.
_COMPARED_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cost_usd",
)


def _fold_receipts(
    attempt_key: str, claims: Sequence[ReceiptClaim]
) -> tuple[UsageLedger, list[ReceiptConflict], list[CounterReset]]:
    """Fold one attempt's receipt claims into a :class:`UsageLedger`.

    Same source: the claims collapse to the GREATEST cumulative claim
    (content-keyed, never arrival-keyed — order invariance) and any
    individual counter that DECREASED along the way is a
    :class:`CounterReset`. Cross source: the surviving per-source claims
    must agree on every field both name, or the pair becomes a
    :class:`ReceiptConflict` and the exact side degrades to unknown with
    the lower bound keeping the honest MINIMUM cost (a bound, never an
    average and never a silent pick). Policy expects ONE receipt per
    attempt, so ``receipts_received`` is 1 the moment any claim arrived.
    A claim that arrived ``partial`` (a streamed artifact the lane wrote
    before its turn ended — R38-09) keeps its counters and cost as a
    LOWER BOUND only: the exact total stays unknown until the final
    receipt reconciles it, never zero.
    """
    conflicts: list[ReceiptConflict] = []
    resets: list[CounterReset] = []
    notes: list[str] = []

    by_source: dict[str, list[ReceiptClaim]] = {}
    for claim in claims:
        by_source.setdefault(claim.source, []).append(claim)

    winners: list[ReceiptClaim] = []
    for source in sorted(by_source):
        group = sorted(
            by_source[source],
            key=lambda claim: (
                claim.known_total_tokens,
                claim.cost_usd if claim.cost_usd is not None else -1.0,
                claim.receipt_id,
            ),
        )
        winner = group[-1]
        winners.append(winner)
        if len(group) > 1:
            distinct_claims = {
                (
                    claim.input_tokens,
                    claim.cached_input_tokens,
                    claim.cache_write_tokens,
                    claim.output_tokens,
                    claim.reasoning_tokens,
                    claim.cost_usd,
                )
                for claim in group
            }
            if len(distinct_claims) == 1:
                notes.append(
                    f"attempt {attempt_key}: source {source} re-delivered its receipt"
                    f" {len(group)} times — collapsed to one by identity, never summed"
                )
            else:
                notes.append(
                    f"attempt {attempt_key}: source {source} replayed a cumulative"
                    f" receipt {len(group)} times — collapsed to the cumulative claim"
                    f" {winner.receipt_id}, never summed"
                )
        for other in group[:-1]:
            for name in _COMPARED_FIELDS:
                mine = getattr(winner, name)
                theirs = getattr(other, name)
                if mine is not None and theirs is not None and theirs > mine:
                    resets.append(
                        CounterReset(
                            attempt_id=attempt_key,
                            source=source,
                            field=name,
                            note=(
                                f"attempt {attempt_key}: source {source} cumulative"
                                f" {name} decreased {theirs} → {mine} — a counter"
                                " reset, not a negative delta; exact usage unknown"
                            ),
                        )
                    )

    exact = not resets
    # A streamed partial never certifies an exact total: its counters are
    # a lower bound until the final receipt reconciles the identity (the
    # R38-09 streaming contract — partials recover, remainders never
    # become zero).
    partials = [claim for claim in winners if claim.completeness == "partial"]
    if partials:
        exact = False
        notes.append(
            f"attempt {attempt_key}: {len(partials)} streamed partial receipt(s)"
            " — counters and cost are a lower bound, the exact total stays"
            " unknown until the final state reconciles it (R38-09)"
        )
    for index, left in enumerate(winners):
        for right in winners[index + 1 :]:
            disagreed = [
                name
                for name in _COMPARED_FIELDS
                if getattr(left, name) is not None
                and getattr(right, name) is not None
                and getattr(left, name) != getattr(right, name)
            ]
            if disagreed:
                conflicts.append(
                    ReceiptConflict(
                        identity_kind="receipt",
                        identity=left.receipt_id,
                        attempt_id=attempt_key,
                        source_a=left.source,
                        source_b=right.source,
                        fields=tuple(disagreed),
                    )
                )
                exact = False
                notes.append(
                    f"attempt {attempt_key}: receipts {left.receipt_id} ({left.source})"
                    f" and {right.receipt_id} ({right.source}) disagree on"
                    f" {', '.join(disagreed)} — the exact value is unknown, never"
                    " averaged"
                )

    cost_claims = [claim.cost_usd for claim in winners if claim.cost_usd is not None]
    inclusive_claims = [
        claim.input_tokens_inclusive
        for claim in winners
        if claim.input_tokens_inclusive is not None
    ]
    output_claims = [claim.output_tokens for claim in winners if claim.output_tokens is not None]
    cached_claims = [
        claim.cached_input_tokens for claim in winners if claim.cached_input_tokens is not None
    ]
    write_claims = [
        claim.cache_write_tokens for claim in winners if claim.cache_write_tokens is not None
    ]
    reasoning_claims = [
        claim.reasoning_tokens for claim in winners if claim.reasoning_tokens is not None
    ]

    def every_winner(values: list[Any]) -> bool:
        # No winners at all claims nothing — an empty fold is unknown,
        # never a zero-filled total.
        return bool(winners) and len(values) == len(winners)

    usage = UsageLedger(
        receipts_received=1 if claims else 0,
        receipts_expected=1,
        exact=exact,
        cost_usd=sum(cost_claims) if (exact and every_winner(cost_claims)) else None,
        known_cost_lower_bound_usd=min(cost_claims) if cost_claims else 0.0,
        input_tokens_inclusive=(
            sum(inclusive_claims) if (exact and every_winner(inclusive_claims)) else None
        ),
        input_tokens_inclusive_lower_bound=sum(inclusive_claims) if inclusive_claims else 0,
        cached_input_tokens=cached_claims[0] if (exact and every_winner(cached_claims)) else None,
        cache_write_tokens=write_claims[0] if (exact and every_winner(write_claims)) else None,
        output_tokens=sum(output_claims) if (exact and every_winner(output_claims)) else None,
        output_tokens_lower_bound=sum(output_claims) if output_claims else 0,
        reasoning_tokens=(
            reasoning_claims[0] if (exact and every_winner(reasoning_claims)) else None
        ),
        completeness=(
            "exact"
            if winners and all(claim.completeness == "exact" for claim in winners)
            else ("aggregate" if winners else "unknown")
        ),
        notes=tuple(notes),
    )
    return usage, conflicts, resets


def _fold_calls(
    level_key: str, calls: Sequence[Mapping[str, Any]], *, expected: int
) -> UsageLedger:
    """Fold call-row telemetry into a lower-bound-only usage block.

    Used when no receipt covers the level (attempt or work): the
    ``llm_calls`` rows are forge's own ledger, a LOWER BOUND on the
    vendor's aggregate — flagged ``call_derived``, exact never claimed
    from per-call counters alone.
    """
    cost_claims = [
        cost for cost in (_nn_float(call.get("total_cost_usd")) for call in calls) if cost
    ]
    inputs = [value for value in (_nn_int(call.get("input_tokens")) for call in calls) if value]
    outputs = [value for value in (_nn_int(call.get("output_tokens")) for call in calls) if value]
    return UsageLedger(
        receipts_received=0,
        receipts_expected=expected,
        exact=False,
        cost_usd=None,
        known_cost_lower_bound_usd=sum(cost_claims),
        input_tokens_inclusive_lower_bound=sum(inputs),
        output_tokens_lower_bound=sum(outputs),
        completeness="aggregate" if calls else "unknown",
        call_derived=True,
        notes=(
            (
                f"{level_key}: no usage receipt — llm_calls telemetry is the known"
                " lower bound (per-call counters, vendor aggregate absent)"
            ),
        )
        if calls
        else (),
    )


def _fold_usage(levels: Sequence[UsageLedger]) -> UsageLedger:
    """Fold usage blocks (attempts + planner calls + orphans) into one."""
    real = [level for level in levels if not level.is_vacuous]
    if not real:
        return UsageLedger(
            receipts_expected=0,
            notes=("no attempts recorded — usage cannot be reconciled",),
        )
    received = sum(level.receipts_received for level in real)
    expected = sum(level.receipts_expected for level in real)
    exact = all(level.exact for level in real) and all(
        level.cost_usd is not None or level.receipts_expected == 0 for level in real
    )
    costs = [level.cost_usd for level in real]
    inclusive = [level.input_tokens_inclusive for level in real]
    outputs = [level.output_tokens for level in real]
    cached = [level.cached_input_tokens for level in real]
    writes = [level.cache_write_tokens for level in real]
    reasoning = [level.reasoning_tokens for level in real]
    completeness = (
        "exact"
        if all(level.completeness == "exact" for level in real)
        else ("aggregate" if any(level.completeness != "unknown" for level in real) else "unknown")
    )
    notes: list[str] = []
    for level in real:
        notes.extend(level.notes)
    return UsageLedger(
        receipts_received=received,
        receipts_expected=expected,
        exact=exact,
        cost_usd=sum(value for value in costs if value is not None)
        if exact and all(value is not None for value in costs)
        else None,
        known_cost_lower_bound_usd=sum(level.known_cost_lower_bound_usd for level in real),
        input_tokens_inclusive=sum(value for value in inclusive if value is not None)
        if exact and all(value is not None for value in inclusive)
        else None,
        input_tokens_inclusive_lower_bound=sum(
            level.input_tokens_inclusive_lower_bound for level in real
        ),
        cached_input_tokens=cached[0] if len(cached) == 1 else None,
        cache_write_tokens=writes[0] if len(writes) == 1 else None,
        output_tokens=sum(value for value in outputs if value is not None)
        if exact and all(value is not None for value in outputs)
        else None,
        output_tokens_lower_bound=sum(level.output_tokens_lower_bound for level in real),
        reasoning_tokens=reasoning[0] if len(reasoning) == 1 else None,
        completeness=completeness,
        call_derived=any(level.call_derived for level in real),
        notes=tuple(notes),
    )


# ----------------------------------------------------------------------
# The linker
# ----------------------------------------------------------------------


class MeasurementLinker:
    """Link records by identity: work → attempt → call/receipt → route.

    ``link`` takes the durable record shapes as plain mappings —
    ``works`` (``{work_id, outcome, model_version, harness_version,
    acceptance_contract}``), ``attempts`` (the evidence ``attempts``
    contract, plus ``work_id``/``outcome``), ``receipts`` (the usage
    block with ``receipt_id``/``attempt_id``/route), ``calls``
    (``llm_calls``-shaped rows) and ``spans`` (typed latency windows) —
    and returns the cohort's :class:`DeliveryLedger`. Everything is
    sorted and folded by identity/content, so the ledger — and every
    aggregate derived from it — is invariant under any input ordering.
    """

    def link(
        self,
        *,
        works: Sequence[Mapping[str, Any]] = (),
        attempts: Sequence[Mapping[str, Any]] = (),
        receipts: Sequence[Mapping[str, Any]] = (),
        calls: Sequence[Mapping[str, Any]] = (),
        spans: Sequence[Mapping[str, Any]] = (),
    ) -> DeliveryLedger:
        notes: list[str] = []
        conflicts: list[ReceiptConflict] = []
        duplicates: list[DuplicateCallIdentity] = []
        resets: list[CounterReset] = []

        # -- works ------------------------------------------------------
        work_meta: dict[str, dict[str, str]] = {}
        for record in works:
            work_id = _text(record.get("work_id") or record.get("run_id"))
            if not work_id:
                notes.append("a work record carries no work_id — skipped, never guessed")
                continue
            outcome = _text(record.get("outcome"))
            if outcome and outcome not in ATTEMPT_OUTCOMES:
                notes.append(
                    f"work {work_id}: outcome {outcome!r} is outside"
                    f" {ATTEMPT_OUTCOMES} — treated as unknown, spend stays in programme"
                )
                outcome = ""
            work_meta[work_id] = {
                "outcome": outcome,
                "model_version": _text(record.get("model_version")),
                "harness_version": _text(record.get("harness_version")),
                "acceptance_contract": _text(record.get("acceptance_contract")),
            }

        # -- attempts (identity: work id + attempt id) --------------------
        attempt_records: dict[tuple[str, str], dict[str, Any]] = {}
        for record in attempts:
            work_id = _text(record.get("work_id") or record.get("run_id"))
            attempt_id = _text(record.get("attempt_id"))
            if not attempt_id:
                # Content-addressed identity keeps anonymous attempts
                # order-invariant (arrival order must not name things).
                attempt_id = f"anonymous:{_digest(_canonical(dict(record)))}"
            key = (work_id, attempt_id)
            existing = attempt_records.get(key)
            # A re-delivered attempt record is content-folded, never
            # arrival-folded: the lexicographically greatest canonical
            # form wins, so shuffling inputs changes nothing.
            if existing is None or _canonical(dict(record)) >= _canonical(existing):
                attempt_records[key] = dict(record)

        # -- receipts (identity: receipt id or content digest) -------------
        receipt_claims: dict[tuple[str, str], list[ReceiptClaim]] = {}
        orphan_receipts: dict[str, list[ReceiptClaim]] = {}
        for record in receipts:
            work_id = _text(record.get("work_id") or record.get("run_id"))
            attempt_id = _text(record.get("attempt_id"))
            route = ProviderRoute(
                provider=_text(record.get("provider") or record.get("driver")),
                model=_text(record.get("model")),
            )
            input_tokens = _nn_int(record.get("input_tokens"))
            cached_input_tokens = _nn_int(record.get("cached_input_tokens"))
            cache_write_tokens = _nn_int(record.get("cache_write_tokens"))
            output_tokens = _nn_int(record.get("output_tokens"))
            reasoning_tokens = _nn_int(record.get("reasoning_tokens"))
            cost_usd = _nn_float(record.get("total_cost_usd", record.get("cost_usd")))
            receipt_id = _text(record.get("receipt_id"))
            if not receipt_id:
                receipt_material = _canonical(
                    {
                        "work": work_id,
                        "attempt": attempt_id,
                        "input": input_tokens,
                        "cached": cached_input_tokens,
                        "write": cache_write_tokens,
                        "output": output_tokens,
                        "reasoning": reasoning_tokens,
                        "cost": cost_usd,
                        "route": route.key,
                    }
                )
                receipt_id = f"receipt:{_digest(receipt_material)}"
            completeness = _text(record.get("completeness"))
            if completeness not in ("exact", "aggregate", "partial"):
                completeness = (
                    "aggregate"
                    if any(
                        value is not None
                        for value in (
                            input_tokens,
                            cached_input_tokens,
                            cache_write_tokens,
                            output_tokens,
                            reasoning_tokens,
                            cost_usd,
                        )
                    )
                    else "unknown"
                )
            claim = ReceiptClaim(
                receipt_id=receipt_id,
                work_id=work_id,
                attempt_id=attempt_id,
                source=_text(record.get("source")) or "usage",
                route=route,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_write_tokens=cache_write_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                cost_usd=cost_usd,
                completeness=completeness,
                cost_basis=_text(record.get("cost_basis")),
                segment=_text(record.get("segment")),
            )
            if (work_id, attempt_id) in attempt_records:
                receipt_claims.setdefault((work_id, attempt_id), []).append(claim)
            else:
                orphan_receipts.setdefault(work_id, []).append(claim)
                notes.append(
                    f"receipt {receipt_id} references unknown attempt"
                    f" {attempt_id!r} of work {work_id!r} — spend kept as a work-level"
                    " lower bound, excluded from exact totals"
                )

        # -- calls (identity: call id; dedup + duplicate attribution) -----
        call_rows: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        for record in calls:
            call_id = _text(record.get("call_id") or record.get("id"))
            if not call_id:
                notes.append("a call record carries no call id — skipped, never guessed")
                continue
            work_id = _text(record.get("work_id") or record.get("flow_run_id"))
            attempt_id = _text(record.get("attempt_id"))
            call_rows.setdefault(call_id, []).append((work_id, attempt_id, dict(record)))

        calls_by_attempt: dict[tuple[str, str], list[dict[str, Any]]] = {}
        calls_by_work: dict[str, list[dict[str, Any]]] = {}
        for call_id in sorted(call_rows):
            rows = call_rows[call_id]
            attributions = sorted(
                {
                    f"{work_id or '?'}/{attempt_id or 'planner'}"
                    for work_id, attempt_id, _record in rows
                }
            )
            claim_shapes = {
                _canonical(
                    {
                        "provider": _text(row.get("provider")),
                        "model": _text(row.get("model")),
                        "duration_ms": _nn_int(row.get("duration_ms")),
                        "input_tokens": _nn_int(row.get("input_tokens")),
                        "output_tokens": _nn_int(row.get("output_tokens")),
                        "cached_tokens": _nn_int(row.get("cached_tokens")),
                    }
                )
                for _work, _attempt, row in rows
            }
            if len(attributions) > 1:
                duplicates.append(
                    DuplicateCallIdentity(
                        call_id=call_id,
                        attributions=tuple(attributions),
                        note=(
                            f"call {call_id} delivered under {len(attributions)}"
                            " attributions — collapsed to one by identity, attributed"
                            f" to {attributions[0]}"
                        ),
                    )
                )
            if len(claim_shapes) > 1:
                conflicts.append(
                    ReceiptConflict(
                        identity_kind="call",
                        identity=call_id,
                        attempt_id=attributions[0].split("/", 1)[1],
                        source_a="llm_calls#1",
                        source_b="llm_calls#2",
                        fields=("duration_ms", "input_tokens", "output_tokens"),
                    )
                )
                notes.append(
                    f"call {call_id} has conflicting rows — excluded from exact"
                    " aggregates, never averaged"
                )
                continue
            work_id, attempt_id, row = rows[0]
            if attempt_id and (work_id, attempt_id) in attempt_records:
                calls_by_attempt.setdefault((work_id, attempt_id), []).append(row)
            else:
                calls_by_work.setdefault(work_id, []).append(row)

        # -- spans (typed latency windows; unknown stays unknown) ---------
        parsed_spans: dict[str, LatencySpan] = {}
        for record in spans:
            work_id = _text(record.get("work_id") or record.get("run_id"))
            attempt_id = _text(record.get("attempt_id"))
            span_type = _text(record.get("span_type"))
            if span_type not in LATENCY_SPAN_TYPES:
                notes.append(
                    f"span with untyped kind {span_type!r} skipped — it belongs to no"
                    " population and is never silently re-typed"
                )
                continue
            origin = _text(record.get("origin")) or _DEFAULT_SPAN_ORIGIN[span_type]
            if origin not in SPAN_ORIGINS:
                origin = _DEFAULT_SPAN_ORIGIN[span_type]
            seconds = _nn_float(record.get("seconds"))
            if seconds is None:
                began = _parse_dt(record.get("began_at"))
                ended = _parse_dt(record.get("ended_at"))
                if began is not None and ended is not None:
                    seconds = max(0.0, (ended - began).total_seconds())
            span_id = _text(record.get("span_id")) or (
                f"span:{span_type}:{origin}:{work_id}:{attempt_id}:"
                f"{_digest(_canonical(dict(record)))}"
            )
            parsed_spans.setdefault(
                span_id,
                LatencySpan(
                    span_id=span_id,
                    work_id=work_id,
                    attempt_id=attempt_id,
                    span_type=span_type,
                    origin=origin,
                    seconds=seconds,
                ),
            )
        span_rows = [parsed_spans[span_id] for span_id in sorted(parsed_spans)]

        # -- fold per work ------------------------------------------------
        work_ids = sorted(
            set(work_meta)
            | {work_id for work_id, _attempt in attempt_records}
            | set(calls_by_work)
            | set(orphan_receipts)
        )
        work_ledgers: list[WorkLedger] = []
        for work_id in work_ids:
            meta = work_meta.get(work_id) or {
                "outcome": "",
                "model_version": "",
                "harness_version": "",
                "acceptance_contract": "",
            }
            if work_id not in work_meta:
                notes.append(
                    f"work {work_id}: no work record — outcome unknown; spend stays in"
                    " programme, excluded from accepted units"
                )
            work_attempts: list[AttemptLedger] = []
            for key in sorted(k for k in attempt_records if k[0] == work_id):
                _work, attempt_id = key
                record = attempt_records[key]
                attempt_key = f"{work_id}:{attempt_id}"
                attempt_claims = receipt_claims.get(key, [])
                usage, attempt_conflicts, attempt_resets = _fold_receipts(
                    attempt_key, attempt_claims
                )
                conflicts.extend(attempt_conflicts)
                resets.extend(attempt_resets)
                attempt_calls = calls_by_attempt.get(key, [])
                if not attempt_claims and attempt_calls:
                    usage = _fold_calls(attempt_key, attempt_calls, expected=1)
                elif not attempt_claims:
                    usage = replace(
                        usage,
                        notes=tuple(
                            (
                                *usage.notes,
                                f"{attempt_key}: no usage receipt and no call"
                                " telemetry — usage unknown, never zero",
                            )
                        ),
                    )
                outcome = _text(record.get("outcome"))
                if outcome and outcome not in ATTEMPT_OUTCOMES:
                    outcome = ""
                attempt_spans = [
                    span
                    for span in span_rows
                    if span.work_id == work_id and span.attempt_id == attempt_id
                ]
                call_duration_spans = [
                    LatencySpan(
                        span_id=f"call:{_text(row.get('call_id') or row.get('id'))}",
                        work_id=work_id,
                        attempt_id=attempt_id,
                        span_type="model",
                        origin="harness",
                        seconds=(
                            duration / 1000.0
                            if (duration := _nn_float(row.get("duration_ms"))) is not None
                            else None
                        ),
                    )
                    for row in attempt_calls
                ]
                work_attempts.append(
                    AttemptLedger(
                        attempt_id=attempt_id,
                        work_id=work_id,
                        outcome=outcome,
                        sources=tuple(sorted({claim.source for claim in attempt_claims})),
                        routes=tuple(
                            sorted(
                                {claim.route for claim in attempt_claims},
                                key=lambda route: route.key,
                            )
                        ),
                        receipts=tuple(sorted(attempt_claims, key=lambda claim: claim.receipt_id)),
                        call_ids=tuple(
                            sorted(
                                _text(row.get("call_id") or row.get("id")) for row in attempt_calls
                            )
                        ),
                        spans=tuple(
                            sorted(
                                [*attempt_spans, *call_duration_spans],
                                key=lambda span: span.span_id,
                            )
                        ),
                        usage=usage,
                    )
                )

            planner_calls = calls_by_work.get(work_id, [])
            planner_usage = (
                _fold_calls(f"{work_id}:planner", planner_calls, expected=0)
                if planner_calls
                else UsageLedger()
            )
            orphans = sorted(orphan_receipts.get(work_id, []), key=lambda claim: claim.receipt_id)
            orphan_usage = UsageLedger(
                receipts_received=0,
                receipts_expected=0,
                exact=False,
                known_cost_lower_bound_usd=sum(
                    claim.cost_usd for claim in orphans if claim.cost_usd is not None
                ),
                input_tokens_inclusive_lower_bound=sum(
                    claim.input_tokens_inclusive or 0 for claim in orphans
                ),
                output_tokens_lower_bound=sum(claim.output_tokens or 0 for claim in orphans),
                notes=tuple(
                    f"orphan receipt {claim.receipt_id} kept as a lower bound" for claim in orphans
                ),
            )
            own_usage = _fold_usage(
                [*(attempt.usage for attempt in work_attempts), planner_usage, orphan_usage]
            )
            fully_linked = bool(
                meta["outcome"]
                and work_attempts
                and all(
                    attempt.usage.exact and attempt.usage.completeness != "unknown"
                    for attempt in work_attempts
                )
                and own_usage.exact
                and all(
                    span.seconds is not None for attempt in work_attempts for span in attempt.spans
                )
                and not any(conflict.attempt_id.startswith(f"{work_id}:") for conflict in conflicts)
            )
            work_notes: list[str] = []
            if not meta["outcome"]:
                work_notes.append("outcome unknown — programme spend yes, accepted unit no")
            work_ledgers.append(
                WorkLedger(
                    work_id=work_id,
                    outcome=meta["outcome"],
                    attempts=tuple(work_attempts),
                    planner_call_ids=tuple(
                        sorted(_text(row.get("call_id") or row.get("id")) for row in planner_calls)
                    ),
                    planner_usage=planner_usage,
                    own_usage=own_usage,
                    model_version=meta["model_version"],
                    harness_version=meta["harness_version"],
                    acceptance_contract=meta["acceptance_contract"],
                    orphan_receipt_ids=tuple(sorted(claim.receipt_id for claim in orphans)),
                    fully_linked=fully_linked,
                    notes=tuple(work_notes),
                )
            )

        return DeliveryLedger(
            works=tuple(work_ledgers),
            conflicts=tuple(
                sorted(conflicts, key=lambda row: (row.identity, row.source_a, row.source_b))
            ),
            duplicate_calls=tuple(sorted(duplicates, key=lambda row: row.call_id)),
            counter_resets=tuple(
                sorted(resets, key=lambda row: (row.attempt_id, row.source, row.field))
            ),
            # Sorted + deduped: the notes are independent strings, and a
            # stable order is part of order invariance (the document must
            # not depend on record arrival order).
            notes=tuple(sorted(set(notes))),
        )


# ----------------------------------------------------------------------
# Aggregation: the report, the trace, the rate guard
# ----------------------------------------------------------------------


def _population_rows(ledger: DeliveryLedger) -> list[dict[str, Any]]:
    """Latency aggregates per (span_type, origin) population — never mixed.

    Each row carries its ``population_identity``: the population key plus
    a digest over the sorted work ids that contributed, so a chart reader
    can see exactly WHICH population a number describes (planner-only vs
    harness-wide vs operator-side are different populations by
    construction).
    """
    grouped: dict[str, dict[str, Any]] = {}
    for work in ledger.works:
        for attempt in work.attempts:
            for span in attempt.spans:
                row = grouped.setdefault(
                    span.population,
                    {
                        "span_type": span.span_type,
                        "origin": span.origin,
                        "works": set(),
                        "spans": 0,
                        "unknown_spans": 0,
                        "known_seconds": 0.0,
                        "exact": True,
                    },
                )
                row["works"].add(work.work_id)
                row["spans"] += 1
                if span.seconds is None:
                    row["unknown_spans"] += 1
                    row["exact"] = False
                else:
                    row["known_seconds"] += span.seconds
    populations: list[dict[str, Any]] = []
    for key in sorted(grouped):
        row = grouped[key]
        works = tuple(sorted(row["works"]))
        works_digest = _digest("\n".join(works))
        populations.append(
            {
                "population_identity": f"latency.{key}:works={works_digest}",
                "span_type": row["span_type"],
                "origin": row["origin"],
                "works": list(works),
                "spans": row["spans"],
                "unknown_spans": row["unknown_spans"],
                "total_seconds": _round6(row["known_seconds"]) if row["exact"] else None,
                "total_seconds_lower_bound": _round6(row["known_seconds"]),
            }
        )
    return populations


def _distribution(values: Sequence[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    median = ordered[mid] if count % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    minimum: float = ordered[0]
    maximum: float = ordered[-1]
    return {
        "count": count,
        "min": round(minimum, 6),
        "median": round(median, 6),
        "max": round(maximum, 6),
    }


def _capacity_block(ledger: DeliveryLedger) -> dict[str, Any] | None:
    """The bounded capacity model — measured service times ONLY, and only
    after the first fully-linked task exists (R36-17 scope item 6).

    A serial upper bound: 86400 s over the mean model-span service seconds
    per fully-linked work. No queue, verification or human-wait time is
    folded in — those are different populations and would only inflate
    the bound's honesty problem, not its usefulness.
    """
    fully_linked = [work for work in ledger.works if work.fully_linked]
    if not fully_linked:
        return None
    service_seconds = [
        sum(
            span.seconds or 0.0
            for attempt in work.attempts
            for span in attempt.spans
            if span.span_type == "model"
        )
        for work in fully_linked
    ]
    mean_service = sum(service_seconds) / len(service_seconds)
    if mean_service <= 0:
        return None
    return {
        "basis": "measured model-span service times only",
        "fully_linked_works": len(fully_linked),
        "model_service_seconds_per_task": _round6(mean_service),
        "serial_upper_bound_tasks_per_day": _round6(86400.0 / mean_service),
        "bound_kind": "upper",
        "note": (
            "an upper bound from service time alone — queue, verification and"
            " human-wait populations are excluded by construction; not a forecast"
        ),
    }


def build_report(ledger: DeliveryLedger) -> dict[str, Any]:
    """Aggregate the ledger into the versioned measurement report.

    Sections: ``usage`` (coverage + lower bounds + conventions),
    ``delivery`` (programme vs accepted-unit economics), ``latency``
    (one row per population, each with its ``population_identity``),
    ``capacity`` (only when a fully-linked task exists), plus the named
    conflicts, duplicate calls, counter resets and notes. Nothing here
    calls a model or reads a clock — the report is a pure fold of the
    stored artifact, which is what makes :func:`replay_report`
    byte-identical.
    """
    works = ledger.works
    accepted = [work for work in works if work.outcome == "accepted"]
    by_outcome: dict[str, int] = {}
    for work in works:
        key = work.outcome or "unknown"
        by_outcome[key] = by_outcome.get(key, 0) + 1

    attempt_count = sum(len(work.attempts) for work in works)
    covered_attempts = sum(
        1 for work in works for attempt in work.attempts if attempt.usage.receipts_received > 0
    )
    receipts_expected = sum(
        attempt.usage.receipts_expected for work in works for attempt in work.attempts
    )
    receipts_received = sum(
        attempt.usage.receipts_received for work in works for attempt in work.attempts
    )

    programme = _fold_usage([work.own_usage for work in works])
    accepted_unit_costs = {work.work_id: _round6(work.own_usage.cost_usd) for work in accepted}
    exact_unit_costs = [
        work.own_usage.cost_usd for work in accepted if work.own_usage.cost_usd is not None
    ]
    programme_notes: list[str] = []
    if not accepted:
        programme_notes.append(
            "no accepted outcomes — per-accepted metrics are undefined, never zero"
        )
    if any(work.outcome not in ("accepted", "") for work in works):
        programme_notes.append(
            "rejected/cancelled/superseded/abandoned spend stays in the programme"
            " total and in the per-accepted denominator (R36-17)"
        )

    populations = _population_rows(ledger)
    capacity = _capacity_block(ledger)
    notes: list[str] = list(ledger.notes)
    for work in works:
        notes.extend(work.notes)
        for attempt in work.attempts:
            notes.extend(attempt.usage.notes)
    notes.append(
        "latency populations never mix: planner-only model time, harness-wide"
        " spans and operator-side waits are separate populations, each named"
        " by its population_identity (AT-12)"
    )

    distribution = _distribution(exact_unit_costs)
    identity_list = [row["population_identity"] for row in populations]
    return {
        "schema": MEASUREMENT_SCHEMA,
        "cohort": {
            "works": len(works),
            "by_outcome": dict(sorted(by_outcome.items())),
            "accepted_outcomes": len(accepted),
            "attempts": attempt_count,
        },
        "usage": {
            "exact_coverage": (covered_attempts / attempt_count) if attempt_count else None,
            "receipt_coverage": (
                receipts_received / receipts_expected if receipts_expected else None
            ),
            "known_cost_lower_bound_usd": _round6(programme.known_cost_lower_bound_usd),
            "exact_programme_cost_usd": _round6(programme.cost_usd),
            "conventions": list(USAGE_CONVENTIONS),
            "unknown_receipt_attempts": attempt_count - covered_attempts,
        },
        "delivery": {
            "programme_cost_usd": _round6(programme.cost_usd),
            "programme_cost_known_lower_bound_usd": _round6(programme.known_cost_lower_bound_usd),
            "programme_cost_per_accepted_usd": _round6(programme.cost_usd / len(accepted))
            if (accepted and programme.cost_usd is not None)
            else None,
            "programme_cost_per_accepted_lower_bound_usd": _round6(
                programme.known_cost_lower_bound_usd / len(accepted)
            )
            if accepted
            else None,
            "accepted_unit_costs_usd": dict(sorted(accepted_unit_costs.items())),
            "delivery.accepted_cost_distribution": distribution,
            "notes": programme_notes,
        },
        "latency": {
            "populations": populations,
            "latency.population_identity": identity_list,
        },
        "capacity": capacity,
        "conflicts": [row.to_json() for row in ledger.conflicts],
        "duplicate_calls": [row.to_json() for row in ledger.duplicate_calls],
        "counter_resets": [row.to_json() for row in ledger.counter_resets],
        "observability": {
            "usage.exact_coverage": (covered_attempts / attempt_count) if attempt_count else None,
            "usage.known_cost_lower_bound": _round6(programme.known_cost_lower_bound_usd),
            "delivery.accepted_cost_distribution": distribution,
            "latency.population_identity": identity_list,
        },
        "notes": notes,
    }


def replay_report(source: DeliveryLedger | Mapping[str, Any]) -> dict[str, Any]:
    """Re-aggregate from the STORED artifact — byte-identical, no model calls.

    Accepts a :class:`DeliveryLedger` or its stored document; a document
    is rebuilt through :meth:`DeliveryLedger.from_document` first, so the
    replay path exercises exactly the serialization a later reader would.
    """
    ledger = source if isinstance(source, DeliveryLedger) else DeliveryLedger.from_document(source)
    return build_report(ledger)


def _population_key(population_identity: str) -> str:
    """The ``span_type:origin`` key an identity names (``''`` when mixed)."""
    key = population_identity.removeprefix("latency.").split(":works=")[0]
    return key if key in {f"{t}:{o}" for t in LATENCY_SPAN_TYPES for o in SPAN_ORIGINS} else ""


def measured_rate(
    label: str, *, tokens: int, seconds: float, population_identity: str
) -> dict[str, Any]:
    """The ONLY rate constructor — and the AT-12 guard.

    ``label`` must exist in :data:`RATE_LABEL_SPACE` and the denominator's
    ``population_identity`` must belong to the label's allowed populations.
    Output tokens over a mixed population (``model + queue + human_wait``
    — total request latency) can never be labeled
    ``decode_output_tokens_per_second``: the population is not in the
    label's set and :class:`RateLabelError` is raised instead.
    """
    allowed = RATE_LABEL_SPACE.get(label)
    if allowed is None:
        raise RateLabelError(
            f"label {label!r} is outside the sanctioned rate-label space"
            f" {sorted(RATE_LABEL_SPACE)} — no mixed-population throughput exists"
        )
    if _population_key(population_identity) not in allowed:
        raise RateLabelError(
            f"population {population_identity!r} cannot carry the label {label!r}:"
            f" allowed populations are {sorted(allowed)} — output over"
            " total-request latency is never decode throughput (AT-12)"
        )
    if seconds <= 0:
        raise RateLabelError("a rate needs positive measured seconds, never zero")
    return {
        "label": label,
        "population_identity": population_identity,
        "tokens": tokens,
        "seconds": _round6(seconds),
        "tokens_per_second": _round6(tokens / seconds),
    }


def trace_accepted_task(ledger: DeliveryLedger, work_id: str) -> dict[str, Any]:
    """AT-12: one work traced from its cost total to every receipt AND gap.

    The trace lists every INCLUDED receipt (identity, source, route,
    amounts) and every EXCLUDED/unknown record with the reason it is
    excluded; when the total is exact the included receipts sum to it.
    Works of any outcome can be traced — a rejected work's trace shows
    its spend staying in the programme denominator, which is the point.
    """
    work = ledger.work_by_id(work_id)
    if work is None:
        raise KeyError(f"work {work_id!r} is not in the ledger")
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for attempt in work.attempts:
        label = f"{work_id}:{attempt.attempt_id}"
        if attempt.usage.exact and attempt.usage.cost_usd is not None:
            for receipt in attempt.receipts:
                included.append(
                    {
                        "receipt_id": receipt.receipt_id,
                        "attempt": label,
                        "source": receipt.source,
                        "route": receipt.route.key,
                        "cost_usd": _round6(receipt.cost_usd),
                        "input_tokens_inclusive": receipt.input_tokens_inclusive,
                        "cached_input_tokens": receipt.cached_input_tokens,
                        "cache_write_tokens": receipt.cache_write_tokens,
                        "output_tokens": receipt.output_tokens,
                        "reasoning_tokens": receipt.reasoning_tokens,
                    }
                )
        elif attempt.usage.call_derived:
            excluded.append(
                {
                    "record": f"attempt {label}",
                    "reason": (
                        "no usage receipt — llm_calls telemetry kept as a known"
                        " lower bound only, the exact total is unknown"
                    ),
                }
            )
        else:
            excluded.append(
                {
                    "record": f"attempt {label}",
                    "reason": (
                        "no usage receipt (vendor telemetry absent) — spend unknown, never zero"
                    ),
                }
            )
        for span in attempt.spans:
            if span.seconds is None:
                excluded.append(
                    {
                        "record": f"span {span.span_id} ({span.population})",
                        "reason": "window not measurable — unknown, never zero",
                    }
                )
    for conflict in ledger.conflicts:
        if conflict.attempt_id.startswith(f"{work_id}:"):
            excluded.append(
                {
                    "record": f"{conflict.identity_kind} {conflict.identity}",
                    "reason": (
                        f"conflicting claims ({conflict.source_a} vs"
                        f" {conflict.source_b} on {', '.join(conflict.fields)})"
                        " — excluded from the exact total, never averaged"
                    ),
                }
            )
    for duplicate in ledger.duplicate_calls:
        if any(attribution.split("/", 1)[0] == work_id for attribution in duplicate.attributions):
            excluded.append(
                {
                    "record": f"call {duplicate.call_id}",
                    "reason": (
                        "duplicated call identity across attempts"
                        f" ({', '.join(duplicate.attributions)}) — collapsed by"
                        " identity, never counted twice"
                    ),
                }
            )
    for reset in ledger.counter_resets:
        if reset.attempt_id.startswith(f"{work_id}:"):
            excluded.append(
                {
                    "record": f"receipt from {reset.source} ({reset.field})",
                    "reason": reset.note or "counter reset — exact usage unknown",
                }
            )
    for orphan in work.orphan_receipt_ids:
        excluded.append(
            {
                "record": f"receipt {orphan}",
                "reason": (
                    "references an attempt the ledger does not know — kept as a"
                    " work-level lower bound, excluded from exact totals"
                ),
            }
        )
    total = work.own_usage.cost_usd
    lower = work.own_usage.known_cost_lower_bound_usd
    included_sum = sum(row["cost_usd"] or 0.0 for row in included)
    return {
        "schema": MEASUREMENT_SCHEMA,
        "work_id": work_id,
        "outcome": work.outcome,
        "model_version": work.model_version,
        "harness_version": work.harness_version,
        "acceptance_contract": work.acceptance_contract,
        "programme_inclusion": (
            "spend stays in the programme total and in the per-accepted denominator"
            " regardless of outcome"
        ),
        "cost_total_usd": _round6(total),
        "known_cost_lower_bound_usd": _round6(lower),
        "included_receipts": included,
        "included_receipts_sum_usd": _round6(included_sum),
        "total_is_complete": total is not None,
        "excluded_or_unknown": excluded,
        "notes": list(work.notes),
    }


def billing_comparison(
    report: Mapping[str, Any], billing_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Report totals vs a provider billing/export — differences STATED.

    Per provider route: the report's known lower bound against the billed
    figure. A gap is either ``reconciled`` (report exact and equal within
    a cent) or ``unresolvable`` — with the reason named (report side
    incomplete, coverage below 1, or an outright mismatch). Nothing is
    silently written off as zero.
    """
    usage_block: Any = report.get("usage")
    usage: Mapping[str, Any] = usage_block if isinstance(usage_block, Mapping) else {}
    report_lower = _nn_float(usage.get("known_cost_lower_bound_usd")) or 0.0
    report_exact = _nn_float(usage.get("exact_programme_cost_usd"))
    coverage = _nn_float(usage.get("receipt_coverage"))
    rows: list[dict[str, Any]] = []
    unresolvable = 0
    for row in billing_rows:
        route = _text(row.get("route") or row.get("provider"))
        billed = _nn_float(row.get("billed_usd"))
        if billed is None:
            rows.append(
                {
                    "route": route,
                    "status": "unresolvable",
                    "reason": "billing row carries no parseable amount",
                    "report_known_usd": _round6(report_lower),
                    "billed_usd": None,
                }
            )
            unresolvable += 1
            continue
        delta = round(billed - report_lower, 6)
        if report_exact is not None and abs(delta) <= 0.01:
            status, reason = "reconciled", "report exact and equal within a cent"
        elif report_exact is None:
            status = "unresolvable"
            reason = (
                "report side incomplete"
                f" (coverage {coverage if coverage is not None else 'n/a'})"
                " — the report total is a lower bound, not a reconciliation"
            )
        else:
            status = "unresolvable"
            reason = f"report exact yet differs from the export by {delta} usd"
        unresolvable += 1 if status == "unresolvable" else 0
        rows.append(
            {
                "route": route,
                "status": status,
                "reason": reason,
                "report_known_usd": _round6(report_lower),
                "billed_usd": _round6(billed),
                "delta_usd": delta,
            }
        )
    return {
        "schema": MEASUREMENT_SCHEMA,
        "rows": rows,
        "unresolvable": unresolvable,
        "note": (
            "unresolvable differences are stated, never absorbed — an incomplete"
            " report side cannot confirm or deny a billing export"
        ),
    }


# ----------------------------------------------------------------------
# The connected path: delivery_metrics output and run evidence → records
# ----------------------------------------------------------------------


def attempt_records_from_evidence(evidence: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The run's per-attempt evidence records — the additive extraction
    :mod:`forge.adaptive.delivery_metrics` now owns and exports, so the
    measurement linker and the metrics loader read ONE spelling of the
    evidence contract."""
    return _attempt_records_from_evidence(evidence)


def ledger_records_from_delivery_metrics(
    metrics: DeliveryMetrics,
    *,
    calls: Sequence[Mapping[str, Any]] = (),
    extra_works: Sequence[Mapping[str, Any]] = (),
) -> dict[str, list[dict[str, Any]]]:
    """One run's :class:`DeliveryMetrics` → linker input records.

    The connected path: ``delivery_metrics_for_run`` reconciles a run's
    durable evidence; this adapter re-keys its per-attempt receipts to
    the linker's record shapes (one receipt per attempt from the
    reconciled spend, planner-side model spans from the episode
    ``turn_s``, harness queue spans from the dispatch window, operator
    human-wait spans from the review window) so a run's honest
    reconciliation flows into the identity-linked cohort ledger without
    re-reading the database. An ``unknown`` attempt receipt is NOT
    emitted — the gap must stay a gap. Per-attempt outcomes are unknown
    at this layer (the metrics record only the run-level acceptance), so
    the attempts carry no outcome and the WORK carries acceptance.
    """
    work = {
        "work_id": metrics.run_id,
        "outcome": "accepted" if metrics.accepted else "",
    }
    attempts: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for row in metrics.per_attempt:
        attempt_id = row.attempt_id
        attempts.append({"work_id": metrics.run_id, "attempt_id": attempt_id})
        if row.spend_usd is not None:
            receipts.append(
                {
                    "receipt_id": f"evidence:{metrics.run_id}:{attempt_id}",
                    "work_id": metrics.run_id,
                    "attempt_id": attempt_id,
                    "source": "+".join(row.sources) or "attempts",
                    "total_cost_usd": row.spend_usd,
                    "completeness": "aggregate",
                }
            )
        if row.model_time_s is not None:
            spans.append(
                {
                    "work_id": metrics.run_id,
                    "attempt_id": attempt_id,
                    "span_type": "model",
                    "origin": "planner",
                    "seconds": row.model_time_s,
                }
            )
        breakdown = row.latency_breakdown
        if breakdown.dispatch_to_start_s is not None:
            spans.append(
                {
                    "work_id": metrics.run_id,
                    "attempt_id": attempt_id,
                    "span_type": "queue",
                    "origin": "harness",
                    "seconds": breakdown.dispatch_to_start_s,
                }
            )
        if breakdown.finish_to_review_s is not None:
            spans.append(
                {
                    "work_id": metrics.run_id,
                    "attempt_id": attempt_id,
                    "span_type": "human_wait",
                    "origin": "operator",
                    "seconds": breakdown.finish_to_review_s,
                }
            )
    return {
        "works": [work, *(dict(extra) for extra in extra_works)],
        "attempts": attempts,
        "receipts": receipts,
        "calls": [dict(row) for row in calls],
        "spans": spans,
    }


def ledger_records_from_ingested_usage(
    ingested: Sequence[Mapping[str, Any]],
    *,
    works: Sequence[Mapping[str, Any]] = (),
    attempts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Ingested usage rows (R38-09) → linker records — the ingestion join.

    The durable-ingested receipt rows
    (:class:`forge.adaptive.usage_ingestion.IngestedUsageRow` documents,
    the ``to_json()`` shape) become a receipt SOURCE beside the existing
    evidence inputs: every row with an ATTEMPT identity becomes a receipt
    record for that attempt (its source attribution, route, cost basis and
    attribution segment carried verbatim — the ``total_cost_usd`` the SDK
    reported rides as the cost claim); every row WITHOUT one (the
    planner/LLM-call ledger rows, work-level by construction) becomes a
    ``calls`` record so planning spend folds into the ledger's planner
    population and never mixes with lane-attempt usage. Populations stay
    distinct by construction, and a model-call id delivered under two
    attempts keeps BOTH rows — the linker's duplicate/cross-join guards
    surface it, one identity is never summed twice.
    """
    receipts: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for row in ingested:
        work_id = _text(row.get("work_id"))
        attempt_id = _text(row.get("attempt_id"))
        counters = row.get("counters") if isinstance(row.get("counters"), Mapping) else {}
        base = {
            "work_id": work_id,
            "source": _text(row.get("source")),
            "provider": _text(row.get("provider")),
            "model": _text(row.get("model")),
            "completeness": _text(row.get("completeness")) or "unknown",
        }
        tokens = {
            name: _nn_int(counters.get(name))
            for name in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "output_tokens",
                "reasoning_tokens",
            )
        }
        cost = _nn_float(row.get("cost_usd"))
        basis = _text(row.get("cost_basis"))
        segment = _text(row.get("segment"))
        if attempt_id:
            record: dict[str, Any] = {
                **base,
                "attempt_id": attempt_id,
                "receipt_id": _text(row.get("receipt_id")),
                **{name: value for name, value in tokens.items() if value is not None},
                "cost_basis": basis,
                "segment": segment,
            }
            if cost is not None:
                record["total_cost_usd"] = cost
            receipts.append(record)
        else:
            # Work-level (planner/LLM-call) rows: the ledger's planner
            # population is CALL-shaped — the call id is the identity and
            # unknown counters stay absent, never zero-filled.
            call: dict[str, Any] = {
                "call_id": _text(row.get("receipt_id")),
                "work_id": work_id,
                "provider": base["provider"],
                "model": base["model"],
                **{name: value for name, value in tokens.items() if value is not None},
            }
            if cost is not None:
                call["total_cost_usd"] = cost
            calls.append(call)
    return {
        "works": [dict(work) for work in works],
        "attempts": [dict(attempt) for attempt in attempts],
        "receipts": receipts,
        "calls": calls,
        "spans": [],
    }
