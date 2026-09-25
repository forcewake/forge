"""R37-13 + R38-09 + Q39-11 — accepted-delivery economics over REAL identities.

``delivery_measurement`` (R36-17 / #276) links the recorded facts by
identity and folds them with honest unknowns; this module is the next
join layer: an :class:`EconomicsLinker` that takes that
:class:`DeliveryLedger`, the pilot's ACCEPTANCE decisions and the exact
PROFILE VERSIONS, and emits an :class:`EconomicsReport` (stamp
``forge.delivery.economics/1``) explaining what one accepted work item
cost — and what the whole programme cost per accepted item — from
receipts that trace to the same population. R38-09 (#310) feeds the same
measures from INGESTED lane usage rows (``forge.adaptive.usage_ingestion``
documents joined through ``ledger_records_from_ingested_usage``): every
priced entry now names its ``cost_basis`` (estimated vs provider-reported
vs billing-reconciliation — never summed into one column) and its
attribution SEGMENT (route + route version + rate card; a change is a new
segment, history never rewritten), and the report adds the
successful-attempt cost as its OWN measure beside the accepted-item
all-attempt total and programme-per-accepted.

Q39-11 (#330) adds the ONE complete evidence chain: the
:class:`AcceptedTaskLedgerBuilder` joins the economics report with the
native-job / candidate / verification / human-decision identities the
durable evidence carries and emits an :class:`AcceptedTaskLedger`
(stamp ``forge.delivery.accepted-ledger/1``) — run → attempt →
model-call → native-job → candidate → verification → human-decision,
every link explicit, a broken link surfaced as an identity gap and
never dropped. Its folds keep the issue's separations: the THREE cost
columns (provider-reported SDK figures vs versioned price-card
estimates vs billing-reconciliation — never blended), the TWO measures
(accepted-task all-attempt cost including failed/paused/superseded
attempts vs programme-per-accepted including rejected/abandoned work),
the SEVEN time measures (model / tool / CI queue / CI runtime /
operator wait / reviewer effort / setup effort — each over its own
recorded windows, never unmatched output ÷ unmatched durations, the
decode guard extended by :func:`assert_no_unmatched_rates`), coverage
beside every aggregate, and the #325 closing-budget shapes (the
closing reserve, budget refusals and the review-only recovery)
separately visible per work.

The honesty rules, all pinned by ``tests/test_delivery_economics.py``:

- **The join chain** — work id → execution attempt id → usage receipt →
  acceptance record, joined by STABLE IDS only. The acceptance record
  is the outcome AUTHORITY; the ledger is the spend AUTHORITY. An
  attempt the acceptance record names but the ledger never saw stays in
  the coverage denominator as unobserved; a receipt id delivered under
  two different works is a :class:`CrossRunJoin` row — counted once
  under its first (sorted) attribution, never cross-joined, and the
  second work's exact total degrades to unknown.
- **Evidence classes** — every receipt carries ``live-model`` or
  ``synthetic-vendor-counter`` (scripted-vendor wire counters; the lab
  pilot's ``lab-lane/vendor-wire`` source). Scripted counters are
  LABELLED and EXCLUDED from throughput comparisons:
  :class:`EvidenceClassError` is raised, never averaged in. The
  ``decode`` rate label needs live-model receipts over a MODEL-span
  population — the #276 :data:`RATE_LABEL_SPACE` rule extended here to
  request-latency populations of any evidence class.
- **Versioned price assumptions** — costs come either from receipts that
  carry a billed figure (``billing``) or from a versioned
  :class:`RateCard` (``estimate``); every priced entry labels its basis,
  and the two never sum into one column. Lab data (unknown costs)
  renders as a known lower bound plus coverage — never an exact zero.
- **Latency stages** — the ledger's typed spans fold into
  ``latency.stage_seconds`` with model / tool / queue / verification /
  human_wait separated, each population named, unknown windows counted
  (never zero-filled), and no stage total fabricated across mixed
  origins.
- **One truth store** — :func:`operator_summary` and
  :func:`reconcile_with_budget` are pure folds of the report document
  (which is a pure fold of the ledger): there is no second metrics
  store, and the operator summary carries no prompt, tool or task
  content — sensitive keys are dropped by :func:`redact_for_operator`.
- **Order invariance** — everything is sorted by identity and summed in
  sorted order, so reordering acceptance records, receipts or spans
  changes no aggregate and no byte of the document.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from forge.adaptive.closing_budget import (
    CandidateBinding,
    ClosingReservePolicy,
    closing_budget_report,
    review_only_continuation,
)
from forge.adaptive.delivery_measurement import (
    RATE_LABEL_SPACE,
    DeliveryLedger,
    ProviderRoute,
    RateLabelError,
    _nn_float,
    _nn_int,
    _round6,
    _text,
    measured_rate,
)
from forge.adaptive.usage_ingestion import (
    COST_BASIS_BILLING,
    COST_BASIS_ESTIMATED,
    COST_BASIS_PROVIDER_REPORTED,
    IngestedUsageRow,
)

__all__ = [
    "ACCEPTED_COST_COLUMNS",
    "ACCEPTED_LEDGER_SCHEMA",
    "AcceptanceRecord",
    "AcceptedTaskLedger",
    "AcceptedTaskLedgerBuilder",
    "AttemptChain",
    "BudgetEventLink",
    "COST_COLUMN_BILLING",
    "COST_COLUMN_PRICE_CARD",
    "COST_COLUMN_PROVIDER_REPORTED",
    "CandidateLink",
    "CrossRunJoin",
    "ECONOMICS_SCHEMA",
    "EVIDENCE_CLASSES",
    "EVIDENCE_LIVE_MODEL",
    "EVIDENCE_SYNTHETIC_COUNTER",
    "EVIDENCE_UNKNOWN",
    "EconomicsConflict",
    "EconomicsLinker",
    "EconomicsReport",
    "EvidenceClassError",
    "HumanDecisionLink",
    "IDENTITY_CHAIN",
    "ModelRate",
    "NativeJobLink",
    "RateCard",
    "ReviewRecoveryInput",
    "STAGE_SECONDS_KEYS",
    "TIME_MEASURES",
    "TimeWindow",
    "VerificationLink",
    "WorkChain",
    "assert_latency_guards",
    "assert_no_unmatched_rates",
    "classify_receipt",
    "decode_throughput",
    "operator_summary",
    "reconcile_with_budget",
    "redact_for_operator",
    "throughput_comparison",
]

#: The versioned stamp of every document this module emits.
ECONOMICS_SCHEMA = "forge.delivery.economics/1"

#: A receipt served by a REAL model gateway — measured inference, the
#: only evidence class a throughput number may rest on.
EVIDENCE_LIVE_MODEL = "live-model"

#: A receipt whose counters are wire observations from the controlled
#: scripted vendor (the lab pilot's vendor speaking the real codex
#: app-server wire): real wire shapes, NO measured inference, NO spend.
EVIDENCE_SYNTHETIC_COUNTER = "synthetic-vendor-counter"

#: A receipt that could not be classified — never promoted to live-model
#: (unclassified evidence is treated like synthetic in every guard).
EVIDENCE_UNKNOWN = "unknown"

#: The closed evidence-class vocabulary of the economics report.
EVIDENCE_CLASSES = (EVIDENCE_LIVE_MODEL, EVIDENCE_SYNTHETIC_COUNTER)

#: Source/provider markers that name the scripted vendor (its receipts
#: are protocol tests, not measured inference).
SYNTHETIC_SOURCE_MARKERS: tuple[str, ...] = (
    "lab-lane/vendor-wire",
    "vendor-wire",
    "scripted",
    "codex-app-server",
)

#: The latency stages the report's ``latency.stage_seconds`` separates
#: (the ledger's span types; the five the issue names come first).
STAGE_SECONDS_KEYS: tuple[str, ...] = (
    "model",
    "tool",
    "queue",
    "verification",
    "human_wait",
    "restore_collection",
)

#: Key-name markers an operator summary must never carry — billing
#: exports and operator surfaces redact prompt/tool/task content.
SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "prompt",
    "tool",
    "content",
    "brief",
    "diff",
    "question",
    "answer",
    "detail",
    "secret",
    "token_value",
    "body",
    "patch",
    "snippet",
)

#: The non-accepted attempt outcomes — repeated work the accepted item
#: keeps in its own total (the R37-13 retention rule).
_REJECTED_OUTCOMES = ("rejected", "cancelled", "superseded", "abandoned")


class EvidenceClassError(ValueError):
    """A throughput comparison was asked to rest on non-live evidence.

    The guarded constructions: a scripted vendor counter (or an
    unclassified receipt) inside a model-throughput comparison, and a
    decode label over receipts that are not :data:`EVIDENCE_LIVE_MODEL`.
    Synthetic counters are labelled and EXCLUDED — never averaged in.
    """


# ----------------------------------------------------------------------
# Versioned price assumptions
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRate:
    """One route's price assumptions — a versioned estimate, not a bill."""

    provider: str
    model: str
    input_per_mtok_usd: float
    output_per_mtok_usd: float
    cached_input_per_mtok_usd: float | None = None
    cache_write_per_mtok_usd: float | None = None
    basis: str = "estimate"

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_per_mtok_usd": self.input_per_mtok_usd,
            "cached_input_per_mtok_usd": self.cached_input_per_mtok_usd,
            "cache_write_per_mtok_usd": self.cache_write_per_mtok_usd,
            "output_per_mtok_usd": self.output_per_mtok_usd,
            "basis": self.basis,
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> ModelRate:
        return cls(
            provider=_text(block.get("provider")),
            model=_text(block.get("model")),
            input_per_mtok_usd=_nn_float(block.get("input_per_mtok_usd")) or 0.0,
            output_per_mtok_usd=_nn_float(block.get("output_per_mtok_usd")) or 0.0,
            cached_input_per_mtok_usd=_nn_float(block.get("cached_input_per_mtok_usd")),
            cache_write_per_mtok_usd=_nn_float(block.get("cache_write_per_mtok_usd")),
            basis=_text(block.get("basis")) or "estimate",
        )


@dataclass(frozen=True)
class RateCard:
    """The versioned price assumptions every estimate is labelled with.

    ``version`` rides the report and every priced entry, so two reports
    can never silently compare estimates across changed assumptions.
    A card rate's ``basis`` is ``estimate`` by construction — only a
    receipt that CARRIES a billed figure is ever labelled ``billing``.
    """

    version: str
    currency: str = "usd"
    rates: tuple[ModelRate, ...] = ()

    def rate_for(self, route: ProviderRoute) -> ModelRate | None:
        """The rate for a route — exact key first, then provider-wide."""
        key = route.key
        for rate in self.rates:
            if rate.key == key:
                return rate
        provider_key = route.provider
        for rate in self.rates:
            if rate.key == provider_key:
                return rate
        return None

    def price(
        self,
        route: ProviderRoute,
        *,
        input_tokens: int | None,
        cached_input_tokens: int | None,
        cache_write_tokens: int | None,
        output_tokens: int | None,
    ) -> float | None:
        """Estimate a receipt's cost from the card — None when it cannot.

        An estimate needs a rate AND at least one token counter; the
        Anthropic-shaped disjoint counters are priced with their own
        rates when the card names them (never added on top of an
        OpenAI-shaped inclusive input — the #276 double-count rule).
        """
        rate = self.rate_for(route)
        if rate is None:
            return None
        parts: list[float] = []
        if input_tokens is not None:
            parts.append(input_tokens / 1_000_000 * rate.input_per_mtok_usd)
        if output_tokens is not None:
            parts.append(output_tokens / 1_000_000 * rate.output_per_mtok_usd)
        if not parts:
            return None
        disjoint = route.provider == "claude-code" or cache_write_tokens is not None
        if disjoint:
            if cached_input_tokens is not None:
                unit = (
                    rate.cached_input_per_mtok_usd
                    if rate.cached_input_per_mtok_usd is not None
                    else rate.input_per_mtok_usd
                )
                parts.append(cached_input_tokens / 1_000_000 * unit)
            if cache_write_tokens is not None and rate.cache_write_per_mtok_usd is not None:
                parts.append(cache_write_tokens / 1_000_000 * rate.cache_write_per_mtok_usd)
        return math.fsum(parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "currency": self.currency,
            "rates": [rate.to_json() for rate in self.rates],
            "note": (
                "versioned price assumptions — every figure priced from this card"
                " is an ESTIMATE, never a billing record"
            ),
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> RateCard:
        rates_raw = block.get("rates")
        return cls(
            version=_text(block.get("version")),
            currency=_text(block.get("currency")) or "usd",
            rates=tuple(ModelRate.from_json(r) for r in rates_raw if isinstance(r, Mapping))
            if isinstance(rates_raw, list)
            else (),
        )


# ----------------------------------------------------------------------
# The joined rows
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PricedEntry:
    """One receipt priced — its basis labelled, its evidence class named.

    ``billed_usd`` is the receipt's OWN cost figure (basis ``billing``);
    ``estimate_usd`` is the rate card's figure over its token counters
    (basis ``estimate``). A receipt carries at most one of them; neither
    column ever sums the other. R38-09 adds two additive identity fields
    ingested lane receipts carry: ``cost_basis`` — WHERE the billed figure
    came from (``provider-reported``: the SDK's own meter; ``estimated``:
    a rate card; ``billing-reconciliation``: a billing export) — and
    ``segment``, the attribution segment over route + route version +
    rate card (a change writes NEW rows; history is never rewritten).
    """

    work_id: str
    attempt_id: str
    receipt_id: str
    source: str
    route_key: str
    evidence_class: str
    basis: str
    billed_usd: float | None
    estimate_usd: float | None
    rate_card_version: str = ""
    input_tokens_inclusive: int | None = None
    output_tokens: int | None = None
    cost_basis: str = ""
    segment: str = ""
    #: Q39-11 (#330): the claim's completeness — a streamed ``partial``
    #: receipt keeps its counters and cost as a LOWER BOUND only; the
    #: accepted-task ledger's columns stay inexact until a final receipt
    #: reconciles the identity (the #324 durable partial→final contract).
    completeness: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "receipt_id": self.receipt_id,
            "source": self.source,
            "route": self.route_key,
            "evidence_class": self.evidence_class,
            "basis": self.basis,
            "billed_usd": _round6(self.billed_usd),
            "estimate_usd": _round6(self.estimate_usd),
            "rate_card_version": self.rate_card_version,
            "input_tokens_inclusive": self.input_tokens_inclusive,
            "output_tokens": self.output_tokens,
            "cost_basis": self.cost_basis,
            "segment": self.segment,
            "completeness": self.completeness,
        }


@dataclass(frozen=True)
class AcceptanceRecord:
    """The outcome authority for one work — the acceptance decision.

    Joins the ledger by STABLE ID (``work_id``; the attempt outcomes by
    ``attempt_id``). ``decided_by`` records WHO accepted (a human
    operator, a review board) so acceptance is never confused with CI
    green or harness success; ``acceptance_contract`` names the bar the
    decision applied (verification check names, a contract id); the
    version fields pin the exact profile the spend ran under.
    """

    work_id: str
    accepted: bool | None
    decided_by: str
    decided_at: str = ""
    acceptance_contract: str = ""
    attempt_outcomes: Mapping[str, str] = field(default_factory=dict)
    profile_version: str = ""
    harness_version: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "accepted": self.accepted,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "acceptance_contract": self.acceptance_contract,
            "attempt_outcomes": dict(sorted(self.attempt_outcomes.items())),
            "profile_version": self.profile_version,
            "harness_version": self.harness_version,
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> AcceptanceRecord:
        accepted = block.get("accepted")
        raw_outcomes = block.get("attempt_outcomes")
        outcomes: dict[str, str] = {}
        if isinstance(raw_outcomes, Mapping):
            for attempt_id, outcome in raw_outcomes.items():
                key = _text(attempt_id)
                value = _text(outcome)
                if key and value in ("accepted", "rejected"):
                    outcomes[key] = value
        return cls(
            work_id=_text(block.get("work_id") or block.get("task_id")),
            accepted=None if accepted is None else bool(accepted),
            decided_by=_text(block.get("decided_by")),
            decided_at=_text(block.get("decided_at")),
            acceptance_contract=_text(block.get("acceptance_contract")),
            attempt_outcomes=outcomes,
            profile_version=_text(block.get("profile_version")),
            harness_version=_text(block.get("harness_version")),
        )


@dataclass(frozen=True)
class AttemptEconomics:
    """One attempt joined to its acceptance outcome and priced receipts."""

    attempt_id: str
    work_id: str
    outcome: str
    joined_to_acceptance: bool
    receipts: tuple[PricedEntry, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "work_id": self.work_id,
            "outcome": self.outcome,
            "joined_to_acceptance": self.joined_to_acceptance,
            "receipts": [entry.to_json() for entry in self.receipts],
        }


@dataclass(frozen=True)
class WorkEconomics:
    """One work's economics: every attempt kept, the decision joined."""

    work_id: str
    outcome: str
    ledger_outcome: str
    accepted: bool | None
    decided_by: str
    decided_at: str
    acceptance_contract: str
    profile_version: str
    harness_version: str
    attempts: tuple[AttemptEconomics, ...] = ()
    unobserved_attempt_ids: tuple[str, ...] = ()
    joined_to_acceptance: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "outcome": self.outcome,
            "ledger_outcome": self.ledger_outcome,
            "accepted": self.accepted,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "acceptance_contract": self.acceptance_contract,
            "profile_version": self.profile_version,
            "harness_version": self.harness_version,
            "attempts": [attempt.to_json() for attempt in self.attempts],
            "unobserved_attempt_ids": list(self.unobserved_attempt_ids),
            "joined_to_acceptance": self.joined_to_acceptance,
        }


@dataclass(frozen=True)
class CrossRunJoin:
    """One receipt identity delivered under more than one work — refused.

    The negative fixture the review named: two runs sharing a local call
    or receipt label must never cross-join. The identity is counted ONCE
    (under its lexicographically first attribution — deterministic, so
    reordering changes nothing) and every other attribution degrades to
    unknown, never to a second copy of the spend.
    """

    receipt_id: str
    attributions: tuple[str, ...] = ()
    kept_attribution: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "attributions": list(self.attributions),
            "kept_attribution": self.kept_attribution,
        }


@dataclass(frozen=True)
class EconomicsConflict:
    """A join-level disagreement — surfaced, never averaged."""

    kind: str
    identity: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "identity": self.identity, "detail": self.detail}


# ----------------------------------------------------------------------
# Evidence classification
# ----------------------------------------------------------------------


def _claim_key(claim: Any) -> str:
    """The canonical content key of a receipt claim — identity for dedup."""
    parts = [
        _text(getattr(claim, "source", "")),
        _text(getattr(getattr(claim, "route", None), "key", "")),
        repr(getattr(claim, "input_tokens", None)),
        repr(getattr(claim, "cached_input_tokens", None)),
        repr(getattr(claim, "cache_write_tokens", None)),
        repr(getattr(claim, "output_tokens", None)),
        repr(getattr(claim, "reasoning_tokens", None)),
        repr(getattr(claim, "cost_usd", None)),
        _text(getattr(claim, "completeness", "")),
    ]
    return "\x1f".join(parts)


def classify_receipt(claim: Any, *, provenance: str = "") -> str:
    """The receipt's evidence class — closed vocabulary, never guessed up.

    Explicit provenance wins (``live-model`` unless a scripted marker
    rides along); then the source/provider markers of the lab's scripted
    vendor; then a carried billed figure means a real gateway served the
    call. What remains is :data:`EVIDENCE_UNKNOWN` — treated like
    synthetic by every throughput guard, never promoted to live-model.
    """
    route = getattr(claim, "route", None)
    markers = " ".join(
        (
            provenance,
            _text(getattr(claim, "source", "")),
            _text(getattr(route, "provider", "")),
            _text(getattr(route, "model", "")),
        )
    ).lower()
    if "live-model" in markers and "scripted" not in markers:
        return EVIDENCE_LIVE_MODEL
    if any(marker in markers for marker in SYNTHETIC_SOURCE_MARKERS):
        return EVIDENCE_SYNTHETIC_COUNTER
    if getattr(claim, "cost_usd", None) is not None:
        # A billed figure only a real gateway produces.
        return EVIDENCE_LIVE_MODEL
    return EVIDENCE_UNKNOWN


# ----------------------------------------------------------------------
# The stage fold (the ledger's typed spans → stage_seconds)
# ----------------------------------------------------------------------


def _fold_stage_seconds(
    spans: Sequence[Mapping[str, Any]],
    keys: Sequence[str] = STAGE_SECONDS_KEYS,
) -> dict[str, Any]:
    """Fold typed span rows into ``stage_seconds`` — populations named.

    Every stage appears, measured or not; an unmeasured stage carries
    ``measured: false`` and names its absence (never a zero total). A
    stage total across multiple origins would mix populations, so the
    stage total is emitted only when the stage has exactly ONE
    population and that population is fully observed. Spans fold by
    identity (span id when present, content otherwise) in sorted order,
    so input reordering changes no byte. ``keys`` generalizes the fold
    beyond the latency vocabulary (Q39-11's time measures reuse it).
    """
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    seen: set[Any] = set()
    ordered: list[tuple[Any, Mapping[str, Any]]] = []
    for record in spans:
        span_type = _text(record.get("span_type"))
        if span_type not in keys:
            continue
        origin = _text(record.get("origin")) or "harness"
        span_id = _text(record.get("span_id"))
        identity = (
            span_id
            if span_id
            else (
                span_type,
                origin,
                _text(record.get("work_id")),
                _text(record.get("attempt_id")),
                record.get("seconds"),
            )
        )
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append((span_id or repr(identity), record))
    for _identity, record in sorted(ordered, key=lambda row: str(row[0])):
        span_type = _text(record.get("span_type"))
        origin = _text(record.get("origin")) or "harness"
        population = grouped.setdefault(span_type, {}).setdefault(
            f"{span_type}:{origin}",
            {"spans": 0, "unknown_spans": 0, "known_seconds": 0.0, "exact": True},
        )
        population["spans"] += 1
        seconds = _nn_float(record.get("seconds"))
        if seconds is None:
            population["unknown_spans"] += 1
            population["exact"] = False
        else:
            population["known_seconds"] += seconds
    stages: dict[str, Any] = {}
    for stage in keys:
        populations = grouped.get(stage, {})
        if not populations:
            stages[stage] = {
                "measured": False,
                "populations": {},
                "stage_total_seconds": None,
                "stage_lower_bound_seconds": None,
                "note": "no recorded windows — unknown, never zero",
            }
            continue
        population_rows: dict[str, Any] = {}
        lower_bound = 0.0
        for key in sorted(populations):
            row = populations[key]
            lower_bound += row["known_seconds"]
            population_rows[key] = {
                "spans": row["spans"],
                "unknown_spans": row["unknown_spans"],
                "total_seconds": _round6(row["known_seconds"]) if row["exact"] else None,
                "lower_bound_seconds": _round6(row["known_seconds"]),
            }
        single = len(population_rows) == 1
        exact_single = single and all(row["exact"] for row in populations.values())
        stages[stage] = {
            "measured": True,
            "populations": population_rows,
            "stage_total_seconds": _round6(lower_bound) if exact_single else None,
            "stage_lower_bound_seconds": _round6(lower_bound),
            "note": (
                ""
                if exact_single
                else (
                    "stage total withheld: multiple origins — populations never mix into one number"
                    if not single
                    else "unknown windows present — lower bound only"
                )
            ),
        }
    return stages


# ----------------------------------------------------------------------
# The linker
# ----------------------------------------------------------------------


class EconomicsLinker:
    """Join the delivery ledger to acceptance decisions and profile versions.

    ``link`` takes the :class:`DeliveryLedger` (or its stored document),
    a sequence of :class:`AcceptanceRecord`-shaped mappings (the pilot's
    task records: work/task id, the human acceptance decision, the
    per-attempt outcomes, the exact profile/harness versions), optional
    per-source provenance labels and a versioned :class:`RateCard`, and
    returns the cohort's :class:`EconomicsReport`. The join is by stable
    id only, everything is sorted by identity, and a receipt identity
    appearing under two works is refused as a :class:`CrossRunJoin`.
    """

    def link(
        self,
        ledger: DeliveryLedger | Mapping[str, Any],
        *,
        acceptance_records: Sequence[Mapping[str, Any] | AcceptanceRecord] = (),
        rate_card: RateCard | None = None,
        provenance_by_source: Mapping[str, str] | None = None,
        pilot: Mapping[str, str] | None = None,
    ) -> EconomicsReport:
        if not isinstance(ledger, DeliveryLedger):
            ledger = DeliveryLedger.from_document(ledger)

        notes: list[str] = []
        conflicts: list[EconomicsConflict] = []

        # -- acceptance records (the outcome authority) -----------------
        decisions: dict[str, AcceptanceRecord] = {}
        for record in acceptance_records:
            parsed = (
                record
                if isinstance(record, AcceptanceRecord)
                else AcceptanceRecord.from_json(record)
            )
            if not parsed.work_id:
                notes.append("an acceptance record carries no work id — skipped, never guessed")
                continue
            if parsed.work_id in decisions:
                conflicts.append(
                    EconomicsConflict(
                        kind="acceptance",
                        identity=parsed.work_id,
                        detail=(
                            "two acceptance records claim one work — the"
                            " lexicographically greatest canonical form wins,"
                            " never a merge"
                        ),
                    )
                )
            decisions[parsed.work_id] = parsed
        for work_id in sorted(set(decisions) - {work.work_id for work in ledger.works}):
            notes.append(
                f"acceptance record {work_id} references a work the ledger does not"
                " know — outcome recorded, no spend joined"
            )

        # -- global receipt identity (the cross-run guard) ---------------
        attributions: dict[str, set[tuple[str, str]]] = {}
        for work in ledger.works:
            for attempt in work.attempts:
                for claim in attempt.receipts:
                    attributions.setdefault(claim.receipt_id, set()).add(
                        (work.work_id, attempt.attempt_id)
                    )
        cross_joins: list[CrossRunJoin] = []
        refused_keys: set[tuple[str, str]] = set()
        for receipt_id in sorted(attributions):
            scopes = attributions[receipt_id]
            if len(scopes) > 1:
                ordered = sorted(scopes)
                cross_joins.append(
                    CrossRunJoin(
                        receipt_id=receipt_id,
                        attributions=tuple(f"{work}/{attempt}" for work, attempt in ordered),
                        kept_attribution=f"{ordered[0][0]}/{ordered[0][1]}",
                    )
                )
                refused_keys.update(ordered[1:])
                notes.append(
                    f"receipt {receipt_id} delivered under {len(scopes)} works —"
                    f" kept at {ordered[0][0]}/{ordered[0][1]} only, no cross-run join"
                )

        # -- fold per work ------------------------------------------------
        works: list[WorkEconomics] = []
        for ledger_work in sorted(ledger.works, key=lambda work: work.work_id):
            work_id = ledger_work.work_id
            decision = decisions.get(work_id)
            profile_version = (decision.profile_version if decision else "") or (
                ledger_work.model_version
            )
            harness_version = (decision.harness_version if decision else "") or (
                ledger_work.harness_version
            )
            if decision is None:
                notes.append(
                    f"work {work_id}: no acceptance record — outcome stays the"
                    " ledger's, spend stays in the programme, never an accepted unit"
                )
                outcome = ledger_work.outcome
            else:
                recorded_outcome = "accepted" if decision.accepted else "rejected"
                if ledger_work.outcome and ledger_work.outcome != recorded_outcome:
                    conflicts.append(
                        EconomicsConflict(
                            kind="outcome",
                            identity=work_id,
                            detail=(
                                f"ledger says {ledger_work.outcome!r}, the acceptance"
                                f" record says {recorded_outcome!r} — the acceptance"
                                " record is the authority"
                            ),
                        )
                    )
                outcome = recorded_outcome

            attempts: list[AttemptEconomics] = []
            for attempt in sorted(ledger_work.attempts, key=lambda row: row.attempt_id):
                attempt_id = attempt.attempt_id
                recorded = (decision.attempt_outcomes.get(attempt_id) if decision else "") or ""
                if not recorded:
                    recorded = attempt.outcome
                priced: list[PricedEntry] = []
                # A re-delivered receipt collapses by identity HERE too (the
                # ledger's usage fold already collapsed it for totals; the
                # claims list keeps every delivery). Content-identical
                # deliveries become one entry; disagreeing claims under one
                # identity surface as a conflict and keep the honest minimum.
                claims_by_id: dict[str, list[Any]] = {}
                for claim in sorted(attempt.receipts, key=lambda row: row.receipt_id):
                    claims_by_id.setdefault(claim.receipt_id, []).append(claim)
                claims: list[Any] = []
                for receipt_id in sorted(claims_by_id):
                    group = claims_by_id[receipt_id]
                    winner = sorted(group, key=_claim_key)[-1]
                    claims.append(winner)
                    if len({_claim_key(claim) for claim in group}) > 1:
                        conflicts.append(
                            EconomicsConflict(
                                kind="receipt",
                                identity=receipt_id,
                                detail=(
                                    f"attempt {work_id}/{attempt_id}: {len(group)}"
                                    " disagreeing claims under one receipt identity"
                                    " — the canonical form is kept, the exact total"
                                    " stays unknown, never averaged"
                                ),
                            )
                        )
                for claim in claims:
                    provenance = (
                        provenance_by_source.get(claim.source, "")
                        if provenance_by_source is not None
                        else ""
                    )
                    evidence_class = classify_receipt(claim, provenance=provenance)
                    if (work_id, attempt_id) in refused_keys:
                        # Refused: this identity was already counted under
                        # its first attribution — no second copy, ever.
                        priced.append(
                            PricedEntry(
                                work_id=work_id,
                                attempt_id=attempt_id,
                                receipt_id=claim.receipt_id,
                                source=claim.source,
                                route_key=claim.route.key,
                                evidence_class=evidence_class,
                                basis="refused",
                                billed_usd=None,
                                estimate_usd=None,
                                rate_card_version=rate_card.version if rate_card else "",
                                input_tokens_inclusive=claim.input_tokens_inclusive,
                                output_tokens=claim.output_tokens,
                                cost_basis=claim.cost_basis,
                                segment=claim.segment,
                                completeness=claim.completeness,
                            )
                        )
                        continue
                    billed = claim.cost_usd if claim.completeness != "unknown" else None
                    # The card prices the cache only for disjoint-counter
                    # (Anthropic-shaped) receipts — an OpenAI-shaped cached
                    # counter rides inside the inclusive input and is never
                    # priced on top (the #276 double-count rule).
                    estimate = (
                        rate_card.price(
                            claim.route,
                            input_tokens=claim.input_tokens,
                            cached_input_tokens=claim.cached_input_tokens,
                            cache_write_tokens=claim.cache_write_tokens,
                            output_tokens=claim.output_tokens,
                        )
                        if rate_card is not None and billed is None
                        else None
                    )
                    priced.append(
                        PricedEntry(
                            work_id=work_id,
                            attempt_id=attempt_id,
                            receipt_id=claim.receipt_id,
                            source=claim.source,
                            route_key=claim.route.key,
                            evidence_class=evidence_class,
                            basis="billing" if billed is not None else "estimate",
                            billed_usd=billed,
                            estimate_usd=estimate,
                            rate_card_version=rate_card.version if rate_card else "",
                            input_tokens_inclusive=claim.input_tokens_inclusive,
                            output_tokens=claim.output_tokens,
                            cost_basis=claim.cost_basis,
                            segment=claim.segment,
                            completeness=claim.completeness,
                        )
                    )
                attempts.append(
                    AttemptEconomics(
                        attempt_id=attempt_id,
                        work_id=work_id,
                        outcome=recorded,
                        joined_to_acceptance=bool(
                            decision and attempt_id in decision.attempt_outcomes
                        ),
                        receipts=tuple(priced),
                    )
                )
            unobserved: tuple[str, ...] = ()
            if decision is not None:
                ledger_ids = {attempt.attempt_id for attempt in ledger_work.attempts}
                unobserved = tuple(
                    sorted(
                        attempt_id
                        for attempt_id in decision.attempt_outcomes
                        if attempt_id not in ledger_ids
                    )
                )
                if unobserved:
                    notes.append(
                        f"work {work_id}: {len(unobserved)} attempt(s) named by the"
                        " acceptance record but absent from the ledger — kept as"
                        " unobserved attempts in the coverage denominator"
                    )
            works.append(
                WorkEconomics(
                    work_id=work_id,
                    outcome=outcome,
                    ledger_outcome=ledger_work.outcome,
                    accepted=decision.accepted if decision else None,
                    decided_by=decision.decided_by if decision else "",
                    decided_at=decision.decided_at if decision else "",
                    acceptance_contract=(
                        decision.acceptance_contract
                        if decision
                        else ledger_work.acceptance_contract
                    ),
                    profile_version=profile_version,
                    harness_version=harness_version,
                    attempts=tuple(attempts),
                    unobserved_attempt_ids=unobserved,
                    joined_to_acceptance=decision is not None,
                )
            )

        # -- the latency stage fold over the LEDGER's own spans ----------
        spans = [
            span.to_json()
            for work in sorted(ledger.works, key=lambda work: work.work_id)
            for attempt in sorted(work.attempts, key=lambda row: row.attempt_id)
            for span in sorted(attempt.spans, key=lambda row: row.span_id)
        ]
        return EconomicsReport(
            pilot=dict(pilot or {}),
            works=tuple(works),
            latency_stages=_fold_stage_seconds(spans),
            cross_joins=tuple(sorted(cross_joins, key=lambda row: row.receipt_id)),
            conflicts=tuple(sorted(conflicts, key=lambda row: (row.kind, row.identity))),
            rate_card=rate_card,
            notes=tuple(sorted(set(notes))),
        )


# ----------------------------------------------------------------------
# The report — the pure fold of the joined rows
# ----------------------------------------------------------------------


def _sum(values: Sequence[float]) -> float:
    """Sum exactly and deterministically over the given (sorted) order."""
    return math.fsum(values)


@dataclass(frozen=True)
class EconomicsReport:
    """The linker's output: joined works, named conflicts, one rate card.

    The latency stages are folded at LINK time from the ledger's own
    spans (there is no second latency truth store); every aggregate in
    :meth:`to_document` is a pure fold of these joined rows, so the
    document is byte-stable under any input ordering.
    """

    pilot: Mapping[str, str] = field(default_factory=dict)
    works: tuple[WorkEconomics, ...] = ()
    latency_stages: Mapping[str, Any] = field(default_factory=dict)
    cross_joins: tuple[CrossRunJoin, ...] = ()
    conflicts: tuple[EconomicsConflict, ...] = ()
    rate_card: RateCard | None = None
    notes: tuple[str, ...] = ()

    @property
    def schema(self) -> str:
        return ECONOMICS_SCHEMA

    def work_by_id(self, work_id: str) -> WorkEconomics | None:
        for work in self.works:
            if work.work_id == work_id:
                return work
        return None

    # -- aggregate helpers (pure, order-invariant) ----------------------

    def _entries(self, *, accepted_only: bool = False) -> list[PricedEntry]:
        rows: list[PricedEntry] = []
        for work in sorted(self.works, key=lambda row: row.work_id):
            if accepted_only and work.outcome != "accepted":
                continue
            for attempt in sorted(work.attempts, key=lambda row: row.attempt_id):
                rows.extend(sorted(attempt.receipts, key=lambda row: row.receipt_id))
        return rows

    def _fold_totals(self, *, accepted_only: bool = False) -> dict[str, Any]:
        """Fold billed and estimate columns over a population of works.

        ``billed_usd`` is EXACT only when the population is non-empty,
        nothing is unobserved, nothing was cross-joined, and every
        attempt carries a receipt priced from a billed figure; otherwise
        ``None`` with the lower bound and coverage naming the gap. The
        estimate column is always a card-labelled ESTIMATE and never
        heals the billed column's unknowns.
        """
        works = sorted(self.works, key=lambda row: row.work_id)
        if accepted_only:
            works = [work for work in works if work.outcome == "accepted"]
        attempt_count = sum(len(work.attempts) for work in works)
        unobserved = sum(len(work.unobserved_attempt_ids) for work in works)
        entries = self._entries(accepted_only=accepted_only)
        billed_entries = [entry for entry in entries if entry.basis == "billing"]
        receipt_conflicts = [row for row in self.conflicts if row.kind == "receipt"]
        billed_exact = bool(
            works
            and attempt_count > 0
            and unobserved == 0
            and not self.cross_joins
            and not receipt_conflicts
            and len(billed_entries) == attempt_count
            and all(entry.billed_usd is not None for entry in billed_entries)
        )
        billed_values = [
            entry.billed_usd for entry in billed_entries if entry.billed_usd is not None
        ]
        estimate_entries = [entry for entry in entries if entry.estimate_usd is not None]
        coverage_expected = attempt_count + unobserved
        coverage_received = len({entry.receipt_id for entry in entries})
        # R38-09: the provider-reported column — the SDK's own meter, named
        # separately from billing-reconciliation figures and never summed
        # with estimates.
        provider_reported = [
            entry.billed_usd
            for entry in billed_entries
            if entry.cost_basis == "provider-reported" and entry.billed_usd is not None
        ]
        return {
            "works": len(works),
            "attempts": attempt_count,
            "unobserved_attempts": unobserved,
            "receipts": coverage_received,
            "billed_usd": _round6(_sum(billed_values)) if billed_exact else None,
            "billed_exact": billed_exact,
            "billed_known_lower_bound_usd": _round6(_sum(billed_values)),
            "provider_reported_usd": _round6(_sum(provider_reported))
            if provider_reported
            else None,
            "estimate_usd": (
                _round6(_sum([entry.estimate_usd or 0.0 for entry in estimate_entries]))
                if estimate_entries
                else None
            ),
            "estimate_basis": "estimate",
            "estimate_rate_card_version": self.rate_card.version if self.rate_card else "",
            "receipt_coverage": (
                coverage_received / coverage_expected if coverage_expected else None
            ),
        }

    def _coverage_block(self) -> dict[str, Any]:
        """Measured / lower-bound / unknown completeness per population."""
        populations: dict[str, dict[str, int]] = {}
        for population, accepted_only in (("programme", False), ("accepted_items", True)):
            counts: dict[str, int] = {
                "attempts": 0,
                "receipts_expected": 0,
                "receipts_received": 0,
                "measured_receipts": 0,
                "cost_known_receipts": 0,
                "unknown_cost_receipts": 0,
            }
            for work in sorted(self.works, key=lambda row: row.work_id):
                if accepted_only and work.outcome != "accepted":
                    continue
                for attempt in sorted(work.attempts, key=lambda row: row.attempt_id):
                    counts["attempts"] += 1
                    counts["receipts_expected"] += 1
                    received = [entry for entry in attempt.receipts if entry.basis != "refused"]
                    if received:
                        counts["receipts_received"] += 1
                        if any(
                            entry.input_tokens_inclusive is not None
                            or entry.output_tokens is not None
                            for entry in received
                        ):
                            counts["measured_receipts"] += 1
                        if any(entry.billed_usd is not None for entry in received):
                            counts["cost_known_receipts"] += 1
                        else:
                            counts["unknown_cost_receipts"] += 1
                counts["receipts_expected"] += len(work.unobserved_attempt_ids)
            populations[population] = counts
        block: dict[str, Any] = {}
        for population in sorted(populations):
            counts = populations[population]
            expected_count = counts["receipts_expected"]
            received_count = counts["receipts_received"]
            block[population] = {
                **counts,
                "receipt_coverage": (received_count / expected_count) if expected_count else None,
                "cost_coverage": (
                    (counts["cost_known_receipts"] / expected_count) if expected_count else None
                ),
                "unknown_costs_rendered_as": "known-lower-bound + coverage, never zero",
            }
        return block

    def _evidence_block(self) -> dict[str, Any]:
        by_class: dict[str, int] = {}
        for entry in self._entries():
            by_class[entry.evidence_class] = by_class.get(entry.evidence_class, 0) + 1
        return {
            "classes": dict(sorted(by_class.items())),
            "synthetic_counters_labelled": by_class.get(EVIDENCE_SYNTHETIC_COUNTER, 0),
            "synthetic_excluded_from": ["throughput comparisons", "decode rate labels"],
            "note": (
                "scripted-vendor counters are wire observations, not measured"
                " inference — labelled synthetic-vendor-counter and excluded from"
                " every throughput comparison (R37-13)"
            ),
        }

    def _cost_basis_census(self) -> dict[str, Any]:
        """The estimated / provider-reported / billing-reconciliation split.

        R38-09 scope item 5: every priced entry names WHERE its figure came
        from, and the three never sum into one column. An entry whose
        source carried no explicit basis keeps the historical default of
        its column (a billed figure without a stated origin is grouped as
        ``billing-reconciliation`` — the conservative reading, never
        promoted to provider-reported).
        """
        census: dict[str, dict[str, Any]] = {}
        for entry in self._entries():
            if entry.basis == "refused":
                continue
            if entry.cost_basis:
                basis = entry.cost_basis
            elif entry.basis == "billing":
                basis = "billing-reconciliation"
            else:
                basis = "estimated"
            row = census.setdefault(basis, {"receipts": 0, "usd": 0.0})
            row["receipts"] += 1
            figure = entry.billed_usd if entry.billed_usd is not None else entry.estimate_usd
            row["usd"] = _round6((row["usd"] or 0.0) + (figure or 0.0))
        return {
            "bases": dict(sorted(census.items())),
            "note": (
                "cost figures are separated by origin: provider-reported (the"
                " SDK's own meter), estimated (a versioned rate card),"
                " billing-reconciliation (a billing export) — never summed"
                " into one column (R38-09)"
            ),
        }

    def _attribution_segments(self) -> list[dict[str, Any]]:
        """The per-segment fold — a route/version or card change is a NEW row.

        Segments group each population's priced entries by their carried
        attribution segment (route + route version + rate-card identity;
        unlabelled historical entries share the ``""`` segment of their
        route). Existing segments are immutable: a changed route, model
        version or rate card writes entries under a NEW segment id and the
        fold simply shows both — history is never rewritten.
        """
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in self._entries():
            if entry.basis == "refused":
                continue
            key = (entry.segment or f"route:{entry.route_key}", entry.route_key)
            row = grouped.setdefault(
                key,
                {
                    "segment": entry.segment or f"route:{entry.route_key}",
                    "route": entry.route_key,
                    "rate_card_versions": set(),
                    "receipts": 0,
                    "cost_usd": 0.0,
                    "exact": True,
                },
            )
            row["receipts"] += 1
            if entry.rate_card_version:
                row["rate_card_versions"].add(entry.rate_card_version)
            figure = entry.billed_usd if entry.billed_usd is not None else entry.estimate_usd
            row["cost_usd"] = _round6((row["cost_usd"] or 0.0) + (figure or 0.0))
            if figure is None:
                row["exact"] = False
        return [
            {
                "segment": row["segment"],
                "route": row["route"],
                "rate_card_versions": sorted(row["rate_card_versions"]),
                "receipts": row["receipts"],
                "cost_usd": row["cost_usd"],
                "note": (
                    "cost figures grouped by attribution segment — a route,"
                    " route-version or rate-card change is a NEW segment;"
                    " historical segments are never rewritten (R38-09)"
                ),
            }
            for row in sorted(grouped.values(), key=lambda row: (row["segment"], row["route"]))
        ]

    def _successful_attempt_costs(self) -> dict[str, dict[str, dict[str, Any]]]:
        """The SUCCESSFUL-ATTEMPT cost — the third distinct measure (R38-09).

        The accepted/successful attempt's OWN receipts only: never the
        work's failed precursors (that is the accepted-item all-attempt
        total), never the programme's other works (that is
        programme-per-accepted). An attempt with no priced receipt renders
        as an explicit unknown with a zero lower bound and a note — never
        an exact zero cost.
        """
        costs: dict[str, dict[str, dict[str, Any]]] = {}
        for work in sorted(self.works, key=lambda row: row.work_id):
            for attempt in sorted(work.attempts, key=lambda row: row.attempt_id):
                if attempt.outcome != "accepted":
                    continue
                entries = [entry for entry in attempt.receipts if entry.basis not in ("refused",)]
                billed = [entry for entry in entries if entry.billed_usd is not None]
                conflicted = any(
                    row.kind == "receipt" and row.identity in {e.receipt_id for e in entries}
                    for row in self.conflicts
                )
                exact = bool(entries and billed and not conflicted and len(billed) == len(entries))
                costs.setdefault(work.work_id, {})[attempt.attempt_id] = {
                    "cost_usd": _round6(_sum([e.billed_usd or 0.0 for e in billed]))
                    if exact
                    else None,
                    "known_cost_lower_bound_usd": _round6(
                        _sum([e.billed_usd or 0.0 for e in billed])
                    ),
                    "receipts": len(entries),
                    "note": (
                        "the successful attempt's OWN receipts — a different"
                        " measure from the accepted item's all-attempt total and"
                        " from programme spend per accepted item (R38-09)"
                        + ("" if entries else " — no priced receipt: unknown, never zero")
                    ),
                }
        return costs

    def to_document(self) -> dict[str, Any]:
        """The stored artifact: the full stamped economics report."""
        programme = self._fold_totals(accepted_only=False)
        accepted = self._fold_totals(accepted_only=True)
        accepted_count = accepted["works"]
        per_accepted: dict[str, Any] = {
            "programme_billed_per_accepted_usd": (
                _round6(programme["billed_usd"] / accepted_count)
                if programme["billed_usd"] is not None and accepted_count
                else None
            ),
            "programme_billed_lower_bound_per_accepted_usd": (
                _round6(programme["billed_known_lower_bound_usd"] / accepted_count)
                if accepted_count
                else None
            ),
            "programme_estimate_per_accepted_usd": (
                _round6(programme["estimate_usd"] / accepted_count)
                if programme["estimate_usd"] is not None and accepted_count
                else None
            ),
        }
        accepted_item_totals: dict[str, dict[str, Any]] = {}
        cross_run_receipt_ids = {row.receipt_id for row in self.cross_joins}
        for work in sorted(self.works, key=lambda row: row.work_id):
            if work.outcome != "accepted":
                continue
            entries = sorted(
                (entry for attempt in work.attempts for entry in attempt.receipts),
                key=lambda row: row.receipt_id,
            )
            billed_entries = [entry for entry in entries if entry.basis == "billing"]
            refused = any(entry.basis == "refused" for entry in entries)
            ambiguous = any(entry.receipt_id in cross_run_receipt_ids for entry in entries)
            conflicted = any(
                row.identity in {entry.receipt_id for entry in entries}
                for row in self.conflicts
                if row.kind == "receipt"
            )
            exact = bool(
                entries
                and not refused
                and not ambiguous
                and not conflicted
                and not work.unobserved_attempt_ids
                and len(billed_entries) == len(work.attempts)
                and all(entry.billed_usd is not None for entry in billed_entries)
            )
            accepted_item_totals[work.work_id] = {
                "attempts": len(work.attempts),
                "failed_or_superseded_attempts_kept": sum(
                    1 for attempt in work.attempts if attempt.outcome in _REJECTED_OUTCOMES
                ),
                "billed_usd": (
                    _round6(_sum([entry.billed_usd or 0.0 for entry in billed_entries]))
                    if exact
                    else None
                ),
                "billed_known_lower_bound_usd": _round6(
                    _sum([entry.billed_usd or 0.0 for entry in billed_entries])
                ),
                "estimate_usd": (
                    _round6(_sum([entry.estimate_usd or 0.0 for entry in entries]))
                    if any(entry.estimate_usd is not None for entry in entries)
                    else None
                ),
                "receipt_ids": [entry.receipt_id for entry in entries],
            }

        notes = list(self.notes)
        notes.append(
            "cost columns never mix: billed figures come from receipts that carry"
            " them; every other figure is a rate-card ESTIMATE labelled with the"
            " card version"
        )
        notes.append(
            "unknown costs render as known lower bound + coverage — an unobserved"
            " attempt or receipt is never zero and never estimated as spend"
        )
        if not accepted_count:
            notes.append("no accepted items — per-accepted economics are undefined, never zero")
        document = {
            "schema": ECONOMICS_SCHEMA,
            "pilot": dict(sorted(self.pilot.items())),
            "rate_card": self.rate_card.to_json() if self.rate_card else None,
            "works": [work.to_json() for work in sorted(self.works, key=lambda row: row.work_id)],
            "costs": {
                "programme": programme,
                "accepted_items": accepted,
                "programme_per_accepted_item": per_accepted,
                "accepted_item_totals": dict(sorted(accepted_item_totals.items())),
                "successful_attempt_costs": dict(
                    sorted(
                        (
                            work_id,
                            dict(sorted(attempts.items())),
                        )
                        for work_id, attempts in self._successful_attempt_costs().items()
                    )
                ),
                "cost_basis_census": self._cost_basis_census(),
                "attribution_segments": self._attribution_segments(),
                "coverage": self._coverage_block(),
            },
            "evidence": self._evidence_block(),
            "latency": {
                "stage_seconds": dict(sorted(self.latency_stages.items())),
                "decode_label_guard": (
                    "no decode label rides a request-latency population: decode"
                    " throughput requires live-model receipts over a model-span"
                    " population (RATE_LABEL_SPACE, extended by R37-13)"
                ),
            },
            "cross_joins": [row.to_json() for row in self.cross_joins],
            "conflicts": [row.to_json() for row in self.conflicts],
            "notes": sorted(set(notes)),
            "observability": {
                "usage.receipt_coverage": programme["receipt_coverage"],
                "cost.lower_bound": programme["billed_known_lower_bound_usd"],
                "cost.accepted_item_total": {
                    work_id: row["billed_usd"]
                    for work_id, row in sorted(accepted_item_totals.items())
                },
                "cost.programme_per_accepted_item": per_accepted[
                    "programme_billed_per_accepted_usd"
                ],
                "latency.stage_seconds": {
                    stage: row["stage_lower_bound_seconds"]
                    for stage, row in sorted(self.latency_stages.items())
                },
            },
        }
        assert_latency_guards(document)
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> EconomicsReport:
        """Rebuild from the stored document — the replay path's parser."""
        stamp = _text(document.get("schema")) or ECONOMICS_SCHEMA
        if stamp != ECONOMICS_SCHEMA:
            raise ValueError(
                f"economics document carries schema {stamp!r}, expected {ECONOMICS_SCHEMA!r}"
            )
        card_raw = document.get("rate_card")
        works_raw = document.get("works") or []
        cross_raw = document.get("cross_joins") or []
        conflicts_raw = document.get("conflicts") or []
        latency_raw = document.get("latency")
        stages_raw = latency_raw.get("stage_seconds") if isinstance(latency_raw, Mapping) else {}

        def _attempt(block: Mapping[str, Any]) -> AttemptEconomics:
            receipts_raw = block.get("receipts") or []
            return AttemptEconomics(
                attempt_id=_text(block.get("attempt_id")),
                work_id=_text(block.get("work_id")),
                outcome=_text(block.get("outcome")),
                joined_to_acceptance=bool(block.get("joined_to_acceptance")),
                receipts=tuple(
                    PricedEntry(
                        work_id=_text(row.get("work_id")),
                        attempt_id=_text(row.get("attempt_id")),
                        receipt_id=_text(row.get("receipt_id")),
                        source=_text(row.get("source")),
                        route_key=_text(row.get("route")),
                        evidence_class=_text(row.get("evidence_class")),
                        basis=_text(row.get("basis")),
                        billed_usd=_nn_float(row.get("billed_usd")),
                        estimate_usd=_nn_float(row.get("estimate_usd")),
                        rate_card_version=_text(row.get("rate_card_version")),
                        input_tokens_inclusive=_nn_int(row.get("input_tokens_inclusive")),
                        output_tokens=_nn_int(row.get("output_tokens")),
                        cost_basis=_text(row.get("cost_basis")),
                        segment=_text(row.get("segment")),
                        completeness=_text(row.get("completeness")),
                    )
                    for row in receipts_raw
                    if isinstance(row, Mapping)
                ),
            )

        def _work(block: Mapping[str, Any]) -> WorkEconomics:
            attempts_raw = block.get("attempts") or []
            return WorkEconomics(
                work_id=_text(block.get("work_id")),
                outcome=_text(block.get("outcome")),
                ledger_outcome=_text(block.get("ledger_outcome")),
                accepted=None if block.get("accepted") is None else bool(block.get("accepted")),
                decided_by=_text(block.get("decided_by")),
                decided_at=_text(block.get("decided_at")),
                acceptance_contract=_text(block.get("acceptance_contract")),
                profile_version=_text(block.get("profile_version")),
                harness_version=_text(block.get("harness_version")),
                attempts=tuple(_attempt(row) for row in attempts_raw if isinstance(row, Mapping)),
                unobserved_attempt_ids=tuple(
                    _text(attempt_id) for attempt_id in block.get("unobserved_attempt_ids") or ()
                ),
                joined_to_acceptance=bool(block.get("joined_to_acceptance")),
            )

        pilot_raw = document.get("pilot")
        return cls(
            pilot=(
                {_text(key): _text(value) for key, value in pilot_raw.items()}
                if isinstance(pilot_raw, Mapping)
                else {}
            ),
            works=tuple(_work(row) for row in works_raw if isinstance(row, Mapping)),
            latency_stages=stages_raw if isinstance(stages_raw, Mapping) else {},
            cross_joins=tuple(
                CrossRunJoin(
                    receipt_id=_text(row.get("receipt_id")),
                    attributions=tuple(_text(item) for item in row.get("attributions") or ()),
                    kept_attribution=_text(row.get("kept_attribution")),
                )
                for row in cross_raw
                if isinstance(row, Mapping)
            ),
            conflicts=tuple(
                EconomicsConflict(
                    kind=_text(row.get("kind")),
                    identity=_text(row.get("identity")),
                    detail=_text(row.get("detail")),
                )
                for row in conflicts_raw
                if isinstance(row, Mapping)
            ),
            rate_card=RateCard.from_json(card_raw) if isinstance(card_raw, Mapping) else None,
            notes=tuple(str(note) for note in document.get("notes") or ()),
        )


# ----------------------------------------------------------------------
# The guards: decode labels, throughput separations
# ----------------------------------------------------------------------


def decode_throughput(
    *,
    tokens: int,
    seconds: float,
    population_identity: str,
    evidence_classes: Sequence[str],
) -> dict[str, Any]:
    """The guarded decode-rate constructor — the #276 rule extended.

    First the population guard (:func:`measured_rate` raises
    :class:`RateLabelError` when the denominator is anything but a model
    span population — output over request latency is not decode speed).
    Then the evidence guard: a synthetic vendor counter (or an
    unclassified receipt) among the receipts raises
    :class:`EvidenceClassError` — scripted counters are excluded from
    throughput comparisons, never averaged in.
    """
    rate = measured_rate(
        "decode_output_tokens_per_second",
        tokens=tokens,
        seconds=seconds,
        population_identity=population_identity,
    )
    classes = set(evidence_classes)
    if classes - {EVIDENCE_LIVE_MODEL}:
        raise EvidenceClassError(
            f"decode throughput over evidence classes {sorted(classes)}:"
            " only live-model receipts support a throughput label —"
            " synthetic-vendor-counter and unknown evidence are excluded"
            " from throughput comparisons (R37-13)"
        )
    return {**rate, "evidence_class": EVIDENCE_LIVE_MODEL}


def throughput_comparison(
    *,
    label: str,
    tokens: int,
    seconds: float,
    population_identity: str,
    evidence_classes: Sequence[str],
) -> dict[str, Any]:
    """Compare throughput between populations — with both guards applied.

    A throughput comparison mixing evidence classes (a scripted-vendor
    trace beside real billing records) is REFUSED with
    :class:`EvidenceClassError`; the caller separates the classes and
    compares each within its own class only. The label must be the one
    sanctioned decode label — anything else is a request-latency
    population wearing a throughput name, and raises.
    """
    if label != "decode_output_tokens_per_second":
        raise RateLabelError(
            f"label {label!r} is outside the sanctioned rate-label space"
            f" {sorted(RATE_LABEL_SPACE)} — no throughput comparison exists"
            " over request-latency populations (R37-13)"
        )
    return decode_throughput(
        tokens=tokens,
        seconds=seconds,
        population_identity=population_identity,
        evidence_classes=evidence_classes,
    )


def assert_latency_guards(document: Mapping[str, Any]) -> None:
    """Assert the document carries no decode label on request-latency data.

    The guard every economics document is subject to at BUILD time (see
    :meth:`EconomicsReport.to_document`) and at READ time: every
    throughput row must carry a model-span population identity and the
    live-model evidence class. A tampered document — a decode label over
    ``model + queue + human_wait`` seconds, or over synthetic receipts —
    raises.
    """
    latency = document.get("latency")
    if not isinstance(latency, Mapping):
        return
    rows = latency.get("throughput")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        label = _text(row.get("label"))
        population_identity = _text(row.get("population_identity"))
        allowed = RATE_LABEL_SPACE.get(label)
        if allowed is None:
            raise RateLabelError(
                f"a throughput row carries label {label!r} outside"
                f" {sorted(RATE_LABEL_SPACE)} — request-latency populations carry"
                " no decode label (R37-13)"
            )
        key = population_identity.removeprefix("latency.").split(":works=")[0]
        if key not in allowed:
            raise RateLabelError(
                f"label {label!r} rides population {population_identity!r} —"
                " no decode label on request-latency populations"
            )
        if _text(row.get("evidence_class")) != EVIDENCE_LIVE_MODEL:
            raise EvidenceClassError(
                f"label {label!r} rides evidence class"
                f" {_text(row.get('evidence_class'))!r} — synthetic counters are"
                " excluded from throughput comparisons"
            )


# ----------------------------------------------------------------------
# The operator summary and the budget reconciliation
# ----------------------------------------------------------------------


def redact_for_operator(value: Any) -> Any:
    """Recursively drop sensitive prompt/tool/task content keys.

    The billing-export rule: an operator-facing dict carries ids, counts
    and dollars — never prompt text, tool payloads, diffs or task
    briefs. Keys naming those are removed at every depth; lists and
    tuples are mapped; scalars pass. This is a projection of the ONE
    truth store (the report), not a second one.
    """
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if any(marker in name for marker in SENSITIVE_KEY_MARKERS):
                redacted[str(key)] = "[redacted]"
                continue
            redacted[str(key)] = redact_for_operator(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_for_operator(item) for item in value]
    return value


def operator_summary(report: EconomicsReport | Mapping[str, Any]) -> dict[str, Any]:
    """The compact, redacted operator summary — from the ledger's report.

    No prompt, tool or task content (every sensitive key redacted); no
    second truth store (a pure fold of the report document, which is a
    pure fold of the ledger). What the operator gets: the join counts,
    the cost columns with their bases, the coverage, the latency stages
    and the evidence-class census — enough to explain total resources
    and spot the bottleneck (model vs CI vs review vs repeated work)
    without seeing code or secrets.
    """
    document = report.to_document() if isinstance(report, EconomicsReport) else dict(report)
    costs: Mapping[str, Any] = document.get("costs") or {}
    programme: Mapping[str, Any] = costs.get("programme") or {}
    accepted: Mapping[str, Any] = costs.get("accepted_items") or {}
    per_accepted: Mapping[str, Any] = costs.get("programme_per_accepted_item") or {}
    coverage: Mapping[str, Any] = costs.get("coverage") or {}
    latency: Mapping[str, Any] = document.get("latency") or {}
    evidence: Mapping[str, Any] = document.get("evidence") or {}
    stage_seconds: Mapping[str, Any] = latency.get("stage_seconds") or {}
    totals: Mapping[str, Any] = costs.get("accepted_item_totals") or {}
    repeats = sum(
        int(row.get("failed_or_superseded_attempts_kept") or 0)
        for row in totals.values()
        if isinstance(row, Mapping)
    )
    summary = {
        "schema": ECONOMICS_SCHEMA,
        "pilot": document.get("pilot") or {},
        "join": {
            "works": programme.get("works"),
            "attempts": programme.get("attempts"),
            "accepted_items": accepted.get("works"),
            "unobserved_attempts": programme.get("unobserved_attempts"),
            "cross_run_joins_refused": len(document.get("cross_joins") or []),
        },
        "costs": {
            "programme_billed_usd": programme.get("billed_usd"),
            "programme_billed_lower_bound_usd": programme.get("billed_known_lower_bound_usd"),
            "programme_estimate_usd": programme.get("estimate_usd"),
            "estimate_rate_card_version": programme.get("estimate_rate_card_version"),
            "per_accepted_item": dict(per_accepted),
            "repeated_work_attempts": repeats,
        },
        "coverage": {
            population: {
                "receipt_coverage": row.get("receipt_coverage"),
                "unknown_cost_receipts": row.get("unknown_cost_receipts"),
            }
            for population, row in sorted(coverage.items())
            if isinstance(row, Mapping)
        },
        "latency_stage_seconds": {
            stage: {
                "total": row.get("stage_total_seconds"),
                "lower_bound": row.get("stage_lower_bound_seconds"),
            }
            for stage, row in sorted(stage_seconds.items())
            if isinstance(row, Mapping)
        },
        "evidence_classes": evidence.get("classes") or {},
        "bottleneck_hint": (
            "compare model stage seconds against queue/verification/human_wait"
            " lower bounds and repeated_work_attempts — each names a different"
            " bottleneck; unknown stages are gaps, not zeros"
        ),
    }
    return redact_for_operator(summary)


def reconcile_with_budget(
    report: EconomicsReport | Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any]:
    """Report totals vs the agreed cap — discrepancies stated, never absorbed.

    The customer-facing reconciliation: one line per cost column against
    the cap. A line is ``within`` only when its column is exact and
    under the cap; ``over`` when exact and above; ``unreconcilable``
    when the column is a lower bound or an estimate — an incomplete
    report side can never certify a budget, and an estimate is an
    assumption, not spend. No code, prompts or secrets appear: ids,
    numbers and reasons only.
    """
    document = report.to_document() if isinstance(report, EconomicsReport) else dict(report)
    costs: Mapping[str, Any] = document.get("costs") or {}
    programme: Mapping[str, Any] = costs.get("programme") or {}
    cap = _nn_float(budget.get("cap_usd") or budget.get("cap_usd_per_pilot"))
    currency = _text(budget.get("currency")) or "usd"
    lines: list[dict[str, Any]] = []

    def _line(
        scope: str,
        basis: str,
        reported: float | None,
        exact: bool,
        extra_reason: str = "",
    ) -> dict[str, Any]:
        if cap is None:
            status = "unreconcilable"
            reason = "the budget carries no parseable cap — cannot reconcile"
        elif reported is None:
            status = "unreconcilable"
            reason = extra_reason or "column unknown — a gap is never zero and never certifies"
        elif not exact:
            status = "unreconcilable"
            reason = (
                extra_reason
                or "column is a known lower bound, not an exact total — cannot certify the cap"
            )
        else:
            delta = round(cap - reported, 6)
            status = "within" if delta >= 0 else "over"
            reason = (
                f"exact billed total {reported} usd vs cap {cap} usd"
                f" ({'headroom' if delta >= 0 else 'overrun'} {abs(delta)} usd)"
            )
        return {
            "scope": scope,
            "basis": basis,
            "cap_usd": _round6(cap) if cap is not None else None,
            "reported_usd": _round6(reported) if reported is not None else None,
            "delta_usd": (
                _round6(cap - reported) if cap is not None and reported is not None else None
            ),
            "status": status,
            "reason": reason,
        }

    coverage = _nn_float(programme.get("receipt_coverage"))
    lines.append(
        _line(
            "programme.billed",
            "billing",
            _nn_float(programme.get("billed_usd")),
            bool(programme.get("billed_exact")),
            extra_reason=(
                "billed spend unknown (receipt coverage"
                f" {_round6(coverage) if coverage is not None else 'n/a'}) —"
                " unknown spend cannot certify the cap, never zero"
            ),
        )
    )
    lines.append(
        _line(
            "programme.lower_bound",
            "billing",
            _nn_float(programme.get("billed_known_lower_bound_usd")),
            False,
            extra_reason=(
                "lower bounds bound from below only — stated beside the cap,"
                " never reconciled against it"
            ),
        )
    )
    lines.append(
        _line(
            "programme.estimate",
            "estimate",
            _nn_float(programme.get("estimate_usd")),
            False,
            extra_reason=(
                "estimate from rate card"
                f" {_text(programme.get('estimate_rate_card_version')) or 'unversioned'}"
                " — an assumption, not spend; can inform, never certify"
            ),
        )
    )
    return {
        "schema": ECONOMICS_SCHEMA,
        "currency": currency,
        "cap_usd": _round6(cap) if cap is not None else None,
        "lines": lines,
        "unreconcilable": sum(1 for line in lines if line["status"] == "unreconcilable"),
        "note": (
            "the customer reconciles totals with the agreed budget through this"
            " shape — ids, numbers and reasons only; no code, prompts or secrets"
        ),
    }


# ----------------------------------------------------------------------
# Q39-11 (#330) — the accepted-task ledger: ONE complete evidence chain
# ----------------------------------------------------------------------


#: The versioned stamp of the accepted-task ledger document.
ACCEPTED_LEDGER_SCHEMA = "forge.delivery.accepted-ledger/1"

#: The identity chain every accepted task is joined over, in join order
#: (the issue's scope item 1): the run (work) → its execution attempts →
#: the model-call receipts that metered them → the native jobs that ran
#: them → the published candidates → the verification that tested the
#: candidate → the human decision that accepted or rejected it.
IDENTITY_CHAIN: tuple[str, ...] = (
    "run",
    "attempt",
    "model_call",
    "native_job",
    "candidate",
    "verification",
    "human_decision",
)

#: The time measures the issue names as DIFFERENT quantities — never
#: folded into one another, never divided across populations (Q39-11
#: scope item 4). Each measure folds only its own recorded windows.
TIME_MEASURES: tuple[str, ...] = (
    "model_time",
    "tool_time",
    "ci_queue",
    "ci_runtime",
    "operator_wait",
    "reviewer_effort",
    "setup_effort",
)

#: The three cost columns of the accepted-task ledger — the issue's
#: scope item 2, kept as LABELED columns that never blend: the provider's
#: own SDK meter, the versioned price card's estimate, and a billing
#: reconciliation. Every priced entry lands in AT MOST one column.
COST_COLUMN_PROVIDER_REPORTED = "provider_reported_usd"
COST_COLUMN_PRICE_CARD = "price_card_estimate_usd"
COST_COLUMN_BILLING = "billing_reconciliation_usd"
ACCEPTED_COST_COLUMNS: tuple[str, ...] = (
    COST_COLUMN_PROVIDER_REPORTED,
    COST_COLUMN_PRICE_CARD,
    COST_COLUMN_BILLING,
)

#: The human-decision states the ledger admits. ``pending`` is a REAL
#: state (a draft MR awaiting its human), never coerced to a neighbour;
#: only ``accepted``/``rejected`` close a work's economics.
HUMAN_DECISION_STATES = ("accepted", "rejected", "pending", "unknown")

#: The budget-event kinds (Q39-11 scope item 6 — visible separately).
BUDGET_EVENT_KINDS = ("budget_refusal", "review_only_recovery")


def _canonical_row(row: Mapping[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def _entry_cost_basis(entry: PricedEntry) -> str:
    """The entry's cost origin — the census mapping, ONE spelling.

    An explicit ``cost_basis`` wins (the ingested lane receipts carry
    one); a historical billed figure without a stated origin groups as
    ``billing-reconciliation`` (the conservative reading); everything
    else is an estimate.
    """
    if entry.cost_basis:
        return entry.cost_basis
    if entry.basis == "billing":
        return COST_BASIS_BILLING
    return COST_BASIS_ESTIMATED


def _entry_column(entry: PricedEntry) -> str | None:
    """The ONE column an entry's figure belongs to — or ``None``.

    ``None`` means the entry carries no figure for any column (an
    unknown-cost receipt: counted in coverage, never summed) or was
    refused by the cross-run guard. An entry NEVER lands in two columns.
    """
    if entry.basis == "refused":
        return None
    basis = _entry_cost_basis(entry)
    if basis == COST_BASIS_PROVIDER_REPORTED and entry.billed_usd is not None:
        return COST_COLUMN_PROVIDER_REPORTED
    if basis == COST_BASIS_BILLING and entry.billed_usd is not None:
        return COST_COLUMN_BILLING
    if basis == COST_BASIS_ESTIMATED and entry.estimate_usd is not None:
        return COST_COLUMN_PRICE_CARD
    return None


def _entry_figure(entry: PricedEntry, column: str) -> float | None:
    """The entry's figure for *column* — ``None`` when it has none there."""
    if _entry_column(entry) != column:
        return None
    if column == COST_COLUMN_PRICE_CARD:
        return entry.estimate_usd
    return entry.billed_usd


@dataclass(frozen=True)
class NativeJobLink:
    """The native CI job one attempt ran as — the chain's job identity."""

    job_id: str
    work_id: str = ""
    attempt_id: str = ""
    pipeline_id: str = ""
    status: str = ""
    began_at: str = ""
    ended_at: str = ""
    recorded_in: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "pipeline_id": self.pipeline_id,
            "status": self.status,
            "began_at": self.began_at,
            "ended_at": self.ended_at,
            "recorded_in": self.recorded_in,
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> NativeJobLink:
        return cls(
            job_id=_text(block.get("job_id")),
            work_id=_text(block.get("work_id")),
            attempt_id=_text(block.get("attempt_id")),
            pipeline_id=_text(block.get("pipeline_id")),
            status=_text(block.get("status")),
            began_at=_text(block.get("began_at")),
            ended_at=_text(block.get("ended_at")),
            recorded_in=_text(block.get("recorded_in")),
        )


@dataclass(frozen=True)
class CandidateLink:
    """The published candidate one attempt produced.

    ``identity_state`` is ``exact`` (a full recorded sha) or ``partial``
    (a recorded prefix — honest, never padded to a sha it is not).
    """

    candidate_sha: str
    work_id: str = ""
    attempt_id: str = ""
    base_sha: str = ""
    entries: int = 0
    identity_state: str = "exact"
    recorded_in: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "candidate_sha": self.candidate_sha,
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "base_sha": self.base_sha,
            "entries": self.entries,
            "identity_state": self.identity_state,
            "recorded_in": self.recorded_in,
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> CandidateLink:
        state = _text(block.get("identity_state")) or "exact"
        return cls(
            candidate_sha=_text(block.get("candidate_sha")),
            work_id=_text(block.get("work_id")),
            attempt_id=_text(block.get("attempt_id")),
            base_sha=_text(block.get("base_sha")),
            entries=_nn_int(block.get("entries")) or 0,
            identity_state=state if state in ("exact", "partial") else "exact",
            recorded_in=_text(block.get("recorded_in")),
        )


@dataclass(frozen=True)
class VerificationLink:
    """The verification that tested a candidate — its own identity.

    Joins the chain by the candidate sha it names (``tested_oid``); the
    producer (an independent pipeline, a check surface) and its verdict
    ride along. Verification is NEVER acceptance: a green pipeline is a
    fact about the candidate, not a human decision.
    """

    verification_id: str
    work_id: str = ""
    candidate_sha: str = ""
    producer: str = ""
    status: str = ""
    tested_oid: str = ""
    observed_at: str = ""
    recorded_in: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "work_id": self.work_id,
            "candidate_sha": self.candidate_sha,
            "producer": self.producer,
            "status": self.status,
            "tested_oid": self.tested_oid,
            "observed_at": self.observed_at,
            "recorded_in": self.recorded_in,
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> VerificationLink:
        return cls(
            verification_id=_text(block.get("verification_id")),
            work_id=_text(block.get("work_id")),
            candidate_sha=_text(block.get("candidate_sha")),
            producer=_text(block.get("producer")),
            status=_text(block.get("status")),
            tested_oid=_text(block.get("tested_oid")),
            observed_at=_text(block.get("observed_at")),
            recorded_in=_text(block.get("recorded_in")),
        )


@dataclass(frozen=True)
class HumanDecisionLink:
    """The human decision point — labelled, never guessed.

    ``state`` is one of :data:`HUMAN_DECISION_STATES`; ``pending`` names
    the real state of a delivered candidate still awaiting its human
    (a draft MR). Only the human decision closes a work into the
    accepted or rejected population — CI green never does.
    """

    work_id: str
    state: str
    decided_by: str = ""
    decided_at: str = ""
    channel: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "state": self.state,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "channel": self.channel,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> HumanDecisionLink:
        state = _text(block.get("state")) or "unknown"
        return cls(
            work_id=_text(block.get("work_id")),
            state=state if state in HUMAN_DECISION_STATES else "unknown",
            decided_by=_text(block.get("decided_by")),
            decided_at=_text(block.get("decided_at")),
            channel=_text(block.get("channel")),
            note=_text(block.get("note")),
        )


@dataclass(frozen=True)
class BudgetEventLink:
    """One recorded budget event — refusals and recoveries stay visible.

    Q39-11 scope item 6: a budget refusal (the reviewer leg the guard
    blocked) and a review-only recovery (the #325 shortcut that repeated
    ONLY the review) are DIFFERENT events, listed separately with the
    observable each feeds.
    """

    work_id: str
    kind: str
    at: str = ""
    reason: str = ""
    detail: str = ""
    observable: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "kind": self.kind,
            "at": self.at,
            "reason": self.reason,
            "detail": self.detail,
            "observable": self.observable,
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> BudgetEventLink:
        kind = _text(block.get("kind"))
        return cls(
            work_id=_text(block.get("work_id")),
            kind=kind if kind in BUDGET_EVENT_KINDS else "budget_refusal",
            at=_text(block.get("at")),
            reason=_text(block.get("reason")),
            detail=_text(block.get("detail")),
            observable=_text(block.get("observable")),
        )


@dataclass(frozen=True)
class ReviewRecoveryInput:
    """The #325 review-only-continuation evaluation input for one work.

    ``budget_decision`` is the recorded decision text at the reviewer
    leg (only an explicit budget decision opens the shortcut); the
    recorded binding is what the decision named, the current binding is
    what the ledger sees now — a moved candidate or tested identity
    invalidates the shortcut (#325's ``review_shortcut_stale``).
    """

    work_id: str
    budget_decision: str
    candidate_sha: str
    tested_identity: str
    current_candidate_sha: str = ""
    current_tested_identity: str = ""


@dataclass(frozen=True)
class TimeWindow:
    """One measured (or explicitly unmeasured) window of ONE measure.

    ``measure`` is one of :data:`TIME_MEASURES`; ``population`` names
    the clock the window was read from (a lane-job window, an operator
    decision gap — populations never mix inside a measure). ``seconds
    is None`` is an honest unknown, never a zero; an empty ``work_id``
    is a cohort-level window (a setup effort that served every work,
    named as its own population).
    """

    window_id: str
    work_id: str
    attempt_id: str
    measure: str
    population: str
    seconds: float | None
    began_at: str = ""
    ended_at: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "measure": self.measure,
            "population": self.population,
            "seconds": _round6(self.seconds),
            "began_at": self.began_at,
            "ended_at": self.ended_at,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> TimeWindow:
        return cls(
            window_id=_text(block.get("window_id")),
            work_id=_text(block.get("work_id")),
            attempt_id=_text(block.get("attempt_id")),
            measure=_text(block.get("measure")),
            population=_text(block.get("population")) or "unlabelled",
            seconds=_nn_float(block.get("seconds")),
            began_at=_text(block.get("began_at")),
            ended_at=_text(block.get("ended_at")),
            note=_text(block.get("note")),
        )


@dataclass(frozen=True)
class AttemptChain:
    """One attempt's stretch of the chain — every link named."""

    attempt_id: str
    work_id: str
    outcome: str
    native_job: NativeJobLink | None = None
    receipt_ids: tuple[str, ...] = ()
    candidate: CandidateLink | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "work_id": self.work_id,
            "outcome": self.outcome,
            "native_job": self.native_job.to_json() if self.native_job else None,
            "model_call_receipt_ids": list(self.receipt_ids),
            "candidate": self.candidate.to_json() if self.candidate else None,
        }


@dataclass(frozen=True)
class WorkChain:
    """One work's complete chain with the human decision point labelled."""

    work_id: str
    outcome: str
    report_outcome: str
    human_decision: HumanDecisionLink | None = None
    attempts: tuple[AttemptChain, ...] = ()
    unobserved_attempt_ids: tuple[str, ...] = ()
    verification: VerificationLink | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "outcome": self.outcome,
            "report_outcome": self.report_outcome,
            "human_decision": (self.human_decision.to_json() if self.human_decision else None),
            "attempts": [attempt.to_json() for attempt in self.attempts],
            "unobserved_attempt_ids": list(self.unobserved_attempt_ids),
            "verification": self.verification.to_json() if self.verification else None,
        }


@dataclass(frozen=True)
class DuplicateCallRow:
    """A model-call id delivered under more than one attribution.

    The Q39-11 negative fixture: the same call id on two attempts never
    false-joins — the identity is counted once (the measurement ledger's
    collapse) and the row stays visible here.
    """

    call_id: str
    attributions: tuple[str, ...] = ()
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "attributions": list(self.attributions),
            "note": self.note,
        }


def _route_from_key(route_key: str) -> ProviderRoute:
    provider, _, model = route_key.partition("/")
    return ProviderRoute(provider=provider, model=model)


class AcceptedTaskLedgerBuilder:
    """Join the economics report into the ONE complete evidence chain.

    ``build`` takes the :class:`EconomicsReport` (the SPEND authority —
    every priced receipt with its cost basis) and the identity links the
    durable evidence carries — native jobs, candidates, verifications,
    human decisions, budget events, review-recovery inputs, time
    windows — and returns the cohort's :class:`AcceptedTaskLedger`.
    Every link joins by STABLE ID; a link that cannot join (a job
    naming an attempt the ledger never saw, a verification naming an
    unknown candidate) surfaces as an identity gap, never a silent
    drop. Duplicate deliveries collapse by identity (the
    canonical-greatest form wins); two DIFFERING records under one
    identity surface as a conflict, never a merge.
    """

    def build(
        self,
        report: EconomicsReport | Mapping[str, Any],
        *,
        native_jobs: Sequence[Mapping[str, Any]] = (),
        candidates: Sequence[Mapping[str, Any]] = (),
        verifications: Sequence[Mapping[str, Any]] = (),
        human_decisions: Sequence[Mapping[str, Any]] = (),
        budget_events: Sequence[Mapping[str, Any]] = (),
        review_recoveries: Sequence[ReviewRecoveryInput | Mapping[str, Any]] = (),
        time_windows: Sequence[Mapping[str, Any]] = (),
        budgets: Mapping[str, Mapping[str, Any]] | None = None,
        measurement_ledger: DeliveryLedger | Mapping[str, Any] | None = None,
        pilot: Mapping[str, str] | None = None,
    ) -> AcceptedTaskLedger:
        if not isinstance(report, EconomicsReport):
            report = EconomicsReport.from_document(report)
        gaps: list[EconomicsConflict] = []
        notes: list[str] = []

        def _collapse(
            rows: Sequence[Mapping[str, Any]],
            key_of: Any,
            *,
            gap_identity: str = "",
        ) -> dict[str, Mapping[str, Any]]:
            """Collapse by identity — canonical-greatest wins, order-free.

            Two DIFFERING records under one identity surface as a
            conflict (``gap_identity`` names the link kind) instead of a
            silent merge; identical re-delivery collapses quietly.
            """
            by_key: dict[str, Mapping[str, Any]] = {}
            for row in rows:
                key = key_of(row)
                if not key:
                    continue
                existing = by_key.get(key)
                if existing is None:
                    by_key[key] = dict(row)
                elif _canonical_row(dict(row)) != _canonical_row(dict(existing)):
                    by_key[key] = max(
                        (dict(row), dict(existing)),
                        key=_canonical_row,
                    )
                    if gap_identity:
                        gaps.append(
                            EconomicsConflict(
                                kind="identity",
                                identity=f"{gap_identity}:{key}",
                                detail=(
                                    f"two differing {gap_identity} records claim one"
                                    f" identity ({key}) — the canonical form is kept,"
                                    " never merged"
                                ),
                            )
                        )
            return by_key

        # -- native jobs (identity: job id) -------------------------------
        jobs = {
            key: NativeJobLink.from_json(row)
            for key, row in _collapse(
                native_jobs,
                lambda row: _text(row.get("job_id") or row.get("id")),
                gap_identity="native_job",
            ).items()
        }

        # -- candidates (identity: sha + work + attempt) ------------------
        candidate_links: dict[tuple[str, str], CandidateLink] = {}
        for _key, row in _collapse(
            candidates,
            lambda row: "\x1f".join(
                (
                    _text(row.get("candidate_sha")),
                    _text(row.get("work_id")),
                    _text(row.get("attempt_id")),
                )
            ),
        ).items():
            candidate_link = CandidateLink.from_json(row)
            if not candidate_link.candidate_sha:
                continue
            scope = (candidate_link.work_id, candidate_link.attempt_id)
            if scope in candidate_links:
                gaps.append(
                    EconomicsConflict(
                        kind="identity",
                        identity=f"candidate:{candidate_link.candidate_sha}",
                        detail=(
                            f"two candidates claim attempt {candidate_link.work_id}/"
                            f"{candidate_link.attempt_id} — the canonical form is kept,"
                            " the attempt's exact candidate set stays unknown"
                        ),
                    )
                )
            candidate_links[scope] = candidate_link

        # -- verifications (identity: verification id) --------------------
        verification_links: dict[str, VerificationLink] = {
            key: VerificationLink.from_json(row)
            for key, row in _collapse(
                verifications,
                lambda row: _text(row.get("verification_id")),
                gap_identity="verification",
            ).items()
        }

        # -- human decisions (identity: work id) --------------------------
        decisions = {
            key: HumanDecisionLink.from_json(row)
            for key, row in _collapse(
                human_decisions,
                lambda row: _text(row.get("work_id")),
                gap_identity="human_decision",
            ).items()
        }

        # -- budget events + review recoveries ----------------------------
        events = [
            BudgetEventLink.from_json(row)
            for _key, row in _collapse(
                budget_events,
                lambda row: "\x1f".join(
                    (
                        _text(row.get("work_id")),
                        _text(row.get("kind")),
                        _text(row.get("at")),
                    )
                ),
            ).items()
        ]
        recoveries: dict[str, ReviewRecoveryInput] = {}
        for item in review_recoveries:
            parsed = (
                item
                if isinstance(item, ReviewRecoveryInput)
                else ReviewRecoveryInput(
                    work_id=_text(item.get("work_id")),
                    budget_decision=_text(item.get("budget_decision")),
                    candidate_sha=_text(item.get("candidate_sha")),
                    tested_identity=_text(item.get("tested_identity")),
                    current_candidate_sha=_text(item.get("current_candidate_sha")),
                    current_tested_identity=_text(item.get("current_tested_identity")),
                )
            )
            if parsed.work_id:
                recoveries[parsed.work_id] = parsed

        # -- time windows (identity: window id) ---------------------------
        windows: list[TimeWindow] = []
        for _key, row in _collapse(time_windows, lambda row: _text(row.get("window_id"))).items():
            measure = _text(row.get("measure"))
            if measure not in TIME_MEASURES:
                if measure:
                    notes.append(
                        f"a time window carries measure {measure!r} outside"
                        f" {TIME_MEASURES} — skipped, never re-typed"
                    )
                continue
            windows.append(TimeWindow.from_json(row))

        # -- join to the report's works -----------------------------------
        chains: list[WorkChain] = []
        for work in sorted(report.works, key=lambda row: row.work_id):
            work_id = work.work_id
            decision = decisions.pop(work_id, None)
            attempts: list[AttemptChain] = []
            for attempt in sorted(work.attempts, key=lambda row: row.attempt_id):
                attempt_id = attempt.attempt_id
                matched = [
                    jobs.pop(key)
                    for key in sorted(jobs)
                    if jobs[key].work_id == work_id and jobs[key].attempt_id == attempt_id
                ]
                native_job = matched[0] if matched else None
                if len(matched) > 1:
                    gaps.append(
                        EconomicsConflict(
                            kind="identity",
                            identity=f"native_job:{work_id}/{attempt_id}",
                            detail=(
                                f"{len(matched)} native jobs claim one attempt —"
                                " the first sorted identity is kept, the rest"
                                " surface here, never silently merged"
                            ),
                        )
                    )
                attempts.append(
                    AttemptChain(
                        attempt_id=attempt_id,
                        work_id=work_id,
                        outcome=attempt.outcome,
                        native_job=native_job,
                        receipt_ids=tuple(
                            sorted(
                                entry.receipt_id
                                for entry in attempt.receipts
                                if entry.basis != "refused"
                            )
                        ),
                        candidate=candidate_links.pop((work_id, attempt_id), None),
                    )
                )
            verification: VerificationLink | None = None
            for vid in sorted(verification_links):
                verification_link = verification_links[vid]
                if verification_link.work_id and verification_link.work_id != work_id:
                    continue
                named = verification_link.candidate_sha or verification_link.tested_oid
                if named and named in {
                    attempt.candidate.candidate_sha if attempt.candidate else ""
                    for attempt in attempts
                }:
                    verification = verification_links.pop(vid)
                    break
            # the effective outcome: the human decision is the authority.
            # A pending decision is a REAL state — the work stays OUT of
            # the accepted population (never coerced to its neighbour).
            effective = work.outcome
            if decision is not None and decision.state in ("accepted", "rejected"):
                effective = decision.state
                if work.outcome and work.outcome != decision.state:
                    gaps.append(
                        EconomicsConflict(
                            kind="human_decision",
                            identity=work_id,
                            detail=(
                                f"the report's acceptance says {work.outcome!r},"
                                f" the human decision record says {decision.state!r}"
                                " — the human decision governs the ledger's"
                                " accepted population"
                            ),
                        )
                    )
            elif decision is not None and decision.state == "pending":
                effective = "pending"
                if work.outcome:
                    gaps.append(
                        EconomicsConflict(
                            kind="human_decision",
                            identity=work_id,
                            detail=(
                                f"the report's acceptance says {work.outcome!r} but"
                                " the human decision record says 'pending' — the"
                                " decision point is still open, no accepted item"
                                " is claimed"
                            ),
                        )
                    )
            chains.append(
                WorkChain(
                    work_id=work_id,
                    outcome=effective,
                    report_outcome=work.outcome,
                    human_decision=decision,
                    attempts=tuple(attempts),
                    unobserved_attempt_ids=tuple(work.unobserved_attempt_ids),
                    verification=verification,
                )
            )
            if decision is None:
                notes.append(
                    f"work {work_id}: no human decision record — the decision"
                    " point stays unknown, the work never enters the accepted"
                    " population"
                )

        # -- links that could not join (surfaced, never dropped) ----------
        for job_id in sorted(jobs):
            job = jobs[job_id]
            gaps.append(
                EconomicsConflict(
                    kind="identity",
                    identity=f"native_job:{job.job_id}",
                    detail=(
                        f"native job {job.job_id} names attempt"
                        f" {job.work_id}/{job.attempt_id!r} which the spend"
                        " ledger never saw — the job stays visible here, its"
                        " spend is unknown, never zero"
                    ),
                )
            )
        for _scope, candidate_link in sorted(candidate_links.items()):
            gaps.append(
                EconomicsConflict(
                    kind="identity",
                    identity=f"candidate:{candidate_link.candidate_sha}",
                    detail=(
                        f"candidate {candidate_link.candidate_sha} names attempt"
                        f" {candidate_link.work_id}/{candidate_link.attempt_id!r} which"
                        " the spend ledger never saw — kept here, never dropped"
                    ),
                )
            )
        for vid in sorted(verification_links):
            verification_link = verification_links[vid]
            gaps.append(
                EconomicsConflict(
                    kind="identity",
                    identity=f"verification:{vid}",
                    detail=(
                        f"verification {vid} names candidate"
                        f" {(verification_link.candidate_sha or verification_link.tested_oid or '?')!r}"
                        " which no attempt of this ledger published — kept"
                        " here, never dropped"
                    ),
                )
            )
        for work_id in sorted(decisions):
            gaps.append(
                EconomicsConflict(
                    kind="identity",
                    identity=f"human_decision:{work_id}",
                    detail=(
                        f"a human decision names work {work_id!r} which the spend"
                        " ledger never saw — the decision stays visible, no"
                        " spend is joined to it"
                    ),
                )
            )
        for work_id in sorted(set(recoveries) - {chain.work_id for chain in chains}):
            gaps.append(
                EconomicsConflict(
                    kind="identity",
                    identity=f"review_only_recovery:{work_id}",
                    detail=(
                        f"a review-only recovery names work {work_id!r} which the"
                        " ledger never saw — kept here, never dropped"
                    ),
                )
            )

        # -- the measurement ledger's identity guards ride along ----------
        duplicates: list[DuplicateCallRow] = []
        if measurement_ledger is not None:
            measurement = (
                measurement_ledger
                if isinstance(measurement_ledger, DeliveryLedger)
                else DeliveryLedger.from_document(measurement_ledger)
            )
            duplicates = [
                DuplicateCallRow(call_id=row.call_id, attributions=row.attributions, note=row.note)
                for row in measurement.duplicate_calls
            ]
            for conflict in measurement.conflicts:
                gaps.append(
                    EconomicsConflict(
                        kind="receipt_conflict",
                        identity=conflict.identity,
                        detail=(
                            f"the measurement ledger holds a {conflict.identity_kind}"
                            f" conflict ({conflict.source_a} vs {conflict.source_b} on"
                            f" {', '.join(conflict.fields)}) — exactness degrades to"
                            " unknown, never averaged"
                        ),
                    )
                )

        ledger = AcceptedTaskLedger(
            report=report,
            chains=tuple(chains),
            budget_events=tuple(sorted(events, key=lambda row: (row.work_id, row.kind, row.at))),
            review_recoveries=dict(sorted(recoveries.items())),
            time_windows=tuple(sorted(windows, key=lambda row: row.window_id)),
            budgets=dict(budgets or {}),
            duplicate_calls=tuple(sorted(duplicates, key=lambda row: row.call_id)),
            identity_gaps=tuple(sorted(gaps, key=lambda row: (row.kind, row.identity))),
            notes=tuple(sorted(set(notes))),
            pilot=dict(pilot or {}),
        )
        # The budget sections are computed ONCE at build time and ride the
        # ledger verbatim — a replay re-emits the stored sections instead of
        # re-deriving them from a reconstructed policy input (the document
        # is the store; there is no second truth to drift from).
        return replace(
            ledger,
            budget_sections={
                chain.work_id: ledger._budget_section(chain) for chain in ledger.chains
            },
        )


@dataclass(frozen=True)
class AcceptedTaskLedger:
    """The ONE exported document: the complete evidence chain per task.

    A pure fold of the builder's joined rows: the economics report (the
    spend authority, embedded verbatim), the per-work chains, the three
    cost columns, the two measures, the seven time measures, the budget
    section and the observability gauges. Everything is sorted by
    identity and summed in sorted order — the document is byte-stable
    under any input ordering, and :meth:`from_document` replays it.
    """

    report: EconomicsReport
    chains: tuple[WorkChain, ...] = ()
    budget_events: tuple[BudgetEventLink, ...] = ()
    review_recoveries: Mapping[str, ReviewRecoveryInput] = field(default_factory=dict)
    time_windows: tuple[TimeWindow, ...] = ()
    budgets: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    budget_sections: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    duplicate_calls: tuple[DuplicateCallRow, ...] = ()
    identity_gaps: tuple[EconomicsConflict, ...] = ()
    notes: tuple[str, ...] = ()
    pilot: Mapping[str, str] = field(default_factory=dict)

    @property
    def schema(self) -> str:
        return ACCEPTED_LEDGER_SCHEMA

    def chain_by_id(self, work_id: str) -> WorkChain | None:
        for chain in self.chains:
            if chain.work_id == work_id:
                return chain
        return None

    # -- the folds (pure, order-invariant) --------------------------------

    def _attempt_rows(self, chain: WorkChain) -> list[tuple[Any, list[PricedEntry]]]:
        """The chain's attempts with their non-refused priced entries."""
        work = self.report.work_by_id(chain.work_id)
        if work is None:
            return []
        rows: list[tuple[Any, list[PricedEntry]]] = []
        for attempt in sorted(work.attempts, key=lambda row: row.attempt_id):
            entries = [entry for entry in attempt.receipts if entry.basis != "refused"]
            rows.append((attempt, sorted(entries, key=lambda row: row.receipt_id)))
        return rows

    def _fold_columns(self, chains: Sequence[WorkChain]) -> dict[str, Any]:
        """Fold the three cost columns over a population of works.

        A column total is EXACT only when the population is non-empty,
        nothing is unobserved, nothing was cross-joined, conflicted or
        duplicated, and EVERY attempt of the population contributed a
        receipt whose figure landed in THAT column — a mixed-basis
        population (some provider-reported, some estimated) makes every
        column a partial view, so every column renders a lower bound
        with its coverage, and the columns are NEVER summed together.
        """
        attempts = sum(len(chain.attempts) for chain in chains)
        unobserved = sum(len(chain.unobserved_attempt_ids) for chain in chains)
        per_attempt: list[list[PricedEntry]] = []
        for chain in sorted(chains, key=lambda row: row.work_id):
            per_attempt.extend(rows for _attempt, rows in self._attempt_rows(chain))
        conflicted = {row.identity for row in self.report.conflicts}
        columns: dict[str, Any] = {}
        for column in ACCEPTED_COST_COLUMNS:
            contributing = [
                entry for rows in per_attempt for entry in rows if _entry_column(entry) == column
            ]
            values = [
                figure
                for entry in contributing
                if (figure := _entry_figure(entry, column)) is not None
            ]
            attempts_covered = sum(
                1 for rows in per_attempt if any(_entry_column(entry) == column for entry in rows)
            )
            exact = bool(
                chains
                and attempts > 0
                and unobserved == 0
                and not self.report.cross_joins
                and not conflicted
                and not self.duplicate_calls
                and attempts_covered == attempts
                # a streamed partial receipt is a lower bound only — the
                # column stays inexact until a final receipt reconciles it
                and not any(entry.completeness == "partial" for entry in contributing)
            )
            columns[column] = {
                "usd": _round6(_sum(values)) if exact else None,
                "exact": exact,
                # an EMPTY column (no figure of this basis exists at all)
                # renders null — never a readable 0.0 that mimics a cost
                "known_lower_bound_usd": _round6(_sum(values)) if values else None,
                "receipts": len(values),
                "attempts_covered": attempts_covered,
                "attempts": attempts,
            }
        return columns

    def _coverage_block(self, chains: Sequence[WorkChain]) -> dict[str, Any]:
        attempts = sum(len(chain.attempts) for chain in chains)
        unobserved = sum(len(chain.unobserved_attempt_ids) for chain in chains)
        received = 0
        cost_known = 0
        for chain in chains:
            for attempt, rows in self._attempt_rows(chain):
                if rows:
                    received += 1
                if any(_entry_column(entry) is not None for entry in rows):
                    cost_known += 1
        expected = attempts + unobserved
        return {
            "attempts": attempts,
            "unobserved_attempts": unobserved,
            "receipts_expected": expected,
            "receipts_received": received,
            "receipt_coverage": (received / expected) if expected else None,
            "cost_known_receipts": cost_known,
            "unknown_cost_receipts": max(attempts - cost_known, 0),
            "cost_coverage": (cost_known / expected) if expected else None,
            "unknown_costs_rendered_as": "known lower bound + coverage, never zero",
        }

    def _all_attempt_totals(self) -> dict[str, Any]:
        """Every work's OWN all-attempt totals — failed attempts kept.

        Computed for EVERY work (accepted, rejected, pending alike): the
        accepted-task all-attempt COST is the accepted subset of these
        rows, and a pending work's row stands beside them honestly.
        """
        totals: dict[str, dict[str, Any]] = {}
        for chain in sorted(self.chains, key=lambda row: row.work_id):
            totals[chain.work_id] = {
                "outcome": chain.outcome,
                "attempts": len(chain.attempts),
                "failed_or_superseded_attempts_kept": sum(
                    1 for attempt in chain.attempts if attempt.outcome in _REJECTED_OUTCOMES
                ),
                "columns": self._fold_columns([chain]),
                "coverage": self._coverage_block([chain]),
            }
        return dict(sorted(totals.items()))

    def _budget_section(self, chain: WorkChain) -> dict[str, Any]:
        """The work's budget section — #325's shapes, surfaced verbatim.

        The closing budget folds the work's own receipts into the five
        distinguishable fields (exact / known subtotal / lower bound /
        reserved liability / unknown intervals) under the work's cap and
        the closing-reserve policy; the recorded budget events (refusals)
        and the review-only-recovery verdict stay SEPARATE lists beside
        it. An unpriced receipt is an UNKNOWN interval — never a zero.
        """
        work_id = chain.work_id
        budget = dict(self.budgets.get(work_id) or {})
        cap = _nn_float(budget.get("cap_usd"))
        policy_raw = budget.get("policy")
        policy_block = policy_raw if isinstance(policy_raw, Mapping) else {}
        policy = ClosingReservePolicy(
            reserve_usd=_nn_float(policy_block.get("reserve_usd")),
            fraction=_nn_float(policy_block.get("fraction")),
            cap_usd=_nn_float(policy_block.get("cap_usd")),
            source=_text(policy_block.get("source")) or "default",
        )
        rows: list[IngestedUsageRow] = []
        for _attempt, entries in self._attempt_rows(chain):
            for entry in entries:
                rows.append(
                    IngestedUsageRow(
                        work_id=work_id,
                        attempt_id=entry.attempt_id,
                        receipt_id=entry.receipt_id,
                        source=entry.source,
                        route=_route_from_key(entry.route_key),
                        cost_usd=entry.billed_usd,
                        cost_basis=(
                            _entry_cost_basis(entry) if entry.billed_usd is not None else ""
                        ),
                        cost_lower_bound_usd=(
                            entry.billed_usd if entry.billed_usd is not None else 0.0
                        ),
                        completeness="aggregate",
                    )
                )
        closing = closing_budget_report(rows, cap_usd=cap, policy=policy)
        refusals = [
            event.to_json()
            for event in self.budget_events
            if event.work_id == work_id and event.kind == "budget_refusal"
        ]
        recoveries = [
            event.to_json()
            for event in self.budget_events
            if event.work_id == work_id and event.kind == "review_only_recovery"
        ]
        recovery_input = self.review_recoveries.get(work_id)
        review_only: dict[str, Any]
        if recovery_input is not None:
            verdict = review_only_continuation(
                budget_decision=recovery_input.budget_decision,
                recorded=CandidateBinding(
                    candidate_sha=recovery_input.candidate_sha,
                    tested_identity=recovery_input.tested_identity,
                ),
                current=CandidateBinding(
                    candidate_sha=(
                        recovery_input.current_candidate_sha or recovery_input.candidate_sha
                    ),
                    tested_identity=(
                        recovery_input.current_tested_identity or recovery_input.tested_identity
                    ),
                ),
            )
            review_only = {"evaluated": True, **verdict.to_json()}
        else:
            review_only = {
                "evaluated": False,
                "recorded": bool(recoveries),
                "note": (
                    "no review-only continuation was recorded or requested for"
                    " this work — absent, stated, never silently zero"
                ),
            }
        return {
            "cap_usd": _round6(cap),
            "currency": _text(budget.get("currency")) or "usd",
            "closing_budget": closing.to_json(),
            "budget_refusals": refusals,
            "review_only_recovery": review_only,
            "recorded_recovery_events": recoveries,
        }

    def _time_measures(self) -> dict[str, Any]:
        """Fold the time windows per measure — populations never mix."""
        rows = [
            {
                "span_id": window.window_id,
                "span_type": window.measure,
                "origin": window.population,
                "work_id": window.work_id,
                "attempt_id": window.attempt_id,
                "seconds": window.seconds,
            }
            for window in self.time_windows
        ]
        return _fold_stage_seconds(rows, keys=TIME_MEASURES)

    def _human_minutes(self) -> float | None:
        """Measured reviewer effort in minutes — or ``None`` (never zero)."""
        windows = [
            window.seconds for window in self.time_windows if window.measure == "reviewer_effort"
        ]
        if not windows or any(seconds is None for seconds in windows):
            return None
        return _round6(_sum([seconds or 0.0 for seconds in windows]) / 60.0)

    def to_document(self) -> dict[str, Any]:
        """The stored artifact: the complete chain, one document."""
        accepted_chains = [chain for chain in self.chains if chain.outcome == "accepted"]
        programme_columns = self._fold_columns(self.chains)
        accepted_columns = self._fold_columns(accepted_chains)
        accepted_count = len(accepted_chains)
        per_accepted: dict[str, Any] = {}
        for column in ACCEPTED_COST_COLUMNS:
            row = programme_columns[column]
            per_accepted[column] = {
                "usd": _round6(row["usd"] / accepted_count)
                if row["usd"] is not None and accepted_count
                else None,
                "known_lower_bound_usd_per_accepted": _round6(
                    row["known_lower_bound_usd"] / accepted_count
                )
                if accepted_count and row["known_lower_bound_usd"] is not None
                else None,
            }
        all_attempt = self._all_attempt_totals()
        notes = list(self.notes)
        notes.extend(
            (
                "the human decision point is labelled per work — a pending draft"
                " MR is a REAL state: the work stays out of the accepted"
                " population and its per-accepted economics stay undefined,"
                " never zero",
                "time measures are separate quantities over their own recorded"
                " windows — never unmatched output divided by unmatched"
                " durations",
            )
        )
        if not accepted_count:
            notes.append(
                "no work carries a closed human decision — accepted measures are"
                " undefined, never zero; every work's all-attempt totals and"
                " coverage stand beside them"
            )
        document = {
            "schema": ACCEPTED_LEDGER_SCHEMA,
            "pilot": dict(sorted(self.pilot.items())),
            "identity_chain": list(IDENTITY_CHAIN),
            "economics": self.report.to_document(),
            "chains": [
                chain.to_json() for chain in sorted(self.chains, key=lambda row: row.work_id)
            ],
            "identity_gaps": [row.to_json() for row in self.identity_gaps],
            "duplicate_calls": [row.to_json() for row in self.duplicate_calls],
            "costs": {
                "columns": list(ACCEPTED_COST_COLUMNS),
                "basis_note": (
                    "three labeled columns, never blended: provider-reported (the"
                    " SDK's own meter), price-card estimate (a versioned card),"
                    " billing-reconciliation (a billing export) — each entry lands"
                    " in at most one column and no column ever sums another"
                ),
                "programme": {
                    "works": len(self.chains),
                    "columns": programme_columns,
                    "coverage": self._coverage_block(self.chains),
                },
                "accepted_items": {
                    "works": accepted_count,
                    "columns": accepted_columns,
                    "coverage": self._coverage_block(accepted_chains),
                },
                "accepted_all_attempt": {
                    work_id: row
                    for work_id, row in all_attempt.items()
                    if row["outcome"] == "accepted"
                },
                "all_attempt_totals": all_attempt,
                "programme_per_accepted_item": per_accepted,
            },
            "time_measures": dict(sorted(self._time_measures().items())),
            "time_windows": [
                window.to_json()
                for window in sorted(self.time_windows, key=lambda row: row.window_id)
            ],
            "throughput": {
                "rows": [],
                "rule": (
                    "no tokens/s figure exists without matched measured model time"
                    " and the #276 token convention — none is emitted otherwise"
                    " (the decode guard extends to this ledger)"
                ),
            },
            "budget": dict(sorted(self.budget_sections.items())),
            "human_decision_points": {
                chain.work_id: (chain.human_decision.to_json() if chain.human_decision else None)
                for chain in sorted(self.chains, key=lambda row: row.work_id)
            },
            "notes": sorted(set(notes)),
            "observability": {
                "delivery.accepted_all_attempt_cost": {
                    work_id: {
                        column: row["columns"][column]["usd"] for column in ACCEPTED_COST_COLUMNS
                    }
                    for work_id, row in all_attempt.items()
                    if row["outcome"] == "accepted"
                },
                "delivery.programme_cost_per_accepted": {
                    column: per_accepted[column]["usd"] for column in ACCEPTED_COST_COLUMNS
                },
                "cost.coverage": self._coverage_block(self.chains)["receipt_coverage"],
                "delivery.human_minutes": self._human_minutes(),
                "budget.closing_reserve": {
                    work_id: row["closing_budget"]["closing_reserve_usd"]
                    for work_id, row in sorted(self.budget_sections.items())
                },
                "budget.phase_exhaustion": {
                    work_id: row["closing_budget"]["phase_exhaustion"]
                    for work_id, row in sorted(self.budget_sections.items())
                },
                "delivery.review_only_recovery": {
                    work_id: {
                        "evaluated": row["review_only_recovery"].get("evaluated"),
                        "allowed": row["review_only_recovery"].get("allowed"),
                        "coder_dispatches": row["review_only_recovery"].get("coder_dispatches"),
                    }
                    for work_id, row in sorted(self.budget_sections.items())
                },
            },
        }
        assert_latency_guards(document)
        assert_no_unmatched_rates(document)
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> AcceptedTaskLedger:
        """Rebuild from the stored document — the replay path's parser.

        The chains, windows, budget events and duplicate-call rows are
        re-read from their stored forms; the folds re-run over them, so
        a replayed document reproduces every aggregate byte-identically
        with no second truth store.
        """
        stamp = _text(document.get("schema")) or ACCEPTED_LEDGER_SCHEMA
        if stamp != ACCEPTED_LEDGER_SCHEMA:
            raise ValueError(
                f"accepted-task ledger carries schema {stamp!r},"
                f" expected {ACCEPTED_LEDGER_SCHEMA!r}"
            )
        report = EconomicsReport.from_document(document.get("economics") or {})

        def _attempt(block: Mapping[str, Any]) -> AttemptChain:
            job = block.get("native_job")
            candidate = block.get("candidate")
            return AttemptChain(
                attempt_id=_text(block.get("attempt_id")),
                work_id=_text(block.get("work_id")),
                outcome=_text(block.get("outcome")),
                native_job=(NativeJobLink.from_json(job) if isinstance(job, Mapping) else None),
                receipt_ids=tuple(
                    _text(receipt_id) for receipt_id in block.get("model_call_receipt_ids") or ()
                ),
                candidate=(
                    CandidateLink.from_json(candidate) if isinstance(candidate, Mapping) else None
                ),
            )

        def _chain(block: Mapping[str, Any]) -> WorkChain:
            decision = block.get("human_decision")
            verification = block.get("verification")
            return WorkChain(
                work_id=_text(block.get("work_id")),
                outcome=_text(block.get("outcome")),
                report_outcome=_text(block.get("report_outcome")),
                human_decision=(
                    HumanDecisionLink.from_json(decision) if isinstance(decision, Mapping) else None
                ),
                attempts=tuple(
                    _attempt(row) for row in block.get("attempts") or () if isinstance(row, Mapping)
                ),
                unobserved_attempt_ids=tuple(
                    _text(attempt_id) for attempt_id in block.get("unobserved_attempt_ids") or ()
                ),
                verification=(
                    VerificationLink.from_json(verification)
                    if isinstance(verification, Mapping)
                    else None
                ),
            )

        budget_raw = document.get("budget")
        budget_sections = (
            {str(key): dict(row) for key, row in budget_raw.items()}
            if isinstance(budget_raw, Mapping)
            else {}
        )
        events: list[Mapping[str, Any]] = []
        for work_id in sorted(budget_sections):
            row = budget_sections[work_id]
            if isinstance(row, Mapping):
                events.extend(row.get("budget_refusals") or [])
                events.extend(row.get("recorded_recovery_events") or [])
        pilot_raw = document.get("pilot")
        return cls(
            report=report,
            chains=tuple(
                _chain(row) for row in document.get("chains") or () if isinstance(row, Mapping)
            ),
            budget_events=tuple(
                BudgetEventLink.from_json(event) for event in events if isinstance(event, Mapping)
            ),
            review_recoveries={},
            time_windows=tuple(
                TimeWindow.from_json(window)
                for window in document.get("time_windows") or ()
                if isinstance(window, Mapping)
            ),
            budgets={},
            budget_sections=budget_sections,
            duplicate_calls=tuple(
                DuplicateCallRow(
                    call_id=_text(row.get("call_id")),
                    attributions=tuple(_text(item) for item in row.get("attributions") or ()),
                    note=_text(row.get("note")),
                )
                for row in document.get("duplicate_calls") or ()
                if isinstance(row, Mapping)
            ),
            identity_gaps=tuple(
                EconomicsConflict(
                    kind=_text(row.get("kind")),
                    identity=_text(row.get("identity")),
                    detail=_text(row.get("detail")),
                )
                for row in document.get("identity_gaps") or ()
                if isinstance(row, Mapping)
            ),
            notes=tuple(str(note) for note in document.get("notes") or ()),
            pilot=(
                {_text(key): _text(value) for key, value in pilot_raw.items()}
                if isinstance(pilot_raw, Mapping)
                else {}
            ),
        )


def assert_no_unmatched_rates(document: Mapping[str, Any]) -> None:
    """The decode guard, extended to the accepted-task ledger document.

    Any rate figure (a ``tokens_per_second`` / ``*_per_second`` key)
    found ANYWHERE in the document must sit in a guarded throughput row:
    the sanctioned decode label, a model-span population and live-model
    evidence (the #276 :data:`RATE_LABEL_SPACE` rule — output over
    unmatched or mixed durations is never a decode rate). A tampered
    ledger — a tokens/s figure pasted next to a cost column or inside a
    time measure — raises :class:`RateLabelError`.
    """
    latency = document.get("latency")
    guarded: list[Mapping[str, Any]] = []
    if isinstance(latency, Mapping):
        rows = latency.get("throughput")
        guarded = (
            [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
        )

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            keys = {str(key) for key in node}
            if "tokens_per_second" in keys or any(key.endswith("_per_second") for key in keys):
                if node not in guarded:
                    label = _text(node.get("label")) or "<unlabelled>"
                    raise RateLabelError(
                        f"a rate figure at {path or 'document root'} carries label"
                        f" {label!r} outside the guarded throughput rows — no"
                        " tokens/s figure without matched measured model time"
                        " and the defined token convention (Q39-11)"
                    )
                row_label = _text(node.get("label"))
                allowed = RATE_LABEL_SPACE.get(row_label)
                population_identity = _text(node.get("population_identity"))
                if (
                    allowed is None
                    or population_identity.removeprefix("latency.").split(":works=")[0]
                    not in allowed
                ):
                    raise RateLabelError(
                        f"a ledger throughput row rides population"
                        f" {population_identity!r} — only model-span populations"
                        " carry decode rates (RATE_LABEL_SPACE)"
                    )
            for key, value in node.items():
                _walk(value, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                _walk(item, f"{path}[{index}]")

    _walk(document, "")
