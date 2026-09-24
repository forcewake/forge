"""R37-12 (issue #293) — the staged design-partner pilot LADDER.

The pilot KIT (:mod:`forge.adaptive.pilot`, R32-21) froze what a pilot
MEASURES; the lab pilot (R36-16) executed the seams with a scripted
vendor.  What neither does is stage the path from "a contract exists"
to "an operational sample of 12–20 real tasks ran" — that path is this
module's ladder:

    ``contract`` → ``one-observed-task`` → ``supervised-batch`` →
    ``operational-sample``

The honesty rules that are structural here:

- **A named external customer is a prerequisite the machinery cannot
  fabricate.**  The shipped contract carries ``customer`` honestly as
  :data:`CUSTOMER_PENDING` (``pending-recruitment``).  Stage
  :data:`STAGE_SUPERVISED_BATCH` fails CLOSED without a non-pending
  customer, a non-pending task-set approval by the named code owner,
  and an OBSERVED baseline (:meth:`LearningContract.stage_prerequisites`,
  :meth:`LearningContract.validate_for_stage`).
- **The baseline is the customer's ACTUAL workflow, measured** —
  assisted coding time, reviewer effort, operations intervention and
  waiting as SEPARATE typed fields, each with provenance
  ``observed | pending`` (:class:`CustomerBaseline`).  An AUTHORED
  baseline — the lab pilot's shape, fine for ITS scope — is REFUSED
  outright for the partner ladder (:class:`BaselineMeasure` rejects the
  ``authored`` provenance), and a pending field never advances the
  ladder.
- **A tripped stop rule halts the ladder and PRESERVES diagnostics**
  (:meth:`PilotLadder.trip_stop`): the verbatim state at trip time is
  retained with its digest, and no stage may start while a rule is
  tripped (:meth:`PilotLadder.gate` refuses first).
- **Spend is capped, not hoped-at**: every stage record carries usage
  receipts; a spend over the contract's cap trips the budget stop rule
  (:meth:`PilotLadder.check_budget`) — "no next paid batch starts
  automatically" is a gate predicate, not a policy note.
- **The final decision separates engineering feasibility from adoption
  willingness** (:class:`DecisionReview`): two TYPED assessments with
  their own verdict vocabularies, never one blended number — and while
  no partner exists the adoption verdict is forced to
  ``unknown-pending-partner`` because nobody has been asked.

The one-observed-task stage may run against the forge LAB with the real
model (bounded spend, disposable project) as the machinery-validation
leg — that is what :mod:`scripts.run_pilot_stage` drives, recording the
stage as ``pending-lab`` with the exact unmet preconditions whenever the
lab preflight refuses.  An external-partner execution remains the
explicitly-recorded next step; nothing in this module can or does claim
it happened.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "ADOPTION_UNDECIDED",
    "ADOPTION_UNKNOWN_PENDING_PARTNER",
    "ADOPTION_UNWILLING",
    "ADOPTION_VERDICTS",
    "ADOPTION_WILLING",
    "BASELINE_AXES",
    "CONTRACT_SCHEMA",
    "CUSTOMER_PENDING",
    "DECISION_CONTINUE",
    "DECISION_NARROW",
    "DECISION_REDESIGN",
    "DECISION_STOP",
    "DECISIONS",
    "FEASIBILITY_DEMONSTRATED",
    "FEASIBILITY_NOT_DEMONSTRATED",
    "FEASIBILITY_PARTIAL",
    "FEASIBILITY_VERDICTS",
    "LADDER_SCHEMA",
    "LADDER_STAGES",
    "PROVENANCE_OBSERVED",
    "PROVENANCE_PENDING",
    "PROVENANCE_VOCABULARY",
    "REPORT_SCHEMA",
    "STAGE_CONTRACT",
    "STAGE_ONE_OBSERVED_TASK",
    "STAGE_OPERATIONAL_SAMPLE",
    "STAGE_SUPERVISED_BATCH",
    "STAGE_RECORD_SCHEMA",
    "STATUS_EXECUTED",
    "STATUS_PENDING_LAB",
    "STATUS_PENDING_PARTNER",
    "STOP_AUTHORITY_VIOLATION",
    "STOP_BUDGET_OVERRUN",
    "STOP_CUSTOMER_STOP",
    "STOP_RULE_OUTCOME",
    "STOP_RULES",
    "TASKSET_PENDING",
    "AdoptionAssessment",
    "BaselineMeasure",
    "Customer",
    "CustomerBaseline",
    "DecisionReview",
    "EngineeringFeasibility",
    "GateDecision",
    "LearningContract",
    "PilotLadder",
    "PilotLadderError",
    "Precondition",
    "StageRecord",
    "UsageReceipt",
    "build_partner_pilot_report",
]

#: The schema stamps — versioned so a breaking change to what a stage or
#: a contract MEANS stays distinguishable from documents published under
#: the old shapes.
CONTRACT_SCHEMA = "forge.partner-pilot.contract/1"
LADDER_SCHEMA = "forge.partner-pilot.ladder-state/1"
STAGE_RECORD_SCHEMA = "forge.partner-pilot.stage-record/1"
REPORT_SCHEMA = "forge.partner-pilot.report/1"

#: The staged ladder, in order.  A stage may only start when every
#: earlier stage is complete and no stop rule is tripped.
STAGE_CONTRACT = "contract"
STAGE_ONE_OBSERVED_TASK = "one-observed-task"
STAGE_SUPERVISED_BATCH = "supervised-batch"
STAGE_OPERATIONAL_SAMPLE = "operational-sample"
LADDER_STAGES: tuple[str, ...] = (
    STAGE_CONTRACT,
    STAGE_ONE_OBSERVED_TASK,
    STAGE_SUPERVISED_BATCH,
    STAGE_OPERATIONAL_SAMPLE,
)

#: The honest customer state until a NAMED external partner is
#: recruited — a value the machinery treats as "not yet", never as a
#: customer.  Only the maintainer can change it (by recruiting).
CUSTOMER_PENDING = "pending-recruitment"

#: The honest task-set approval state until the named code owner
#: approves the task set (an acceptance criterion, not a formality).
TASKSET_PENDING = "pending-code-owner-approval"

#: Stage-record statuses: ``executed`` (the stage's work really ran),
#: ``pending-lab`` (a LAB precondition is unmet — re-checkable), and
#: ``pending-partner`` (the missing prerequisite is the partner).
STATUS_EXECUTED = "executed"
STATUS_PENDING_LAB = "pending-lab"
STATUS_PENDING_PARTNER = "pending-partner"

#: The closed stop-rule vocabulary.  A tripped rule preserves
#: diagnostics and halts the ladder.
STOP_AUTHORITY_VIOLATION = "authority_violation"
STOP_BUDGET_OVERRUN = "budget_overrun"
STOP_RULE_OUTCOME = "stop_rule_outcome"
STOP_CUSTOMER_STOP = "customer_stop"
STOP_RULES: tuple[str, ...] = (
    STOP_AUTHORITY_VIOLATION,
    STOP_BUDGET_OVERRUN,
    STOP_RULE_OUTCOME,
    STOP_CUSTOMER_STOP,
)

#: The continuation-decision vocabulary (the decision review's verdict).
DECISION_CONTINUE = "continue"
DECISION_NARROW = "narrow"
DECISION_REDESIGN = "redesign"
DECISION_STOP = "stop"
DECISIONS: tuple[str, ...] = (DECISION_CONTINUE, DECISION_NARROW, DECISION_REDESIGN, DECISION_STOP)

#: The engineering-feasibility verdicts (typed, separate from adoption).
FEASIBILITY_DEMONSTRATED = "demonstrated"
FEASIBILITY_PARTIAL = "partial"
FEASIBILITY_NOT_DEMONSTRATED = "not-demonstrated"
FEASIBILITY_VERDICTS: tuple[str, ...] = (
    FEASIBILITY_DEMONSTRATED,
    FEASIBILITY_PARTIAL,
    FEASIBILITY_NOT_DEMONSTRATED,
)

#: The adoption-willingness verdicts (typed, separate from feasibility).
#: ``unknown-pending-partner`` is FORCED while the customer is pending —
#: nobody has been asked, so no willingness exists to report.
ADOPTION_WILLING = "willing"
ADOPTION_UNDECIDED = "undecided"
ADOPTION_UNWILLING = "unwilling"
ADOPTION_UNKNOWN_PENDING_PARTNER = "unknown-pending-partner"
ADOPTION_VERDICTS: tuple[str, ...] = (
    ADOPTION_WILLING,
    ADOPTION_UNDECIDED,
    ADOPTION_UNWILLING,
    ADOPTION_UNKNOWN_PENDING_PARTNER,
)

#: The customer-baseline axes — the customer's ACTUAL workflow measured
#: SEPARATELY, exactly as R37-12 scope item 2 demands (never one blended
#: "cycle time" standing in for four different costs).
BASELINE_AXES: tuple[str, ...] = (
    "assisted_coding_minutes",
    "reviewer_minutes",
    "ops_intervention_minutes",
    "waiting_minutes",
)

#: The baseline-provenance vocabulary.  ``authored`` is deliberately
#: ABSENT: an authored baseline is refused outright for the partner
#: ladder (the lab pilot's authored baseline was valid for ITS scope —
#: the partner ladder requires observed).
PROVENANCE_OBSERVED = "observed"
PROVENANCE_PENDING = "pending"
PROVENANCE_VOCABULARY: tuple[str, ...] = (PROVENANCE_OBSERVED, PROVENANCE_PENDING)

#: The stages that REQUIRE the named external customer (and everything
#: the customer brings: task-set approval, observed baseline).  The
#: one-observed-task stage may run on the lab as the machinery leg —
#: supervised-batch onward is partner work, fail-closed.
_PARTNER_STAGES: frozenset[str] = frozenset({STAGE_SUPERVISED_BATCH, STAGE_OPERATIONAL_SAMPLE})


class PilotLadderError(ValueError):
    """A ladder contract, stage record, gate or report is invalid."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_datetime(value: str) -> datetime:
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PilotLadderError(f"expected an ISO datetime, got {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The customer and the OBSERVED baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Customer:
    """The named external customer — or the honest pending placeholder.

    ``name``/``organization``/``code_owner`` are the named humans/org the
    acceptance criteria require; the pending state (the default) is what
    the machinery can honestly say until the maintainer recruits one.
    """

    name: str = CUSTOMER_PENDING
    organization: str = ""
    code_owner: str = ""

    @property
    def is_pending(self) -> bool:
        return self.name.strip().lower() == CUSTOMER_PENDING or not self.name.strip()

    def validate(self) -> None:
        if self.is_pending:
            if self.organization.strip() or self.code_owner.strip():
                raise PilotLadderError(
                    "a pending customer carries no organization or code owner — "
                    "recruit the partner first, then name all three"
                )
            return
        for label, value in (("name", self.name), ("code_owner", self.code_owner)):
            if not value.strip():
                raise PilotLadderError(f"customer needs a named {label} (a human, not a role)")

    def as_document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "organization": self.organization,
            "code_owner": self.code_owner,
            "state": CUSTOMER_PENDING if self.is_pending else "named",
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> Customer:
        entry = dict(doc or {})
        return cls(
            name=str(entry.get("name") or CUSTOMER_PENDING),
            organization=str(entry.get("organization") or ""),
            code_owner=str(entry.get("code_owner") or ""),
        )


@dataclass(frozen=True)
class BaselineMeasure:
    """One baseline axis: a value, its provenance and its source.

    ``provenance`` is :data:`PROVENANCE_OBSERVED` (measured on the
    customer's real workflow) or :data:`PROVENANCE_PENDING` (not yet
    measured).  ``authored`` is REFUSED with a message that names why —
    the lab pilot's authored baseline was valid for the lab's scope, and
    the partner ladder requires the customer's own numbers.
    """

    value_minutes: float | None = None
    provenance: str = PROVENANCE_PENDING
    source: str = ""

    def validate(self, axis: str = "") -> None:
        label = axis or "baseline measure"
        if self.provenance == "authored":
            raise PilotLadderError(
                f"{label}: an AUTHORED baseline is refused for the partner ladder — the "
                "lab pilot's authored baseline was scoped to the lab; the partner ladder "
                "requires the customer's OBSERVED workflow"
            )
        if self.provenance not in PROVENANCE_VOCABULARY:
            raise PilotLadderError(
                f"{label}: provenance {self.provenance!r} not in {list(PROVENANCE_VOCABULARY)}"
            )
        if self.provenance == PROVENANCE_OBSERVED:
            if self.value_minutes is None or self.value_minutes <= 0:
                raise PilotLadderError(
                    f"{label}: an OBSERVED measure needs a positive value (never a guessed zero)"
                )
            if not self.source.strip():
                raise PilotLadderError(
                    f"{label}: an OBSERVED measure needs its source (how/where it was measured)"
                )
        elif self.value_minutes is not None:
            raise PilotLadderError(
                f"{label}: a PENDING measure carries no value — pending is pending, "
                "not zero dressed as measured"
            )

    def as_document(self, axis: str = "") -> dict[str, Any]:
        return {
            "axis": axis,
            "value_minutes": self.value_minutes,
            "provenance": self.provenance,
            "source": self.source,
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> BaselineMeasure:
        entry = dict(doc or {})
        value = entry.get("value_minutes")
        return cls(
            value_minutes=float(value) if value is not None else None,
            provenance=str(entry.get("provenance") or PROVENANCE_PENDING),
            source=str(entry.get("source") or ""),
        )


def _pending_measure() -> BaselineMeasure:
    return BaselineMeasure(value_minutes=None, provenance=PROVENANCE_PENDING, source="")


@dataclass(frozen=True)
class CustomerBaseline:
    """The customer's ACTUAL workflow, measured on four separate axes.

    Each axis is its own typed measure — assisted coding time, reviewer
    effort, operations intervention, waiting — because they are four
    different costs paid by four different people; blending them into
    one "cycle time" is exactly the substitution R37-12 rejects.  The
    baseline is ladder-ready only when EVERY axis is observed
    (:meth:`all_observed`); a pending axis keeps it honestly pending.
    """

    assisted_coding_minutes: BaselineMeasure = field(default_factory=_pending_measure)
    reviewer_minutes: BaselineMeasure = field(default_factory=_pending_measure)
    ops_intervention_minutes: BaselineMeasure = field(default_factory=_pending_measure)
    waiting_minutes: BaselineMeasure = field(default_factory=_pending_measure)
    window_start: str = ""
    window_end: str = ""
    method: str = ""

    def measure(self, axis: str) -> BaselineMeasure:
        if axis not in BASELINE_AXES:
            raise PilotLadderError(
                f"unknown baseline axis {axis!r}; the closed vocabulary is {list(BASELINE_AXES)}"
            )
        return getattr(self, axis)

    @property
    def all_observed(self) -> bool:
        return all(self.measure(axis).provenance == PROVENANCE_OBSERVED for axis in BASELINE_AXES)

    @property
    def pending_axes(self) -> tuple[str, ...]:
        return tuple(
            axis for axis in BASELINE_AXES if self.measure(axis).provenance != PROVENANCE_OBSERVED
        )

    def validate(self) -> None:
        for axis in BASELINE_AXES:
            self.measure(axis).validate(axis)
        if self.all_observed:
            if not (self.window_start.strip() and self.window_end.strip()):
                raise PilotLadderError(
                    "an OBSERVED baseline needs its measurement window (when it was measured)"
                )
            if not self.method.strip():
                raise PilotLadderError(
                    "an OBSERVED baseline needs its method (how the four axes were measured)"
                )

    def as_document(self) -> dict[str, Any]:
        return {
            "axes": {axis: self.measure(axis).as_document(axis) for axis in BASELINE_AXES},
            "all_observed": self.all_observed,
            "window": {"start": self.window_start, "end": self.window_end},
            "method": self.method,
            "note": (
                "the customer's ACTUAL workflow measured per axis; an authored baseline is "
                "refused for the partner ladder (provenance vocabulary is observed|pending)"
            ),
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> CustomerBaseline:
        entry = dict(doc or {})
        axes = entry.get("axes") if isinstance(entry.get("axes"), Mapping) else {}
        window = entry.get("window") if isinstance(entry.get("window"), Mapping) else {}
        kwargs: dict[str, Any] = {
            axis: BaselineMeasure.from_document(axes.get(axis)) for axis in BASELINE_AXES
        }
        kwargs.update(
            window_start=str(window.get("start") or ""),
            window_end=str(window.get("end") or ""),
            method=str(entry.get("method") or ""),
        )
        return cls(**kwargs)


def customer_baseline_from_pilot_baseline(pilot_baseline: Any) -> CustomerBaseline:
    """Adapt a KIT :class:`~forge.adaptive.pilot.PilotBaseline` honestly.

    The kit baseline (one authored ``cycle_time_minutes`` over a window)
    is NOT a customer baseline: every axis comes out :data:`PROVENANCE_PENDING`
    with the kit's digest as the source note — the adapter never smuggles
    an authored number into the partner ladder as if it were observed.
    """
    note = (
        "adapted from a kit PilotBaseline (authored cycle-time) — carries NO observed "
        "partner measurement; every axis stays pending until the customer's workflow "
        "is measured"
    )
    return CustomerBaseline(
        window_start=getattr(pilot_baseline, "window_start", ""),
        window_end=getattr(pilot_baseline, "window_end", ""),
        method=note,
    )


# ---------------------------------------------------------------------------
# The narrow learning contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Precondition:
    """One named prerequisite outcome — met or unmet, with its reason.

    ``check`` names the predicate (``customer-named``, ``baseline-observed``,
    ``budget-remaining``, …); an unmet precondition carries the reason
    and, where one exists, the re-check or resolution path.
    """

    check: str
    met: bool
    reason: str = ""
    resolution: str = ""

    def as_document(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "met": self.met,
            "reason": self.reason,
            "resolution": self.resolution,
        }


@dataclass(frozen=True)
class LearningContract:
    """The narrow learning contract (``forge.partner-pilot.contract/1``).

    Everything R37-12 scope item 1 freezes BEFORE a partner task runs:
    task eligibility, acceptance (the independent oracle — customer
    acceptance separate from PR/harness/test success), available data,
    the spend cap, prohibited operations and the stop rules — plus the
    two fail-closed prerequisites the machinery cannot fabricate: the
    NAMED customer and the code-owner's task-set approval.

    :meth:`validate` checks the structure (a pending customer is valid —
    recruiting is the maintainer's act, not the machinery's).
    :meth:`validate_for_stage` is the fail-closed gate: the
    partner stages refuse while the customer, the task-set approval or
    the observed baseline is pending.
    """

    contract_id: str
    customer: Customer = field(default_factory=Customer)
    task_eligibility: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    available_data: tuple[str, ...] = ()
    prohibited_operations: tuple[str, ...] = ()
    stop_rules: tuple[str, ...] = ()
    spend_cap_usd: float = 0.0
    stage_spend_caps_usd: Mapping[str, float] = field(default_factory=dict)
    baseline: CustomerBaseline = field(default_factory=CustomerBaseline)
    task_set_approval: str = TASKSET_PENDING
    window_start: str = ""
    window_end: str = ""
    schema: str = CONTRACT_SCHEMA

    @property
    def frozen_digest(self) -> str:
        """sha256 over the canonical contract document — the freeze proof."""
        return _canonical_digest(self.as_document())

    def validate(self) -> None:
        if self.schema != CONTRACT_SCHEMA:
            raise PilotLadderError(f"contract schema {self.schema!r} is not {CONTRACT_SCHEMA!r}")
        if not self.contract_id.strip():
            raise PilotLadderError("contract needs a non-empty contract_id")
        self.customer.validate()
        for label, values in (
            ("task_eligibility", self.task_eligibility),
            ("acceptance", self.acceptance),
            ("available_data", self.available_data),
            ("prohibited_operations", self.prohibited_operations),
        ):
            if not values:
                raise PilotLadderError(f"contract needs a non-empty {label} list")
            if any(not str(value).strip() for value in values):
                raise PilotLadderError(f"contract {label} carries an empty entry")
        unknown_rules = [rule for rule in self.stop_rules if rule not in STOP_RULES]
        if unknown_rules:
            raise PilotLadderError(
                f"stop rules {unknown_rules} not in the closed vocabulary {list(STOP_RULES)}"
            )
        if not self.stop_rules:
            raise PilotLadderError("contract needs its stop rules (an unbounded pilot is drift)")
        if self.spend_cap_usd <= 0:
            raise PilotLadderError(
                "contract needs a positive spend cap (an uncapped paid pilot is not a pilot)"
            )
        for stage, cap in self.stage_spend_caps_usd.items():
            if stage not in LADDER_STAGES:
                raise PilotLadderError(
                    f"stage spend cap names unknown stage {stage!r}; the ladder is {list(LADDER_STAGES)}"
                )
            if cap <= 0 or cap > self.spend_cap_usd:
                raise PilotLadderError(
                    f"stage {stage} spend cap {cap} must be positive and within the "
                    f"contract cap {self.spend_cap_usd}"
                )
        self.baseline.validate()

    def stage_prerequisites(self, stage: str) -> tuple[Precondition, ...]:
        """The named prerequisites for *stage* — the fail-closed gate data.

        The ``contract`` stage needs only the validated contract.  The
        ``one-observed-task`` stage needs the validated contract plus a
        configured per-stage spend cap (the machinery leg may run on the
        lab, honestly bounded).  ``supervised-batch`` onward ADDS the
        partner prerequisites the machinery cannot fabricate: the NAMED
        customer, the code owner's task-set approval and the OBSERVED
        baseline.
        """
        if stage not in LADDER_STAGES:
            raise PilotLadderError(f"unknown stage {stage!r}; the ladder is {list(LADDER_STAGES)}")
        preconditions = [Precondition("contract-valid", True, "the contract validates")]
        if stage == STAGE_CONTRACT:
            return tuple(preconditions)
        if stage == STAGE_ONE_OBSERVED_TASK:
            cap = self.stage_spend_caps_usd.get(STAGE_ONE_OBSERVED_TASK)
            preconditions.append(
                Precondition(
                    "stage-spend-cap-configured",
                    cap is not None,
                    (
                        f"per-stage spend cap configured ({cap} USD)"
                        if cap is not None
                        else "no per-stage spend cap for one-observed-task — an uncapped "
                        "paid task is refused"
                    ),
                    "the runner refuses to start without a bounded per-stage cap",
                )
            )
            return tuple(preconditions)
        # The partner stages — fail closed on everything pending.
        preconditions.append(
            Precondition(
                "customer-named",
                not self.customer.is_pending,
                (
                    f"customer named: {self.customer.name}"
                    if not self.customer.is_pending
                    else "the customer is pending-recruitment — a NAMED external customer "
                    "is a prerequisite the machinery cannot fabricate"
                ),
                "recruit the design partner (maintainer's act), then re-freeze the contract",
            )
        )
        preconditions.append(
            Precondition(
                "task-set-approved",
                self.task_set_approval.strip().lower() != TASKSET_PENDING
                and bool(self.task_set_approval.strip()),
                (
                    f"task set approved by {self.task_set_approval}"
                    if self.task_set_approval.strip().lower() != TASKSET_PENDING
                    and self.task_set_approval.strip()
                    else "the task set awaits the named code owner's approval"
                ),
                "the named code owner approves the task set (an acceptance criterion, "
                "not a formality)",
            )
        )
        pending_axes = self.baseline.pending_axes
        preconditions.append(
            Precondition(
                "baseline-observed",
                self.baseline.all_observed,
                (
                    "the customer's baseline is observed on every axis"
                    if not pending_axes
                    else f"baseline axes still pending: {list(pending_axes)}"
                ),
                "measure the customer's ACTUAL workflow per axis (assisted coding, "
                "review, ops intervention, waiting) — an authored baseline is refused",
            )
        )
        return tuple(preconditions)

    def validate_for_stage(self, stage: str) -> None:
        """Fail CLOSED: raise listing every unmet prerequisite for *stage*."""
        unmet = [
            preconditions
            for preconditions in self.stage_prerequisites(stage)
            if not preconditions.met
        ]
        if unmet:
            lines = "\n".join(
                f"  - {entry.check}: {entry.reason} ({entry.resolution})" for entry in unmet
            )
            raise PilotLadderError(f"stage {stage!r} fails closed — unmet prerequisites:\n{lines}")

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "contract_id": self.contract_id,
            "customer": self.customer.as_document(),
            "task_eligibility": list(self.task_eligibility),
            "acceptance": list(self.acceptance),
            "available_data": list(self.available_data),
            "prohibited_operations": list(self.prohibited_operations),
            "stop_rules": list(self.stop_rules),
            "spend_cap_usd": self.spend_cap_usd,
            "stage_spend_caps_usd": dict(self.stage_spend_caps_usd),
            "baseline": self.baseline.as_document(),
            "task_set_approval": self.task_set_approval,
            "window": {"start": self.window_start, "end": self.window_end},
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> LearningContract:
        if not isinstance(doc, Mapping):
            raise PilotLadderError("contract document is not an object")
        window = doc.get("window") if isinstance(doc.get("window"), Mapping) else {}
        caps = doc.get("stage_spend_caps_usd")
        return cls(
            schema=str(doc.get("schema") or ""),
            contract_id=str(doc.get("contract_id") or ""),
            customer=Customer.from_document(doc.get("customer")),
            task_eligibility=tuple(str(entry) for entry in doc.get("task_eligibility") or ()),
            acceptance=tuple(str(entry) for entry in doc.get("acceptance") or ()),
            available_data=tuple(str(entry) for entry in doc.get("available_data") or ()),
            prohibited_operations=tuple(
                str(entry) for entry in doc.get("prohibited_operations") or ()
            ),
            stop_rules=tuple(str(rule) for rule in doc.get("stop_rules") or ()),
            spend_cap_usd=float(doc.get("spend_cap_usd") or 0.0),
            stage_spend_caps_usd=(
                {str(key): float(value) for key, value in caps.items()}
                if isinstance(caps, Mapping)
                else {}
            ),
            baseline=CustomerBaseline.from_document(doc.get("baseline")),
            task_set_approval=str(doc.get("task_set_approval") or TASKSET_PENDING),
            window_start=str(window.get("start") or ""),
            window_end=str(window.get("end") or ""),
        )

    @classmethod
    def load(cls, path: Path | str) -> LearningContract:
        contract = cls.from_document(json.loads(Path(path).read_text(encoding="utf-8")))
        contract.validate()
        return contract


# ---------------------------------------------------------------------------
# Stage records: what one stage execution actually observed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageReceipt:
    """One attempt's usage receipt — the #276 delivery-ledger shape."""

    attempt_id: str
    source: str
    spend_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_time_s: float | None = None
    tool_call_count: int | None = None

    @property
    def cost_state(self) -> str:
        return "known" if self.spend_usd is not None else "unknown"

    def validate(self) -> None:
        if not self.attempt_id.strip():
            raise PilotLadderError("usage receipt needs a non-empty attempt_id")
        if not self.source.strip():
            raise PilotLadderError(f"{self.attempt_id}: usage receipt needs a named source")
        if self.spend_usd is not None and self.spend_usd < 0:
            raise PilotLadderError(f"{self.attempt_id}: spend cannot be negative")

    def as_document(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "source": self.source,
            "spend_usd": self.spend_usd,
            "cost_state": self.cost_state,
            "cost_note": (
                "spend unknown — never zero" if self.spend_usd is None else "spend recorded"
            ),
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
            "model_time_s": self.model_time_s,
            "tool_call_count": self.tool_call_count,
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> UsageReceipt:
        if not isinstance(doc, Mapping):
            raise PilotLadderError("usage receipt document is not an object")
        spend = doc.get("spend_usd")
        return cls(
            attempt_id=str(doc.get("attempt_id") or ""),
            source=str(doc.get("source") or ""),
            spend_usd=float(spend) if spend is not None else None,
            input_tokens=int(doc["input_tokens"]) if doc.get("input_tokens") is not None else None,
            output_tokens=(
                int(doc["output_tokens"]) if doc.get("output_tokens") is not None else None
            ),
            model_time_s=(
                float(doc["model_time_s"]) if doc.get("model_time_s") is not None else None
            ),
            tool_call_count=(
                int(doc["tool_call_count"]) if doc.get("tool_call_count") is not None else None
            ),
        )


@dataclass(frozen=True)
class StageRecord:
    """One stage's record: every observable the stage produced.

    ``status`` is :data:`STATUS_EXECUTED` (work really ran), or one of
    the pending statuses whose ``unmet_preconditions`` name EXACTLY what
    blocks the stage and how to re-check it.  ``acceptance`` is the
    independent oracle's verdict — customer acceptance separate from PR
    creation, harness success and test success.  ``stage_complete``
    marks the record that satisfies the stage's completion contract.
    """

    stage: str
    status: str
    recorded_at: str
    task_refs: tuple[str, ...] = ()
    acceptance: Mapping[str, Any] = field(default_factory=dict)
    attempts: tuple[Mapping[str, Any], ...] = ()
    manual_rescues: int = 0
    rescue_notes: tuple[str, ...] = ()
    abandoned_tasks: int = 0
    timings: Mapping[str, float] = field(default_factory=dict)
    usage_receipts: tuple[UsageReceipt, ...] = ()
    unmet_preconditions: tuple[Precondition, ...] = ()
    notes: tuple[str, ...] = ()
    stage_complete: bool = False
    schema: str = STAGE_RECORD_SCHEMA

    def validate(self) -> None:
        if self.schema != STAGE_RECORD_SCHEMA:
            raise PilotLadderError(
                f"stage record schema {self.schema!r} is not {STAGE_RECORD_SCHEMA!r}"
            )
        if self.stage not in LADDER_STAGES:
            raise PilotLadderError(f"stage record names unknown stage {self.stage!r}")
        if self.status not in (STATUS_EXECUTED, STATUS_PENDING_LAB, STATUS_PENDING_PARTNER):
            raise PilotLadderError(f"stage record status {self.status!r} is not a known status")
        _parse_datetime(self.recorded_at)
        if self.manual_rescues < 0 or self.abandoned_tasks < 0:
            raise PilotLadderError(f"{self.stage}: counters cannot be negative")
        if len(self.rescue_notes) > self.manual_rescues:
            raise PilotLadderError(
                f"{self.stage}: more rescue notes ({len(self.rescue_notes)}) than "
                f"rescues ({self.manual_rescues})"
            )
        for name in ("setup_minutes", "wait_minutes", "review_minutes"):
            value = self.timings.get(name)
            if value is not None and value < 0:
                raise PilotLadderError(f"{self.stage}: timing {name} cannot be negative")
        for receipt in self.usage_receipts:
            receipt.validate()
        if self.status != STATUS_EXECUTED:
            if not self.unmet_preconditions:
                raise PilotLadderError(
                    f"{self.stage}: a {self.status} record needs its unmet preconditions — "
                    "the exact blockers, re-checkable, never a bare 'later'"
                )
            if self.stage_complete:
                raise PilotLadderError(
                    f"{self.stage}: a {self.status} record cannot mark the stage complete — "
                    "pending is pending"
                )
        if self.stage_complete and self.status != STATUS_EXECUTED:
            raise PilotLadderError("only an executed record can complete a stage")

    @property
    def spend_usd(self) -> float | None:
        """The record's known spend; ``None`` while any receipt is unknown."""
        total = 0.0
        for receipt in self.usage_receipts:
            if receipt.spend_usd is None:
                return None
            total += receipt.spend_usd
        return round(total, 6) if self.usage_receipts else None

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "stage": self.stage,
            "status": self.status,
            "recorded_at": self.recorded_at,
            "task_refs": list(self.task_refs),
            "acceptance": dict(self.acceptance),
            "attempts": [dict(attempt) for attempt in self.attempts],
            "manual_rescues": self.manual_rescues,
            "rescue_notes": list(self.rescue_notes),
            "abandoned_tasks": self.abandoned_tasks,
            "timings": dict(self.timings),
            "usage_receipts": [receipt.as_document() for receipt in self.usage_receipts],
            "spend_usd": self.spend_usd,
            "unmet_preconditions": [entry.as_document() for entry in self.unmet_preconditions],
            "notes": list(self.notes),
            "stage_complete": self.stage_complete,
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> StageRecord:
        if not isinstance(doc, Mapping):
            raise PilotLadderError("stage record document is not an object")
        acceptance = doc.get("acceptance")
        attempts = doc.get("attempts")
        timings = doc.get("timings")
        unmet = doc.get("unmet_preconditions")
        return cls(
            schema=str(doc.get("schema") or ""),
            stage=str(doc.get("stage") or ""),
            status=str(doc.get("status") or ""),
            recorded_at=str(doc.get("recorded_at") or ""),
            task_refs=tuple(str(ref) for ref in doc.get("task_refs") or ()),
            acceptance=dict(acceptance) if isinstance(acceptance, Mapping) else {},
            attempts=tuple(
                dict(entry) if isinstance(entry, Mapping) else {} for entry in attempts or ()
            ),
            manual_rescues=int(doc.get("manual_rescues") or 0),
            rescue_notes=tuple(str(note) for note in doc.get("rescue_notes") or ()),
            abandoned_tasks=int(doc.get("abandoned_tasks") or 0),
            timings=dict(timings) if isinstance(timings, Mapping) else {},
            usage_receipts=tuple(
                UsageReceipt.from_document(entry) for entry in doc.get("usage_receipts") or ()
            ),
            unmet_preconditions=tuple(
                Precondition(
                    check=str(entry.get("check") or ""),
                    met=bool(entry.get("met") or False),
                    reason=str(entry.get("reason") or ""),
                    resolution=str(entry.get("resolution") or ""),
                )
                for entry in unmet or ()
                if isinstance(entry, Mapping)
            ),
            notes=tuple(str(note) for note in doc.get("notes") or ()),
            stage_complete=bool(doc.get("stage_complete") or False),
        )


# ---------------------------------------------------------------------------
# The decision review
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineeringFeasibility:
    """The engineering-feasibility half of the decision — TYPED, alone.

    Does the supported workflow actually do the work on the qualified
    profile (tasks completed, oracle acceptance, bounded spend)?  This
    is a statement about the MACHINE, judged on the ladder's records.
    """

    verdict: str = ""
    statement: str = ""
    evidence_pointers: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.verdict not in FEASIBILITY_VERDICTS:
            raise PilotLadderError(
                f"feasibility verdict {self.verdict!r} not in {list(FEASIBILITY_VERDICTS)}"
            )
        if not self.statement.strip():
            raise PilotLadderError("the feasibility assessment needs its statement")

    def as_document(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "statement": self.statement,
            "evidence_pointers": list(self.evidence_pointers),
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> EngineeringFeasibility:
        entry = dict(doc or {})
        return cls(
            verdict=str(entry.get("verdict") or ""),
            statement=str(entry.get("statement") or ""),
            evidence_pointers=tuple(
                str(pointer) for pointer in entry.get("evidence_pointers") or ()
            ),
        )


@dataclass(frozen=True)
class AdoptionAssessment:
    """The adoption-willingness half — TYPED, separate, never blended.

    Does the CUSTOMER want to keep using or paying for the workflow?
    This is a statement about the human decision, and while no partner
    exists the only honest verdict is
    :data:`ADOPTION_UNKNOWN_PENDING_PARTNER` — nobody has been asked.
    """

    verdict: str = ""
    statement: str = ""
    evidence_pointers: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.verdict not in ADOPTION_VERDICTS:
            raise PilotLadderError(
                f"adoption verdict {self.verdict!r} not in {list(ADOPTION_VERDICTS)}"
            )
        if not self.statement.strip():
            raise PilotLadderError("the adoption assessment needs its statement")

    def as_document(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "statement": self.statement,
            "evidence_pointers": list(self.evidence_pointers),
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> AdoptionAssessment:
        entry = dict(doc or {})
        return cls(
            verdict=str(entry.get("verdict") or ""),
            statement=str(entry.get("statement") or ""),
            evidence_pointers=tuple(
                str(pointer) for pointer in entry.get("evidence_pointers") or ()
            ),
        )


@dataclass(frozen=True)
class DecisionReview:
    """The decision record: continue / narrow / redesign / stop.

    The two assessments are separate typed fields — engineering
    feasibility and adoption willingness — because a workflow can be
    feasible and unwanted, or wanted and not yet feasible; blending
    them into one verdict is the lie R37-12 acceptance criterion 6
    forbids.  While the customer is pending, the adoption verdict is
    FORCED to :data:`ADOPTION_UNKNOWN_PENDING_PARTNER` (validated, not
    trusted).  ``template()`` produces the UNFILLED shape the report
    ships — an honest empty form, never an invented verdict.
    """

    decision: str = ""
    reasons: tuple[str, ...] = ()
    evidence_pointers: tuple[str, ...] = ()
    feasibility: EngineeringFeasibility = field(default_factory=EngineeringFeasibility)
    adoption: AdoptionAssessment = field(default_factory=AdoptionAssessment)
    decided_at: str = ""
    decided_by: str = ""
    template: bool = False

    @classmethod
    def unfilled_template(cls) -> DecisionReview:
        """The unfilled decision-review template the report ships."""
        return cls(
            template=True,
            reasons=(
                "UNFILLED — the decision review happens at the ladder's decision point; "
                "no verdict is invented before the evidence exists",
            ),
        )

    def validate(self, *, customer_pending: bool | None = None) -> None:
        if self.template:
            if self.decision.strip():
                raise PilotLadderError("an unfilled decision-review template carries no decision")
            return
        if self.decision not in DECISIONS:
            raise PilotLadderError(
                f"decision {self.decision!r} not in {list(DECISIONS)} (continue/narrow/redesign/stop)"
            )
        if not self.reasons:
            raise PilotLadderError("a decision review needs its reasons — never a bare verdict")
        self.feasibility.validate()
        self.adoption.validate()
        pending = self.adoption.verdict == ADOPTION_UNKNOWN_PENDING_PARTNER
        if pending and customer_pending is False:
            raise PilotLadderError(
                "the customer is named — an unknown-pending-partner adoption verdict is "
                "no longer honest; ask the customer"
            )
        if not pending and customer_pending is True:
            raise PilotLadderError(
                "the customer is pending-recruitment — the only honest adoption verdict "
                f"is {ADOPTION_UNKNOWN_PENDING_PARTNER!r} (nobody has been asked)"
            )
        if not self.decided_by.strip():
            raise PilotLadderError("a filled decision review names its decider")

    def as_document(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "filled": not self.template,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "evidence_pointers": list(self.evidence_pointers),
            "engineering_feasibility": self.feasibility.as_document(),
            "adoption_willingness": self.adoption.as_document(),
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "note": (
                "feasibility and adoption are separate typed verdicts — a workflow can be "
                "feasible and unwanted; the review never blends them"
            ),
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> DecisionReview:
        if not isinstance(doc, Mapping):
            raise PilotLadderError("decision review document is not an object")
        return cls(
            decision=str(doc.get("decision") or ""),
            reasons=tuple(str(reason) for reason in doc.get("reasons") or ()),
            evidence_pointers=tuple(str(pointer) for pointer in doc.get("evidence_pointers") or ()),
            feasibility=EngineeringFeasibility.from_document(doc.get("engineering_feasibility")),
            adoption=AdoptionAssessment.from_document(doc.get("adoption_willingness")),
            decided_at=str(doc.get("decided_at") or ""),
            decided_by=str(doc.get("decided_by") or ""),
            template=bool(doc.get("template") or False),
        )


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """One gate's verdict: allowed, or blocked with named reasons."""

    stage: str
    allowed: bool
    stop_reason: str = ""
    unmet: tuple[Precondition, ...] = ()

    def as_document(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "allowed": self.allowed,
            "stop_reason": self.stop_reason,
            "unmet": [entry.as_document() for entry in self.unmet],
        }


@dataclass(frozen=True)
class _StopState:
    """The tripped stop rule with its preserved diagnostics."""

    rule: str
    reason: str
    tripped_at: str
    diagnostics_digest: str
    preserved_state: Mapping[str, Any]


class PilotLadder:
    """The staged contract-executor: gates, records, stop rules, replay.

    The state is persisted as a versioned document
    (:meth:`state_document` / :meth:`from_document`) and REPLAYS
    deterministically: rebuilding from the document and re-emitting it
    produces byte-identical bytes — the same proof discipline as the
    kit's report.
    """

    def __init__(
        self,
        contract: LearningContract,
        *,
        ladder_id: str = "",
        records: Mapping[str, tuple[StageRecord, ...]] | None = None,
        stop: _StopState | None = None,
        decision_review: DecisionReview | None = None,
        created_at: str = "",
        updated_at: str = "",
    ) -> None:
        contract.validate()
        self._contract = contract
        self._ladder_id = ladder_id or contract.contract_id
        self._records: dict[str, list[StageRecord]] = {stage: [] for stage in LADDER_STAGES}
        for stage, stage_records in (records or {}).items():
            self._records[stage] = list(stage_records)
        self._stop = stop
        self._decision_review = decision_review
        self._created_at = created_at or _now_iso()
        self._updated_at = updated_at or self._created_at

    # -- read-only surface -------------------------------------------------

    @property
    def contract(self) -> LearningContract:
        return self._contract

    @property
    def contract_digest(self) -> str:
        return self._contract.frozen_digest

    @property
    def ladder_id(self) -> str:
        return self._ladder_id

    @property
    def stop_reason(self) -> str:
        """The tripped stop rule as one honest string ("" while clear)."""
        if self._stop is None:
            return ""
        return f"{self._stop.rule}: {self._stop.reason}"

    @property
    def stop(self) -> _StopState | None:
        return self._stop

    @property
    def decision_review(self) -> DecisionReview | None:
        return self._decision_review

    @property
    def records(self) -> dict[str, tuple[StageRecord, ...]]:
        return {stage: tuple(stage_records) for stage, stage_records in self._records.items()}

    def stage_records(self, stage: str) -> tuple[StageRecord, ...]:
        if stage not in LADDER_STAGES:
            raise PilotLadderError(f"unknown stage {stage!r}; the ladder is {list(LADDER_STAGES)}")
        return tuple(self._records[stage])

    def stage_status(self, stage: str) -> str:
        """``not-started | in-progress | pending-lab | pending-partner | complete``.

        The status is derived from the records: an executed record that
        marked the stage complete completes it; otherwise the latest
        record's pending status names what blocks it.
        """
        stage_records = self.stage_records(stage)
        if not stage_records:
            return "not-started"
        if any(record.stage_complete for record in stage_records):
            return "complete"
        latest = stage_records[-1]
        if latest.status == STATUS_PENDING_LAB:
            return STATUS_PENDING_LAB
        if latest.status == STATUS_PENDING_PARTNER:
            return STATUS_PENDING_PARTNER
        return "in-progress"

    @property
    def current_stage(self) -> str:
        """The earliest stage that is not complete."""
        for stage in LADDER_STAGES:
            if self.stage_status(stage) != "complete":
                return stage
        return LADDER_STAGES[-1]

    @property
    def spend_usd(self) -> float:
        """Known spend across every executed record's receipts."""
        total = 0.0
        for stage_records in self._records.values():
            for record in stage_records:
                if record.spend_usd is not None:
                    total += record.spend_usd
        return round(total, 6)

    @property
    def budget_remaining_usd(self) -> float:
        return round(self._contract.spend_cap_usd - self.spend_usd, 6)

    # -- the gate ----------------------------------------------------------

    def gate(self, stage: str) -> GateDecision:
        """Whether *stage* may START — checked, never trusted.

        Order matters, worst first: a tripped stop rule refuses before
        anything else (diagnostics stay preserved); then the ladder
        order (every earlier stage complete); then the contract's
        per-stage prerequisites (fail-closed on the partner stages);
        then the budget state (a cap already reached blocks the next
        paid batch automatically).
        """
        if self._stop is not None:
            return GateDecision(
                stage=stage,
                allowed=False,
                stop_reason=self.stop_reason,
                unmet=(
                    Precondition(
                        check="stop-rules-clear",
                        met=False,
                        reason=self.stop_reason,
                        resolution="the preserved diagnostics are retained verbatim; a "
                        "stopped ladder is never silently retried — start a new ladder",
                    ),
                ),
            )
        unmet: list[Precondition] = []
        for earlier in LADDER_STAGES[: LADDER_STAGES.index(stage)]:
            status = self.stage_status(earlier)
            if status != "complete":
                unmet.append(
                    Precondition(
                        check=f"stage-{earlier}-complete",
                        met=False,
                        reason=f"stage {earlier!r} is {status} — the ladder advances in order",
                        resolution=f"complete stage {earlier!r} first",
                    )
                )
        unmet.extend(
            [entry for entry in self._contract.stage_prerequisites(stage) if not entry.met]
        )
        if self.spend_usd >= self._contract.spend_cap_usd:
            unmet.append(
                Precondition(
                    check="budget-remaining",
                    met=False,
                    reason=(
                        f"spend {self.spend_usd} USD reached the contract cap "
                        f"{self._contract.spend_cap_usd} USD — no next paid batch starts"
                    ),
                    resolution="the budget stop rule governs; a cap increase is a NEW contract",
                )
            )
        return GateDecision(stage=stage, allowed=not unmet, unmet=tuple(unmet))

    # -- recording ---------------------------------------------------------

    def record_stage(self, record: StageRecord) -> None:
        """Record one stage outcome, enforcing the honest invariants.

        A pending record (``pending-lab`` / ``pending-partner``) is the
        honest state marker — it never completes a stage.  An EXECUTED
        record must have passed the stage's gate; an executed record
        whose spend would exceed the contract cap trips the budget stop
        rule (the record still lands — the overrun is evidence, never a
        dropped row).
        """
        record.validate()
        if self._stop is not None and record.status == STATUS_EXECUTED:
            raise PilotLadderError(
                f"cannot record an executed {record.stage} outcome: the ladder stopped "
                f"({self.stop_reason}) — the state is preserved, never silently retried"
            )
        if record.status == STATUS_EXECUTED:
            decision = self.gate(record.stage)
            if not decision.allowed:
                lines = "\n".join(f"  - {entry.check}: {entry.reason}" for entry in decision.unmet)
                raise PilotLadderError(
                    f"stage {record.stage!r} may not execute — the gate refuses:\n{lines}"
                )
        self._records[record.stage].append(record)
        self._updated_at = _now_iso()
        if record.status == STATUS_EXECUTED:
            self.check_budget()
        if record.stage_complete:
            self._check_stop_rule_outcomes(record)

    def _check_stop_rule_outcomes(self, record: StageRecord) -> None:
        """Stop-rule outcomes ride on the record's acceptance document."""
        outcome = str(record.acceptance.get("stop_rule_outcome") or "")
        if outcome and outcome in STOP_RULES and outcome != STOP_BUDGET_OVERRUN:
            self.trip_stop(
                outcome,
                str(record.acceptance.get("stop_rule_detail") or "the stage record named the rule"),
            )

    def check_budget(self) -> bool:
        """Trip the budget stop rule when recorded spend passed the cap.

        Returns whether the budget is still clear.  The diagnostics are
        preserved verbatim at trip time.
        """
        if self._stop is not None:
            return False
        if self.spend_usd > self._contract.spend_cap_usd:
            self.trip_stop(
                STOP_BUDGET_OVERRUN,
                f"recorded spend {self.spend_usd} USD exceeded the contract cap "
                f"{self._contract.spend_cap_usd} USD — no next paid batch starts automatically",
            )
            return False
        return True

    def trip_stop(self, rule: str, reason: str = "") -> _StopState:
        """Trip a stop rule: preserve diagnostics verbatim and halt.

        Idempotent-by-retention: the FIRST tripped rule wins and its
        preserved state is final — a later trip attempt returns the
        original stop (the state at the first trip is the evidence).
        """
        if self._stop is not None:
            return self._stop
        if rule not in STOP_RULES:
            raise PilotLadderError(
                f"stop rule {rule!r} not in the closed vocabulary {list(STOP_RULES)}"
            )
        if not reason.strip():
            raise PilotLadderError(f"stop rule {rule!r} needs its reason (never a bare halt)")
        preserved = self.state_document()
        self._stop = _StopState(
            rule=rule,
            reason=reason.strip(),
            tripped_at=_now_iso(),
            diagnostics_digest=_canonical_digest(preserved),
            preserved_state=preserved,
        )
        self._updated_at = self._stop.tripped_at
        return self._stop

    def record_decision_review(self, review: DecisionReview) -> None:
        """Attach the decision review (validated against the customer state)."""
        review.validate(customer_pending=self._contract.customer.is_pending)
        self._decision_review = review
        self._updated_at = _now_iso()

    # -- observability -----------------------------------------------------

    def observability_gauges(self) -> dict[str, Any]:
        """The issue's named observability, folded from the records.

        ``pilot.customer_review_minutes`` is honestly ``None`` (unknown,
        never zero) until a partner review exists — a lab operator's
        minutes are not customer minutes.
        """
        executed = [
            record
            for stage_records in self._records.values()
            for record in stage_records
            if record.status == STATUS_EXECUTED
        ]
        partner_records = [record for record in executed if record.stage in _PARTNER_STAGES]
        review_minutes: float | None = None
        if partner_records:
            review_minutes = round(
                sum(
                    float(record.timings.get("review_minutes") or 0.0) for record in partner_records
                ),
                4,
            )
        review = self._decision_review
        # accepted TASKS — the contract stage's acceptance is not a task; a
        # record without task refs never counts here.
        task_records = [record for record in executed if record.task_refs]
        return {
            "pilot.accepted_tasks": sum(
                1 for record in task_records if record.acceptance.get("accepted") is True
            ),
            "pilot.manual_rescue_count": sum(record.manual_rescues for record in executed),
            "pilot.customer_review_minutes": review_minutes,
            "pilot.abandoned_tasks": sum(record.abandoned_tasks for record in executed),
            "pilot.stop_reason": self.stop_reason,
            "pilot.continuation_decision": (
                review.decision if review is not None and not review.template else "pending-review"
            ),
        }

    # -- persistence + replay ----------------------------------------------

    def state_document(self) -> dict[str, Any]:
        stop_document = None
        if self._stop is not None:
            stop_document = {
                "rule": self._stop.rule,
                "reason": self._stop.reason,
                "tripped_at": self._stop.tripped_at,
                "diagnostics_digest": self._stop.diagnostics_digest,
                "note": "the preserved state is retained verbatim below and never retried",
                "preserved_state": dict(self._stop.preserved_state),
            }
        return {
            "schema": LADDER_SCHEMA,
            "ladder_id": self._ladder_id,
            "contract_digest": self.contract_digest,
            "contract": self._contract.as_document(),
            "created_at": self._created_at,
            "updated_at": self._updated_at,
            "stages": {
                stage: {
                    "status": self.stage_status(stage),
                    "records": [record.as_document() for record in self._records[stage]],
                }
                for stage in LADDER_STAGES
            },
            "spend": {
                "recorded_usd": self.spend_usd,
                "cap_usd": self._contract.spend_cap_usd,
                "remaining_usd": self.budget_remaining_usd,
            },
            "stop": stop_document,
            "decision_review": (
                self._decision_review.as_document() if self._decision_review is not None else None
            ),
            "observability": self.observability_gauges(),
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> PilotLadder:
        """Rebuild a ladder from its state document — the replay entry point.

        The rebuilt ladder re-emits a byte-identical document (the
        determinism proof): every field round-trips, including a tripped
        stop's preserved diagnostics.
        """
        if not isinstance(doc, Mapping):
            raise PilotLadderError("ladder state document is not an object")
        if str(doc.get("schema") or "") != LADDER_SCHEMA:
            raise PilotLadderError(f"ladder state schema is not {LADDER_SCHEMA!r}")
        contract = LearningContract.from_document(doc.get("contract") or {})
        contract.validate()
        digest = str(doc.get("contract_digest") or "")
        if digest and digest != contract.frozen_digest:
            raise PilotLadderError(
                f"state is bound to a different contract ({digest[:12]}… != "
                f"{contract.frozen_digest[:12]}…)"
            )
        stages = doc.get("stages") if isinstance(doc.get("stages"), Mapping) else {}
        records: dict[str, tuple[StageRecord, ...]] = {
            stage: tuple(
                StageRecord.from_document(entry)
                for entry in (stages.get(stage) or {}).get("records") or ()
                if isinstance(stages.get(stage), Mapping)
            )
            for stage in LADDER_STAGES
            if isinstance(stages.get(stage), Mapping)
        }
        stop: _StopState | None = None
        stop_doc = doc.get("stop")
        if isinstance(stop_doc, Mapping):
            stop = _StopState(
                rule=str(stop_doc.get("rule") or ""),
                reason=str(stop_doc.get("reason") or ""),
                tripped_at=str(stop_doc.get("tripped_at") or ""),
                diagnostics_digest=str(stop_doc.get("diagnostics_digest") or ""),
                preserved_state=dict(stop_doc.get("preserved_state") or {}),
            )
        review_doc = doc.get("decision_review")
        ladder = cls(
            contract,
            ladder_id=str(doc.get("ladder_id") or contract.contract_id),
            records=records,
            stop=stop,
            decision_review=(
                DecisionReview.from_document(review_doc)
                if isinstance(review_doc, Mapping)
                else None
            ),
            created_at=str(doc.get("created_at") or ""),
            updated_at=str(doc.get("updated_at") or ""),
        )
        return ladder


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


#: The standing prerequisite the report always states — the thing only
#: the maintainer can do, which no amount of machinery substitutes for.
EXTERNAL_PARTNER_PREREQUISITE = (
    "A NAMED external design partner (buyer/user + code owner) is the prerequisite the "
    "machinery cannot fabricate: recruiting one is the maintainer's act. Until then the "
    "customer field stays honestly pending-recruitment, the partner stages fail closed, "
    "and no customer outcome is claimed."
)


def _honest_status(ladder: PilotLadder) -> dict[str, Any]:
    """The ladder's honest one-look status for the report."""
    stage_statuses = {stage: ladder.stage_status(stage) for stage in LADDER_STAGES}
    one_observed = ladder.stage_records(STAGE_ONE_OBSERVED_TASK)
    executed = [record for record in one_observed if record.status == STATUS_EXECUTED]
    pendings = [record for record in one_observed if record.status != STATUS_EXECUTED]
    if executed:
        latest = executed[-1]
        status = "stage-1-executed"
        detail = (
            f"one observed task executed at {latest.recorded_at}; acceptance "
            f"{latest.acceptance.get('accepted')!r} by the independent oracle"
        )
    elif pendings:
        latest = pendings[-1]
        status = f"stage-1-{latest.status}"
        detail = (
            "; ".join(f"{entry.check}: {entry.reason}" for entry in latest.unmet_preconditions)
            or latest.status
        )
    else:
        status = "stage-1-not-started"
        detail = "the one-observed-task stage has no record yet"
    return {
        "ladder_statuses": stage_statuses,
        "stage_one_status": status,
        "stage_one_detail": detail,
        "external_partner_prerequisite": EXTERNAL_PARTNER_PREREQUISITE,
        "customer_state": (
            CUSTOMER_PENDING
            if ladder.contract.customer.is_pending
            else ladder.contract.customer.name
        ),
        "note": (
            "the honest maximum executable now: the staged machinery, the measurement "
            "format and the decision record; stage one validated against the lab; the "
            "partner stages are the explicitly-recorded next step"
        ),
    }


def build_partner_pilot_report(
    ladder: PilotLadder,
    *,
    decision_review: DecisionReview | None = None,
) -> dict[str, Any]:
    """Build the partner-pilot report from the ladder state.

    The report carries: the ladder state (statuses, spend vs cap, stop),
    every stage record, the honest status (stage-1 executed or pending
    with its exact unmet preconditions), the external-partner
    prerequisite stated verbatim, the observability gauges, and the
    decision review — the UNFILLED template when none exists.  The build
    is deterministic over the ladder state: no clocks, no randomness.
    """
    review = decision_review or ladder.decision_review or DecisionReview.unfilled_template()
    review.validate(customer_pending=ladder.contract.customer.is_pending)
    state = ladder.state_document()
    return {
        "schema": REPORT_SCHEMA,
        "ladder_id": ladder.ladder_id,
        "contract": {
            "contract_id": ladder.contract.contract_id,
            "digest": ladder.contract_digest,
            "customer": ladder.contract.customer.as_document(),
            "spend_cap_usd": ladder.contract.spend_cap_usd,
            "stop_rules": list(ladder.contract.stop_rules),
        },
        "honest_status": _honest_status(ladder),
        "ladder_state": state,
        "stage_records": {
            stage: [record.as_document() for record in ladder.stage_records(stage)]
            for stage in LADDER_STAGES
        },
        "observability": ladder.observability_gauges(),
        "decision_review": review.as_document(),
        "limitations": [
            "No named external design partner exists yet — the customer is "
            "pending-recruitment and the partner stages fail closed; no customer "
            "outcome, demand signal or willingness is claimed.",
            "The one-observed-task stage may run on the forge LAB with the real model "
            "as the machinery-validation leg — that validates the MACHINERY, not "
            "customer demand.",
            "The customer baseline requires OBSERVED measurement on all four axes; an "
            "authored baseline is refused outright for this ladder.",
            "Acceptance is the independent oracle's verdict — separate from PR "
            "creation, harness success and test success.",
            "A tripped stop rule preserves diagnostics verbatim; a stopped ladder is "
            "never silently retried.",
        ],
    }
