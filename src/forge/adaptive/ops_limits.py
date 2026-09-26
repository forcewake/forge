"""Q39-15 (#334) — measured operating limits, exposed on the operator surface.

The issue's basis: the R38-18 (#319) drills and the #330 delivery
economics measured the deployment's limits, but a supported CUSTOMER
profile needs those limits and the recovery responsibility EXPOSED
through the one authorized operator surface — so an operator answers
"is this queued / progressing / paused / unverified / blocked, and what
is the next SAFE action" without reading source. This module is the
pure read-model fold behind that exposure; the surface itself is the
EXISTING operator detail route (:mod:`forge.api_operator`), which
renders :func:`ops_limits_read_model` as the run's ``ops_limits``
section. No new route, no orphan module: the code lives where the
operator read-model ships.

Three families of content, each with its own honesty rule:

- **The four observability quantities** — ``ops.command_application_
  latency``, ``ops.native_occupancy``, ``ops.recovery_duration`` and
  ``ops.manual_intervention_minutes`` (:func:`ops_measures`) — are
  SEPARATE records with their OWN windows, populations and coverage.
  They are never summed, averaged or divided into one another:
  :func:`assert_measures_separate` refuses a document that grows a
  blended field, and every unknown window is COUNTED
  (:func:`_measure` never zero-fills a duration it could not match).
  The four names deliberately mirror the issue's observability list;
  the delivery-economics seven-time family (#330) stays the accepted-
  cost view — this module never re-derives it.

- **The admission accounting** (:func:`admission_accounting` over
  :func:`forge.adaptive.admission.admission_report`) — intake counters
  (requests refused / admitted-to-queue) and execution counters (slots
  held / draining / completed) are DIFFERENT populations rendered side
  by side, and the capped-admission VERDICT
  (:func:`capped_admission_verdict`) is DERIVED from execution
  occupancy alone. There is no parameter through which an intake count
  can become the verdict's basis — "intake count never claimed as
  execution slots" is the function's shape, not a promise, and
  :func:`assert_intake_never_slots` pins it for every caller.

- **The six-quantity read-model** (:func:`ops_limits_read_model`) —
  current attempt, native occupancy, exact checkpoint, unresolved
  effects, required checks and accounting coverage in ONE document,
  each quantity carrying its own evidence links and coverage word
  (``present`` / ``missing`` / ``unknown`` — a section the caller did
  not query renders unknown, never empty), plus the
  :func:`customer_state` mapping onto the customer outcome vocabulary
  (queued / progressing / paused / unverified / verified / blocked) and
  the #325 review-budget distinction (:func:`review_budget_distinction`):
  a FAILED REVIEW BUDGET is rendered as exactly that — never as lost
  code, never as failed independent checks, with the guarded recovery
  (the review-only continuation, the auditable top-up) named beside it.

History is never relabelled as current state: every "current" field
derives from the LATEST row of its section, every historical sample
inside a measure carries its own ``from`` / ``to`` moments, and the
document says so in ``history_separation``. Scope is enforced upstream
(the operator reader filters by canonical subject before any of this
runs) and pinned by the route's tests: an unauthorized repository is a
404, indistinguishable from unknown, on every path.

Everything here is a PURE fold over snapshot rows and occupancy rows
the reader already assembled — zero I/O, zero clocks (``as_of`` is
always a caller-supplied reading moment), zero state transitions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from forge.adaptive.operator_view import (
    DELIVERY_VIEW_SCHEMA,
    STALE_ACTION_REFUSAL,
    OperatorProjection,
    current_candidate,
    delivery_view,
)

__all__ = [
    "COMMAND_APPLICATION_LATENCY",
    "CUSTOMER_STATE_OF_OPERATOR_STATE",
    "HUMAN_ACTOR_ORIGINS",
    "MANUAL_INTERVENTION_MINUTES",
    "MEASURE_BLENDING_ERROR",
    "NATIVE_OCCUPANCY",
    "OPS_LIMITS_READ_MODEL_SCHEMA",
    "OPS_MEASURE_NAMES",
    "OPS_MEASURES_SCHEMA",
    "QUEUE_AGE",
    "RECOVERY_DURATION",
    "RECOVERY_ROUNDS",
    "STALE_ACTION_REFUSAL",
    "TIME_TO_SAFE_ACTION",
    "UNRESOLVED_EFFECT_AGE",
    "AdmissionAccountingError",
    "admission_accounting",
    "assert_intake_never_slots",
    "assert_measures_separate",
    "capped_admission_verdict",
    "command_application_latency",
    "customer_state",
    "delivery_round_block",
    "manual_intervention_minutes",
    "native_occupancy_measure",
    "ops_limits_read_model",
    "queue_age_measure",
    "ops_measures",
    "recovery_duration",
    "review_budget_distinction",
    "recovery_rounds_measure",
    "time_to_safe_action_measure",
    "unresolved_effect_age_measure",
]

#: The document stamps (versioned meaning, the house convention).
OPS_MEASURES_SCHEMA: Final = "forge.ops.measures/1"
OPS_LIMITS_READ_MODEL_SCHEMA: Final = "forge.ops.limits/1"

#: The issue's four observability quantities, each its own record.
COMMAND_APPLICATION_LATENCY: Final = "ops.command_application_latency"
NATIVE_OCCUPANCY: Final = "ops.native_occupancy"
RECOVERY_DURATION: Final = "ops.recovery_duration"
MANUAL_INTERVENTION_MINUTES: Final = "ops.manual_intervention_minutes"

#: R40-13 (#349) — the operator observability dimensions, carried by the
#: read-model as their own records beside the four above:
#: ``operator.recovery_rounds`` (the lineage's recorded follow-up
#: rounds), ``operator.unresolved_effect_age`` (the ages of the
#: unresolved external effects) and ``operator.time_to_safe_action``
#: (how long the current world has stood — a LOWER BOUND on how long a
#: safe action has been decidable). ``operator.stale_action_refusal`` is
#: the typed refusal code every action-versioned command carries
#: (:data:`forge.adaptive.operator_view.STALE_ACTION_REFUSAL`, re-exported)
#: — a refusal event, not a quantity, so it renders in the refusal
#: reasons and never as a measure sample.
RECOVERY_ROUNDS: Final = "operator.recovery_rounds"
UNRESOLVED_EFFECT_AGE: Final = "operator.unresolved_effect_age"
TIME_TO_SAFE_ACTION: Final = "operator.time_to_safe_action"
#: R40-15 (#351) — the sustained-queue-age alert's observable: the QUEUED
#: population's own ages (admitted-not-executing runs — the #334 separate
#: population, never a slots claim).
QUEUE_AGE: Final = "operator.queue_age"

OPS_MEASURE_NAMES: Final[tuple[str, ...]] = (
    COMMAND_APPLICATION_LATENCY,
    NATIVE_OCCUPANCY,
    RECOVERY_DURATION,
    MANUAL_INTERVENTION_MINUTES,
)

#: The typed refusal when a measures document grows a blended field.
MEASURE_BLENDING_ERROR: Final = "ops.measures.blended"

#: The actor origins whose commands are HUMAN intervention. The
#: reconciler's own commands are automation — they never count as
#: manual intervention minutes, and they are excluded visibly.
HUMAN_ACTOR_ORIGINS: Final[frozenset[str]] = frozenset(
    {"server_authenticated_human", "operator_token"}
)

#: The customer-outcome vocabulary (the issue's six states) mapped from
#: the projection's closed :data:`~forge.adaptive.operator_view.
#: OPERATOR_STATES` vocabulary. The health-flavored projection states
#: (``wedged`` / ``stale`` / ``dead`` — a projection carries exactly one
#: state, and these ARE states, not just overlays) re-classify to
#: ``blocked``: a possibly-stuck, superseded or unreconciled run is the
#: state an operator must act on, whatever the underlying lifecycle
#: says. The mapping is TOTAL over the operator vocabulary: every state
#: the projection derives has exactly one customer word, and an unmapped
#: state raises (the surface is total or it is lying).
CUSTOMER_STATE_OF_OPERATOR_STATE: Final[Mapping[str, str]] = {
    "requested": "queued",
    "authorized": "queued",
    "pause_pending": "progressing",
    "executing": "progressing",
    "resumed": "progressing",
    "safely_paused": "paused",
    "unverified": "unverified",
    "verified_ready": "verified",
    "accepted": "verified",
    "wedged": "blocked",
    "stale": "blocked",
    "dead": "blocked",
    "rejected": "blocked",
    "cancelled": "blocked",
}

#: The command statuses whose ``applied_at`` is an APPLICATION
#: observation (the mailbox ladder's settled-applied rungs). Everything
#: else — pending rungs, refusals, expiries — has no application
#: latency and is counted, never zero-filled.
_APPLIED_STATUSES: Final[frozenset[str]] = frozenset({"applied", "checkpointed"})

#: The occupancy words whose native occupancy is NOT proven — the leases
#: an operator watches after an incident (Q35-04's watchlist).
_UNKNOWN_OCCUPANCY: Final[frozenset[str]] = frozenset({"dispatched_unknown", "draining"})


#: The typed error both admission guards raise. RuntimeError subclass so
#: a wrong accounting shape fails loudly in tests and drills, never
#: renders as a confident verdict.
class AdmissionAccountingError(RuntimeError):
    """The intake/slots separation was violated — refuse, never render."""


def _parse(moment: Any) -> Any:
    """Parse an ISO timestamp; naive reads as UTC; unreadable → None."""
    from datetime import datetime, timezone

    text = str(moment or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _seconds(start: Any, end: Any) -> float | None:
    """Seconds from *start* to *end*; ``None`` when either is unreadable
    or the pair is inverted (an inverted pair is a data defect, never a
    negative duration)."""
    begin, finish = _parse(start), _parse(end)
    if begin is None or finish is None:
        return None
    span = (finish - begin).total_seconds()
    return span if span >= 0 else None


def _measure(
    name: str,
    *,
    kind: str,
    unit: str,
    definition: str,
    window: str,
    samples: list[dict[str, Any]],
    extra: Mapping[str, Any] | None = None,
    coverage: str = "present",
    unknown_reason: str = "",
) -> dict[str, Any]:
    """One measure record — the common shape all four share.

    ``population`` is the count of MATCHED samples; everything the fold
    could not match rides as its own named count (``not_yet_applied``,
    ``incomplete_cycles``, ``open_windows`` …) — a duration the rows do
    not prove is never zero-filled into the population."""
    record: dict[str, Any] = {
        "measure": name,
        "kind": kind,
        "unit": unit,
        "definition": definition,
        "window": window,
        "population": len(samples),
        "samples": samples,
        "coverage": coverage,
    }
    if coverage == "unknown":
        record["reason"] = unknown_reason or "the measure's source section was not queried"
        record["samples"] = []
        record["population"] = 0
    if extra:
        record.update(extra)
    return record


def _unknown_measure(name: str, section: str) -> dict[str, Any]:
    return _measure(
        name,
        kind="duration",
        unit="s",
        definition=f"not measured: the {section} authority was not queried for this render",
        window="",
        samples=[],
        coverage="unknown",
        unknown_reason=(
            f"the {section} section was not queried (unselected or unreachable) — "
            "this measure renders unknown, never an empty success"
        ),
    )


# ---------------------------------------------------------------------------
# The four measures — separate records, separate windows
# ---------------------------------------------------------------------------


def command_application_latency(commands: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """``ops.command_application_latency`` — per control command, the
    received→applied window (seconds).

    The machinery's OWN latency: from the mailbox row's ``created_at``
    (the command received durably) to ``applied_at`` (the ladder's
    APPLICATION observation — the lane acked applying it). Pending
    rungs, refusals and expiries have no application latency: they are
    counted in ``not_yet_applied``, never zero-filled, and never
    averaged into the applied population."""
    if commands is None:
        return _unknown_measure(COMMAND_APPLICATION_LATENCY, "commands")
    samples: list[dict[str, Any]] = []
    not_applied = 0
    for command in commands:
        status = str(command.get("status") or "")
        applied_at = str(command.get("applied_at") or "")
        if status in _APPLIED_STATUSES and applied_at:
            seconds = _seconds(command.get("created_at"), applied_at)
            if seconds is None:
                not_applied += 1  # an unreadable clock is not a sample
                continue
            samples.append(
                {
                    "command_id": str(command.get("command_id") or ""),
                    "kind": str(command.get("kind") or ""),
                    "actor_origin": str(command.get("actor_origin") or ""),
                    "from": str(command.get("created_at") or ""),
                    "to": applied_at,
                    "seconds": round(seconds, 6),
                }
            )
        else:
            not_applied += 1
    return _measure(
        COMMAND_APPLICATION_LATENCY,
        kind="duration",
        unit="s",
        definition=(
            "per control command: received (the durable mailbox row's created_at) "
            "→ the application observation (applied_at, the CTL-04 ladder's settled "
            "applied/checkpointed rungs) — the control plane's own application "
            "latency, never mixed with queue wait or native runtime"
        ),
        window="created_at → applied_at, per command row",
        samples=samples,
        extra={"not_yet_applied": not_applied},
    )


def native_occupancy_measure(
    occupancy: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int | None,
    as_of: str = "",
) -> dict[str, Any]:
    """``ops.native_occupancy`` — the point-in-time GAUGE of the run's
    execution leases by derived occupancy state.

    A gauge, not a duration: counts per occupancy word, open vs
    released, ``occupied_vs_limit`` against the deployment's admission
    bound (``None`` when no policy is mounted — an honest unknown), and
    the ages of the leases whose occupancy is NOT proven. The operator
    row carries no draining stamp, so unknown ages are measured from
    ``acquired_at`` — an honest LOWER bound of the uncertain occupancy's
    age, stated in ``age_basis``."""
    if occupancy is None:
        return _unknown_measure(NATIVE_OCCUPANCY, "occupancy")
    counts: dict[str, int] = {}
    open_leases = released = 0
    unknown_ages: list[dict[str, Any]] = []
    for row in occupancy:
        word = str(row.get("occupancy") or "")
        counts[word] = counts.get(word, 0) + 1
        if row.get("released_at"):
            released += 1
        else:
            open_leases += 1
        if word in _UNKNOWN_OCCUPANCY:
            entry: dict[str, Any] = {
                "lease_id": str(row.get("lease_id") or ""),
                "occupancy": word,
            }
            age = _seconds(row.get("acquired_at"), as_of) if as_of else None
            entry["age_seconds"] = round(age, 3) if age is not None else None
            unknown_ages.append(entry)
    at_limit = None if not limit or limit <= 0 else open_leases >= limit
    return _measure(
        NATIVE_OCCUPANCY,
        kind="gauge",
        unit="leases",
        definition=(
            "point-in-time reading of the run's execution leases by DERIVED "
            "occupancy state (never_dispatched / dispatched_unknown / native_running "
            "/ draining / observed_terminal) — a capacity gauge, never a duration, "
            "never blended with the latency measures"
        ),
        window=f"ages read against as_of={as_of or 'unknown'}",
        samples=[],
        extra={
            "as_of": as_of,
            "counts": counts,
            "open": open_leases,
            "released": released,
            "occupied_vs_limit": {
                "occupied": open_leases,
                "limit": limit if limit and limit > 0 else None,
                "at_limit": at_limit,
            },
            "occupancy_unknown": len(unknown_ages),
            "unknown_ages": unknown_ages,
            "age_basis": (
                "acquired_at — the operator occupancy row carries no draining "
                "stamp, so an unknown-occupancy age is a lower bound"
            ),
        },
    )


def recovery_duration(
    commands: Sequence[Mapping[str, Any]] | None,
    checkpoints: Sequence[Mapping[str, Any]] | None,
    *,
    as_of: str = "",
) -> dict[str, Any]:
    """``ops.recovery_duration`` — per pause/resume cycle, the
    safe-state-entered → exact-restore-observed window (seconds).

    From the pause command's ``applied_at`` (the safe-state ORDER the
    lane applied) to the checkpoint's ``activated_at`` where the R37-03
    matching holds (an applied resume whose recorded ``checkpoint_ref``
    named THIS checkpoint — ``activation: "matched"``). An activation
    that matched a DIFFERENT checkpoint is not a recovery of this cycle:
    it is counted in ``unmatched_activations``. The window INCLUDES any
    human wait inside it — the human-attention slice is carried
    separately by :func:`manual_intervention_minutes`, and this record
    says so, so no caller silently subtracts one from the other. Cycles
    still open (paused, no matched activation yet) are
    ``incomplete_cycles`` with their age — never a zero sample."""
    if commands is None or checkpoints is None:
        return _unknown_measure(RECOVERY_DURATION, "commands+checkpoints")
    pauses = [
        command
        for command in commands
        if str(command.get("kind") or "") == "pause"
        and str(command.get("status") or "") in _APPLIED_STATUSES
        and str(command.get("applied_at") or "")
    ]
    matched = [
        checkpoint
        for checkpoint in checkpoints
        if checkpoint.get("activated_at") and str(checkpoint.get("activation") or "") == "matched"
    ]
    unmatched = sum(
        1
        for checkpoint in checkpoints
        if str(checkpoint.get("activation") or "") == "unmatched-command"
    )
    samples: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for pause in pauses:
        applied_at = str(pause.get("applied_at") or "")
        restore = next(
            (
                checkpoint
                for checkpoint in matched
                if (_seconds(applied_at, checkpoint.get("activated_at")) or -1) >= 0
            ),
            None,
        )
        if restore is None:
            entry: dict[str, Any] = {
                "pause_command_id": str(pause.get("command_id") or ""),
                "awaiting": "a matched checkpoint activation after this pause",
            }
            age = _seconds(applied_at, as_of) if as_of else None
            entry["age_seconds"] = round(age, 3) if age is not None else None
            incomplete.append(entry)
            continue
        seconds = _seconds(applied_at, restore.get("activated_at"))
        if seconds is None:
            incomplete.append(
                {
                    "pause_command_id": str(pause.get("command_id") or ""),
                    "awaiting": "an unreadable clock on one endpoint — not measured",
                }
            )
            continue
        samples.append(
            {
                "pause_command_id": str(pause.get("command_id") or ""),
                "checkpoint_id": str(restore.get("checkpoint_id") or ""),
                "from": applied_at,
                "to": str(restore.get("activated_at") or ""),
                "seconds": round(seconds, 6),
            }
        )
    return _measure(
        RECOVERY_DURATION,
        kind="duration",
        unit="s",
        definition=(
            "per pause/resume cycle: the pause reaching APPLIED (the safe-state "
            "order) → the checkpoint's ACTIVATION receipt (the R37-03 matched "
            "command — the exact restore observed, never a resume request alone). "
            "The window includes any human wait inside it; "
            "ops.manual_intervention_minutes carries the human-attention slice as "
            "its own record — the two are never blended or subtracted silently"
        ),
        window="pause applied_at → matched checkpoint activated_at, per cycle",
        samples=samples,
        extra={
            "incomplete_cycles": incomplete,
            "unmatched_activations": unmatched,
        },
    )


def manual_intervention_minutes(
    commands: Sequence[Mapping[str, Any]] | None,
    checkpoints: Sequence[Mapping[str, Any]] | None,
    *,
    as_of: str = "",
) -> dict[str, Any]:
    """``ops.manual_intervention_minutes`` — the WAIT-for-human windows
    (minutes).

    Per pause/resume cycle: from the checkpoint's ``committed_at`` (the
    safely-paused state — the moment the work began waiting on a human
    decision) to the FIRST human-origin resume command's ``created_at``
    (the human acted — the moment the wait ended). Two honesty rules are
    structural: automation resumes (``automation_reconciler``) are
    excluded and counted in ``automation_resumes_excluded`` — a machine
    acting is not manual intervention; and the measure is a WAIT, not
    active work — how long the human actively worked is recorded nowhere
    and is never invented (the definition says so). Cycles still waiting
    are ``open_windows`` with their open wait, never a zero sample."""
    if commands is None or checkpoints is None:
        return _unknown_measure(MANUAL_INTERVENTION_MINUTES, "commands+checkpoints")
    committed = [
        checkpoint for checkpoint in checkpoints if str(checkpoint.get("committed_at") or "")
    ]
    resumes = sorted(
        (
            command
            for command in commands
            if str(command.get("kind") or "") == "resume"
            and str(command.get("status") or "") not in {"rejected", "expired"}
            and str(command.get("created_at") or "")
        ),
        key=lambda command: str(command.get("created_at") or ""),
    )
    automation = sum(
        1
        for command in resumes
        if str(command.get("actor_origin") or "") not in HUMAN_ACTOR_ORIGINS
    )
    samples: list[dict[str, Any]] = []
    open_windows: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for checkpoint in committed:
        committed_at = str(checkpoint.get("committed_at") or "")
        match = next(
            (
                (index, command)
                for index, command in enumerate(resumes)
                if index not in consumed
                and str(command.get("actor_origin") or "") in HUMAN_ACTOR_ORIGINS
                and (_seconds(committed_at, command.get("created_at")) or -1) >= 0
            ),
            None,
        )
        if match is None:
            entry: dict[str, Any] = {
                "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
                "awaiting": "a human-origin resume after this safe state",
            }
            age = _seconds(committed_at, as_of) if as_of else None
            entry["open_wait_minutes"] = round(age / 60, 3) if age is not None else None
            open_windows.append(entry)
            continue
        consumed.add(match[0])
        human = match[1]
        seconds = _seconds(committed_at, human.get("created_at"))
        if seconds is None:
            open_windows.append(
                {
                    "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
                    "awaiting": "an unreadable clock on one endpoint — not measured",
                }
            )
            continue
        samples.append(
            {
                "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
                "human_command_id": str(human.get("command_id") or ""),
                "actor_origin": str(human.get("actor_origin") or ""),
                "waited_from": committed_at,
                "acted_at": str(human.get("created_at") or ""),
                "minutes": round(seconds / 60, 6),
            }
        )
    return _measure(
        MANUAL_INTERVENTION_MINUTES,
        kind="duration",
        unit="min",
        definition=(
            "WAIT-for-human windows only: the safely-paused state (the committed "
            "checkpoint) → the first HUMAN-origin resume received. A lower bound "
            "on intervention latency — active human working time is recorded "
            "nowhere and is never invented; automation resumes are excluded, "
            "never counted"
        ),
        window="checkpoint committed_at → human resume created_at, per cycle",
        samples=samples,
        extra={
            "open_windows": open_windows,
            "automation_resumes_excluded": automation,
        },
    )


def ops_measures(
    rows: Mapping[str, Any],
    *,
    occupancy: Sequence[Mapping[str, Any]] | None,
    limit: int | None = None,
    as_of: str = "",
) -> dict[str, Any]:
    """The four observability quantities as FOUR SEPARATE records.

    Coverage-honest: a section the caller did not query (the R37-16
    ``?sections=`` selection, or an unreachable authority) renders that
    measure ``unknown`` — never an empty success. The document carries
    no blended field: :func:`assert_measures_separate` pins the shape,
    and the ``separation`` sentence states the rule for the operator
    reading the render."""
    commands = rows.get("commands")
    checkpoints = rows.get("checkpoints")
    document: dict[str, Any] = {
        "schema": OPS_MEASURES_SCHEMA,
        "as_of": as_of,
        COMMAND_APPLICATION_LATENCY: command_application_latency(commands),
        NATIVE_OCCUPANCY: native_occupancy_measure(occupancy, limit=limit, as_of=as_of),
        RECOVERY_DURATION: recovery_duration(commands, checkpoints, as_of=as_of),
        MANUAL_INTERVENTION_MINUTES: manual_intervention_minutes(
            commands, checkpoints, as_of=as_of
        ),
        "separation": (
            "four separate records, each over its own recorded windows — never "
            "summed, averaged or divided into one another; a window a measure "
            "could not match is counted, never zero-filled"
        ),
    }
    assert_measures_separate(document)
    return document


def assert_measures_separate(document: Mapping[str, Any]) -> None:
    """Refuse a measures document that grew a blended field.

    The four measure names plus the schema/as_of/separation metadata are
    the ONLY permitted top-level keys, and no single measure record may
    carry a ``combined`` / ``total`` / ``blended`` key — the issue's
    "never blended" is a shape rule this function enforces for every
    caller (the fold itself and any future renderer)."""
    allowed = set(OPS_MEASURE_NAMES) | {"schema", "as_of", "separation"}
    extra = sorted(set(document) - allowed)
    if extra:
        raise ValueError(
            f"{MEASURE_BLENDING_ERROR}: unknown top-level field(s) {extra} — the "
            "four quantities are separate records; blended fields are refused"
        )
    for name in OPS_MEASURE_NAMES:
        record = document.get(name)
        if isinstance(record, Mapping):
            blended = sorted(set(record) & {"combined", "total", "blended"})
            if blended:
                raise ValueError(
                    f"{MEASURE_BLENDING_ERROR}: measure {name} carries blended "
                    f"field(s) {blended} — refused"
                )


# ---------------------------------------------------------------------------
# Admission accounting — intake counters and execution slots are different
# populations, and the capped verdict is derived from occupancy only
# ---------------------------------------------------------------------------


def capped_admission_verdict(*, held: int | None, limit: int | None) -> dict[str, Any]:
    """The capped-admission verdict, DERIVED from execution occupancy.

    ``capped`` is ``held >= limit`` when a positive limit is known —
    ``None`` when the limit is unknown or disabled (an honest unknown,
    never a confident "not capped"). The function accepts NO intake
    count: there is no parameter through which a queue or request count
    could become the verdict's basis, which is the issue's "intake
    count never claimed as execution slots" made structural."""
    if held is None:
        return {
            "capped": None,
            "occupied": None,
            "limit": limit if limit and limit > 0 else None,
            "basis": (
                "execution occupancy unknown (no lease rows observed) — the verdict "
                "renders unknown, never a confident not-capped"
            ),
        }
    bound = limit if limit and limit > 0 else None
    return {
        "capped": (held >= bound) if bound is not None else None,
        "occupied": held,
        "limit": bound,
        "basis": (
            "execution occupancy (open leases) against the configured bound — the "
            "intake count never enters this verdict"
        ),
    }


def admission_accounting(
    *,
    rejected_requests: int | None,
    admitted_work: int | None,
    held: int | None,
    draining: int | None = None,
    completed: int | None = None,
    limit: int | None,
) -> dict[str, Any]:
    """The two counter families side by side, never merged.

    ``intake`` counts REQUESTS (refused at admission / admitted to the
    queue — capacity held in the queue, never on execution slots);
    ``execution`` counts SLOTS (leases held, draining, completed — the
    durable lease ledger). The ``capped_admission`` verdict comes from
    :func:`capped_admission_verdict` over the EXECUTION side alone. A
    refusal at admission consumed no execution attempt, and a queued
    intake count is a claim about NONE of the slots — the ``separation``
    sentence states both for the operator reading the render."""
    verdict = capped_admission_verdict(held=held, limit=limit)
    document = {
        "intake": {
            "rejected_requests": rejected_requests,
            "admitted_queued": admitted_work,
            "unit": "requests",
            "claim": (
                "requests refused at admission / admitted to the QUEUE — a queue "
                "count is never a count of active execution slots"
            ),
        },
        "execution": {
            "held": held,
            "draining": draining,
            "completed": completed,
            "limit": limit if limit and limit > 0 else None,
            "unit": "slots",
        },
        "capped_admission": verdict,
        "separation": (
            "intake and execution are different populations over different durable "
            "rows: a request refused at admission consumed no execution attempt, "
            "and admitting work to the queue claims no execution slot"
        ),
    }
    assert_intake_never_slots(document)
    return document


def assert_intake_never_slots(document: Mapping[str, Any]) -> None:
    """Pin the separation on every accounting document.

    Refuses (typed, loudly) when the rendered ``capped`` verdict
    disagrees with the verdict derived from the document's OWN
    execution counters — the exact drift a caller that computed
    "capped" from the intake side would produce."""
    execution = document.get("execution")
    verdict = document.get("capped_admission")
    if not isinstance(execution, Mapping) or not isinstance(verdict, Mapping):
        raise AdmissionAccountingError(
            "admission accounting needs its execution counters and its "
            "capped_admission verdict — a partial document is refused"
        )
    expected = capped_admission_verdict(held=execution.get("held"), limit=execution.get("limit"))
    if verdict.get("capped") != expected["capped"]:
        raise AdmissionAccountingError(
            "the capped-admission verdict disagrees with the execution occupancy — "
            "the verdict is derived from open leases only; an intake-derived "
            "verdict (a queue count claimed as execution slots) is refused"
        )


# ---------------------------------------------------------------------------
# The customer state and the review-budget distinction
# ---------------------------------------------------------------------------


def customer_state(state: str, health: Sequence[str] = ()) -> dict[str, Any]:
    """Map the projection's state onto the customer vocabulary.

    The customer outcome is one of the six words; the health-flavored
    states (``wedged`` / ``stale`` / ``dead`` — states in their own
    right in the projection vocabulary) map to ``blocked``, and a
    ``wedged``/``dead`` overlay arriving beside a lifecycle state
    re-classifies to ``blocked`` too: a possibly-stuck or unreconciled
    run is precisely the state an operator must act on. An unmapped
    state is a modelling error and raises — the mapping is total or the
    surface is lying."""
    word = CUSTOMER_STATE_OF_OPERATOR_STATE.get(state)
    if word is None:
        raise ValueError(
            f"no customer state maps the projection state {state!r} — the mapping "
            "is total by contract; an unmapped state is a modelling error"
        )
    overlays = [str(entry) for entry in health if str(entry) in ("wedged", "dead", "stale")]
    if any(entry in ("wedged", "dead") for entry in overlays):
        word = "blocked"
    return {
        "state": word,
        "overlays": overlays,
        "basis": (
            f"derived from the projection state {state!r}"
            + (f" with health overlays {overlays}" if overlays else "")
        ),
    }


def review_budget_distinction(rows: Mapping[str, Any]) -> dict[str, Any]:
    """The #325-facing distinction: a FAILED REVIEW BUDGET is not lost
    code and not failed independent checks.

    A pure fold over the SNAPSHOT ROWS (never the projection — the
    distinction is about what the rows PROVE survived, not the derived
    state). When the run's typed blocked reason names budget exhaustion,
    the block renders WHAT failed (the review leg when a candidate
    exists — the code was delivered; the implementation leg otherwise),
    WHAT SURVIVED (the current candidate, the committed checkpoint —
    each with its identity, each honestly absent when no row proves
    it), and the CHECKS' own verdicts (a passed independent verification
    stays a pass; a missing one stays missing — neither becomes a
    failure). The guarded recovery is named beside it: the review-only
    continuation (repeats ONLY the review of the same candidate/tested
    identity) and the auditable top-up — never a silent re-plan or a
    lost run."""
    run_raw = rows.get("run")
    run: Mapping[str, Any] = run_raw if isinstance(run_raw, Mapping) else {}
    blocked = str(run.get("blocked_reason") or "")
    commands = rows.get("commands")
    checkpoints = rows.get("checkpoints")
    verifications = rows.get("verifications")
    candidate_shas = [str(sha) for sha in run.get("candidate_shas") or [] if str(sha or "")]
    candidate = current_candidate(run, candidate_shas) if candidate_shas else ""
    budget_refusal = "budget_exhausted" in blocked
    committed = (
        [checkpoint for checkpoint in checkpoints if str(checkpoint.get("committed_at") or "")]
        if isinstance(checkpoints, list)
        else None
    )
    checks: list[dict[str, Any]] = []
    if isinstance(verifications, list):
        for verification in verifications:
            checks.append(
                {
                    "check": "independent_verification",
                    "result": str(verification.get("result") or ""),
                    "candidate_sha": str(verification.get("candidate_sha") or ""),
                    "at": str(verification.get("at") or ""),
                }
            )
    review_leg = bool(budget_refusal and candidate)
    return {
        "failed_review_budget": review_leg,
        "budget_refusal": budget_refusal,
        "what_failed": (
            "the closing review leg's budget (the #325 reserve seam) — NOT the code "
            "and NOT the independent checks"
            if review_leg
            else (
                "the implementation budget (no candidate was delivered yet)"
                if budget_refusal
                else "no budget refusal is recorded for this run"
            )
        ),
        "code": {
            "candidate_sha": candidate or None,
            "candidate_state": (
                "delivered — the candidate row and its digests survive the refusal"
                if candidate
                else "no candidate recorded (honest absent, never invented)"
            ),
            "checkpoint": (
                {
                    "checkpoint_id": str(committed[-1].get("checkpoint_id") or ""),
                    "digest": str(committed[-1].get("digest") or ""),
                    "committed_at": str(committed[-1].get("committed_at") or ""),
                }
                if committed
                else None
            ),
        },
        "independent_checks": checks,
        "recovery": (
            {
                "review_only_continuation": (
                    "the guarded #325 continuation repeats ONLY the review of the "
                    "SAME candidate/tested identity — zero coder dispatches, zero "
                    "commits by construction; a moved identity invalidates the "
                    "shortcut and reruns the verification"
                ),
                "top_up": (
                    "the explicit auditable operator top-up (BudgetTopUp: amount + "
                    "reason, replay-idempotent) — never a silent re-plan"
                ),
            }
            if budget_refusal
            else None
        ),
        "commands_coverage": "unknown" if commands is None else "present",
    }


# ---------------------------------------------------------------------------
# R40-13 (#349) — the delivery-round read-model arm: the operator.*
# observability records and the five linked facts' fold
# ---------------------------------------------------------------------------


def recovery_rounds_measure(rows: Mapping[str, Any]) -> dict[str, Any]:
    """``operator.recovery_rounds`` — the lineage's recorded follow-up
    review rounds (a GAUGE over the #338 ``review_rounds`` rows plus the
    child evidence fragment).

    Counted, never narrated: the total recorded, how many still hold the
    lineage's ONE outstanding slot, and the newest round's reference —
    the number an operator reads to know how many correction cycles a
    delivery lineage has been through (the bounded round policy's input).
    An unqueried rounds authority renders ``unknown``, never a confident
    zero."""
    rounds = rows.get("rounds")
    if rounds is None:
        run_raw = rows.get("run")
        run: Mapping[str, Any] = run_raw if isinstance(run_raw, Mapping) else {}
        own = run.get("evidence")
        if isinstance(own, Mapping) and isinstance(own.get("review_round"), Mapping):
            fragment = own["review_round"]
            return _measure(
                RECOVERY_ROUNDS,
                kind="gauge",
                unit="rounds",
                definition=(
                    "the lineage's recorded follow-up review rounds — counted from the "
                    "run's own round fragment (the rounds table was not queried)"
                ),
                window="point-in-time",
                samples=[],
                extra={
                    "recorded": 1,
                    "open": 1,
                    "newest": f"round:{fragment.get('round_number')}:{fragment.get('decision_id')}",
                },
            )
        return _measure(
            RECOVERY_ROUNDS,
            kind="gauge",
            unit="rounds",
            definition="not measured: the rounds authority was not queried for this render",
            window="",
            samples=[],
            coverage="unknown",
            unknown_reason=(
                "the review-rounds section was not queried (unselected or unreachable) — "
                "this gauge renders unknown, never a confident zero"
            ),
        )
    open_rounds = sum(
        1 for row in rounds if str((row or {}).get("status") or "") in ("admitted", "dispatched")
    )
    newest_number = 0
    newest_ref = ""
    for row in rounds:
        try:
            number = int((row or {}).get("round_number") or 0)
        except (TypeError, ValueError):
            continue
        if number >= newest_number:
            newest_number = number
            newest_ref = f"round:{number}:{str((row or {}).get('decision_id') or '')}"
    return _measure(
        RECOVERY_ROUNDS,
        kind="gauge",
        unit="rounds",
        definition=(
            "the lineage's recorded follow-up review rounds (the #338 review_rounds "
            "rows naming this run as parent or child) — the correction-cycle count "
            "the bounded round policy reads"
        ),
        window="point-in-time",
        samples=[],
        extra={"recorded": len(rounds), "open": open_rounds, "newest": newest_ref},
    )


def unresolved_effect_age_measure(rows: Mapping[str, Any], *, as_of: str = "") -> dict[str, Any]:
    """``operator.unresolved_effect_age`` — the AGE of every unresolved
    external effect (seconds), per publication-intent row still in an
    unresolved state.

    The reconciler's urgency number: an effect whose landing is unproven
    blocks every safe retry, and its age says how long that uncertainty
    has stood. Ages read against *as_of* from the row's own ``at``
    moment (``None`` when either clock is unreadable — never a
    synthesized age)."""
    publications = rows.get("publications")
    unresolved = ("requested", "dispatched", "probing", "unknown")
    samples: list[dict[str, Any]] = []
    if publications is None:
        return _measure(
            UNRESOLVED_EFFECT_AGE,
            kind="duration",
            unit="s",
            definition=("not measured: the publications authority was not queried for this render"),
            window="",
            samples=[],
            coverage="unknown",
            unknown_reason=(
                "the publications section was not queried — this measure renders "
                "unknown, never an empty success"
            ),
        )
    for row in publications:
        if str((row or {}).get("status") or "") not in unresolved:
            continue
        entry: dict[str, Any] = {
            "operation_key": str((row or {}).get("operation_key") or ""),
            "status": str((row or {}).get("status") or ""),
        }
        age = _seconds((row or {}).get("at"), as_of) if as_of else None
        entry["age_seconds"] = round(age, 3) if age is not None else None
        samples.append(entry)
    return _measure(
        UNRESOLVED_EFFECT_AGE,
        kind="duration",
        unit="s",
        definition=(
            "per unresolved external effect (a publication intent whose landing is "
            "unproven): the seconds it has stood unresolved, read against the render "
            "moment — the reconciler's urgency number, never blended with the latency "
            "measures"
        ),
        window="effect at → as_of, per unresolved intent",
        samples=samples,
        extra={"unresolved": len(samples)},
    )


def queue_age_measure(rows: Mapping[str, Any], *, as_of: str = "") -> dict[str, Any]:
    """``operator.queue_age`` — the AGE of the QUEUED population
    (seconds), per admitted-not-executing run.

    R40-15 (#351): the sustained-queue-age alert's observable. The queued
    population is the #334 SEPARATE population — runs admitted but not
    holding execution capacity (the pre-execution statuses) — so its age
    is NEVER a slot claim and never blended with the latency measures.
    Ages read against *as_of* from each run's ``created_at``; an unqueried
    runs authority renders ``unknown``, never a confident empty queue."""
    runs = rows.get("runs")
    if runs is None:
        return _measure(
            QUEUE_AGE,
            kind="duration",
            unit="s",
            definition="not measured: the runs authority was not queried for this render",
            window="",
            samples=[],
            coverage="unknown",
            unknown_reason=(
                "the runs section was not queried (unselected or unreachable) — this "
                "measure renders unknown, never a confident empty queue"
            ),
        )
    queued = {
        "accepted",
        "preflight",
        "planning",
        "waiting_approval",
        "waiting_harness",
        "waiting_ci",
        "proposing",
    }
    samples: list[dict[str, Any]] = []
    for row in runs:
        status = str((row or {}).get("status") or "")
        if status not in queued:
            continue
        entry: dict[str, Any] = {"run_id": str((row or {}).get("run_id") or ""), "status": status}
        age = _seconds((row or {}).get("created_at"), as_of) if as_of else None
        entry["age_seconds"] = round(age, 3) if age is not None else None
        samples.append(entry)
    ages = [float(entry["age_seconds"]) for entry in samples if entry["age_seconds"] is not None]
    return _measure(
        QUEUE_AGE,
        kind="duration",
        unit="s",
        definition=(
            "per admitted-not-executing run (the pre-execution statuses — a population "
            "SEPARATE from execution slots): the seconds it has waited, read against the "
            "render moment — the sustained-queue-age alert's input"
        ),
        window="run created_at → as_of, per queued run",
        samples=samples,
        extra={
            "queued": len(samples),
            "oldest_seconds": round(max(ages), 3) if ages else None,
            "ages_unknown": sum(1 for entry in samples if entry["age_seconds"] is None),
        },
    )


def time_to_safe_action_measure(
    projection: OperatorProjection | Mapping[str, Any], *, as_of: str = ""
) -> dict[str, Any]:
    """``operator.time_to_safe_action`` — how long the current world has
    stood (seconds): from the projection's last semantic transition to
    the render moment.

    An honest LOWER BOUND on how long a safe action has been decidable:
    the world stopped moving at the last transition, so every second
    after it is time a safe action existed and went unacted. It is NOT
    reaction time (whether a human LOOKED is recorded nowhere) and never
    a blended average."""
    last_transition = str(getattr(projection, "last_transition_at", "") or "")
    age = _seconds(last_transition, as_of) if as_of and last_transition else None
    return _measure(
        TIME_TO_SAFE_ACTION,
        kind="duration",
        unit="s",
        definition=(
            "from the last recorded semantic transition to the render moment — a "
            "LOWER BOUND on how long a safe action has been decidable against a "
            "stable world; human reaction time is recorded nowhere and is never invented"
        ),
        window="last transition → as_of",
        samples=[],
        extra={
            "last_transition_at": last_transition,
            "stood_seconds": round(age, 3) if age is not None else None,
        },
    )


def delivery_round_block(
    rows: Mapping[str, Any],
    *,
    projection: OperatorProjection | Mapping[str, Any],
    as_of: str = "",
) -> dict[str, Any]:
    """The R40-13 five-fact fold for the read-model: the delivery/round
    subject, the lineage customer state, the supersession and the typed
    next-actions — one block over the SAME rows the read-model read.

    The lineage customer state reconciles the projection's own state
    with the round facts: a delivery whose lineage holds an OPEN round
    is ``progressing`` (the round's child is the current execution),
    whatever the parent's own terminal-looking state says; a SUPERSEDED
    parent never reports its old ``verified`` readiness as the
    lineage's current state. Actions offered against the block carry
    :data:`STALE_ACTION_REFUSAL` as their refusal contract — the
    expected candidate/round/version ticket the guarded route checks."""
    view = delivery_view(
        rows,
        projection=projection if isinstance(projection, OperatorProjection) else None,
        now=as_of or None,
    )
    facts = view.get("facts") or {}
    round_fact = facts.get("review_round") or {}
    open_round = bool(round_fact.get("open"))
    superseded = bool((facts.get("candidate") or {}).get("superseded_by"))
    projection_state = str(getattr(projection, "state", "") or "")
    if open_round:
        lineage_state: dict[str, Any] = {
            "state": "progressing",
            "basis": (
                f"the lineage's {round_fact.get('ref')} is open — the round's child "
                "run is the current execution, whatever the parent's own record says"
            ),
        }
    elif superseded:
        lineage_state = {
            "state": "unverified",
            "basis": (
                "the parent's delivery was superseded by a follow-up round — its green "
                "evidence is history; the lineage's current candidate carries no "
                "current-pass verification in these rows"
            ),
        }
    else:
        lineage_state = (
            customer_state(projection_state) if projection_state else {"state": "unknown"}
        )
    return {
        "schema": DELIVERY_VIEW_SCHEMA,
        "round_ref": view.get("round_ref"),
        "superseded_by": (facts.get("candidate") or {}).get("superseded_by") or "",
        "lineage_customer_state": lineage_state,
        "acceptance": facts.get("acceptance") or {},
        "next_actions": view.get("next_actions") or [],
        "stale_action_refusal": STALE_ACTION_REFUSAL,
        "history_separation": (
            "the parent's ready delivery, verdict and evidence are immutable history "
            "the moment a superseding round exists — they never re-render as the "
            "lineage's current readiness"
        ),
    }


# ---------------------------------------------------------------------------
# The six-quantity read-model
# ---------------------------------------------------------------------------


def _required_checks(
    rows: Mapping[str, Any],
    projection: OperatorProjection | Mapping[str, Any],
    coverage: Mapping[str, str],
) -> list[dict[str, Any]]:
    """The checks the CURRENT candidate still needs, from rows only.

    The independent-verification check states ``passed`` only for the
    CURRENT candidate (a pass naming an older candidate is a historical
    pass, never current readiness — the R37-03 binding rule); the
    closing-review check states what the run row proves about the
    reviewer leg. An unqueried authority renders ``unknown`` — never a
    confident "no checks"."""
    run_raw = rows.get("run")
    run: Mapping[str, Any] = run_raw if isinstance(run_raw, Mapping) else {}
    checks: list[dict[str, Any]] = []
    verifications = rows.get("verifications")
    if not isinstance(verifications, list):
        checks.append(
            {
                "check": "independent_verification",
                "status": "unknown",
                "evidence": "the verifications authority was not queried",
            }
        )
    else:
        candidate_shas = [str(sha) for sha in run.get("candidate_shas") or [] if str(sha or "")]
        current = current_candidate(run, candidate_shas) if candidate_shas else ""
        passed = any(
            str(verification.get("result") or "") in ("passed", "ok", "green", "success")
            and (not current or str(verification.get("candidate_sha") or "") == current)
            for verification in verifications
        )
        checks.append(
            {
                "check": "independent_verification",
                "status": (
                    "passed"
                    if passed and current
                    else ("pending" if current else "not_applicable_no_candidate")
                ),
                "evidence": (
                    f"a passed verification bound to the current candidate {current[:12]}"
                    if passed and current
                    else "no passed verification bound to the current candidate"
                ),
            }
        )
    blocked = str(run.get("blocked_reason") or "")
    status = str(run.get("status") or "")
    if "budget_exhausted" in blocked:
        closing = "refused_budget"  # the review-budget seam, rendered separately
    elif status in {"ready_for_human", "accepted"}:
        closing = "awaiting_human_merge_decision"
    elif status in {"failed", "cancelled", "rejected"}:
        closing = "not_applicable_terminal"
    else:
        closing = "pending"
    checks.append(
        {"check": "closing_review", "status": closing, "evidence": f"run status {status!r}"}
    )
    return checks


def ops_limits_read_model(
    rows: Mapping[str, Any],
    *,
    occupancy: Sequence[Mapping[str, Any]] | None,
    coverage: Mapping[str, str],
    projection: OperatorProjection | Mapping[str, Any],
    limit: int | None = None,
    as_of: str = "",
    admission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ONE operator read-model: the six quantities, the customer
    state, the four measures and the review-budget distinction.

    The six quantities, each with its own evidence and coverage:

    - ``current_attempt`` — the LATEST attempt row (identity + status);
      history rides in ``measures`` samples with their own moments;
    - ``native_occupancy`` — the gauge record
      (:func:`native_occupancy_measure`);
    - ``exact_checkpoint`` — the committed checkpoint's identity,
      activation receipt and fence word (``unmatched-command`` says an
      applied resume named a DIFFERENT checkpoint — history never
      relabels the current one);
    - ``unresolved_effects`` — the projection's own list;
    - ``required_checks`` — :func:`_required_checks`;
    - ``accounting_coverage`` — which section authorities were observed
      (``present``/``missing``/``unknown`` — the honesty map), whether
      the occupancy limit is known, and the admission accounting when
      the caller could derive it.

    Scope and history are enforced upstream and stated here: the reader
    filtered by canonical subject BEFORE any of this ran, and the
    ``history_separation`` sentence names the rule the render follows."""
    attempts = rows.get("attempts")
    checkpoints = rows.get("checkpoints")
    if isinstance(attempts, list) and attempts:
        latest = attempts[-1]
        current_attempt: dict[str, Any] = {
            "attempt_id": str(latest.get("attempt_id") or ""),
            "kind": str(latest.get("kind") or ""),
            "status": str(latest.get("status") or "unknown"),
            "at": str(latest.get("started_at") or ""),
        }
    else:
        current_attempt = {
            "attempt_id": "",
            "kind": "",
            "status": "unknown",
            "coverage": str(coverage.get("attempts", "unknown")),
            "note": (
                "the attempts authority was not queried or holds no row — never "
                "an invented current attempt"
            ),
        }
    committed = (
        [cp for cp in checkpoints if str(cp.get("committed_at") or "")]
        if isinstance(checkpoints, list)
        else []
    )
    if committed:
        newest = committed[-1]
        exact_checkpoint: dict[str, Any] = {
            "checkpoint_id": str(newest.get("checkpoint_id") or ""),
            "digest": str(newest.get("digest") or ""),
            "committed_at": str(newest.get("committed_at") or ""),
            "activated_at": newest.get("activated_at"),
            "activation": str(newest.get("activation") or ""),
            "fence": str(newest.get("fence") or ""),
            "coverage": str(coverage.get("checkpoints", "unknown")),
        }
    else:
        exact_checkpoint = {
            "checkpoint_id": "",
            "digest": "",
            "committed_at": "",
            "activated_at": None,
            "activation": "",
            "fence": "",
            "coverage": str(coverage.get("checkpoints", "unknown")),
            "note": (
                "no committed checkpoint observed"
                if coverage.get("checkpoints") in ("present", "missing")
                else "the checkpoints authority was not queried — unknown, never empty"
            ),
        }
    unresolved = list(getattr(projection, "unresolved_effects", ()) or [])
    state = str(getattr(projection, "state", "") or "")
    health = list(getattr(projection, "health", ()) or ())
    document: dict[str, Any] = {
        "schema": OPS_LIMITS_READ_MODEL_SCHEMA,
        "as_of": as_of,
        "customer_state": customer_state(state, health) if state else {"state": "unknown"},
        "current_attempt": current_attempt,
        "native_occupancy": native_occupancy_measure(occupancy, limit=limit, as_of=as_of),
        "exact_checkpoint": exact_checkpoint,
        "unresolved_effects": unresolved,
        "required_checks": _required_checks(rows, projection, coverage),
        "accounting_coverage": {
            "sources": {name: word for name, word in sorted(coverage.items())},
            "occupancy_limit_known": bool(limit and limit > 0),
            "admission": (
                dict(admission)
                if admission is not None
                else {
                    "available": False,
                    "reason": (
                        "the project's admission accounting was not derivable from "
                        "this render (no policy mounted or no lease rows naming the "
                        "project) — an honest unknown, never a zeroed report"
                    ),
                }
            ),
        },
        "measures": ops_measures(rows, occupancy=occupancy, limit=limit, as_of=as_of),
        "review_budget": review_budget_distinction(rows),
        # R40-13 (#349): the delivery-round arm — the five linked facts'
        # fold (round subject, lineage customer state, supersession, the
        # typed next-actions) plus the three operator.* observability
        # records (recovery rounds, unresolved-effect ages, time to safe
        # action), each its own record.
        "delivery_round": delivery_round_block(rows, projection=projection, as_of=as_of),
        RECOVERY_ROUNDS: recovery_rounds_measure(rows),
        UNRESOLVED_EFFECT_AGE: unresolved_effect_age_measure(rows, as_of=as_of),
        TIME_TO_SAFE_ACTION: time_to_safe_action_measure(projection, as_of=as_of),
        QUEUE_AGE: queue_age_measure(rows, as_of=as_of),
        "history_separation": (
            "every current field derives from the LATEST row of its section; every "
            "historical sample inside a measure carries its own from/to moments — "
            "history is never relabelled as current state"
        ),
        "scope": (
            "assembled from subject-scoped rows the authorized reader already "
            "filtered — repositories outside the verified scope never entered "
            "this document"
        ),
    }
    return document
