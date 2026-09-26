"""R38-09 — idempotent ingestion of REAL lane usage into durable economics.

The recorded gap (issue #310 / live single-writer record): the SDK lanes
report receipt costs ($0.2180 successful attempt, $0.8038 total) while the
durable ``usage_receipts`` rows for harness lanes stayed EMPTY — the
lane's usage artifacts (the R23 ``.forge/usage.json`` riding the candidate
meta, the SDK's own receipt JSONs) never ingested into durable rows, so
the economics report rendered unknown-cost. This module is the missing
front door, and every rule here is pinned by
``tests/test_usage_ingestion.py``:

- **Idempotent by natural key** — :func:`ingest_usage_artifact` upserts by
  ``(work_id, attempt_id, receipt_id, source)``: a re-ingest of the same
  artifact, a replayed webhook, a re-downloaded candidate, all write
  NOTHING new (:class:`UsageIngestStore`). The source artifact's digest is
  recorded on the row (tamper-evident: a changed artifact under the same
  identity is a :class:`IngestionConflict`, never a silent overwrite).
- **One normalization, REUSED** — the #294/R23 provider counter contract,
  not re-derived: OpenAI-compatible counters are INCLUSIVE (the cache
  rides inside ``input_tokens`` — a cached column is a breakdown, never
  added on top); Anthropic-compatible counters are DISJOINT (inclusive
  input is ``input + cache_read + cache_write``); ``reasoning_tokens`` is
  a breakdown inside the inclusive OUTPUT on every shape; unknown stays
  unknown, never zero. :class:`NormalizedCounters` is the fold.
- **Streaming + crash reconciliation** — receipts stream as calls
  complete (the lane writes partial artifacts; a partial carries
  ``final: False``). Ingestion accepts partials; a later FINAL row for
  the same identity REPLACES the partial (reconciled, never summed);
  after process death the re-run reconciles to the artifact's final
  state; an unresolved remainder (no final ever arrived) stays partial
  with its known lower bound — never zero, never healed.
- **Rate-card identity + attribution segments** — every row carries the
  rate-card id that priced it and its ``cost_basis``
  (``provider-reported`` | ``estimated`` | ``billing-reconciliation``);
  the attribution segment is derived from (route, route version,
  rate-card id) — a route/version or card change creates a NEW segment;
  existing rows are immutable, history is never rewritten.
- **Spend caps consult the ingested totals; FINALITY, not value
  presence, settles** (Q39-06/#325, corrected by R40-03/#339) —
  :func:`spend_cap_check` is the check the existing budget/caps gate
  consults BEFORE the next chargeable action
  (:meth:`forge.adaptive.research_cohort_live.SpendLedger.allows_call` is
  the projection seam; ``budget_block_reason`` the dispatch gate). The
  exposure fold (:func:`exposure_fold`, :class:`SpendExposure`) keeps the
  quantities SEPARATE: ``settled_usd`` (FINAL rows' costs — settlement
  is decided by the ``final`` marker, never by the presence of a cost),
  ``accrued_unsettled_usd`` (nonfinal rows' observed subtotals —
  accrued, NOT settled), ``unknown_lower_bound`` (the unresolved
  intervals' floors) and ``retained_liability_usd`` (the unresolved
  intervals' worst-case retained envelope). Each unresolved interval —
  a nonfinal row, or a final row that never reported a cost — retains
  ``max(its finite upper envelope, its accrued cost, its lower bound)``
  WITHOUT double-counting the accrued cost: the HARD CAP consults
  ``settled + retained_liability + projection``, never a lower bound. A
  partial's subtotal never releases its envelope (the P01 probe: cap 10,
  settled 8, a partial at 0.5 inside an envelope of 3 "allowed" 9.5
  while the bounded exposure is 8 + 3 + 1 = 12); an unresolved interval
  with no finite upper bound (neither its own ``cost_upper_bound_usd``
  nor the explicit bounded policy ceiling) — including a nonfinal row
  that already carries a cost — is a typed ``unbounded-exposure`` finding
  that sets ``requires_bounded_policy`` and BLOCKS the next chargeable
  action; an upper bound BELOW an interval's observed accrued cost is a
  typed ``incoherent-bound`` inconsistency (never free headroom); NaN/
  infinite/negative costs, bounds and projections surface as typed
  findings, never silent defaults.
- **Duplicate call identities never false-join** — the same model-call id
  under two attempts is ingested under BOTH natural keys but the ledger
  join (:func:`forge.adaptive.delivery_measurement.
  ledger_records_from_ingested_usage`) passes each to its own attempt and
  the linker's duplicate/cross-join guards surface it — one receipt id is
  never summed twice.

- **The durable identity, ONE contract (Q39-05/#324)** — the durable
  ``usage_receipts`` row keys on the SAME four components the store
  does: ``(run_id, attempt_id, receipt_id, source_namespace)`` with the
  stable ``identity_digest`` beside. A partial RECONCILES to its final
  at the database (``ON CONFLICT ... DO UPDATE ... WHERE final IS NOT
  TRUE`` — a late partial never downgrades a final, a repeated
  identical final is a no-op, a conflicting final is recorded in
  ``usage_ingestion_conflicts`` and never merged); the same label from
  two source namespaces stays TWO rows; an overlength component is
  hashed (full value preserved in ``raw``), never silently truncated; a
  payload claiming work-B on a transport attributed to work-A writes NO
  row to B — the refusal is preserved as a diagnostic
  (``usage.attribution_refused``), because the TRUSTED caller
  attribution is AUTHORITATIVE.

Everything here is pure (mapping-shaped inputs, no database, no clocks)
except :func:`persist_ingested_rows`, the durable write through the
conditional-upsert seam on the canonical four-column identity
(:func:`forge.durable.budgets.ingest_usage_receipt` remains the R23
front door, keyed on the same identity since Q39-05).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from forge.adaptive.delivery_measurement import ProviderRoute

__all__ = [
    "ATTRIBUTION_REFUSED",
    "BLOCKING_FINDINGS",
    "COST_BASIS_BILLING",
    "COST_BASIS_ESTIMATED",
    "COST_BASIS_PROVIDER_REPORTED",
    "COST_BASES",
    "IDENTITY_REJECTED",
    "INGESTION_SCHEMA",
    "INCOHERENT_BOUND",
    "INVALID_BOUND",
    "INVALID_CAP",
    "INVALID_COST",
    "INVALID_LOWER_BOUND",
    "INVALID_POLICY_CEILING",
    "INVALID_PROJECTION",
    "IngestedUsageRow",
    "IngestionConflict",
    "IngestionRefusal",
    "IngestionResult",
    "NormalizedCounters",
    "PersistOutcome",
    "SOURCE_LANE_ARTIFACT",
    "SOURCE_PLANNER_LEDGER",
    "SOURCE_SDK_RECEIPT",
    "SpendExposure",
    "UNBOUNDED_EXPOSURE",
    "UsageIngestStore",
    "attribution_segment",
    "exposure_fold",
    "ingest_usage_artifact",
    "normalize_counters",
    "persist_ingested_rows",
    "reconcile_after_death",
    "rows_from_documents",
    "spend_cap_check",
]

#: The versioned stamp of the ingestion contract's documents.
INGESTION_SCHEMA = "forge.usage.ingestion/1"

#: Source attribution labels — each parsed source kind carries its own.
SOURCE_LANE_ARTIFACT = "lane/.forge/usage.json"
SOURCE_SDK_RECEIPT = "sdk-receipt"
SOURCE_PLANNER_LEDGER = "planner/llm_calls"

#: The receipt's cost figure is what the PROVIDER's own SDK reported for
#: the call (``total_cost_usd`` on the lane receipt) — measured spend at
#: the SDK's meter, not an invoice.
COST_BASIS_PROVIDER_REPORTED = "provider-reported"

#: The figure was priced from a versioned rate card — an assumption
#: labelled with the card id, never a billing record.
COST_BASIS_ESTIMATED = "estimated"

#: The figure came from a billing export/reconciliation — the invoice is
#: the authority, the receipt row records the joined figure.
COST_BASIS_BILLING = "billing-reconciliation"

#: The closed cost-basis vocabulary (estimated vs provider-reported vs
#: billing-reconciliation distinguished, R38-09 scope item 5).
COST_BASES = (COST_BASIS_PROVIDER_REPORTED, COST_BASIS_ESTIMATED, COST_BASIS_BILLING)

#: Q39-05 (#324): a payload's work/attempt claim CONFLICTED with the
#: trusted caller attribution — the row is refused, the diagnostic is
#: preserved (``usage.attribution_refused``).
ATTRIBUTION_REFUSED = "attribution-refused"
#: Q39-05 (#324): an identity component cannot live in its durable column
#: (a work id longer than the run id column — no run row could ever own
#: it) — the row is rejected outright, never silently truncated.
IDENTITY_REJECTED = "identity-rejected"

# ----------------------------------------------------------------------
# The typed spend-cap findings (R40-03/#339) — surfaced, never defaulted
# ----------------------------------------------------------------------

#: An unresolved interval with NO finite upper envelope — a nonfinal row
#: (with or without an accrued cost) or a final row that never reported
#: one — cannot be bounded from above: the next chargeable action is
#: REFUSED until an explicit bounded-policy ceiling is supplied or the
#: interval settles (:func:`spend_cap_check`'s ``requires_bounded_policy``).
UNBOUNDED_EXPOSURE = "unbounded-exposure"
#: An interval's declared upper bound sits BELOW its observed accrued cost
#: (or a settlement landed above its own declared envelope) — a visible
#: INCONSISTENCY: the interval retains at least its accrued cost, never
#: free headroom.
INCOHERENT_BOUND = "incoherent-bound"
#: ``cap_usd`` is not a nonnegative number (NaN/negative/−inf/not a
#: number) — the decision refuses rather than clamping. ``+inf`` is the
#: deliberate un-bounded fold arm (the closing report folds against an
#: infinite cap when none is configured) and is NOT a finding.
INVALID_CAP = "invalid-cap"
#: ``projection_usd`` is not a finite nonnegative number.
INVALID_PROJECTION = "invalid-projection"
#: ``unknown_interval_ceiling_usd`` is not a finite nonnegative number —
#: the bounded-policy field is corrupt: treated as NOT supplied (never
#: silently coerced), so every interval relying on it is unbounded.
INVALID_POLICY_CEILING = "invalid-policy-ceiling"
#: A row's ``cost_usd`` is not a finite nonnegative number — the figure
#: is untrustworthy: the interval stays unresolved (never settled at a
#: garbage value).
INVALID_COST = "invalid-cost"
#: A row's ``cost_upper_bound_usd`` is not a finite nonnegative number —
#: treated as absent, never clamped.
INVALID_BOUND = "invalid-bound"
#: A row's ``cost_lower_bound_usd`` is not a finite nonnegative number —
#: reported at 0.0 for the (non-gating) lower-bound sum, with the
#: corruption visible.
INVALID_LOWER_BOUND = "invalid-lower-bound"

#: The finding types that REFUSE the next chargeable action outright.
BLOCKING_FINDINGS = frozenset(
    {UNBOUNDED_EXPOSURE, INVALID_CAP, INVALID_PROJECTION, INVALID_POLICY_CEILING}
)


# ----------------------------------------------------------------------
# Small helpers (the delivery_measurement spelling — shared, not re-derived)
# ----------------------------------------------------------------------


def _nn_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nn_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or math.isnan(number) or math.isinf(number):
        return None
    return number


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------
# The normalized counters — the #294/R23 contract, one fold
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedCounters:
    """One receipt's counters under the provider contract.

    ``input_tokens_inclusive`` is the spend-bearing input under the
    receipt's OWN shape: OpenAI-compatible shapes count the cache INSIDE
    ``input_tokens`` (the cached column is a breakdown — never added on
    top); Anthropic-compatible shapes carry DISJOINT counters and the
    inclusive input is ``input + cached + cache_write``. Shape is decided
    by the two tells the R23 research table names: a ``cache_write``
    counter only an Anthropic-compatible endpoint exposes, and the
    claude-code driver/claude-sdk lane that always talks to one. Unknown
    counters stay ``None`` — never zero, never guessed.
    """

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    anthropic_shaped: bool = False

    @property
    def input_tokens_inclusive(self) -> int | None:
        """The INCLUSIVE input under the receipt's own convention."""
        if self.anthropic_shaped:
            parts = (self.input_tokens, self.cached_input_tokens, self.cache_write_tokens)
            known = [part for part in parts if part is not None]
            return sum(known) if known else None
        # OpenAI-shaped: the cache rides inside input — a cached column is
        # a breakdown, NEVER added on top (the double-count guard).
        return self.input_tokens

    @property
    def known_total_tokens(self) -> int | None:
        """The known magnitude — ``None`` when nothing at all is known."""
        inclusive = self.input_tokens_inclusive
        parts = [value for value in (inclusive, self.output_tokens) if value is not None]
        return sum(parts) if parts else None

    def to_json(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "anthropic_shaped": self.anthropic_shaped,
            "input_tokens_inclusive": self.input_tokens_inclusive,
        }


def normalize_counters(
    artifact: Mapping[str, Any], *, driver: str = "", provider: str = ""
) -> NormalizedCounters:
    """Normalize a source artifact's vendor counters (the #294 rules).

    Accepts the canonical R23 names (``input_tokens`` /
    ``cached_input_tokens`` / ``cache_write_tokens`` / ``output_tokens`` /
    ``reasoning_tokens``) AND the vendor spellings the lane receipts carry
    (Anthropic's ``cache_read_input_tokens`` / ``cache_creation_input_tokens``;
    OpenAI's ``cached_tokens`` breakdown inside an inclusive input).
    ``reasoning_tokens`` / ``thinking_tokens`` are recorded as the output
    breakdown they are — never added on top of anything.
    """
    raw_input = _nn_int(artifact.get("input_tokens"))
    cached = _nn_int(
        artifact.get(
            "cached_input_tokens",
            artifact.get("cache_read_input_tokens", artifact.get("cached_tokens")),
        )
    )
    cache_write = _nn_int(
        artifact.get("cache_write_tokens", artifact.get("cache_creation_input_tokens"))
    )
    output = _nn_int(artifact.get("output_tokens"))
    reasoning = _nn_int(artifact.get("reasoning_tokens", artifact.get("thinking_tokens")))
    shape_marker = " ".join((_text(driver), _text(provider), _text(artifact.get("driver")))).lower()
    anthropic_shaped = (
        cache_write is not None
        or "claude" in shape_marker
        or bool(_text(artifact.get("anthropic_shaped")).lower() == "true")
    )
    return NormalizedCounters(
        input_tokens=raw_input,
        cached_input_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=output,
        reasoning_tokens=reasoning,
        anthropic_shaped=anthropic_shaped,
    )


# ----------------------------------------------------------------------
# Attribution segments — rate-card identity, history never rewritten
# ----------------------------------------------------------------------


def attribution_segment(
    route: ProviderRoute, *, rate_card_id: str = "", route_version: str = ""
) -> str:
    """The attribution segment identity: route + version + rate card.

    A model route/version change or a rate-card change yields a DIFFERENT
    segment id, so the same work's receipts split into per-segment folds
    instead of silently comparing across changed assumptions. Existing
    rows are never rewritten — a changed world writes NEW rows carrying
    the new segment (the ON CONFLICT DO NOTHING upsert guarantees it).
    """
    material = _canonical(
        {
            "provider": route.provider,
            "model": route.model,
            "route_version": _text(route_version),
            "rate_card_id": _text(rate_card_id),
        }
    )
    return f"segment:{_digest(material)}"


# ----------------------------------------------------------------------
# The ingested row — the durable natural-key unit
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class IngestedUsageRow:
    """One usage receipt ingested durably — identity-keyed, tamper-evident.

    Natural key: ``(work_id, attempt_id, receipt_id, source)``. The
    normalized counters ride as :class:`NormalizedCounters`; ``cost_usd``
    carries its ``cost_basis``; ``rate_card_id`` names the card that
    priced an estimate; ``final`` marks a streamed receipt's finality
    (``False`` = a partial artifact accepted during streaming); the
    ``artifact_digest`` binds the row to the exact source bytes.
    """

    work_id: str
    attempt_id: str
    receipt_id: str
    source: str
    route: ProviderRoute
    counters: NormalizedCounters = field(default_factory=NormalizedCounters)
    cost_usd: float | None = None
    cost_basis: str = ""
    cost_lower_bound_usd: float = 0.0
    #: Q39-06 (#325): the interval's own FINITE worst case when one is
    #: known (a per-call ceiling the lane stamped, a contracted maximum).
    #: ``None`` — no finite upper bound is known — is a first-class fact:
    #: the spend cap cannot bound such an interval from above and demands
    #: an explicit bounded policy (:func:`spend_cap_check`'s
    #: ``requires_bounded_policy``).
    cost_upper_bound_usd: float | None = None
    rate_card_id: str = ""
    route_version: str = ""
    final: bool = True
    completeness: str = "unknown"
    #: The source artifact's digest (sha256 over its exact bytes when the
    #: lane stamped it, a content digest otherwise) — tamper-evident: a
    #: changed artifact delivered under the same identity is a conflict,
    #: never a silent overwrite.
    artifact_digest: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.work_id, self.attempt_id, self.receipt_id, self.source)

    @property
    def segment(self) -> str:
        return attribution_segment(
            self.route, rate_card_id=self.rate_card_id, route_version=self.route_version
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "receipt_id": self.receipt_id,
            "source": self.source,
            "provider": self.route.provider,
            "model": self.route.model,
            "counters": self.counters.to_json(),
            "cost_usd": self.cost_usd,
            "cost_basis": self.cost_basis,
            "cost_lower_bound_usd": self.cost_lower_bound_usd,
            "cost_upper_bound_usd": self.cost_upper_bound_usd,
            "rate_card_id": self.rate_card_id,
            "route_version": self.route_version,
            "segment": self.segment,
            "final": self.final,
            "completeness": self.completeness,
            "artifact_digest": self.artifact_digest,
        }

    @classmethod
    def from_json(cls, block: Mapping[str, Any]) -> IngestedUsageRow:
        counters_raw = block.get("counters")
        counters = (
            normalize_counters(counters_raw)
            if isinstance(counters_raw, Mapping)
            else NormalizedCounters()
        )
        cost = _nn_float(block.get("cost_usd"))
        return cls(
            work_id=_text(block.get("work_id")),
            attempt_id=_text(block.get("attempt_id")),
            receipt_id=_text(block.get("receipt_id")),
            source=_text(block.get("source")),
            route=ProviderRoute(
                provider=_text(block.get("provider")), model=_text(block.get("model"))
            ),
            counters=counters,
            cost_usd=cost,
            cost_basis=_text(block.get("cost_basis")),
            cost_lower_bound_usd=_nn_float(block.get("cost_lower_bound_usd")) or 0.0,
            cost_upper_bound_usd=_nn_float(block.get("cost_upper_bound_usd")),
            rate_card_id=_text(block.get("rate_card_id")),
            route_version=_text(block.get("route_version")),
            final=bool(block.get("final", True)),
            completeness=_text(block.get("completeness")) or "unknown",
            artifact_digest=_text(block.get("artifact_digest")),
        )


def rows_from_documents(documents: Sequence[Mapping[str, Any]]) -> list[IngestedUsageRow]:
    """Rebuild ingested rows from their stored documents (the replay path)."""
    return [IngestedUsageRow.from_json(document) for document in documents]


# ----------------------------------------------------------------------
# The store — natural-key upsert, replay is a no-op
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class IngestionConflict:
    """The same natural key delivered with different FINAL content.

    Surfaced, never averaged and never silently overwritten: the FIRST
    final row stands, the exact fold degrades to the honest minimum.
    """

    work_id: str
    attempt_id: str
    receipt_id: str
    source: str
    note: str

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "receipt_id": self.receipt_id,
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True)
class IngestionRefusal:
    """One REFUSED delivery, preserved as a diagnostic (Q39-05, #324).

    The payload's own attribution claim (``usage.work_id`` /
    ``usage.run_id`` / a call's ``attempt_id``) conflicted with the
    TRUSTED caller context — the caller-supplied transport attribution is
    AUTHORITATIVE, so NO row is written to the payload-claimed work and
    the refusal itself (both identities + the digest of what was
    delivered) is preserved: :func:`persist_ingested_rows` lands it in
    ``usage_ingestion_conflicts`` under kind ``attribution-refused``
    (the ``usage.attribution_refused`` observable). ``identity-rejected``
    is the sibling arm: an identity component that cannot live in its
    durable column (a work id wider than the run id column — no run row
    could own it) is rejected, never silently truncated.
    """

    kind: str = ATTRIBUTION_REFUSED
    trusted_work_id: str = ""
    claimed_work_id: str = ""
    attempt_id: str = ""
    claimed_attempt_id: str = ""
    receipt_id: str = ""
    source: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "trusted_work_id": self.trusted_work_id,
            "claimed_work_id": self.claimed_work_id,
            "attempt_id": self.attempt_id,
            "claimed_attempt_id": self.claimed_attempt_id,
            "receipt_id": self.receipt_id,
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True)
class IngestionResult:
    """What one ingestion pass did — created, replayed, reconciled, refused."""

    created: tuple[IngestedUsageRow, ...] = ()
    replayed: tuple[IngestedUsageRow, ...] = ()
    reconciled: tuple[IngestedUsageRow, ...] = ()
    conflicts: tuple[IngestionConflict, ...] = ()
    #: Q39-05: refused deliveries (attribution conflicts / rejected
    #: identities) — diagnostics, never rows; nothing was written to any
    #: payload-claimed identity.
    refused: tuple[IngestionRefusal, ...] = ()

    @property
    def wrote_something_new(self) -> bool:
        return bool(self.created) or bool(self.reconciled)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": INGESTION_SCHEMA,
            "created": [row.to_json() for row in self.created],
            "replayed": [row.to_json() for row in self.replayed],
            "reconciled": [row.to_json() for row in self.reconciled],
            "conflicts": [conflict.to_json() for conflict in self.conflicts],
            "refused": [refusal.to_json() for refusal in self.refused],
        }


class UsageIngestStore:
    """The in-memory natural-key store: idempotent by construction.

    Keyed by ``(work, attempt, receipt, source)``. Ingest rules:

    - new key → the row is created;
    - same key, identical content → REPLAY (a re-ingest, a webhook
      replay, a re-downloaded artifact): nothing moves;
    - same key, stored row is a PARTIAL (``final=False``) and the new row
      is FINAL → RECONCILED: the final state replaces the partial (the
      streaming contract — replaced, never summed);
    - same key, two different FINAL contents → a :class:`IngestionConflict`
      (the first stands; never averaged).
    """

    def __init__(self, rows: Sequence[IngestedUsageRow] = ()) -> None:
        self._rows: dict[tuple[str, str, str, str], IngestedUsageRow] = {}
        for row in rows:
            self._rows.setdefault(row.key, row)

    def ingest(self, rows: Sequence[IngestedUsageRow]) -> IngestionResult:
        created: list[IngestedUsageRow] = []
        replayed: list[IngestedUsageRow] = []
        reconciled: list[IngestedUsageRow] = []
        conflicts: list[IngestionConflict] = []
        for row in rows:
            existing = self._rows.get(row.key)
            if existing is None:
                self._rows[row.key] = row
                created.append(row)
                continue
            if _canonical(existing.to_json()) == _canonical(row.to_json()):
                replayed.append(row)
                continue
            if not existing.final and row.final:
                # The streamed partial reconciles to the artifact's final
                # state — replaced, never summed (R38-09 scope item 3).
                self._rows[row.key] = row
                reconciled.append(row)
                continue
            if not row.final and existing.final:
                # A late partial after the final state: the final stands.
                replayed.append(row)
                continue
            conflicts.append(
                IngestionConflict(
                    work_id=row.work_id,
                    attempt_id=row.attempt_id,
                    receipt_id=row.receipt_id,
                    source=row.source,
                    note=(
                        f"{row.work_id}:{row.attempt_id}:{row.receipt_id} ({row.source})"
                        " delivered two different final contents — the first"
                        " stands, the difference is surfaced, never averaged"
                    ),
                )
            )
        return IngestionResult(
            created=tuple(created),
            replayed=tuple(replayed),
            reconciled=tuple(reconciled),
            conflicts=tuple(conflicts),
        )

    def rows(self) -> list[IngestedUsageRow]:
        """Every stored row, sorted by identity (order invariance)."""
        return [self._rows[key] for key in sorted(self._rows)]

    def by_work(self, work_id: str) -> list[IngestedUsageRow]:
        return [row for row in self.rows() if row.work_id == work_id]

    def documents(self) -> list[dict[str, Any]]:
        return [row.to_json() for row in self.rows()]


# ----------------------------------------------------------------------
# Artifact parsing — the three source kinds, each with its own label
# ----------------------------------------------------------------------


def _artifact_rows(
    artifact: Mapping[str, Any],
    *,
    trusted_work_id: str,
    trusted_attempt_id: str,
    source: str,
    rate_card_id: str,
    route_version: str,
    final: bool | None,
    default_provider: str,
    default_model: str,
    cost_basis: str,
    cost_lower_bound_usd: float,
) -> tuple[list[IngestedUsageRow], list[IngestionRefusal]]:
    """Parse ONE source artifact into ingested rows (identity-keyed).

    Q39-05 (#324): the TRUSTED caller attribution (``trusted_work_id`` /
    ``trusted_attempt_id``) is AUTHORITATIVE. The payload's own claims
    are compared against it, never preferred:

    - a payload ``work_id``/``run_id`` that CONFLICTS with the trusted
      work → the WHOLE artifact is refused (one diagnostic per receipt
      identity it would have written): no row lands under the claimed
      work, and nothing lands under the trusted work either — the
      delivery is untrustworthy end to end;
    - a payload ``attempt_id`` that conflicts with the trusted attempt →
      that row is refused; without a trusted attempt claim the payload's
      own attempt stands (nothing to contradict);
    - without a trusted work claim the payload's work id is used (no
      trusted context exists to be authoritative).
    """

    def _row(
        payload: Mapping[str, Any], *, receipt_id: str, row_attempt: str, row_source: str
    ) -> IngestedUsageRow:
        provider = _text(payload.get("provider") or payload.get("driver")) or default_provider
        model = _text(payload.get("model")) or default_model
        counters = normalize_counters(payload, driver=provider)
        cost = _nn_float(payload.get("total_cost_usd", payload.get("cost_usd")))
        completeness = _text(payload.get("completeness"))
        if completeness not in ("exact", "aggregate", "partial"):
            completeness = (
                "aggregate"
                if counters.known_total_tokens is not None or cost is not None
                else "unknown"
            )
        is_final = bool(payload.get("final", True)) if final is None else final
        row = IngestedUsageRow(
            work_id=trusted_work_id,
            attempt_id=row_attempt,
            receipt_id=receipt_id,
            source=row_source,
            route=ProviderRoute(provider=provider, model=model),
            counters=counters,
            cost_usd=cost,
            cost_basis=(_text(payload.get("cost_basis")) or cost_basis) if cost is not None else "",
            cost_lower_bound_usd=cost_lower_bound_usd if cost is None else cost,
            cost_upper_bound_usd=_nn_float(payload.get("cost_upper_bound_usd")),
            rate_card_id=_text(payload.get("rate_card_id")) or rate_card_id,
            route_version=_text(payload.get("route_version")) or route_version,
            final=is_final,
            completeness="partial" if not is_final else completeness,
            artifact_digest=_text(payload.get("artifact_digest")),
        )
        if not row.artifact_digest:
            # No lane-stamped bytes digest: the row's own content digest —
            # still tamper-evident (same identity + different content can
            # never collide with it).
            row = replace(row, artifact_digest=f"content:{_digest(_canonical(row.to_json()))}")
        return row

    # Per-call receipts stream inside one artifact (the SDK's own receipt
    # JSONs / an llm_calls ledger extract): each call is its own identity.
    calls = artifact.get("receipts") if isinstance(artifact.get("receipts"), list) else None
    if calls is None and isinstance(artifact.get("calls"), list):
        calls = artifact["calls"]

    def _stable_receipt_id(payload: Mapping[str, Any], index: int | None = None) -> str:
        """The identity a source artifact carries, or its stable fallback.

        A declared id (``receipt_id`` / ``call_id``) always wins. The
        fallbacks keep DISTINCT deliveries distinct (Q39-05: missing
        per-call IDs never collapse an array):

        - an ARRAY entry without a declared id keys on its POSITION —
          never on the aggregate lane identity (the old order let an
          id-less array collapse into one ``lane:...`` key, summing N
          calls as one receipt);
        - the lane's per-attempt AGGREGATE artifact (no array) falls
          back to the STABLE artifact identity ``(work, attempt,
          source)`` — never a content digest, because the streaming
          contract must reconcile a partial to its final state UNDER ONE
          identity;
        - work-level (planner) rows fall back to a content digest — they
          are final facts, never reconciled.
        """
        declared = _text(payload.get("receipt_id") or payload.get("call_id") or payload.get("id"))
        if declared:
            return declared
        if index is not None:
            return f"{_text(artifact.get('receipt_id')) or source}:call:{index}"
        if trusted_attempt_id or _text(payload.get("attempt_id")):
            return (
                f"lane:{trusted_work_id}:"
                f"{_text(payload.get('attempt_id')) or trusted_attempt_id}:{source}"
            )
        return f"receipt:{_digest(_canonical(dict(artifact)))}"

    claimed_work = _text(artifact.get("work_id") or artifact.get("run_id"))
    entries: list[tuple[Mapping[str, Any], int | None]] = (
        [(call, index) for index, call in enumerate(calls) if isinstance(call, Mapping)]
        if calls is not None
        else [(artifact, None)]
    )

    # The WORK-level conflict refuses the whole artifact: the transport
    # said work-A, the payload says work-B — nothing it carries is
    # attributable, so nothing is written to either side.
    if trusted_work_id and claimed_work and claimed_work != trusted_work_id:
        refusals = [
            IngestionRefusal(
                kind=ATTRIBUTION_REFUSED,
                trusted_work_id=trusted_work_id,
                claimed_work_id=claimed_work,
                attempt_id=trusted_attempt_id,
                claimed_attempt_id=_text(payload.get("attempt_id")),
                receipt_id=_stable_receipt_id(payload, index=index),
                source=source or _text(payload.get("source")),
                note=(
                    f"payload claimed work {claimed_work!r} on a transport attributed to "
                    f"{trusted_work_id!r} — the caller attribution is authoritative; no "
                    "row is written to the claimed work (usage.attribution_refused)"
                ),
            )
            for payload, index in entries
        ]
        return [], refusals

    rows: list[IngestedUsageRow] = []
    attempt_refusals: list[IngestionRefusal] = []
    for payload, index in entries:
        claimed_attempt = _text(payload.get("attempt_id"))
        if trusted_attempt_id and claimed_attempt and claimed_attempt != trusted_attempt_id:
            attempt_refusals.append(
                IngestionRefusal(
                    kind=ATTRIBUTION_REFUSED,
                    trusted_work_id=trusted_work_id,
                    claimed_work_id=claimed_work or trusted_work_id,
                    attempt_id=trusted_attempt_id,
                    claimed_attempt_id=claimed_attempt,
                    receipt_id=_stable_receipt_id(payload, index=index),
                    source=source or _text(payload.get("source")),
                    note=(
                        f"payload claimed attempt {claimed_attempt!r} on a delivery the "
                        f"trusted context attributes to attempt {trusted_attempt_id!r} — "
                        "the caller attribution is authoritative; the row is refused "
                        "(usage.attribution_refused)"
                    ),
                )
            )
            continue
        rows.append(
            _row(
                payload,
                receipt_id=_stable_receipt_id(payload, index=index),
                # The TRUSTED attempt wins when the caller claimed one;
                # the payload's own marker is the fallback, never the
                # other way round (Q39-05's attribution contract).
                row_attempt=trusted_attempt_id or claimed_attempt,
                row_source=source or _text(payload.get("source")),
            )
        )
    return rows, attempt_refusals


def ingest_usage_artifact(
    store: UsageIngestStore,
    artifact: Mapping[str, Any],
    *,
    work_id: str = "",
    attempt_id: str = "",
    source: str = "",
    rate_card_id: str = "",
    route_version: str = "",
    final: bool | None = None,
    cost_basis: str = COST_BASIS_PROVIDER_REPORTED,
    cost_lower_bound_usd: float = 0.0,
) -> IngestionResult:
    """Ingest one usage artifact idempotently — replay writes NOTHING new.

    *artifact* is one of the source shapes, each with its own attribution
    label (the ``source`` argument / the artifact's own ``source``):

    - the lane's R23 ``.forge/usage.json`` shape (the meta ``usage``
      block: canonical counters, ``completeness``, optional
      ``total_cost_usd`` the SDK reported);
    - an SDK receipt JSON (the claude-sdk cost block: vendor usage
      counters + ``total_cost_usd``, optionally a list of per-call
      receipts under ``receipts``/``calls``);
    - a planner/LLM-call ledger row extract (``call_id`` identities with
      per-call counters and optional cost).

    The context arguments (``work_id`` / ``attempt_id`` / ``source`` /
    ``rate_card_id`` / ``route_version`` / ``final``) are the TRUSTED
    attribution — what the caller knows about where the artifact came
    from; that attribution is AUTHORITATIVE (Q39-05, #324): a payload
    ``work_id``/``run_id`` or per-call ``attempt_id`` that CONFLICTS with
    the trusted context is REFUSED and preserved as a diagnostic
    (:class:`IngestionRefusal`, the ``usage.attribution_refused``
    observable) — no row is ever written under the payload-claimed
    identity. Returns what the pass did; the store's natural-key upsert
    makes a re-ingest or a webhook replay a no-op.
    """
    rows, refusals = _artifact_rows(
        artifact,
        trusted_work_id=_text(work_id),
        trusted_attempt_id=_text(attempt_id),
        source=_text(source),
        rate_card_id=rate_card_id,
        route_version=route_version,
        final=final,
        default_provider=_text(artifact.get("provider") or artifact.get("driver")),
        default_model=_text(artifact.get("model")),
        cost_basis=cost_basis,
        cost_lower_bound_usd=cost_lower_bound_usd,
    )
    result = store.ingest(rows)
    return IngestionResult(
        created=result.created,
        replayed=result.replayed,
        reconciled=result.reconciled,
        conflicts=result.conflicts,
        refused=tuple(refusals),
    )


# ----------------------------------------------------------------------
# Streaming + crash reconciliation
# ----------------------------------------------------------------------


def reconcile_after_death(
    store: UsageIngestStore,
    final_artifacts: Sequence[Mapping[str, Any]],
    *,
    work_id: str = "",
    source: str = "",
    rate_card_id: str = "",
    route_version: str = "",
) -> dict[str, Any]:
    """The post-crash re-run: reconcile streamed partials to final state.

    The lane streamed receipts as calls completed, then the process died
    before the final artifact upload. The re-run ingests whatever final
    artifacts exist (each partial's final state REPLACES the streamed
    partial under the SAME natural key — pass the same trusted
    ``work_id``/``source`` attribution the streamed delivery carried,
    otherwise the final state lands under a different key and reconciles
    nothing); whatever no final artifact covers stays a PARTIAL with its
    known lower bound — the unresolved remainder, preserved as unknown
    with a lower bound, never zero and never silently healed.
    """
    reconciled: list[str] = []
    for artifact in final_artifacts:
        result = ingest_usage_artifact(
            store,
            artifact,
            work_id=work_id,
            source=source,
            rate_card_id=rate_card_id,
            route_version=route_version,
            final=True,
        )
        reconciled.extend(row.receipt_id for row in result.reconciled)
    remainder = [row for row in store.rows() if not row.final]
    return {
        "schema": INGESTION_SCHEMA,
        "reconciled_receipt_ids": sorted(reconciled),
        "unresolved_remainder": [
            {
                "receipt_id": row.receipt_id,
                "attempt_id": row.attempt_id,
                "source": row.source,
                "cost_lower_bound_usd": row.cost_lower_bound_usd,
                "note": (
                    "no final artifact covers this streamed receipt — kept"
                    " partial with its known lower bound, never zero"
                ),
            }
            for row in remainder
        ],
        "unresolved_cost_lower_bound_usd": round(
            math.fsum(row.cost_lower_bound_usd for row in remainder), 6
        ),
    }


# ----------------------------------------------------------------------
# The exposure fold — finality settles; the spend-cap check over it
# ----------------------------------------------------------------------


def _finite_nonneg(value: Any) -> bool:
    """Whether *value* is a finite nonnegative number (bools excluded)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


@dataclass(frozen=True)
class SpendExposure:
    """The run's exposure folded by FINALITY (R40-03/#339).

    Settlement is decided by the row's ``final`` marker, NEVER by the
    presence of a cost — a partial that already carries a subtotal is
    ACCRUED, not settled, and keeps its whole retained envelope. The
    separated quantities:

    - ``settled_usd`` — final rows' costs. A settlement releases its
      interval's envelope exactly once (the natural-key contract makes a
      replayed identical final a no-op); ``settlement_release_usd`` is
      the net envelope headroom the settlements returned (the envelope
      each settled row would still retain as nonfinal, minus its
      settled cost).
    - ``accrued_unsettled_usd`` — nonfinal rows' observed subtotals:
      the streaming provider's intermediate figures. Reported, never
      treated as settled.
    - ``unknown_lower_bound`` — the unresolved intervals' floors (a
      partial's own subtotal is its floor); a lower bound can never
      bound spend from ABOVE.
    - ``retained_liability_usd`` — the worst-case retained envelope over
      the unresolved intervals: each contributes ``max(finite upper
      envelope, accrued cost, lower bound)`` — the accrued cost rides
      INSIDE the envelope, never double-counted, and an envelope below
      the accrued cost is an ``incoherent-bound`` inconsistency, never
      free headroom.

    Unresolved intervals are nonfinal rows AND final rows that never
    reported a cost. An unresolved interval with no finite envelope
    (neither its own ``cost_upper_bound_usd`` nor the bounded-policy
    ceiling) is a typed ``unbounded-exposure`` finding.
    """

    settled_usd: float
    accrued_unsettled_usd: float
    unknown_lower_bound: float
    retained_liability_usd: float
    settlement_release_usd: float
    unresolved_intervals: int
    unbounded_intervals: int
    incoherent_bounds: int
    requires_bounded_policy: bool
    findings: tuple[dict[str, Any], ...] = ()

    @property
    def reserved_usd(self) -> float:
        """The hard cap's reservation: settled + retained liability."""
        return self.settled_usd + self.retained_liability_usd

    def to_json(self) -> dict[str, Any]:
        return {
            "settled_usd": round(self.settled_usd, 6),
            "accrued_unsettled_usd": round(self.accrued_unsettled_usd, 6),
            "unknown_lower_bound": round(self.unknown_lower_bound, 6),
            "retained_liability_usd": round(self.retained_liability_usd, 6),
            "settlement_release_usd": round(self.settlement_release_usd, 6),
            "unresolved_intervals": self.unresolved_intervals,
            "unbounded_intervals": self.unbounded_intervals,
            "incoherent_bounds": self.incoherent_bounds,
            "requires_bounded_policy": self.requires_bounded_policy,
            "findings": [dict(finding) for finding in self.findings],
        }


def exposure_fold(
    rows: Sequence[IngestedUsageRow],
    *,
    unknown_interval_ceiling_usd: float | None = None,
) -> SpendExposure:
    """Fold ingested rows into the run's exposure, classified by FINALITY.

    Every quantity is cap-independent; malformed numbers surface as
    typed findings and are treated conservatively (an invalid cost never
    settles an interval, an invalid bound is no bound, an invalid policy
    ceiling is treated as not supplied) — never silently coerced.
    """
    findings: list[dict[str, Any]] = []
    ceiling: float | None = None
    if unknown_interval_ceiling_usd is not None:
        if _finite_nonneg(unknown_interval_ceiling_usd):
            ceiling = float(unknown_interval_ceiling_usd)
        else:
            findings.append(
                {
                    "type": INVALID_POLICY_CEILING,
                    "value": repr(unknown_interval_ceiling_usd),
                    "note": (
                        "the bounded-policy ceiling is not a finite nonnegative"
                        " number — treated as NOT supplied (never silently"
                        " coerced); every interval relying on it is unbounded"
                    ),
                }
            )

    settled: list[float] = []
    accrued: list[float] = []
    lowers: list[float] = []
    retained: list[float] = []
    releases: list[float] = []
    unresolved = unbounded = incoherent = 0

    for row in rows:
        cost = row.cost_usd
        cost_ok = cost is not None and _finite_nonneg(cost)
        if cost is not None and not cost_ok:
            findings.append(
                {
                    "type": INVALID_COST,
                    "receipt_id": row.receipt_id,
                    "work_id": row.work_id,
                    "value": repr(cost),
                    "note": (
                        "the row's cost is not a finite nonnegative number —"
                        " the figure is untrustworthy: the interval stays"
                        " unresolved (never settled at a garbage value)"
                    ),
                }
            )
        observed = float(cost) if cost is not None and _finite_nonneg(cost) else None
        upper = row.cost_upper_bound_usd
        if upper is not None and not _finite_nonneg(upper):
            findings.append(
                {
                    "type": INVALID_BOUND,
                    "receipt_id": row.receipt_id,
                    "work_id": row.work_id,
                    "value": repr(upper),
                    "note": (
                        "the row's upper bound is not a finite nonnegative"
                        " number — treated as absent, never clamped"
                    ),
                }
            )
            upper = None
        lower = row.cost_lower_bound_usd
        if not _finite_nonneg(lower):
            findings.append(
                {
                    "type": INVALID_LOWER_BOUND,
                    "receipt_id": row.receipt_id,
                    "work_id": row.work_id,
                    "value": repr(lower),
                    "note": (
                        "the row's lower bound is not a finite nonnegative"
                        " number — reported at 0 for the (non-gating)"
                        " lower-bound sum, corruption visible here"
                    ),
                }
            )
            lower = 0.0
        else:
            lower = float(lower)

        if row.final and observed is not None:
            # SETTLED — finality settles, exactly once: the envelope this
            # interval would still retain as nonfinal is released, and
            # the settled cost is all that remains of it.
            settled.append(observed)
            twin = upper if upper is not None else ceiling
            envelope = max(observed, lower) if twin is None else max(twin, observed, lower)
            if upper is not None and upper < observed:
                incoherent += 1
                findings.append(
                    {
                        "type": INCOHERENT_BOUND,
                        "receipt_id": row.receipt_id,
                        "work_id": row.work_id,
                        "settled_usd": observed,
                        "upper_bound_usd": upper,
                        "note": (
                            "the settlement landed ABOVE the interval's own"
                            " declared envelope — a visible inconsistency;"
                            " the settled figure stands"
                        ),
                    }
                )
            releases.append(max(envelope - observed, 0.0))
            continue

        # UNRESOLVED interval — a nonfinal row (a partial, whatever cost
        # it already carries) or a final row that never reported a cost.
        unresolved += 1
        floor = max(lower, observed) if observed is not None else lower
        lowers.append(floor)
        if observed is not None:
            accrued.append(observed)
        bound = upper if upper is not None else ceiling
        if bound is None:
            unbounded += 1
            findings.append(
                {
                    "type": UNBOUNDED_EXPOSURE,
                    "receipt_id": row.receipt_id,
                    "work_id": row.work_id,
                    "final": row.final,
                    "accrued_cost_usd": observed,
                    "note": (
                        "an unresolved interval with no finite upper envelope"
                        " (a partial that already carries a cost included) —"
                        " the next chargeable action is refused until an"
                        " explicit bounded-policy ceiling is supplied or the"
                        " interval settles"
                    ),
                }
            )
            continue
        if observed is not None and bound < observed:
            incoherent += 1
            findings.append(
                {
                    "type": INCOHERENT_BOUND,
                    "receipt_id": row.receipt_id,
                    "work_id": row.work_id,
                    "accrued_cost_usd": observed,
                    "upper_bound_usd": bound,
                    "note": (
                        "the interval's upper bound is BELOW its observed"
                        " accrued cost — a visible inconsistency; the interval"
                        " retains at least its accrued cost, never free"
                        " headroom"
                    ),
                }
            )
        # The retained liability, WITHOUT double-counting the accrued
        # cost: max(envelope, accrued, lower) — the envelope covers the
        # subtotal, the subtotal never releases it (R40-03/P01).
        retained.append(max(bound, floor))

    return SpendExposure(
        settled_usd=math.fsum(settled),
        accrued_unsettled_usd=math.fsum(accrued),
        unknown_lower_bound=math.fsum(lowers),
        retained_liability_usd=math.fsum(retained),
        settlement_release_usd=math.fsum(releases),
        unresolved_intervals=unresolved,
        unbounded_intervals=unbounded,
        incoherent_bounds=incoherent,
        requires_bounded_policy=unbounded > 0,
        findings=tuple(findings),
    )


def spend_cap_check(
    rows: Sequence[IngestedUsageRow],
    *,
    cap_usd: float,
    projection_usd: float = 0.0,
    unknown_interval_ceiling_usd: float | None = None,
) -> dict[str, Any]:
    """Whether the NEXT chargeable action fits the cap over ingested spend.

    The check the existing budget/caps gate consults before the next
    chargeable action (the dispatch gate / the SpendLedger projection
    seam). The quantities stay separated (:class:`SpendExposure`):
    settled spend (FINAL rows — finality, never value presence,
    settles), the nonfinal rows' accrued-unsettled subtotals, the
    unresolved intervals' lower bound, and their retained liability (the
    worst-case envelope; each interval contributes ``max(envelope,
    accrued, lower)`` so a partial's subtotal rides INSIDE the envelope
    and never releases it). The HARD CAP consults ``settled +
    retained_liability + projection`` — never a lower bound.

    A partial that already carries a cost cannot spend the headroom its
    envelope still fences (the P01 counterexample: cap 10, settled 8, a
    partial at 0.5 inside an envelope of 3, projection 1 — the old
    value-presence math "allowed" 9.5 while the bounded exposure is
    8 + 3 + 1 = 12). An unresolved interval with NO finite upper bound —
    a nonfinal row with a cost included (typed ``unbounded-exposure``)
    — sets ``requires_bounded_policy`` and refuses the next chargeable
    action until an explicit bounded policy ceiling is supplied or the
    interval settles. A settlement releases the envelope exactly once
    (``settlement_release_usd``; a replayed identical final is a store
    no-op by the natural-key contract). Malformed numbers (NaN,
    infinity, negative) surface as typed findings and refuse — never
    silent defaults.
    """
    findings: list[dict[str, Any]] = []
    cap: float | None = None
    if isinstance(cap_usd, bool) or not isinstance(cap_usd, (int, float)):
        findings.append(
            {
                "type": INVALID_CAP,
                "value": repr(cap_usd),
                "note": "the cap is not a number — the check refuses, never clamps",
            }
        )
    else:
        candidate = float(cap_usd)
        if math.isnan(candidate) or candidate < 0:
            findings.append(
                {
                    "type": INVALID_CAP,
                    "value": repr(cap_usd),
                    "note": (
                        "the cap is negative or not-a-number — the check"
                        " refuses, never clamps (+inf is the deliberate"
                        " un-bounded fold arm and is not a finding)"
                    ),
                }
            )
        else:
            cap = candidate
    projection = 0.0
    if _finite_nonneg(projection_usd):
        projection = float(projection_usd)
    else:
        findings.append(
            {
                "type": INVALID_PROJECTION,
                "value": repr(projection_usd),
                "note": (
                    "the projection is not a finite nonnegative number — the"
                    " check refuses, never silently zeroed"
                ),
            }
        )

    exposure = exposure_fold(rows, unknown_interval_ceiling_usd=unknown_interval_ceiling_usd)
    findings.extend(dict(finding) for finding in exposure.findings)
    blocked = any(finding["type"] in BLOCKING_FINDINGS for finding in findings)
    reserved = exposure.reserved_usd
    allowed = (not blocked) and cap is not None and reserved + projection <= cap

    notes = [
        "hard cap consults settled + retained_liability + projection"
        " (finality, not value presence, settles — Q39-06/P03, R40-03/P01)"
    ]
    if exposure.accrued_unsettled_usd > 0:
        notes.append(
            f"{round(exposure.accrued_unsettled_usd, 6)} usd accrued-unsettled"
            " rides inside the retained envelope — a partial's subtotal"
            " never settles liability"
        )
    if exposure.unresolved_intervals:
        notes.append(
            f"{exposure.unresolved_intervals} unresolved interval(s): lower"
            f" bound {round(exposure.unknown_lower_bound, 6)} usd, retained"
            f" envelope {round(exposure.retained_liability_usd, 6)} usd —"
            " never reserved as zero"
        )
    if exposure.requires_bounded_policy:
        notes.append(
            f"{exposure.unbounded_intervals} unresolved interval(s) have NO"
            " finite upper bound — the next chargeable action is blocked"
            " until an explicit bounded policy ceiling is supplied or the"
            " interval reconciles"
        )
    if exposure.settlement_release_usd > 0:
        notes.append(
            f"settlement released {round(exposure.settlement_release_usd, 6)}"
            " usd of retained envelope — exactly once per settled interval"
        )
    return {
        "schema": INGESTION_SCHEMA,
        "allowed": allowed,
        "cap_usd": cap_usd,
        "projection_usd": projection_usd,
        # the finality-separated quantities (R40-03/#339)
        "settled_usd": exposure.settled_usd,
        "accrued_unsettled_usd": exposure.accrued_unsettled_usd,
        "unknown_lower_bound": exposure.unknown_lower_bound,
        "retained_liability_usd": exposure.retained_liability_usd,
        "settlement_release_usd": exposure.settlement_release_usd,
        "unresolved_intervals": exposure.unresolved_intervals,
        "unbounded_intervals": exposure.unbounded_intervals,
        "incoherent_bounds": exposure.incoherent_bounds,
        "requires_bounded_policy": exposure.requires_bounded_policy,
        # legacy aliases (the seam the live gate/report reads): the known
        # spend is the SETTLED figure — accrued subtotals live inside the
        # retained liability, never added on top.
        "known_spend": exposure.settled_usd,
        "known_cost_usd": exposure.settled_usd,
        "reserved_liability": exposure.retained_liability_usd,
        "unknown_intervals": exposure.unresolved_intervals,
        "reserved_usd": reserved,
        "headroom_usd": (cap - reserved) if cap is not None else None,
        "findings": findings,
        "notes": notes,
    }


# ----------------------------------------------------------------------
# The durable write — ONE canonical identity, conditional reconciliation
# ----------------------------------------------------------------------

#: The durable identity components' column widths (usage_receipts).
_WORK_ID_WIDTH: int = 32
_ATTEMPT_ID_WIDTH: int = 100
_RECEIPT_ID_WIDTH: int = 64
_SOURCE_WIDTH: int = 100

_CONFLICTING_FINAL = "conflicting-final"
_CONFLICTING_CONTENT = "conflicting-content"


@dataclass(frozen=True)
class PersistOutcome:
    """What one durable pass did (Q39-05, #324).

    ``created`` — new durable identities; ``reconciled`` — a stored
    PARTIAL replaced by its FINAL (``usage.partial_to_final``, the
    conditional ``DO UPDATE ... WHERE final IS NOT TRUE``); ``replayed``
    — the standing row already matches (a repeated identical final is a
    NO-OP); ``conflicts`` — conflicting deliveries recorded in
    ``usage_ingestion_conflicts`` (the standing row never moved);
    ``refusals`` — attribution/identity refusals recorded as diagnostics
    (no spend row written for any of them).
    """

    created: int = 0
    replayed: int = 0
    reconciled: int = 0
    conflicts: int = 0
    refusals: int = 0


def _hash_component(value: str) -> str:
    """The stable overlength spelling: ``sha256:<16 hex>`` of the FULL value.

    Two identifiers sharing their first 64 characters hash to DIFFERENT
    digests — the collapse the silent ``[:64]`` truncation used to cause
    is structurally gone. The full original always rides ``raw`` beside
    (``identity_overlength``), so nothing is lost to the hash.
    """
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True)
class _DurableIdentity:
    """One row's canonical durable identity, width-safe."""

    attempt_id: str
    receipt_id: str
    source_namespace: str
    identity_digest: str
    overlength: dict[str, str]


def _durable_identity(row: IngestedUsageRow) -> _DurableIdentity:
    overlength: dict[str, str] = {}

    def _fit(value: str, width: int, name: str) -> str:
        if len(value) > width:
            overlength[name] = value
            return _hash_component(value)
        return value

    # The stable digest over the FULL identity — canonical regardless of
    # column widths, recorded on EVERY row (not only overlength ones) so
    # the identity is joinable even when its parts are hashed spellings.
    identity_digest = (
        "identity:"
        + hashlib.sha256(
            _canonical([row.work_id, row.attempt_id, row.receipt_id, row.source or ""]).encode(
                "utf-8"
            )
        ).hexdigest()[:40]
    )
    return _DurableIdentity(
        attempt_id=_fit(row.attempt_id, _ATTEMPT_ID_WIDTH, "attempt_id"),
        receipt_id=_fit(row.receipt_id, _RECEIPT_ID_WIDTH, "receipt_id"),
        source_namespace=_fit(row.source or "", _SOURCE_WIDTH, "source"),
        identity_digest=identity_digest,
        overlength=overlength,
    )


def _raw_document(row: IngestedUsageRow, identity: _DurableIdentity) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "cost_usd": row.cost_usd,
        "cost_basis": row.cost_basis,
        "cost_lower_bound_usd": row.cost_lower_bound_usd,
        "cost_upper_bound_usd": row.cost_upper_bound_usd,
        "rate_card_id": row.rate_card_id,
        "route_version": row.route_version,
        "segment": row.segment,
        "final": row.final,
        "artifact_digest": row.artifact_digest,
        "identity_digest": identity.identity_digest,
    }
    if identity.overlength:
        # The FULL originals beside their hashed column spellings — the
        # hash is never a lossy replacement (Q39-05: reject or hash,
        # never silently truncate).
        raw["identity_overlength"] = dict(identity.overlength)
    return raw


def _row_values(row: IngestedUsageRow) -> dict[str, Any]:
    identity = _durable_identity(row)
    return {
        "run_id": row.work_id,
        "attempt_id": identity.attempt_id,
        "receipt_id": identity.receipt_id,
        "source_namespace": identity.source_namespace,
        "identity_digest": identity.identity_digest,
        "driver": row.route.provider or None,
        "model": row.route.model or None,
        "input_tokens": row.counters.input_tokens,
        "cached_input_tokens": row.counters.cached_input_tokens,
        "cache_write_tokens": row.counters.cache_write_tokens,
        "output_tokens": row.counters.output_tokens,
        "completeness": row.completeness,
        "source": identity.source_namespace or None,
        "final": row.final,
        "cost_usd": row.cost_usd,
        "cost_basis": row.cost_basis or None,
        "rate_card_id": row.rate_card_id or None,
        "route_version": row.route_version or None,
        "segment": row.segment,
        "artifact_digest": row.artifact_digest or None,
        "raw": _raw_document(row, identity),
    }


_IDENTITY_COLUMNS = (
    "run_id",
    "attempt_id",
    "receipt_id",
    "source_namespace",
)


def _standing_matches(standing: Any, row: IngestedUsageRow) -> bool:
    """Whether the standing row's durable content already equals the delivery.

    A repeated identical final (or an identical re-delivered partial) is
    a REPLAY — nothing moves. The comparison covers every durable
    content column (counters, cost lineage, digest, finality), never the
    audit columns (id, created_at).
    """
    return (
        standing.input_tokens == row.counters.input_tokens
        and standing.cached_input_tokens == row.counters.cached_input_tokens
        and standing.cache_write_tokens == row.counters.cache_write_tokens
        and standing.output_tokens == row.counters.output_tokens
        and standing.completeness == row.completeness
        and bool(standing.final) == bool(row.final)
        and standing.cost_usd == row.cost_usd
        and (standing.cost_basis or "") == (row.cost_basis or "")
        and (standing.rate_card_id or "") == (row.rate_card_id or "")
        and (standing.route_version or "") == (row.route_version or "")
        and (standing.artifact_digest or "") == (row.artifact_digest or "")
    )


def _delivered_digest(row: IngestedUsageRow) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            _canonical(
                {
                    "identity": [row.work_id, row.attempt_id, row.receipt_id, row.source],
                    "counters": row.counters.to_json(),
                    "cost_usd": row.cost_usd,
                    "cost_basis": row.cost_basis,
                    "final": row.final,
                    "artifact_digest": row.artifact_digest,
                }
            ).encode("utf-8")
        ).hexdigest()[:48]
    )


async def _record_ingestion_event(
    session: Any,
    conflict_model: Any,
    pg_insert: Any,
    *,
    kind: str,
    detail: dict[str, Any],
    digest: str,
    run_id: str,
    attempt_id: str,
    receipt_id: str,
    source_namespace: str,
) -> None:
    """Land ONE diagnostic event in ``usage_ingestion_conflicts``.

    Idempotent under replay: the unique (identity, kind, content digest)
    index + ON CONFLICT DO NOTHING leaves exactly one row per distinct
    delivered content, no matter how often the same conflicting delivery
    re-arrives.
    """
    await session.execute(
        pg_insert(conflict_model)
        .values(
            run_id=run_id,
            attempt_id=attempt_id,
            receipt_id=receipt_id,
            source_namespace=source_namespace,
            kind=kind,
            content_digest=digest,
            detail=detail,
        )
        .on_conflict_do_nothing(
            index_elements=[
                conflict_model.run_id,
                conflict_model.attempt_id,
                conflict_model.receipt_id,
                conflict_model.source_namespace,
                conflict_model.kind,
                conflict_model.content_digest,
            ]
        )
    )


async def persist_ingested_rows(
    session: Any,
    rows: Sequence[IngestedUsageRow],
    *,
    refusals: Sequence[IngestionRefusal] = (),
) -> PersistOutcome:
    """Write ingested rows durably under ONE canonical identity (Q39-05).

    The durable identity is ``(run_id, attempt_id, receipt_id,
    source_namespace)`` — the SAME four components the in-memory store
    keys on — with the stable ``identity_digest`` recorded beside. Per
    row, in one transaction (the CALLER commits):

    1. ``ON CONFLICT DO NOTHING`` — a new identity is CREATED, a replay
       of a known identity falls through;
    2. a CONDITIONAL upsert — ``ON CONFLICT ... DO UPDATE SET ... WHERE
       existing.final IS NOT TRUE`` — reconciles a stored PARTIAL to the
       incoming FINAL (replaced, never summed; a late partial NEVER
       downgrades a final — a non-final delivery never enters this
       branch). Two concurrent finalizers race safely: the conditional
       WHERE makes exactly one reconcile win, the loser classifies
       against the standing row below;
    3. the standing row is compared — identical content is a REPLAY (a
       repeated identical final is a no-op); different content is a
       CONFLICT recorded in ``usage_ingestion_conflicts`` (kind
       ``conflicting-final`` when both are final, else
       ``conflicting-content``) — the standing row never moves, the
       difference is never averaged and never dropped.

    An overlength component (attempt > 100, receipt > 64, source > 100)
    is stored as the stable ``sha256:`` hash of the FULL value with the
    original preserved in ``raw.identity_overlength`` — never silently
    truncated; a work id wider than the run id column is REJECTED
    outright (no run row could own it) and recorded as an
    ``identity-rejected`` diagnostic. *refusals* (attribution conflicts
    from :func:`ingest_usage_artifact`) land as ``attribution-refused``
    diagnostics keyed by the TRUSTED work — no row is ever written to a
    payload-claimed identity.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from forge.durable.models import UsageIngestionConflict, UsageReceipt

    created = replayed = reconciled = conflicts = refusal_count = 0
    for row in rows:
        identity = _durable_identity(row)
        if len(row.work_id) > _WORK_ID_WIDTH:
            # REJECT, never truncate: a work id the run-id column cannot
            # hold names no run row — writing it (hashed or cut) would
            # fabricate attribution. The rejection is a durable
            # diagnostic keyed by the HASHED claim (the full value rides
            # ``detail``).
            hashed_work = _hash_component(row.work_id)
            await _record_ingestion_event(
                session,
                UsageIngestionConflict,
                pg_insert,
                kind=IDENTITY_REJECTED,
                run_id=hashed_work,
                attempt_id=identity.attempt_id,
                receipt_id=identity.receipt_id,
                source_namespace=identity.source_namespace,
                digest=_delivered_digest(row),
                detail={
                    "full_work_id": row.work_id,
                    "reason": (
                        "the claimed work id exceeds the durable run id width — no "
                        "run row could own it; rejected, never truncated"
                    ),
                    "delivered": row.to_json(),
                },
            )
            refusal_count += 1
            continue
        values = _row_values(row)
        arbiter = [
            UsageReceipt.run_id,
            UsageReceipt.attempt_id,
            UsageReceipt.receipt_id,
            UsageReceipt.source_namespace,
        ]
        inserted = await session.execute(
            pg_insert(UsageReceipt)
            .values(**values)
            # Step 1 — the portable DO NOTHING arbiter (Postgres and
            # SQLite): a new identity lands, a known one falls through.
            .on_conflict_do_nothing(index_elements=arbiter)
        )
        if inserted.rowcount == 1:  # type: ignore[attr-defined]
            created += 1
            continue
        if row.final:
            # Step 2 — the conditional reconcile (the PostgreSQL docs'
            # DO UPDATE ... WHERE semantics): only a NON-final standing
            # row is upgraded; a final one (or a racing winner that just
            # made it final) matches zero rows and falls through.
            upgraded = await session.execute(
                pg_insert(UsageReceipt)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=arbiter,
                    set_={
                        "driver": values["driver"],
                        "model": values["model"],
                        "input_tokens": values["input_tokens"],
                        "cached_input_tokens": values["cached_input_tokens"],
                        "cache_write_tokens": values["cache_write_tokens"],
                        "output_tokens": values["output_tokens"],
                        "completeness": values["completeness"],
                        "source": values["source"],
                        "final": True,
                        "cost_usd": values["cost_usd"],
                        "cost_basis": values["cost_basis"],
                        "rate_card_id": values["rate_card_id"],
                        "route_version": values["route_version"],
                        "segment": values["segment"],
                        "artifact_digest": values["artifact_digest"],
                        "raw": values["raw"],
                    },
                    where=UsageReceipt.final.isnot(True),
                )
            )
            if upgraded.rowcount == 1:  # type: ignore[attr-defined]
                reconciled += 1
                continue
        # Step 3 — replay or explicit conflict, judged against the
        # standing row inside the same transaction.
        standing = (
            (
                await session.execute(
                    select(UsageReceipt).where(
                        UsageReceipt.run_id == values["run_id"],
                        UsageReceipt.attempt_id == values["attempt_id"],
                        UsageReceipt.receipt_id == values["receipt_id"],
                        UsageReceipt.source_namespace == values["source_namespace"],
                    )
                )
            )
            .unique()
            .scalar_one_or_none()
        )
        if standing is None:  # pragma: no cover — the arbiter said duplicate
            replayed += 1
            continue
        if not row.final and bool(standing.final):
            # A late partial after the final state: the final stands — a
            # REPLAY exactly as the in-memory contract classifies it
            # (never a downgrade, never a conflict: the artifact's own
            # final already superseded this stale snapshot).
            replayed += 1
            continue
        if _standing_matches(standing, row):
            replayed += 1
            continue
        kind = _CONFLICTING_FINAL if row.final and bool(standing.final) else _CONFLICTING_CONTENT
        await _record_ingestion_event(
            session,
            UsageIngestionConflict,
            pg_insert,
            kind=kind,
            run_id=values["run_id"],
            attempt_id=values["attempt_id"],
            receipt_id=values["receipt_id"],
            source_namespace=values["source_namespace"],
            digest=_delivered_digest(row),
            detail={
                "standing": {
                    "final": bool(standing.final),
                    "cost_usd": standing.cost_usd,
                    "input_tokens": standing.input_tokens,
                    "output_tokens": standing.output_tokens,
                    "completeness": standing.completeness,
                    "artifact_digest": standing.artifact_digest,
                },
                "delivered": row.to_json(),
                "note": (
                    "the same durable identity was delivered with different "
                    "content — the first row stands, the difference is "
                    "surfaced (usage.ingestion_conflict), never averaged"
                ),
            },
        )
        conflicts += 1
    for refusal in refusals:
        trusted = refusal.trusted_work_id
        await _record_ingestion_event(
            session,
            UsageIngestionConflict,
            pg_insert,
            kind=refusal.kind,
            run_id=(trusted if len(trusted) <= _WORK_ID_WIDTH else _hash_component(trusted)),
            attempt_id=(refusal.attempt_id or "")[:_ATTEMPT_ID_WIDTH],
            receipt_id=(
                refusal.receipt_id
                if len(refusal.receipt_id) <= _RECEIPT_ID_WIDTH
                else _hash_component(refusal.receipt_id)
            ),
            source_namespace=(refusal.source or "")[:_SOURCE_WIDTH],
            digest="sha256:"
            + hashlib.sha256(_canonical(refusal.to_json()).encode("utf-8")).hexdigest()[:48],
            detail={**refusal.to_json(), "event": "usage.attribution_refused"},
        )
        refusal_count += 1
    return PersistOutcome(
        created=created,
        replayed=replayed,
        reconciled=reconciled,
        conflicts=conflicts,
        refusals=refusal_count,
    )
