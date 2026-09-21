"""Cohort aggregation — cost/time per ACCEPTED unit, honestly (A17).

The R24 lesson applied to harness comparison: a faster harness that drafts
more PRs while leaking more human fixes is NOT a better harness, and READY
counts plus raw tokens/s hide exactly that. This module turns one ledger
(:mod:`evaluation.cohort.ledger`) into the report artifact with the honesty
rules pinned by the review:

- **The denominator is accepted units** — units with an explicit human
  ``accepted`` verdict against the predeclared checks. READY counts,
  self-reported agent success and drafted-PR counts are never denominators.
- **All-attempt spend stays in** — failed/blocked/cancelled/superseded
  attempts (and their receipts) are summed into cohort totals forever; the
  per-accepted-unit view prices a unit by ALL of its attempts, repairs
  included.
- **Token classes never merge** — input, cached-input, cache-write and
  output are reported as four separate sums with per-class unknown counts;
  unknown stays unknown (never zero, ADR-0013/R23).
- **Decode throughput is output-only** — the one rate this module builds is
  known ``output_tokens`` over known ``llm_calls.duration_ms``. Logical
  traffic (input + cache) is never divided by decode time, and no mixed
  "total tokens/s" is ever produced.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from evaluation.cohort.ledger import LEDGER_SCHEMA
from evaluation.cohort.tasks import CONTRACT_VERSION

__all__ = [
    "REPORT_SCHEMA",
    "TOKEN_CLASSES",
    "aggregate_attempt",
    "aggregate_llm_calls",
    "aggregate_receipts",
    "cohort_report",
    "count_unknown",
    "merge_llm",
    "merge_usage",
    "percentile",
    "phase_seconds",
    "receipt_cost_usd",
    "sum_known",
    "unit_rollup",
]

REPORT_SCHEMA = "forge.cohort.report/1"

#: The four receipt token classes, kept separate everywhere (R23 shapes).
TOKEN_CLASSES: tuple[str, ...] = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
)

#: Verdict order used for the report's per-verdict counts.
VERDICT_ORDER: tuple[str, ...] = ("pending", "accepted", "rejected", "superseded", "cancelled")

#: Terminal statuses that keep an attempt in the rejected denominator
#: (mirrors ``forge.runs.metrics.TERMINAL_FAILURE_STATUSES``).
_FAILURE_STATUSES: frozenset[str] = frozenset({"failed", "blocked", "cancelled"})


# ---------------------------------------------------------------------------
# Unknown-honest numeric helpers
# ---------------------------------------------------------------------------


def _num(raw: Any) -> int | None:
    """The int counter behind a raw receipt field, or None (unknown ≠ zero)."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _price(raw: Any) -> float | None:
    """A pricebook entry as float, or None when absent/malformed."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


def sum_known(values: Iterable[int | None]) -> int | None:
    """Sum of the KNOWN members; None when every member is unknown."""
    total: int | None = None
    for value in values:
        if value is not None:
            total = (total or 0) + value
    return total


def count_unknown(values: Iterable[int | None]) -> int:
    """How many members were unknown (the honest-unknown headcount)."""
    return sum(1 for value in values if value is None)


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile over known values only; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = min(len(ordered), max(1, math.ceil(pct / 100 * len(ordered))))
    return ordered[rank - 1]


def _iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _phase(later: Any, earlier: Any) -> float | None:
    """(later - earlier) seconds, clamped at zero; None unless both known."""
    end, start = _iso(later), _iso(earlier)
    if end is None or start is None:
        return None
    return max((end - start).total_seconds(), 0.0)


def _mapping(raw: Any) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def _rows(raw: Any) -> list[Mapping[str, Any]]:
    return [row for row in raw if isinstance(row, Mapping)] if isinstance(raw, list) else []


def phase_seconds(timing: Mapping[str, Any]) -> dict[str, float]:
    """Wall-clock phase decomposition from the runner's stamps.

    A phase is PRESENT only when both of its stamps are known — a zero would
    be a lie about an unobserved interval (the R24 ``human_wait`` rule).
    """
    phases: dict[str, float] = {}
    for name, later, earlier in (
        ("plan_wait_s", "plan_seen_at", "started_at"),
        ("gate_wait_s", "go_posted_at", "plan_seen_at"),
        ("execution_wait_s", "candidate_seen_at", "go_posted_at"),
        ("ci_wait_s", "ci_concluded_at", "candidate_seen_at"),
    ):
        seconds = _phase(timing.get(later), timing.get(earlier))
        if seconds is not None:
            phases[name] = seconds
    return phases


# ---------------------------------------------------------------------------
# Usage / latency aggregation (per attempt, per unit, per cohort — one shape)
# ---------------------------------------------------------------------------


def aggregate_receipts(receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Receipt rows → the four token-class sums plus per-class unknown counts.

    Cache counters stay OUT of ``input_tokens`` — the classes are reported
    disjointly exactly as the R23 ledger stores them; no total is folded
    here, because Anthropic-shaped and OpenAI-shaped totals differ.
    """
    class_sums: dict[str, int | None] = {}
    class_unknowns: dict[str, int] = {}
    for token_class in TOKEN_CLASSES:
        values = [_num(receipt.get(token_class)) for receipt in receipts]
        class_sums[token_class] = sum_known(values)
        class_unknowns[token_class] = count_unknown(values)
    return {
        "receipt_count": len(receipts),
        "token_classes": class_sums,
        "unknown_counts": class_unknowns,
    }


def aggregate_llm_calls(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``llm_calls`` rows → active seconds and the TTFT/latency view.

    ``llm_active_s`` sums ``duration_ms`` (failed calls included when they
    carry a duration). TTFT uses only rows that REPORT ``first_token_ms``;
    rows without one count into ``ttft.unknown_calls`` — never as zero.
    """
    durations = [_num(call.get("duration_ms")) for call in calls]
    known_duration_total = sum_known(durations)
    # B09: ALL-unknown durations are UNKNOWN activity (None), never 0.0;
    # a known-and-unknown mix keeps the known sum (a lower bound the
    # unknown_counts already flag).
    active_s = None if known_duration_total is None else known_duration_total / 1000.0
    first_tokens = [_num(call.get("first_token_ms")) for call in calls]
    known_ttft = [float(value) for value in first_tokens if value is not None]
    failed = sum(1 for call in calls if str(call.get("status") or "ok") != "ok")
    ttft: dict[str, Any] = {
        "p50_ms": percentile(known_ttft, 50),
        "p95_ms": percentile(known_ttft, 95),
        "known_calls": len(known_ttft),
        "unknown_calls": count_unknown(first_tokens),
    }
    return {
        "call_count": len(calls),
        "failed_calls": failed,
        "llm_active_s": active_s,
        "ttft": ttft,
    }


def merge_usage(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Combine already-aggregated usage blocks (attempt → unit → cohort)."""
    class_sums: dict[str, int | None] = {}
    class_unknowns: dict[str, int] = {}
    for token_class in TOKEN_CLASSES:
        class_sums[token_class] = sum_known(
            _num(_mapping(part.get("token_classes")).get(token_class)) for part in parts
        )
        class_unknowns[token_class] = sum(
            int(_mapping(part.get("unknown_counts")).get(token_class) or 0) for part in parts
        )
    return {
        "receipt_count": sum(int(part.get("receipt_count") or 0) for part in parts),
        "token_classes": class_sums,
        "unknown_counts": class_unknowns,
    }


def merge_llm(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Combine aggregated llm blocks (counts and seconds merge exactly; the
    percentile fields do not — re-derive them upstream from raw rows when a
    cohort-wide TTFT percentile is needed, as :func:`cohort_report` does)."""
    known = sum(int(_mapping(part.get("ttft")).get("known_calls") or 0) for part in parts)
    unknown = sum(int(_mapping(part.get("ttft")).get("unknown_calls") or 0) for part in parts)
    # B09: ALL-unknown propagates as None (never 0); any known part keeps
    # the known sum.
    active = [part.get("llm_active_s") for part in parts]
    known_active = [float(v) for v in active if v is not None]
    merged_active = sum(known_active) if known_active else None
    return {
        "call_count": sum(int(part.get("call_count") or 0) for part in parts),
        "failed_calls": sum(int(part.get("failed_calls") or 0) for part in parts),
        "llm_active_s": merged_active,
        "ttft": {"p50_ms": None, "p95_ms": None, "known_calls": known, "unknown_calls": unknown},
    }


def _ttft_values(calls: Sequence[Mapping[str, Any]]) -> list[float]:
    return [
        float(value)
        for value in (_num(call.get("first_token_ms")) for call in calls)
        if value is not None
    ]


# ---------------------------------------------------------------------------
# Per-attempt / per-unit rollups
# ---------------------------------------------------------------------------

_TIMING_KEYS: tuple[str, ...] = (
    "started_at",
    "plan_seen_at",
    "go_posted_at",
    "candidate_seen_at",
    "ci_concluded_at",
    "terminal_seen_at",
)


def aggregate_attempt(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """One attempt → its usage, latency, phases, repairs and linkage."""
    commit_cycle = _num(attempt.get("commit_cycle"))
    return {
        "attempt_no": _num(attempt.get("attempt_no")),
        "run_id": str(attempt.get("run_id") or ""),
        "kind": str(attempt.get("kind") or "initial"),
        "terminal_status": attempt.get("terminal_status"),
        "export_attached": bool(attempt.get("export_attached")),
        "rejected": attempt.get("terminal_status") in _FAILURE_STATUSES,
        "rework_of": attempt.get("rework_of"),
        "superseded_by": attempt.get("superseded_by"),
        "repairs": max(commit_cycle - 1, 0) if commit_cycle is not None else None,
        "usage": aggregate_receipts(_rows(attempt.get("receipts"))),
        "llm": aggregate_llm_calls(_rows(attempt.get("llm_calls"))),
        "phases": phase_seconds({key: attempt.get(key) for key in _TIMING_KEYS}),
    }


def _checks_verdict(checks: Sequence[Mapping[str, Any]]) -> bool | None:
    """True iff every recorded check passed; None when nothing was recorded."""
    if not checks:
        return None
    return all(bool(check.get("passed")) for check in checks)


def unit_rollup(unit: Mapping[str, Any]) -> dict[str, Any]:
    """One unit → verdict + ALL-attempt totals (the honest unit cost)."""
    raw_attempts = _rows(unit.get("attempts"))
    attempts = [
        aggregate_attempt({**attempt, "attempt_no": attempt_no})
        for attempt_no, attempt in enumerate(raw_attempts, start=1)
    ]
    acceptance = _mapping(unit.get("acceptance"))
    checks = _rows(acceptance.get("checks"))
    usage = merge_usage([attempt["usage"] for attempt in attempts])
    llm = merge_llm([attempt["llm"] for attempt in attempts])
    started = min(
        (stamp for stamp in (_iso(attempt.get("started_at")) for attempt in raw_attempts) if stamp),
        default=None,
    )
    wall_s = _phase(acceptance.get("decided_at"), started.isoformat() if started else None)
    return {
        "unit_id": str(unit.get("unit_id") or ""),
        "axis": str(unit.get("axis") or ""),
        "verdict": str(acceptance.get("verdict") or "pending"),
        "decided_by": str(acceptance.get("decided_by") or ""),
        "checks_passed": _checks_verdict(checks),
        "attempt_count": len(attempts),
        "rejected_attempts": sum(1 for attempt in attempts if attempt["rejected"]),
        "rework_count": sum(1 for attempt in attempts if attempt["rework_of"]),
        "repairs": sum_known([attempt["repairs"] for attempt in attempts]),
        "wall_s": wall_s,
        "usage": usage,
        "llm": llm,
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# Costing (optional pricebook) and the cohort report
# ---------------------------------------------------------------------------


def receipt_cost_usd(receipt: Mapping[str, Any], pricebook: Mapping[str, Any]) -> float | None:
    """USD cost of ONE receipt, or None when it cannot be priced exactly.

    A receipt prices only when its model is in the pricebook AND all four
    token classes are known — a partially-reported receipt is never averaged
    in with a fabricated remainder. Prices are USD per MILLION tokens per
    class (``input_tokens``, ``cached_input_tokens``, ``cache_write_tokens``,
    ``output_tokens``).
    """
    prices = pricebook.get(str(receipt.get("model") or ""))
    if not isinstance(prices, Mapping):
        return None
    amounts = [_num(receipt.get(token_class)) for token_class in TOKEN_CLASSES]
    if any(amount is None for amount in amounts):
        return None
    total = 0.0
    for token_class, amount in zip(TOKEN_CLASSES, amounts, strict=True):
        price = _price(prices.get(token_class))
        if price is None or amount is None:
            return None
        total += amount * price / 1_000_000
    return total


def _unit_cost_usd(unit: Mapping[str, Any], pricebook: Mapping[str, Any]) -> float | None:
    """All-attempt USD cost of a unit; None unless EVERY receipt prices.

    A unit with NO receipts at all has UNKNOWN spend — never a zero.
    """
    total = 0.0
    priced_any = False
    for attempt in _rows(unit.get("attempts")):
        receipts = _rows(attempt.get("receipts"))
        if not receipts:
            # B09: an attempt with NO receipts has UNKNOWN spend — its
            # absence must not read as free (the priced siblings are only
            # a lower bound, reported as such upstream).
            return None
        for receipt in receipts:
            cost = receipt_cost_usd(receipt, pricebook)
            if cost is None:
                return None
            total += cost
            priced_any = True
    return total if priced_any else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _units_of(ledger: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    units = ledger.get("units")
    return (
        [unit for unit in units.values() if isinstance(unit, Mapping)]
        if isinstance(units, Mapping)
        else []
    )


def _per_accepted_block(
    accepted: Sequence[Mapping[str, Any]],
    accepted_raw: Sequence[Mapping[str, Any]],
    denominator: int,
    pricebook: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Cost/time/tokens per ACCEPTED unit — None when nothing was accepted.

    *accepted* carries the rollups (usage/llm/wall already merged);
    *accepted_raw* the matching raw units — costing must read the raw
    receipts, not the rollup, so partially-reported spend stays unpriced
    instead of silently zeroing.
    """
    if denominator == 0:
        return None
    usage = merge_usage([_mapping(unit.get("usage")) for unit in accepted])
    llm = merge_llm([_mapping(unit.get("llm")) for unit in accepted])
    walls = [float(unit["wall_s"]) for unit in accepted if unit.get("wall_s") is not None]
    per_class: dict[str, float | None] = {}
    for token_class in TOKEN_CLASSES:
        known = usage["token_classes"][token_class]
        per_class[token_class] = known / denominator if known is not None else None
    repairs_known = sum_known([_num(unit.get("repairs")) for unit in accepted])
    block: dict[str, Any] = {
        "denominator": denominator,
        "token_classes_per_unit": per_class,
        # B09: unknown activity/repairs propagate as None — never zero.
        "llm_active_s_per_unit": (
            None if llm["llm_active_s"] is None else llm["llm_active_s"] / denominator
        ),
        "wall_s_mean": _mean(walls),
        "wall_known_units": len(walls),
        "repairs_per_unit": None if repairs_known is None else repairs_known / denominator,
    }
    if pricebook is not None:
        per_unit_costs = [_unit_cost_usd(unit, pricebook) for unit in accepted_raw]

        def _lower_bound(unit: Mapping[str, Any]) -> float | None:
            """B09: the priced receipts' sum even when other attempts are
            receipt-less — the KNOWN part of an unknown-exact unit."""
            total: float | None = None
            for attempt in _rows(unit.get("attempts")):
                for receipt in _rows(attempt.get("receipts")):
                    cost = receipt_cost_usd(receipt, pricebook)
                    if cost is None:
                        continue
                    total = (total or 0.0) + cost
            return total

        costs = [cost for cost in per_unit_costs if cost is not None]
        bounds = [
            bound
            for bound in (
                cost if cost is not None else _lower_bound(unit)
                for cost, unit in zip(per_unit_costs, accepted_raw, strict=True)
            )
            if bound is not None
        ]
        # B09: a unit with unpriced attempts has UNKNOWN exact cost — the
        # priced ones are a LOWER BOUND, never the mean (a receipt-less
        # attempt is missing spend, not free spend).
        block["cost_usd_mean"] = _mean(costs) if len(costs) == len(per_unit_costs) else None
        block["cost_known_units"] = len(costs)
        block["cost_lower_bound_usd_mean"] = _mean(bounds)
        block["cost_exact"] = len(costs) == len(per_unit_costs)
    return block


def effective_output_rate(
    receipts: Sequence[Mapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The EFFECTIVE output rate (C07) over IDENTITY-JOINED records.

    A rate divides output tokens by the duration of the SAME calls. The
    only honest join available in the ledger is by attempt identity: a
    receipt's ``attempt_id`` must match the llm-call's ``attempt`` (both
    sides carry it when exported from the same attempt). Records without
    the join key, or whose populations don't fully pair, produce ``None``
    with the reason — never a number over mismatched populations. Even a
    perfect join divides output by the FULL request duration (queue,
    prefill, decode) — hence the honest name *effective*, never decode
    speed.
    """

    def _key(record: Mapping[str, Any]) -> str:
        # D07: the join key is the FULL identity — run + attempt — when the
        # records carry it (the cohort-level flattening adds it); a bare
        # attempt label alone does not prove two records are the same call
        # population across different runs.
        run = str(record.get("run_id") or "")
        attempt = str(record.get("attempt_id") or record.get("attempt") or "")
        return f"{run}/{attempt}" if run else attempt

    joined_tokens = 0
    joined_ms: float | None = 0.0
    unpaired_receipts = 0
    unpaired_calls = 0
    calls_by_key: dict[str, list[Mapping[str, Any]]] = {}
    for call in calls:
        calls_by_key.setdefault(_key(call), []).append(call)
    for receipt in receipts:
        key = _key(receipt)
        bucket = calls_by_key.get(key)
        if not key or not bucket:
            unpaired_receipts += 1
            continue
        call = bucket.pop(0)
        duration = _num(call.get("duration_ms"))
        output = _num(receipt.get("output_tokens"))
        if duration is None:
            joined_ms = None  # duration unknown poisons the denominator
            continue
        if output is None:
            unpaired_receipts += 1
            continue
        joined_tokens += int(output)
        if joined_ms is not None:
            joined_ms += float(duration)
    unpaired_calls = sum(len(bucket) for bucket in calls_by_key.values())
    if unpaired_receipts or unpaired_calls or joined_ms is None or joined_ms <= 0:
        return {
            "effective_output_tokens_per_s": None,
            "unpaired_receipts": unpaired_receipts,
            "unpaired_calls": unpaired_calls,
            "joined": False,
        }
    return {
        "effective_output_tokens_per_s": joined_tokens / (joined_ms / 1000.0),
        "unpaired_receipts": 0,
        "unpaired_calls": 0,
        "joined": True,
    }


def _flatten_receipts(attempts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every attempt's receipts, each stamped with its run+attempt identity
    (D07: the join key survives the cohort-level flattening)."""
    flat: list[dict[str, Any]] = []
    for attempt in attempts:
        run_id = str(attempt.get("run_id") or "")
        attempt_id = str(attempt.get("attempt_id") or "")
        for receipt in _rows(attempt.get("receipts")):
            row = dict(receipt)
            row.setdefault("run_id", run_id)
            row.setdefault("attempt_id", attempt_id)
            flat.append(row)
    return flat


def _flatten_calls(attempts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every attempt's llm calls, identity-stamped like the receipts."""
    flat: list[dict[str, Any]] = []
    for attempt in attempts:
        run_id = str(attempt.get("run_id") or "")
        attempt_id = str(attempt.get("attempt_id") or "")
        for call in _rows(attempt.get("llm_calls")):
            row = dict(call)
            row.setdefault("run_id", run_id)
            row.setdefault("attempt", attempt_id)
            flat.append(row)
    return flat


def cohort_report(
    ledger: Mapping[str, Any],
    pricebook: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ledger → the cohort report artifact (``forge.cohort.report/1``).

    The report NEVER divides by READY counts or attempt counts when the
    question is unit economics: ``per_accepted_unit`` divides by
    ``accepted_units`` only, and collapses to ``None`` with an explanatory
    note when nothing was accepted. ``all_attempt_spend`` keeps the
    failed/cancelled/superseded spend visible beside it — waste is reported,
    never hidden inside a mean.
    """
    units = _units_of(ledger)
    rollups = [unit_rollup(unit) for unit in units]
    by_verdict = {
        verdict: sum(1 for rollup in rollups if rollup["verdict"] == verdict)
        for verdict in VERDICT_ORDER
    }
    raw_attempts = [attempt for unit in units for attempt in _rows(unit.get("attempts"))]

    usage = merge_usage(
        [aggregate_receipts(_rows(attempt.get("receipts"))) for attempt in raw_attempts]
    )
    llm = merge_llm(
        [aggregate_llm_calls(_rows(attempt.get("llm_calls"))) for attempt in raw_attempts]
    )
    all_ttft: list[float] = []
    for attempt in raw_attempts:
        all_ttft.extend(_ttft_values(_rows(attempt.get("llm_calls"))))
    llm["ttft"]["p50_ms"] = percentile(all_ttft, 50)
    llm["ttft"]["p95_ms"] = percentile(all_ttft, 95)

    accepted = [rollup for rollup in rollups if rollup["verdict"] == "accepted"]
    accepted_raw = [
        unit
        for unit in units
        if str(_mapping(unit.get("acceptance")).get("verdict") or "pending") == "accepted"
    ]
    denominator = len(accepted)
    unknown_usage_attempts = sum(
        1
        for attempt in raw_attempts
        if not attempt.get("export_attached")
        or (not _rows(attempt.get("receipts")) and not _rows(attempt.get("llm_calls")))
    )

    return {
        "schema": REPORT_SCHEMA,
        "contract_version": str(ledger.get("contract_version") or CONTRACT_VERSION),
        "ledger_schema": LEDGER_SCHEMA,
        "repo": str(ledger.get("repo") or ""),
        "profile": dict(_mapping(ledger.get("profile"))),
        "counts": {
            "units": len(rollups),
            "attempts": len(raw_attempts),
            **{f"{verdict}_units": count for verdict, count in by_verdict.items()},
            "rejected_attempts": sum(
                1 for attempt in raw_attempts if attempt.get("terminal_status") in _FAILURE_STATUSES
            ),
            "rework_count": sum(rollup["rework_count"] for rollup in rollups),
            "repairs_total": sum_known([rollup["repairs"] for rollup in rollups]),
        },
        "all_attempt_spend": {"usage": usage, "llm": llm},
        "per_accepted_unit": _per_accepted_block(accepted, accepted_raw, denominator, pricebook),
        "latency": {
            # C07/D07: the ONLY rate is identity-joined and honestly named,
            # and it is a RATIO OF SUMS over the WHOLE cohort population —
            # every attempt's receipts paired with every attempt's calls
            # under full run+attempt identity (the old form divided the
            # LAST attempt's numbers alone — order-dependent, and crashed
            # on an empty cohort).
            "effective_output_tokens_per_s": effective_output_rate(
                _flatten_receipts(raw_attempts), _flatten_calls(raw_attempts)
            )["effective_output_tokens_per_s"],
        },
        "honesty": {
            "denominator": (
                f"{denominator} accepted units (explicit operator verdict); "
                "READY counts and attempts are never the denominator"
            ),
            "attempts_retained": True,
            "token_classes_separate": True,
            "decode_rate_scope": (
                "known output_tokens / known llm_calls.duration_ms; logical traffic "
                "is never divided by decode throughput"
            ),
            "unknown_usage_attempts": unknown_usage_attempts,
            "zero_accepted_note": (
                "Nothing was accepted in this pass — per-accepted-unit economics are "
                "undefined and deliberately omitted, not zero."
                if denominator == 0
                else None
            ),
        },
        "units": rollups,
    }
