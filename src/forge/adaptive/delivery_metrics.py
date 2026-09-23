"""R28-22 — all-attempt delivery economics for ONE run, reconciled honestly.

The review's demand: "all-attempt spend and time to accepted task, with
model time, tools, CI queue, and human wait shown separately" — and the
review's honesty rules: a paused-and-retried task retains BOTH attempts'
spend, a missing receipt leaves cost INCOMPLETE rather than zero, and
accepted-work metrics never treat driver-completed as human-accepted.

Two layers:

- :func:`reconcile_delivery` — the PURE reconciliation over the run's
  recorded attempt history. Inputs are the per-attempt facts the lane's
  published meta already carries (the usage receipt with
  ``total_cost_usd``, the ``episode`` timing breakdown whose ``turn_s``
  is the model time, a ``tool_call_count`` counter), the CI dispatch
  observations (``dispatched_at`` → ``started_at``: the queue window) and
  the gate windows (``approved_at`` → the next command after it: the
  human-wait window). Every metric is known only when EVERY attempt (or
  observation, or gate) contributes a known value; anything missing
  degrades the metric to ``None`` — unknown, never zero — with a note
  naming the gap. NEXT-23 keys every metric to the attempt IDENTITY, not
  just the run: the reconciliation emits a ``per_attempt`` receipt row
  beside the totals, and duplicate records sharing one attempt id are
  identity-matched — an agreeing replay (a cumulative receipt re-published
  by its own source) collapses to that source's LATEST claim, while two
  DIFFERENT evidence sources claiming different values for the same
  attempt surface as ``conflicting_receipts`` (``{attempt_id, source_a,
  source_b}`` with the disagreeing fields) and degrade the value to
  unknown — never silently averaged, never quietly latest-winned across
  sources.
- :func:`delivery_metrics_for_run` — the durable loader: reads the
  ``FlowRun`` evidence (the additive ``attempts`` list, the harness
  fragment, the acceptance record), corroborates the attempt count with
  the publication intents (each distinct idempotency scope is one
  attempt's publication leg), and derives the gate windows from
  ``gate_approvals.consumed_at`` to the next control command in
  ``control_commands`` (skip-clean when the deployment carries no
  mailbox table).

:method:`DeliveryMetrics.as_status_field` is the additive ``/status``
fragment — just the metrics dict; timelines are a sibling's surface.
``accepted`` is the run's recorded MERGED acceptance (a driver's
``completed`` exit is never acceptance; the bot never merges).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from forge.adaptive.mailbox_db import ControlCommandRow
from forge.durable.models import FlowRun, GateApproval, PublicationIntent
from forge.runs.metrics import acceptance_state

__all__ = [
    "ATTEMPT_EVIDENCE_KEY",
    "AttemptReceipt",
    "ConflictingReceipt",
    "DeliveryMetrics",
    "delivery_metrics_for_run",
    "reconcile_delivery",
]

#: Where the run's per-attempt facts ride the evidence blob (additive:
#: each item is one attempt's published meta fragment — ``attempt_id``,
#: ``usage``, ``episode``, ``tool_call_count``, optionally a ``ci``
#: sub-dict with ``dispatched_at``/``started_at``, and optionally a
#: ``source`` label naming the evidence source the fragment arrived
#: through — NEXT-23's identity-matching key beside the attempt id).
ATTEMPT_EVIDENCE_KEY = "attempts"

#: The source label an unlabelled attempt record carries: the run's own
#: evidence ``attempts`` list. Records from the same source replay with
#: latest-wins semantics (a cumulative receipt re-published); records
#: from DIFFERENT sources must agree or they conflict.
DEFAULT_RECEIPT_SOURCE = "attempts"

#: The receipt fields identity-matching compares, in canonical order.
RECEIPT_FIELDS = ("spend_usd", "model_time_s", "tool_call_count")


def _as_datetime(value: Any) -> datetime | None:
    """A datetime, or an ISO-8601 string (``Z`` tolerated), or None.

    Unparseable garbage is None — an unjudgeable timestamp degrades its
    metric to unknown, it never raises and never becomes epoch zero.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _as_nonnegative_number(value: Any) -> float | None:
    """A real non-negative number as float (bools are not numbers here).

    None for everything else — an unjudgeable counter degrades its
    metric to unknown, it never becomes zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _attempt_label(attempt_id: Any, index: int) -> str:
    return str(attempt_id) if attempt_id else f"#{index}"


def _source_of(record: Mapping[str, Any]) -> str:
    """The record's evidence-source label (the attempts list by default)."""
    return str(record.get("source") or "").strip() or DEFAULT_RECEIPT_SOURCE


def _claimed_spend(record: Mapping[str, Any]) -> float | None:
    """The record's EXPLICIT cost claim — ``None`` claims nothing."""
    usage = record.get("usage")
    if isinstance(usage, Mapping):
        return _as_nonnegative_number(usage.get("total_cost_usd"))
    return None


def _claimed_turn(record: Mapping[str, Any]) -> float | None:
    """The record's explicit model-turn claim — ``None`` claims nothing."""
    episode = record.get("episode")
    if isinstance(episode, Mapping):
        return _as_nonnegative_number(episode.get("turn_s"))
    return None


def _claimed_tools(record: Mapping[str, Any]) -> float | None:
    """The record's explicit tool-counter claim — ``None`` claims nothing."""
    return _as_nonnegative_number(record.get("tool_call_count"))


#: One merged attempt: the identity-matched fold of every record sharing
#: one attempt id (NEXT-23). ``latest_by_source`` keeps each source's own
#: LATEST claim per receipt field (the cumulative-replay collapse);
#: cross-source comparison happens over those, so a superseded replay
#: never fabricates a conflict against a corroborating second source.
@dataclass
class _MergedAttempt:
    attempt_id: str
    sources: list[str]
    latest_by_source: dict[str, dict[str, float]]
    usage_seen: bool
    episode_seen: bool


def _merge_attempts(
    attempts: Sequence[Mapping[str, Any]],
) -> tuple[list[_MergedAttempt], dict[str, list[tuple[str, str, str]]]]:
    """Group records by attempt IDENTITY and fold each group (NEXT-23).

    Records sharing one NON-EMPTY ``attempt_id`` are ONE attempt. Within
    a group, each distinct ``source`` label keeps its own LATEST claim
    per receipt field (a source's cumulative receipt replaying with
    fresher counters replaces its own earlier claim — never added, never
    kept stale). The returned conflict map names, per attempt label,
    every ``(field, source_a, source_b)`` pair whose LATEST claims
    disagree: two evidence sources claiming different values for the
    same attempt is a CONFLICT, not an average and not a silent pick.
    Records with no attempt id are each their own attempt (an anonymous
    record cannot be identity-matched, so it can neither replay nor
    conflict — it just is one attempt).
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    anonymous: list[Mapping[str, Any]] = []
    for record in attempts:
        attempt_id = str(record.get("attempt_id") or "")
        if not attempt_id:
            anonymous.append(record)
        else:
            groups.setdefault(attempt_id, []).append(record)

    def _fold(records: list[Mapping[str, Any]], attempt_id: str) -> _MergedAttempt:
        sources: list[str] = []
        latest: dict[str, dict[str, float]] = {}
        usage_seen = False
        episode_seen = False
        for record in records:
            source = _source_of(record)
            if source not in latest:
                sources.append(source)
                latest[source] = {}
            claims = latest[source]
            spend = _claimed_spend(record)
            if spend is not None:
                claims["spend_usd"] = spend
            turn = _claimed_turn(record)
            if turn is not None:
                claims["model_time_s"] = turn
            tools = _claimed_tools(record)
            if tools is not None:
                claims["tool_call_count"] = tools
            usage_seen = usage_seen or isinstance(record.get("usage"), Mapping)
            episode_seen = episode_seen or isinstance(record.get("episode"), Mapping)
        return _MergedAttempt(attempt_id, sources, latest, usage_seen, episode_seen)

    merged = [_fold(records, attempt_id) for attempt_id, records in groups.items()]
    merged.extend(_fold([record], "") for record in anonymous)

    conflicts: dict[str, list[tuple[str, str, str]]] = {}
    for attempt in merged:
        label = attempt.attempt_id
        for field in RECEIPT_FIELDS:
            distinct: list[tuple[str, float]] = [
                (source, attempt.latest_by_source[source][field])
                for source in attempt.sources
                if field in attempt.latest_by_source[source]
            ]
            values = {value for _source, value in distinct}
            if len(values) <= 1:
                continue  # unclaimed, or every source agrees
            (source_a, _value_a), (source_b, _value_b) = next(
                (left, right)
                for index, left in enumerate(distinct)
                for right in distinct[index + 1 :]
                if left[1] != right[1]
            )
            conflicts.setdefault(label, []).append((field, source_a, source_b))
    return merged, conflicts


def _seconds_between(start: Any, end: Any) -> float | None:
    """Clamped seconds from *start* to *end* (None when either is missing)."""
    began = _as_datetime(start)
    ended = _as_datetime(end)
    if began is None or ended is None:
        return None
    return max(0.0, (ended - began).total_seconds())


@dataclass(frozen=True)
class AttemptReceipt:
    """One attempt's reconciled receipt, keyed to its attempt id (NEXT-23).

    Every total in :class:`DeliveryMetrics` is the fold of these rows, so
    a metric can always be traced to the attempts that produced it. A
    ``None`` field is the per-attempt honesty rule: that fact is unknown
    for THIS attempt (no receipt, a token-only receipt, a timing gap, or
    a conflict between sources), never a zero. ``model_time_s`` of 0.0 on
    an attempt that never drove (no episode, no usage) is the honest
    zero, matching the total's rule.
    """

    attempt_id: str
    sources: tuple[str, ...] = ()
    spend_usd: float | None = None
    model_time_s: float | None = None
    tool_call_count: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "source": "+".join(self.sources) or DEFAULT_RECEIPT_SOURCE,
            "spend_usd": self.spend_usd,
            "model_time_s": self.model_time_s,
            "tool_call_count": self.tool_call_count,
        }


@dataclass(frozen=True)
class ConflictingReceipt:
    """Two evidence sources claiming different values for ONE attempt (NEXT-23).

    The review's rule: a receipt mismatch is REPORTED, never averaged and
    never silently resolved — ``source_a`` and ``source_b`` name the two
    evidence sources, ``fields`` the receipt fields their latest claims
    disagree on. The conflicting fields degrade to unknown (``None``) for
    that attempt, which degrades the affected total with a note: a
    disagreement means the true value is not known, and neither side of
    it is more authoritative for having arrived last.
    """

    attempt_id: str
    source_a: str
    source_b: str
    fields: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "source_a": self.source_a,
            "source_b": self.source_b,
            "fields": list(self.fields),
        }


@dataclass(frozen=True)
class DeliveryMetrics:
    """One run's reconciled all-attempt economics — unknown is ``None``.

    ``attempts_count`` and ``accepted`` are always known (zero attempts
    and not-accepted are real states). Every OTHER field is a
    reconciliation: ``None`` means "at least one input fact is missing,
    so the total would be a lie" — never a zero dressed up as measured.
    :attr:`notes` names every gap so an operator can tell "cheap" from
    "unmeasured". NEXT-23 keys the reconciliation to the attempt
    identity: :attr:`per_attempt` carries one receipt row per attempt
    beside the totals, and :attr:`conflicting_receipts` names every
    attempt two evidence sources disagreed on — the disagreement is
    data, not something the totals paper over.
    """

    run_id: str
    attempts_count: int
    accepted: bool
    total_spend_usd: float | None = None
    model_time_seconds: float | None = None
    tool_call_count: int | None = None
    ci_queue_seconds: float | None = None
    human_wait_seconds: float | None = None
    notes: tuple[str, ...] = ()
    per_attempt: tuple[AttemptReceipt, ...] = ()
    conflicting_receipts: tuple[ConflictingReceipt, ...] = ()

    def as_status_field(self) -> dict[str, Any]:
        """The additive ``/status`` fragment — just the metrics dict."""
        return {
            "attempts_count": self.attempts_count,
            "accepted": self.accepted,
            "total_spend_usd": self.total_spend_usd,
            "model_time_seconds": self.model_time_seconds,
            "tool_call_count": self.tool_call_count,
            "ci_queue_seconds": self.ci_queue_seconds,
            "human_wait_seconds": self.human_wait_seconds,
            "notes": list(self.notes),
            "per_attempt": [row.to_json() for row in self.per_attempt],
            "conflicting_receipts": [row.to_json() for row in self.conflicting_receipts],
        }


def reconcile_delivery(
    *,
    attempts: Sequence[Mapping[str, Any]] = (),
    ci_observations: Sequence[Mapping[str, Any]] = (),
    gate_waits: Sequence[Mapping[str, Any]] = (),
    accepted: bool = False,
    run_id: str = "",
) -> DeliveryMetrics:
    """Fold the recorded facts into one :class:`DeliveryMetrics`.

    The honesty rules, per field:

    - ``per_attempt`` (NEXT-23) — one :class:`AttemptReceipt` per
      identity-matched attempt; the totals below are its fold, so every
      metric traces to attempt ids. Records sharing an attempt id are
      ONE attempt: a source's own replay collapses to its LATEST claim
      (the cumulative-receipt rule), while two sources claiming
      different values surface as :attr:`conflicting_receipts` — the
      field degrades to unknown for that attempt, never averaged, never
      quietly picked.
    - ``total_spend_usd`` — the fold of every attempt receipt's
      ``spend_usd``. Known only when EVERY attempt carries a receipt WITH
      a cost figure; an attempt without usage, or with a token-only
      receipt (drivers with no cost API), or with conflicting receipts,
      leaves the TOTAL unknown — incomplete, never zeroed.
    - ``model_time_seconds`` — the fold of every attempt's
      ``model_time_s`` (the driven model turn). An attempt with an
      episode but no ``turn_s`` is a gap; an attempt with NEITHER
      episode nor usage never drove (the lane's honest "no episode to
      time") and contributes zero.
    - ``tool_call_count`` — the fold of every attempt's counter; known
      only when every attempt records one (a driver that does not count
      tools says so by its absence).
    - ``ci_queue_seconds`` — the sum of ``dispatched_at`` →
      ``started_at`` over every CI observation; an incomplete
      observation (one side missing) leaves the total unknown.
    - ``human_wait_seconds`` — the sum of ``approved_at`` → the next
      command after it over every gate window; a gate with no following
      command yet is a wait still open: unknown, not "zero so far".
    """
    notes: list[str] = []
    merged, conflict_map = _merge_attempts(attempts)
    if not merged:
        notes.append("no attempt history recorded — usage and time cannot be reconciled")

    conflicting_receipts: list[ConflictingReceipt] = []
    per_attempt: list[AttemptReceipt] = []
    spend: float | None = 0.0
    model_time: float | None = 0.0
    tool_calls: float | None = 0.0
    for index, attempt in enumerate(merged, start=1):
        label = _attempt_label(attempt.attempt_id, index)
        conflicts = conflict_map.get(label, [])
        conflicted_fields = {field for field, _a, _b in conflicts}

        # The per-attempt receipt, each field honest on its own.
        attempt_spend: float | None = None
        attempt_turn: float | None = None
        attempt_tools: float | None = None
        for source in attempt.sources:
            claims = attempt.latest_by_source[source]
            if "spend_usd" in claims and "spend_usd" not in conflicted_fields:
                attempt_spend = claims["spend_usd"]
            if "model_time_s" in claims and "model_time_s" not in conflicted_fields:
                attempt_turn = claims["model_time_s"]
            if "tool_call_count" in claims and "tool_call_count" not in conflicted_fields:
                attempt_tools = claims["tool_call_count"]

        # Spend: a receipt without a parsable cost figure is a gap, with
        # the note naming WHICH shape of gap (token-only vs none at all).
        if attempt_spend is None and "spend_usd" not in conflicted_fields:
            if attempt.usage_seen:
                notes.append(
                    f"attempt {label} records no total_cost_usd (tokens only or no"
                    " cost API) — spend unknown"
                )
            else:
                notes.append(f"attempt {label} carries no usage receipt — spend unknown")

        # Model time: a drove-but-untimed attempt is a gap; an attempt
        # with NEITHER episode nor usage never drove (the honest zero).
        if attempt_turn is None and "model_time_s" not in conflicted_fields:
            if attempt.episode_seen:
                notes.append(f"attempt {label} records an episode without turn time")
            elif attempt.usage_seen:
                notes.append(f"attempt {label} records usage but no episode — model time unknown")
            else:
                attempt_turn = 0.0  # never drove: no episode to time

        if attempt_tools is None and "tool_call_count" not in conflicted_fields:
            notes.append(f"attempt {label} records no tool_call_count — tool use unknown")

        # The conflict rows: named, never averaged, degrading the field.
        for field, source_a, source_b in conflicts:
            conflicting_receipts.append(
                ConflictingReceipt(
                    attempt_id=label,
                    source_a=source_a,
                    source_b=source_b,
                    fields=(field,),
                )
            )
            notes.append(
                f"attempt {label} has conflicting receipts for {field}"
                f" ({source_a} vs {source_b}) — the value is unknown, never averaged"
            )

        per_attempt.append(
            AttemptReceipt(
                attempt_id=label,
                sources=tuple(attempt.sources),
                spend_usd=attempt_spend,
                model_time_s=attempt_turn,
                tool_call_count=int(attempt_tools) if attempt_tools is not None else None,
            )
        )
        spend = None if (spend is None or attempt_spend is None) else spend + attempt_spend
        model_time = (
            None if (model_time is None or attempt_turn is None) else model_time + attempt_turn
        )
        tool_calls = (
            None if (tool_calls is None or attempt_tools is None) else tool_calls + attempt_tools
        )

    if not merged:
        # No attempt history at all: the summed metrics are unknown, not
        # the zero their empty accumulator happened to hold.
        spend = None
        model_time = None
        tool_calls = None

    queue_total = 0.0
    ci_known = bool(ci_observations)
    if not ci_observations:
        notes.append("no CI dispatch timestamps recorded — queue time unknown")
    else:
        for observation in ci_observations:
            window = _seconds_between(
                observation.get("dispatched_at"), observation.get("started_at")
            )
            if window is None:
                ci_known = False
                notes.append(
                    "a CI observation lacks dispatched_at or started_at — queue time unknown"
                )
                break
            queue_total += window
    ci_queue: float | None = queue_total if ci_known else None

    wait_total = 0.0
    wait_known = bool(gate_waits)
    if not gate_waits:
        notes.append("no gate approvals recorded — human wait unknown")
    else:
        for gate in gate_waits:
            approved = _as_datetime(gate.get("approved_at"))
            if approved is None:
                wait_known = False
                notes.append("a gate window carries no parseable approved_at — human wait unknown")
                break
            if gate.get("next_command_at") is None:
                wait_known = False
                notes.append(
                    f"gate approved at {approved.isoformat()} has no following command yet"
                    " — the wait is still open, not zero"
                )
                break
            window = _seconds_between(approved, gate.get("next_command_at"))
            if window is None:
                wait_known = False
                notes.append("a gate window's next command timestamp is unparseable")
                break
            wait_total += window
    human_wait: float | None = wait_total if wait_known else None

    return DeliveryMetrics(
        run_id=run_id,
        attempts_count=len(merged),
        accepted=accepted,
        total_spend_usd=round(spend, 6) if spend is not None else None,
        model_time_seconds=model_time,
        tool_call_count=int(tool_calls) if tool_calls is not None else None,
        ci_queue_seconds=ci_queue,
        human_wait_seconds=human_wait,
        notes=tuple(notes),
        per_attempt=tuple(per_attempt),
        conflicting_receipts=tuple(conflicting_receipts),
    )


# ----------------------------------------------------------------------
# The durable loader
# ----------------------------------------------------------------------


async def _has_control_commands(session: AsyncSession) -> bool:
    """Whether the mailbox table exists (skip-clean probe, never a crash)."""
    connection: AsyncConnection = await session.connection()
    return bool(
        await connection.run_sync(lambda sync: sa_inspect(sync).has_table("control_commands"))
    )


def _ci_observations_from_evidence(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The CI dispatch timestamps the run's evidence carries.

    Per-attempt ``ci`` fragments (``{dispatched_at, started_at}``) plus
    the harness fragment when it carries either timestamp — the loader
    never invents an observation; it surfaces what was journaled.
    """
    observations: list[dict[str, Any]] = []
    attempts = evidence.get(ATTEMPT_EVIDENCE_KEY)
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, Mapping) and isinstance(attempt.get("ci"), Mapping):
                observations.append(dict(attempt["ci"]))
    harness = evidence.get("harness")
    if isinstance(harness, Mapping) and ("dispatched_at" in harness or "started_at" in harness):
        observations.append(dict(harness))
    return observations


async def delivery_metrics_for_run(
    run_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> DeliveryMetrics:
    """Reconcile ONE run's all-attempt economics from the durable state.

    Reads the run's evidence (the ``attempts`` list, the harness
    fragment, the acceptance record), corroborates the attempt count
    with the publication intents when the evidence carries no attempt
    history, and derives the human-wait windows from the consumed gate
    approvals to the next control command (skip-clean without the
    mailbox table: those windows degrade to unknown, recorded in the
    notes). A run id with no row is a typed empty answer — count 0,
    everything else unknown — never a zeroed "free" run.
    """
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            return DeliveryMetrics(
                run_id=run_id,
                attempts_count=0,
                accepted=False,
                notes=(f"no flow run {run_id!r} — nothing to reconcile",),
            )
        evidence = run.evidence if isinstance(run.evidence, Mapping) else {}
        attempts = evidence.get(ATTEMPT_EVIDENCE_KEY)
        attempt_records: list[Mapping[str, Any]] = (
            [record for record in attempts if isinstance(record, Mapping)]
            if isinstance(attempts, list)
            else []
        )

        gates: list[dict[str, Any]] = []
        approvals = (
            (
                await session.execute(
                    select(GateApproval)
                    .where(
                        GateApproval.flow_run_id == run_id, GateApproval.consumed_at.is_not(None)
                    )
                    .order_by(GateApproval.consumed_at)
                )
            )
            .scalars()
            .all()
        )
        if approvals:
            has_commands = await _has_control_commands(session)
            commands: list[tuple[datetime, str]] = []
            if has_commands:
                rows = (
                    await session.execute(
                        select(ControlCommandRow.created_at, ControlCommandRow.id).where(
                            or_(
                                ControlCommandRow.run_id == run_id,
                                ControlCommandRow.work_id == run_id,
                            )
                        )
                    )
                ).all()
                commands = sorted(
                    (created_at, str(command_id))
                    for created_at, command_id in rows
                    if created_at is not None
                )
            for approval in approvals:
                consumed = approval.consumed_at
                if consumed is None:
                    continue  # the query filtered NULLs; this belt is for the type checker
                next_command = next(
                    (created_at for created_at, _command_id in commands if created_at > consumed),
                    None,
                )
                gates.append(
                    {
                        "approved_at": consumed,
                        "next_command_at": next_command,
                    }
                )

        intents = (
            (
                await session.execute(
                    select(PublicationIntent.idempotency_scope).where(
                        PublicationIntent.run_id == run_id
                    )
                )
            )
            .scalars()
            .all()
        )

    metrics = reconcile_delivery(
        attempts=attempt_records,
        ci_observations=_ci_observations_from_evidence(evidence),
        gate_waits=gates,
        accepted=acceptance_state(evidence) == "merged",
        run_id=run_id,
    )

    if not attempt_records:
        # No per-attempt facts on the evidence: corroborate the COUNT from
        # the durable state (commit cycles, recorded candidates, distinct
        # publication scopes) — the count is knowable even when the spend
        # is not, and a wrong count is the worse lie.
        candidate_count = len(run.candidate_shas or [])
        scope_count = len(set(intents))
        corroborated = max(int(run.commit_cycle or 1), candidate_count, scope_count)
        metrics = replace(
            metrics,
            attempts_count=corroborated,
            notes=metrics.notes
            + (
                f"attempt count corroborated from durable state (commit_cycle="
                f"{int(run.commit_cycle or 1)}, candidates={candidate_count},"
                f" publication scopes={scope_count}) — per-attempt usage and"
                " episode facts are not recorded on the evidence",
            ),
        )
    return metrics
