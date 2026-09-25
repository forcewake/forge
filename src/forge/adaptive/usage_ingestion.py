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
- **Spend caps consult the ingested totals** — :func:`spend_cap_check`
  is the check the existing budget/caps gate consults BEFORE the next
  chargeable action (:meth:`forge.adaptive.research_cohort_live.
  SpendLedger.allows_call` is the projection seam; ``budget_block_reason``
  the dispatch gate): reserved spend = every known cost plus, for unknown
  intervals, their KNOWN LOWER BOUND (conservative reservation — the cap
  may over-stop, never under-stop).
- **Duplicate call identities never false-join** — the same model-call id
  under two attempts is ingested under BOTH natural keys but the ledger
  join (:func:`forge.adaptive.delivery_measurement.
  ledger_records_from_ingested_usage`) passes each to its own attempt and
  the linker's duplicate/cross-join guards surface it — one receipt id is
  never summed twice.

Everything here is pure (mapping-shaped inputs, no database, no clocks)
except :func:`persist_ingested_rows`, the durable write through the same
``INSERT ... ON CONFLICT DO NOTHING`` seam
:func:`forge.durable.budgets.ingest_usage_receipt` established (R23).
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
    "COST_BASIS_BILLING",
    "COST_BASIS_ESTIMATED",
    "COST_BASIS_PROVIDER_REPORTED",
    "COST_BASES",
    "INGESTION_SCHEMA",
    "IngestedUsageRow",
    "IngestionConflict",
    "IngestionResult",
    "NormalizedCounters",
    "SOURCE_LANE_ARTIFACT",
    "SOURCE_PLANNER_LEDGER",
    "SOURCE_SDK_RECEIPT",
    "UsageIngestStore",
    "attribution_segment",
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
class IngestionResult:
    """What one ingestion pass did — created, replayed, reconciled."""

    created: tuple[IngestedUsageRow, ...] = ()
    replayed: tuple[IngestedUsageRow, ...] = ()
    reconciled: tuple[IngestedUsageRow, ...] = ()
    conflicts: tuple[IngestionConflict, ...] = ()

    @property
    def wrote_something_new(self) -> bool:
        return bool(self.created) or bool(self.reconciled)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": INGESTION_SCHEMA,
            "created": [row.to_json() for row in self.created],
            "replayed": [row.to_json() for row in self.replayed],
            "reconciled": [row.to_json() for row in self.reconciled],
            "conflicts": [row.to_json() for row in self.conflicts],
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
    work_id: str,
    attempt_id: str,
    source: str,
    rate_card_id: str,
    route_version: str,
    final: bool | None,
    default_provider: str,
    default_model: str,
    cost_basis: str,
    cost_lower_bound_usd: float,
) -> list[IngestedUsageRow]:
    """Parse ONE source artifact into ingested rows (identity-keyed)."""

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
            work_id=work_id,
            attempt_id=row_attempt,
            receipt_id=receipt_id,
            source=row_source,
            route=ProviderRoute(provider=provider, model=model),
            counters=counters,
            cost_usd=cost,
            cost_basis=(_text(payload.get("cost_basis")) or cost_basis) if cost is not None else "",
            cost_lower_bound_usd=cost_lower_bound_usd if cost is None else cost,
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

        A declared id (``receipt_id`` / ``call_id``) always wins. The lane's
        per-attempt aggregate artifact without one falls back to the
        STABLE artifact identity ``(work, attempt, source)`` — never a
        content digest, because the streaming contract must reconcile a
        partial to its final state UNDER ONE identity (a content-derived
        id would make every state a new receipt and reconciliation
        impossible). Work-level (planner) rows fall back to a content
        digest — they are final facts, never reconciled.
        """
        declared = _text(payload.get("receipt_id") or payload.get("call_id") or payload.get("id"))
        if declared:
            return declared
        if attempt_id or _text(payload.get("attempt_id")):
            return f"lane:{work_id}:{_text(payload.get('attempt_id')) or attempt_id}:{source}"
        if index is not None:
            return f"{_text(artifact.get('receipt_id')) or source}:call:{index}"
        return f"receipt:{_digest(_canonical(dict(artifact)))}"

    if calls is not None:
        rows: list[IngestedUsageRow] = []
        for index, call in enumerate(calls):
            if not isinstance(call, Mapping):
                continue
            rows.append(
                _row(
                    call,
                    receipt_id=_stable_receipt_id(call, index=index),
                    row_attempt=_text(call.get("attempt_id")) or attempt_id,
                    # The TRUSTED caller attribution wins; the payload's own
                    # marker is the fallback, never the other way round.
                    row_source=source or _text(call.get("source")),
                )
            )
        return rows
    return [
        _row(
            artifact,
            receipt_id=_stable_receipt_id(artifact),
            row_attempt=_text(artifact.get("attempt_id")) or attempt_id,
            row_source=source or _text(artifact.get("source")),
        )
    ]


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
    from; fields inside the artifact override only their own row's
    identity, never the work. Returns what the pass did; the store's
    natural-key upsert makes a re-ingest or a webhook replay a no-op.
    """
    rows = _artifact_rows(
        artifact,
        work_id=_text(artifact.get("work_id") or artifact.get("run_id")) or work_id,
        attempt_id=attempt_id,
        source=source,
        rate_card_id=rate_card_id,
        route_version=route_version,
        final=final,
        default_provider=_text(artifact.get("provider") or artifact.get("driver")),
        default_model=_text(artifact.get("model")),
        cost_basis=cost_basis,
        cost_lower_bound_usd=cost_lower_bound_usd,
    )
    return store.ingest(rows)


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
# The spend-cap check — conservative reservation over ingested totals
# ----------------------------------------------------------------------


def spend_cap_check(
    rows: Sequence[IngestedUsageRow],
    *,
    cap_usd: float,
    projection_usd: float = 0.0,
) -> dict[str, Any]:
    """Whether the NEXT chargeable action fits the cap over ingested spend.

    The check the existing budget/caps gate consults before the next
    chargeable action (the dispatch gate / the SpendLedger projection
    seam): reserved spend is every KNOWN cost plus, for unknown-cost
    intervals, their known LOWER BOUND (the documented conservative
    reservation — an unknown interval is never reserved as zero unless
    nothing at all is known about it). The cap may over-stop relative to
    an eventual bill that comes in under a lower bound; it may never
    under-stop relative to a known cost.
    """
    known = [row.cost_usd for row in rows if row.cost_usd is not None]
    unknown_rows = [row for row in rows if row.cost_usd is None]
    unknown_reserved = math.fsum(row.cost_lower_bound_usd for row in unknown_rows)
    reserved = math.fsum(known) + unknown_reserved
    allowed = reserved + projection_usd <= cap_usd
    notes = [
        "reserved = known costs + lower bounds of unknown intervals"
        " (conservative reservation: the cap may over-stop, never under-stop)"
    ]
    if unknown_rows:
        notes.append(
            f"{len(unknown_rows)} unknown-cost interval(s) reserved at their"
            f" lower bounds ({round(unknown_reserved, 6)} usd) — never zero"
        )
    return {
        "schema": INGESTION_SCHEMA,
        "allowed": allowed,
        "cap_usd": cap_usd,
        "reserved_usd": reserved,
        "known_cost_usd": math.fsum(known),
        "unknown_intervals": len(unknown_rows),
        "unknown_reserved_lower_bound_usd": unknown_reserved,
        "projection_usd": projection_usd,
        "headroom_usd": cap_usd - reserved,
        "notes": notes,
    }


# ----------------------------------------------------------------------
# The durable write — the R23 ON CONFLICT seam, one spelling
# ----------------------------------------------------------------------


async def persist_ingested_rows(
    session: Any,
    rows: Sequence[IngestedUsageRow],
) -> tuple[int, int]:
    """Write ingested rows into the durable ``usage_receipts`` table.

    The store seam :func:`forge.durable.budgets.ingest_usage_receipt`
    established (R23), opened to the ingested row shape: every row lands
    as one ``INSERT ... ON CONFLICT DO NOTHING`` against the UNIQUE
    ``(run_id, attempt_id, receipt_id)`` index, so a replayed webhook or
    a re-read artifact writes NOTHING new at the database no matter how
    often it is re-delivered. The source attribution (the natural key's
    fourth component) rides the ``source`` column; unknown counters stay
    NULL — never zero. Returns ``(created, replayed)``.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from forge.durable.models import UsageReceipt

    created = 0
    replayed = 0
    for row in rows:
        inserted = await session.execute(
            pg_insert(UsageReceipt)
            .values(
                run_id=row.work_id,
                attempt_id=row.attempt_id[:100],
                receipt_id=row.receipt_id[:64],
                driver=row.route.provider or None,
                model=row.route.model or None,
                input_tokens=row.counters.input_tokens,
                cached_input_tokens=row.counters.cached_input_tokens,
                cache_write_tokens=row.counters.cache_write_tokens,
                output_tokens=row.counters.output_tokens,
                completeness=row.completeness,
                source=row.source[:100] or None,
                raw={
                    "cost_usd": row.cost_usd,
                    "cost_basis": row.cost_basis,
                    "rate_card_id": row.rate_card_id,
                    "route_version": row.route_version,
                    "segment": row.segment,
                    "final": row.final,
                    "artifact_digest": row.artifact_digest,
                },
            )
            # ON CONFLICT DO NOTHING is portable across Postgres and
            # SQLite (tests) — the same arbiter as the R23 front door.
            .on_conflict_do_nothing(
                index_elements=[
                    UsageReceipt.run_id,
                    UsageReceipt.attempt_id,
                    UsageReceipt.receipt_id,
                ]
            )
        )
        # rowcount is the INSERT's inserted-row count; SQLAlchemy 2.0
        # stubs only type it on CursorResult, hence the runtime attr.
        if inserted.rowcount == 1:  # type: ignore[attr-defined]
            created += 1
        else:
            replayed += 1
    return created, replayed
