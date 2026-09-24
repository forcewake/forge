"""R32-21 — the bounded design-partner pilot kit (research topic 04).

The 2026 dev-tool playbook this module operationalizes is unusually
converged, and converged on two things that shape every class here:

- **A pilot is a one-page LEARNING CONTRACT, not a feature trial**: paid,
  4–8 weeks, one business slice with ONE writable repo first, a FROZEN
  pre-pilot baseline, a staged capability ladder (read/plan → small
  fixes → branch+PR → gate), and **promotion decided by ≥ 2 of 3
  pre-agreed measurable criteria by a named end date** — with exactly
  three exits: expand, extend-once-for-a-NAMED-gap, stop
  (:class:`PilotSpec`, :class:`AcceptanceCriterion`).
- **PR counts and acceptance rates are documented liars** for agent
  fleets.  The five metrics that hold up — autonomy rate, cost per
  merged PR, defect/rollback rate, intervention rate COUNTING steering,
  cycle time vs the frozen baseline — plus reviewer load as the explicit
  capacity tax, are the only numbers this kit computes
  (:class:`PilotTracker`, :class:`PilotMetrics`; every definition is
  pinned in :data:`METRIC_DEFINITIONS` and rides the report).

Honesty rules that are structural, not aspirational:

- The unsupported-features list is part of the spec and snapshotted by
  :func:`record_onboarding` BEFORE any task runs; the report REFUSES an
  onboarding timestamp that is not strictly earlier than the first task
  start (the ordering is checked, never trusted).
- The measurement window is frozen pre-kickoff (the J-curve
  pre-commitment): once a task is recorded the spec cannot be retargeted
  — :meth:`PilotTracker.retarget_spec` refuses — and
  :func:`evaluate_stop` never judges the criteria before the named end
  date ("no judging during the dip").
- A stop PRESERVES the tracker snapshot verbatim
  (:meth:`PilotTracker.preserve_diagnostics`); a preserved pilot refuses
  new task records — the state is retained for reading, NEVER silently
  retried.
- Unaccepted tasks and operator rescues appear EXPLICITLY in the report;
  all-attempt spend (unaccepted attempts included) is the only cost
  denominator, and a missing spend degrades the cost to unknown — never
  zero.

This module is scaffolding for running a pilot with a real design
partner: it never contacts a harness, a repo, or a model.  The shipped
example under ``evaluation/pilot/`` is an example plan, not a claim that
a pilot ran.  See ``docs/evaluation/2026-09-23-pilot-kit/README.md``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "CRITERIA_REQUIRED",
    "CRITERION_METRICS",
    "DECISION_CONTINUE",
    "DECISION_EXPAND",
    "DECISION_EXTEND_ONCE",
    "DECISION_STOP",
    "DECISIONS",
    "EXIT_EXPAND",
    "EXIT_EXTEND_ONCE",
    "EXIT_STOP",
    "INTERVENTION_KINDS",
    "LIMITATIONS",
    "MAX_PILOT_TASKS",
    "METRIC_DEFINITIONS",
    "MIN_PILOT_TASKS",
    "PLAN_SCHEMA",
    "PILOT_DECISION_DATE_NOTE",
    "REPORT_SCHEMA",
    "SCENARIO_TAGS",
    "SPEC_SCHEMA",
    "STAGES",
    "STAGE_BRANCH_PR",
    "STAGE_GATE",
    "STAGE_READ_PLAN",
    "STAGE_SMALL_FIXES",
    "VIOLATION_AUTO_MERGE",
    "VIOLATION_PRODUCTION_SECRET",
    "VIOLATION_SCOPE",
    "VIOLATION_KINDS",
    "AcceptanceCriterion",
    "AttemptUsage",
    "DataBoundary",
    "ExitsPolicy",
    "ExtensionRecord",
    "InterventionEvent",
    "LadderStage",
    "OnboardingRecord",
    "PilotBaseline",
    "PilotDiagnostics",
    "PilotError",
    "PilotMetrics",
    "PilotPlan",
    "PilotReport",
    "PilotSpec",
    "PilotTask",
    "PilotTracker",
    "StopDecision",
    "TaskRecord",
    "VerificationCheck",
    "build_pilot_report",
    "evaluate_stop",
    "record_blocked_task",
    "record_onboarding",
    "task_record_from_document",
    "tracker_from_snapshot",
    "usage_ledger_document",
]

#: The schema stamp of a pilot spec document (the one-page learning
#: contract).  Versioned: bumping the tag is how a breaking change to
#: what a pilot MEANS stays distinguishable from pilots published under
#: the old shape.
SPEC_SCHEMA = "forge.pilot.spec/1"

#: The schema stamp of a pilot plan document (spec + the bounded task set).
PLAN_SCHEMA = "forge.pilot.plan/1"

#: The schema stamp of the report artifact a replayed pilot produces.
REPORT_SCHEMA = "forge.pilot.report/1"

#: The staged capability ladder — read/plan → small fixes → branch+PR →
#: gate.  Full autonomy from week 1 is the documented failure mode; the
#: ladder is the fix, and the stages are the report's capability axes
#: ("ready to support" vs "remains experimental" is decided PER STAGE).
STAGE_READ_PLAN = "read_plan"
STAGE_SMALL_FIXES = "small_fixes"
STAGE_BRANCH_PR = "branch_pr"
STAGE_GATE = "gate"
STAGES: tuple[str, ...] = (STAGE_READ_PLAN, STAGE_SMALL_FIXES, STAGE_BRANCH_PR, STAGE_GATE)

#: The closed vocabulary of pilot task scenarios.  Every pilot plan must
#: exercise EVERY tag at least once — a pilot that never probes the
#: unhappy paths (runner loss, resource revocation, human intervention,
#: the real adaptive control path) measures only the demo path.
SCENARIO_TAGS: tuple[str, ...] = (
    "missing_requirement",
    "neighbor_evidence",
    "test_repair",
    "human_intervention",
    "runner_loss",
    "ambiguous_task",
    "resource_revocation",
    "cold_reinstall",
    "adaptive_control",
)

#: The closed vocabulary of criterion metrics — the five that hold up
#: for agent fleets.  PR counts and acceptance rates are deliberately
#: absent: they are documented liars (topic 04 §4) and never criteria.
CRITERION_METRICS: tuple[str, ...] = (
    "autonomy_rate",
    "cost_per_accepted_usd",
    "defect_rollback_rate",
    "intervention_rate",
    "cycle_time_vs_baseline",
)

#: The closed vocabulary of intervention kinds.  PARTIAL steering counts
#: as an intervention exactly like a full takeover — "count steering
#: events, not just takeovers" is forge's own /steer semantics made a
#: metric rule.
INTERVENTION_KINDS: tuple[str, ...] = (
    "steer",
    "pause",
    "resume",
    "cancel",
    "takeover",
    "question_answer",
)

#: The named end date is the decision date: criteria are judged when the
#: evaluation clock reaches the LATEST criterion end date, never during
#: the adjustment dip (the J-curve pre-commitment).
PILOT_DECISION_DATE_NOTE = (
    "The measurement window is fixed in the spec BEFORE kickoff; criteria are "
    "judged only at the named end date — never during the expected early dip."
)

#: The exits vocabulary: expand / extend-once-for-a-named-gap / stop.
#: Free open-ended pilots hide urgency; these three are the only ways
#: out, each with documented learning.
EXIT_EXPAND = "expand"
EXIT_EXTEND_ONCE = "extend_once"
EXIT_STOP = "stop"

#: The stop-condition decision vocabulary (the 2-of-3 rule's outcomes).
DECISION_CONTINUE = "continue"
DECISION_EXTEND_ONCE = "extend_once"
DECISION_EXPAND = "expand"
DECISION_STOP = "stop"
DECISIONS: tuple[str, ...] = (
    DECISION_CONTINUE,
    DECISION_EXTEND_ONCE,
    DECISION_EXPAND,
    DECISION_STOP,
)

#: The violation kinds that can end a pilot immediately.
VIOLATION_SCOPE = "scope_violation"
VIOLATION_AUTO_MERGE = "auto_merge_attempt"
VIOLATION_PRODUCTION_SECRET = "production_secret_used"
VIOLATION_KINDS: tuple[str, ...] = (
    VIOLATION_SCOPE,
    VIOLATION_AUTO_MERGE,
    VIOLATION_PRODUCTION_SECRET,
)
#: The authority violations — an attempted automatic merge or a used
#: production secret.  These are instant stops with preserved state.
_AUTHORITY_VIOLATIONS: frozenset[str] = frozenset(
    {VIOLATION_AUTO_MERGE, VIOLATION_PRODUCTION_SECRET}
)

#: The bounded task range: a pilot measures a business slice, not a
#: marathon — fewer than 12 tasks is an anecdote, more than 20 is a
#: second product.
MIN_PILOT_TASKS = 12
MAX_PILOT_TASKS = 20

#: The 2-of-3 rule needs exactly three criteria, each with a threshold,
#: a measurement method and a named end date.
CRITERIA_REQUIRED = 3

#: Every metric definition, pinned once and published in every report.
#: These are the definitions the tracker computes; nothing else may call
#: itself a pilot criterion.
METRIC_DEFINITIONS: Mapping[str, str] = {
    "autonomy_rate": (
        "tasks accepted WITHOUT a human code change ÷ total recorded tasks — "
        "merged-as-is semantics; watch for rubber-stamping (paired with the "
        "reviewer-load counterweight)"
    ),
    "cost_per_accepted_usd": (
        "ALL-attempt spend (unaccepted attempts included) ÷ accepted tasks — "
        "never the accepted attempts' spend alone; unknown when any attempt "
        "spend is unrecorded, never zero"
    ),
    "defect_rollback_rate": (
        "(defects + rollbacks on agent-authored deliverables) ÷ accepted "
        "tasks — quality lags speed by 8–12 weeks, so this number is "
        "revisited after the observation window, not just at the end date"
    ),
    "intervention_rate": (
        "tasks with ≥ 1 intervention event ÷ total recorded tasks — steering "
        "counts as an intervention, not only full takeovers"
    ),
    "cycle_time_vs_baseline": (
        "mean completion latency ÷ the FROZEN pre-pilot baseline cycle time "
        "— 1.0 is parity, below is faster; judged only at the named end date"
    ),
    "reviewer_load_minutes_per_task": (
        "senior-reviewer minutes per task — the capacity tax published "
        "beside every speed number, never averaged into them"
    ),
}

#: The report's standing limitations — the honesty section, always
#: present, never edited per-run.
LIMITATIONS: tuple[str, ...] = (
    "This kit is scaffolding plus an example spec — NOT a run pilot; no "
    "real design-partner tasks are claimed or implied by the shipped plan.",
    "PR counts and acceptance rates are documented liars for agent fleets; "
    "they appear nowhere as criteria and never in the headline.",
    "Quality lags speed by 8–12 weeks: the defect/rollback window outlives "
    "the pilot and the numbers must be revisited after the observation "
    "window closes.",
    "Metrics fold operator-recorded facts; self-reported time savings is "
    "disqualified as a headline number and appears nowhere in this kit.",
    "The baseline is the partner's own frozen pre-pilot numbers; a "
    "re-recorded baseline is a different pilot and a new spec.",
    "Early dips are expected (the J-curve): the measurement window was "
    "fixed before kickoff precisely so the pilot is not judged mid-dip.",
)


class PilotError(ValueError):
    """A pilot spec, task set, tracker record or report is invalid."""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise PilotError(f"expected an ISO date, got {value!r}") from exc


def _parse_datetime(value: str) -> datetime:
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PilotError(f"expected an ISO datetime, got {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _parse_date(str(value))


def _canonical_digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# The one-page learning contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataBoundary:
    """What the partner grants — and the two authority lines no pilot crosses.

    ``writable_repos`` is the ONE writable repo the first gate holds to
    (expansion to 2–3 only after the gate); ``neighbor_repos`` are the
    read grants whose evidence tasks may cite.  ``production_secrets``,
    ``automatic_merge`` and ``automatic_deploy`` must all be False: a
    pilot that can touch production secrets or merge/deploy on its own
    is not a pilot, it is an incident.
    """

    writable_repos: tuple[str, ...] = ()
    neighbor_repos: tuple[str, ...] = ()
    secrets_scope: str = ""
    production_secrets: bool = False
    automatic_merge: bool = False
    automatic_deploy: bool = False

    def covers_repo(self, repo: str) -> bool:
        """Whether *repo* is inside the granted boundary (writable OR neighbor)."""
        return repo in self.writable_repos or repo in self.neighbor_repos

    def validate(self) -> None:
        if not self.writable_repos:
            raise PilotError("data boundary needs at least one writable repo (one, first)")
        if not self.secrets_scope.strip():
            raise PilotError("data boundary needs a named secrets scope")
        if self.production_secrets:
            raise PilotError("production secrets are outside EVERY pilot boundary")
        if self.automatic_merge:
            raise PilotError(
                "automatic merge is outside every pilot boundary (the bot never merges)"
            )
        if self.automatic_deploy:
            raise PilotError("automatic deploy is outside every pilot boundary")
        if set(self.writable_repos) & set(self.neighbor_repos):
            raise PilotError("a repo cannot be both writable and a neighbor read")

    def as_document(self) -> dict[str, Any]:
        return {
            "writable_repos": list(self.writable_repos),
            "neighbor_repos": list(self.neighbor_repos),
            "secrets_scope": self.secrets_scope,
            "production_secrets": self.production_secrets,
            "automatic_merge": self.automatic_merge,
            "automatic_deploy": self.automatic_deploy,
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> DataBoundary:
        entry = dict(doc or {})
        return cls(
            writable_repos=tuple(str(repo) for repo in entry.get("writable_repos") or ()),
            neighbor_repos=tuple(str(repo) for repo in entry.get("neighbor_repos") or ()),
            secrets_scope=str(entry.get("secrets_scope") or ""),
            production_secrets=bool(entry.get("production_secrets") or False),
            automatic_merge=bool(entry.get("automatic_merge") or False),
            automatic_deploy=bool(entry.get("automatic_deploy") or False),
        )


@dataclass(frozen=True)
class PilotBaseline:
    """The FROZEN pre-pilot baseline — without it none of the metrics compute.

    ``window_start``/``window_end`` bound the 60–90-day pre-pilot sample
    and must END before the pilot window starts (frozen BEFORE kickoff).
    ``cycle_time_minutes`` is the partner's own mean PR cycle time over
    that window; ``dora`` carries the rest of the DORA four verbatim
    (deployment frequency, lead time, change failure rate, recovery
    time) for context.
    """

    window_start: str = ""
    window_end: str = ""
    cycle_time_minutes: float = 0.0
    dora: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        began = _parse_date(self.window_start)
        ended = _parse_date(self.window_end)
        if began >= ended:
            raise PilotError(
                f"baseline window must be non-empty ({self.window_start}..{self.window_end})"
            )
        if self.cycle_time_minutes <= 0:
            raise PilotError(
                "baseline cycle_time_minutes must be > 0 (a frozen baseline, not a guess)"
            )

    def as_document(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "cycle_time_minutes": self.cycle_time_minutes,
            "dora": dict(self.dora),
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> PilotBaseline:
        entry = dict(doc or {})
        dora = entry.get("dora")
        return cls(
            window_start=str(entry.get("window_start") or ""),
            window_end=str(entry.get("window_end") or ""),
            cycle_time_minutes=float(entry.get("cycle_time_minutes") or 0.0),
            dora=dict(dora) if isinstance(dora, Mapping) else {},
        )


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One of the exactly-three measurable criteria — with its named end date.

    ``metric`` must be one of :data:`CRITERION_METRICS` (the five that
    hold up); ``op``/``value`` are the threshold (``">=" 0.5`` reads
    "autonomy rate at or above 50 %"); ``method`` is HOW it is measured
    ("we will measure it in our system", never "use the product
    regularly"); ``end_date`` is the named end date the 2-of-3 verdict
    is decided by.  A criterion without any of these pieces is refused —
    an unmeasurable success criterion is a veto on the pilot contract.
    """

    criterion_id: str
    statement: str
    metric: str
    op: str
    value: float
    method: str
    end_date: str

    def validate(self) -> None:
        if not self.criterion_id.strip():
            raise PilotError("criterion needs a non-empty criterion_id")
        if not self.statement.strip():
            raise PilotError(f"{self.criterion_id}: empty criterion statement")
        if self.metric not in CRITERION_METRICS:
            raise PilotError(
                f"{self.criterion_id}: metric {self.metric!r} not in the closed vocabulary "
                f"{list(CRITERION_METRICS)} (PR counts and acceptance rates are liars)"
            )
        if self.op not in (">=", "<="):
            raise PilotError(f"{self.criterion_id}: op must be '>=' or '<=', got {self.op!r}")
        if not self.method.strip():
            raise PilotError(f"{self.criterion_id}: empty measurement method")
        _parse_date(self.end_date)

    def met_by(self, metrics: PilotMetrics) -> bool | None:
        """``True``/``False`` when the metric is measured, ``None`` when not.

        ``None`` is unjudgeable — an unknown metric is never a silently
        failed criterion; it is reported as unjudgeable and the verdict
        names it.
        """
        measured = getattr(metrics, self.metric, None)
        if not isinstance(measured, (int, float)) or isinstance(measured, bool):
            return None
        return measured >= self.value if self.op == ">=" else measured <= self.value

    def as_document(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "statement": self.statement,
            "metric": self.metric,
            "op": self.op,
            "value": self.value,
            "method": self.method,
            "end_date": self.end_date,
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> AcceptanceCriterion:
        if not isinstance(doc, Mapping):
            raise PilotError("criterion entry is not an object")
        return cls(
            criterion_id=str(doc.get("criterion_id") or ""),
            statement=str(doc.get("statement") or ""),
            metric=str(doc.get("metric") or ""),
            op=str(doc.get("op") or ""),
            value=float(doc.get("value") or 0.0),
            method=str(doc.get("method") or ""),
            end_date=str(doc.get("end_date") or ""),
        )


@dataclass(frozen=True)
class LadderStage:
    """One rung of the staged capability ladder.

    Week 1 is read/planning only; distribution, DB migration and
    confidential access are excluded stage-appropriately (they belong on
    the unsupported-features list, not in week 1's scope).
    """

    stage: str
    scope: str
    authority: str

    def validate(self) -> None:
        if self.stage not in STAGES:
            raise PilotError(f"ladder stage {self.stage!r} not in {list(STAGES)}")
        if not self.scope.strip() or not self.authority.strip():
            raise PilotError(f"ladder stage {self.stage!r} needs a scope and an authority")


@dataclass(frozen=True)
class ExitsPolicy:
    """The only exits: expand / extend-once-for-a-named-gap / stop.

    ``extension_limit`` is 1 — extend ONCE, with an explicit decision
    date; a second extension is a stop.
    """

    allowed: tuple[str, ...] = (EXIT_EXPAND, EXIT_EXTEND_ONCE, EXIT_STOP)
    extension_limit: int = 1

    def validate(self) -> None:
        if set(self.allowed) != {EXIT_EXPAND, EXIT_EXTEND_ONCE, EXIT_STOP}:
            raise PilotError(
                f"exits policy must be exactly expand/extend_once/stop, got {list(self.allowed)}"
            )
        if self.extension_limit < 1:
            raise PilotError("extension_limit must be >= 1 (extend-ONCE)")

    def as_document(self) -> dict[str, Any]:
        return {"allowed": list(self.allowed), "extension_limit": self.extension_limit}

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> ExitsPolicy:
        entry = dict(doc or {})
        return cls(
            allowed=tuple(str(exit_name) for exit_name in entry.get("allowed") or ()),
            extension_limit=int(entry.get("extension_limit") or 1),
        )


@dataclass(frozen=True)
class PilotSpec:
    """The one-page learning contract (``forge.pilot.spec/1``).

    Everything the converged playbook demands a pilot freeze BEFORE
    kickoff, in one versioned document:

    - **identity** — ``pilot_id``, ``platform``, ``recipe_id``,
      ``harness_id``, ``fee_note`` (payment as the demand test) and the
      named ``decision_owners``;
    - **boundary** — :attr:`data_boundary` (what the partner grants; no
      production secrets, no automatic merge/deploy);
    - **honest maturity** — :attr:`unsupported_features`, the explicit
      versioned list recorded BEFORE onboarding so expectations are
      clear ("customers can handle rough edges; they cannot handle sold
      maturity that delivers experimentation");
    - **the ladder** — :attr:`ladder`, the four stages in order;
    - **the deal** — :attr:`criteria` (exactly three, 2-of-3 decides),
      :attr:`window_start`/:attr:`window_end` (frozen),
      :attr:`baseline` (frozen pre-pilot numbers),
      :attr:`review_groups`/:attr:`review_cadence` (weekly operator
      cadence per task group), :attr:`exits`;
    - **labels** — :attr:`provenance_labels` from day one (retrofitting
      provenance is close to impossible) and
      :attr:`measurement_window_note` (the J-curve pre-commitment).

    :meth:`validate` refuses a spec missing any load-bearing piece — a
    criterion without threshold/method/end date, an empty
    unsupported-features list, a boundary without a writable repo, a
    baseline that was not frozen before kickoff.
    """

    pilot_id: str
    platform: str
    recipe_id: str
    harness_id: str
    decision_owners: tuple[str, ...]
    data_boundary: DataBoundary
    unsupported_features: tuple[str, ...]
    ladder: tuple[LadderStage, ...]
    criteria: tuple[AcceptanceCriterion, ...]
    window_start: str
    window_end: str
    baseline: PilotBaseline
    review_groups: tuple[str, ...]
    review_cadence: str
    exits: ExitsPolicy = field(default_factory=ExitsPolicy)
    provenance_labels: tuple[str, ...] = ("human", "ai-assisted", "agent-authored")
    fee_note: str = ""
    measurement_window_note: str = PILOT_DECISION_DATE_NOTE
    schema: str = SPEC_SCHEMA

    @property
    def frozen_digest(self) -> str:
        """sha256 over the canonical spec document — the freeze proof."""
        return _canonical_digest(self.as_document())

    @property
    def decision_date(self) -> date:
        """The LATEST criterion end date — the 2-of-3 verdict's named date."""
        return max(_parse_date(criterion.end_date) for criterion in self.criteria)

    def validate(self) -> None:
        """Structural integrity (raises :class:`PilotError`).

        Pins the contract: the schema tag, non-empty identity fields and
        named decision owners, EXACTLY three criteria each with metric +
        threshold + method + end date at/after the window start, a
        non-empty unsupported-features list recorded before onboarding,
        a data boundary with a writable repo and no production secrets /
        automatic merge / deploy, the four ladder stages IN ORDER, a
        window whose baseline was frozen before kickoff, named review
        groups with a per-group cadence, the three-exit policy, and
        provenance labels from day one.
        """
        if self.schema != SPEC_SCHEMA:
            raise PilotError(f"spec schema {self.schema!r} is not {SPEC_SCHEMA!r}")
        for name, value in (
            ("pilot_id", self.pilot_id),
            ("platform", self.platform),
            ("recipe_id", self.recipe_id),
            ("harness_id", self.harness_id),
        ):
            if not value.strip():
                raise PilotError(f"spec {name} must be non-empty")
        if not self.decision_owners:
            raise PilotError("spec needs named decision owners (humans, not roles)")
        self.data_boundary.validate()
        if not self.unsupported_features:
            raise PilotError(
                "spec must record its unsupported features BEFORE onboarding "
                "(an empty list claims maturity the pilot cannot prove)"
            )
        for feature in self.unsupported_features:
            if not feature.strip():
                raise PilotError("unsupported-features list carries an empty entry")
        if tuple(stage.stage for stage in self.ladder) != STAGES:
            raise PilotError(
                f"the capability ladder must be the four stages in order {list(STAGES)}, "
                f"got {[stage.stage for stage in self.ladder]}"
            )
        for stage in self.ladder:
            stage.validate()
        if len(self.criteria) != CRITERIA_REQUIRED:
            raise PilotError(
                f"the 2-of-3 rule needs exactly {CRITERIA_REQUIRED} criteria, "
                f"got {len(self.criteria)}"
            )
        seen_ids: set[str] = set()
        window_began = _parse_date(self.window_start)
        window_ends = _parse_date(self.window_end)
        if window_began >= window_ends:
            raise PilotError(
                f"pilot window must be non-empty ({self.window_start}..{self.window_end})"
            )
        for criterion in self.criteria:
            criterion.validate()
            if criterion.criterion_id in seen_ids:
                raise PilotError(f"duplicate criterion id {criterion.criterion_id!r}")
            seen_ids.add(criterion.criterion_id)
            if _parse_date(criterion.end_date) < window_began:
                raise PilotError(
                    f"{criterion.criterion_id}: end date {criterion.end_date} precedes the "
                    f"pilot window start {self.window_start}"
                )
        self.baseline.validate()
        if _parse_date(self.baseline.window_end) > window_began:
            raise PilotError(
                f"baseline window must be frozen BEFORE kickoff: baseline ends "
                f"{self.baseline.window_end}, pilot starts {self.window_start}"
            )
        if not self.review_groups or len(set(self.review_groups)) != len(self.review_groups):
            raise PilotError("spec needs unique, non-empty review groups (per-group cadence)")
        if not self.review_cadence.strip():
            raise PilotError("spec needs a named review cadence (weekly operator cadence)")
        self.exits.validate()
        if not self.provenance_labels:
            raise PilotError("spec needs provenance labels from day one")

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "pilot_id": self.pilot_id,
            "platform": self.platform,
            "recipe_id": self.recipe_id,
            "harness_id": self.harness_id,
            "decision_owners": list(self.decision_owners),
            "fee_note": self.fee_note,
            "data_boundary": self.data_boundary.as_document(),
            "unsupported_features": list(self.unsupported_features),
            "ladder": [
                {"stage": stage.stage, "scope": stage.scope, "authority": stage.authority}
                for stage in self.ladder
            ],
            "criteria": [criterion.as_document() for criterion in self.criteria],
            "window": {"start": self.window_start, "end": self.window_end},
            "baseline": self.baseline.as_document(),
            "review_groups": list(self.review_groups),
            "review_cadence": self.review_cadence,
            "exits": self.exits.as_document(),
            "provenance_labels": list(self.provenance_labels),
            "measurement_window_note": self.measurement_window_note,
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> PilotSpec:
        if not isinstance(doc, Mapping):
            raise PilotError("spec document is not an object")
        window = doc.get("window") if isinstance(doc.get("window"), Mapping) else {}
        return cls(
            schema=str(doc.get("schema") or ""),
            pilot_id=str(doc.get("pilot_id") or ""),
            platform=str(doc.get("platform") or ""),
            recipe_id=str(doc.get("recipe_id") or ""),
            harness_id=str(doc.get("harness_id") or ""),
            decision_owners=tuple(str(owner) for owner in doc.get("decision_owners") or ()),
            data_boundary=DataBoundary.from_document(doc.get("data_boundary")),
            unsupported_features=tuple(
                str(feature) for feature in doc.get("unsupported_features") or ()
            ),
            ladder=tuple(
                LadderStage(
                    stage=str(entry.get("stage") or ""),
                    scope=str(entry.get("scope") or ""),
                    authority=str(entry.get("authority") or ""),
                )
                for entry in doc.get("ladder") or ()
                if isinstance(entry, Mapping)
            ),
            criteria=tuple(
                AcceptanceCriterion.from_document(entry) for entry in doc.get("criteria") or ()
            ),
            window_start=str(window.get("start") or ""),
            window_end=str(window.get("end") or ""),
            baseline=PilotBaseline.from_document(doc.get("baseline")),
            review_groups=tuple(str(group) for group in doc.get("review_groups") or ()),
            review_cadence=str(doc.get("review_cadence") or ""),
            exits=ExitsPolicy.from_document(doc.get("exits")),
            provenance_labels=tuple(str(label) for label in doc.get("provenance_labels") or ()),
            fee_note=str(doc.get("fee_note") or ""),
            measurement_window_note=str(doc.get("measurement_window_note") or ""),
        )

    @classmethod
    def load(cls, path: Path) -> PilotSpec:
        spec = cls.from_document(json.loads(Path(path).read_text(encoding="utf-8")))
        spec.validate()
        return spec


# ---------------------------------------------------------------------------
# The bounded task set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationCheck:
    """One independent verification step — never the agent's self-report."""

    name: str
    method: str

    def validate(self) -> None:
        if not self.name.strip() or not self.method.strip():
            raise PilotError("verification check needs a name and a method")


@dataclass(frozen=True)
class PilotTask:
    """One pilot unit: an owner, an outcome, non-goals, a budget, a contract.

    Unlike a cohort task (a fixture seed), a pilot task names its HUMAN
    owner and decision path: ``owner`` (the named decision owner),
    ``expected_outcome`` (the specific workflow completion, never "use
    the product"), ``non_goals`` (what this task explicitly does not
    attempt), ``budget`` (cost ceiling), ``verification`` (independent
    checks — the partner's own CI, an independent reviewer, a recount —
    never self-report), ``scenario`` (the closed vocabulary), ``group``
    (the review group the weekly operator cadence reviews it in) and
    ``stage`` (the ladder rung the task runs under).
    """

    task_id: str
    title: str
    owner: str
    expected_outcome: str
    non_goals: tuple[str, ...]
    budget: Mapping[str, Any] = field(default_factory=dict)
    verification: tuple[VerificationCheck, ...] = ()
    scenario: str = ""
    group: str = ""
    stage: str = ""

    def validate(self) -> None:
        if not self.task_id.strip():
            raise PilotError("task needs a non-empty task_id")
        for name, value in (
            ("title", self.title),
            ("owner", self.owner),
            ("expected_outcome", self.expected_outcome),
        ):
            if not value.strip():
                raise PilotError(f"{self.task_id}: {name} must be non-empty")
        if not self.non_goals:
            raise PilotError(f"{self.task_id}: non-goals are required (what this task refuses)")
        if not self.verification:
            raise PilotError(
                f"{self.task_id}: no verification contract — self-report is not acceptance"
            )
        for check in self.verification:
            check.validate()
        if float(self.budget.get("max_spend_usd") or 0.0) <= 0:
            raise PilotError(f"{self.task_id}: budget needs max_spend_usd > 0 (a cost ceiling)")
        if self.scenario not in SCENARIO_TAGS:
            raise PilotError(
                f"{self.task_id}: unknown scenario {self.scenario!r}; "
                f"the closed vocabulary is {list(SCENARIO_TAGS)}"
            )
        if not self.group.strip():
            raise PilotError(f"{self.task_id}: task needs a review group assignment")
        if self.stage not in STAGES:
            raise PilotError(f"{self.task_id}: unknown ladder stage {self.stage!r}")

    def as_document(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "owner": self.owner,
            "expected_outcome": self.expected_outcome,
            "non_goals": list(self.non_goals),
            "budget": dict(self.budget),
            "verification": [
                {"name": check.name, "method": check.method} for check in self.verification
            ],
            "scenario": self.scenario,
            "group": self.group,
            "stage": self.stage,
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> PilotTask:
        if not isinstance(doc, Mapping):
            raise PilotError("task entry is not an object")
        budget = doc.get("budget")
        return cls(
            task_id=str(doc.get("task_id") or ""),
            title=str(doc.get("title") or ""),
            owner=str(doc.get("owner") or ""),
            expected_outcome=str(doc.get("expected_outcome") or ""),
            non_goals=tuple(str(goal) for goal in doc.get("non_goals") or ()),
            budget=dict(budget) if isinstance(budget, Mapping) else {},
            verification=tuple(
                VerificationCheck(
                    name=str(entry.get("name") or ""),
                    method=str(entry.get("method") or ""),
                )
                for entry in doc.get("verification") or ()
                if isinstance(entry, Mapping)
            ),
            scenario=str(doc.get("scenario") or ""),
            group=str(doc.get("group") or ""),
            stage=str(doc.get("stage") or ""),
        )


@dataclass(frozen=True)
class PilotPlan:
    """A spec plus its bounded task set — the pilot's whole workload.

    Validation pins: 12–20 tasks (a pilot measures a slice), unique ids,
    EVERY scenario tag present at least once (the unhappy paths are part
    of the measurement, and the real adaptive control path must be
    exercised), every group declared in the spec and every group mapped
    to exactly ONE ladder stage with the groups staged IN ORDER.
    """

    plan_id: str
    spec: PilotSpec
    tasks: tuple[PilotTask, ...] = ()
    schema: str = PLAN_SCHEMA

    def validate(self) -> None:
        if self.schema != PLAN_SCHEMA:
            raise PilotError(f"plan schema {self.schema!r} is not {PLAN_SCHEMA!r}")
        if not self.plan_id.strip():
            raise PilotError("plan_id must be non-empty")
        self.spec.validate()
        if not MIN_PILOT_TASKS <= len(self.tasks) <= MAX_PILOT_TASKS:
            raise PilotError(
                f"pilot plan must hold {MIN_PILOT_TASKS}-{MAX_PILOT_TASKS} tasks "
                f"(a bounded slice, not a marathon), got {len(self.tasks)}"
            )
        seen: set[str] = set()
        scenarios_seen: set[str] = set()
        stages_seen: set[str] = set()
        group_stage: dict[str, str] = {}
        for task in self.tasks:
            task.validate()
            if task.task_id in seen:
                raise PilotError(f"duplicate task id {task.task_id!r}")
            seen.add(task.task_id)
            if task.group not in self.spec.review_groups:
                raise PilotError(
                    f"{task.task_id}: group {task.group!r} is not declared in the spec"
                )
            if task.group in group_stage and group_stage[task.group] != task.stage:
                raise PilotError(
                    f"{task.task_id}: group {task.group!r} mixes ladder stages "
                    f"({group_stage[task.group]} and {task.stage}) — a staged group is one stage"
                )
            group_stage[task.group] = task.stage
            scenarios_seen.add(task.scenario)
            stages_seen.add(task.stage)
        missing_tags = set(SCENARIO_TAGS) - scenarios_seen
        if missing_tags:
            raise PilotError(f"scenario tags without a task: {sorted(missing_tags)}")
        missing_stages = set(STAGES) - stages_seen
        if missing_stages:
            raise PilotError(f"ladder stages without a task: {sorted(missing_stages)}")
        ordered = [group_stage[group] for group in self.spec.review_groups if group in group_stage]
        stage_order = [stage for stage in STAGES if stage in set(ordered)]
        if ordered != stage_order:
            raise PilotError(
                f"review groups must be staged in ladder order {list(STAGES)}, "
                f"got {ordered} (capability advances group by group)"
            )

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "spec": self.spec.as_document(),
            "tasks": [task.as_document() for task in self.tasks],
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> PilotPlan:
        if not isinstance(doc, Mapping):
            raise PilotError("plan document is not an object")
        return cls(
            schema=str(doc.get("schema") or ""),
            plan_id=str(doc.get("plan_id") or ""),
            spec=PilotSpec.from_document(doc.get("spec") or {}),
            tasks=tuple(PilotTask.from_document(entry) for entry in doc.get("tasks") or ()),
        )

    @classmethod
    def load(cls, path: Path) -> PilotPlan:
        plan = cls.from_document(json.loads(Path(path).read_text(encoding="utf-8")))
        plan.validate()
        return plan


# ---------------------------------------------------------------------------
# The pre-onboarding record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OnboardingRecord:
    """The provably-pre-task snapshot of what the pilot does NOT support.

    ``recorded_at`` is checked against the first task start by the
    report: the unsupported-features list is provably recorded BEFORE
    any task ran, so "you never told us it couldn't do migrations" is an
    argument the record settles.  ``spec_digest`` binds the record to
    THIS spec.
    """

    recorded_at: str
    unsupported_features: tuple[str, ...]
    spec_digest: str


def record_onboarding(spec: PilotSpec, recorded_at: str | None = None) -> OnboardingRecord:
    """Snapshot the spec's unsupported-features list, timestamped.

    The timestamp defaults to now (UTC); tests and replays pass an
    explicit one.  The returned record is what the tracker stores before
    any task may be recorded.
    """
    spec.validate()
    stamp = recorded_at or datetime.now(timezone.utc).isoformat()
    return OnboardingRecord(
        recorded_at=stamp,
        unsupported_features=tuple(spec.unsupported_features),
        spec_digest=spec.frozen_digest,
    )


# ---------------------------------------------------------------------------
# The tracker: per-task facts and their metric fold
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptUsage:
    """One attempt's usage — unaccepted attempts keep their spend.

    ``accepted`` marks the attempt the acceptance decision landed on;
    ``spend_usd`` of ``None`` is an unrecorded spend (an unknown, never
    a zero — it degrades the pilot's cost metrics).
    """

    attempt_id: str
    accepted: bool = False
    spend_usd: float | None = None


@dataclass(frozen=True)
class InterventionEvent:
    """One operator intervention — steering counts exactly like a takeover."""

    kind: str
    note: str = ""

    def validate(self) -> None:
        if self.kind not in INTERVENTION_KINDS:
            raise PilotError(
                f"unknown intervention kind {self.kind!r}; "
                f"the closed vocabulary is {list(INTERVENTION_KINDS)}"
            )


@dataclass(frozen=True)
class Violation:
    """A recorded boundary or authority violation."""

    kind: str
    detail: str = ""
    task_id: str = ""


@dataclass(frozen=True)
class ExtensionRecord:
    """One granted extend-once — the named gap it was granted for."""

    gap: str


@dataclass(frozen=True)
class TaskRecord:
    """One task's operator-recorded facts — the tracker's unit of evidence.

    Everything the five metrics + reviewer load need, recorded by the
    operator as it happens (never self-reported by the agent):
    ``setup_minutes``, ``plan_corrections`` (+ notes),
    ``manual_rescues`` (+ what), ``completion_latency_minutes`` (None
    until complete), ``attempts`` (ALL attempts with their usage,
    unaccepted included), ``interventions`` (steering counted),
    ``review_minutes``, ``defects``/``rollbacks``, ``accepted`` and
    ``human_code_change`` (the autonomy-rate inputs), and the repos the
    task actually touched (the scope check runs over these).

    ``blocked`` (R36-16) names a task whose scenario genuinely could not
    run — a seam gap, a missing dependency — recorded BY NAME instead of
    silently dropping the task from the pilot.  A blocked task is never
    accepted and stays in every denominator: a pilot that only counts
    the tasks it could run is a selection lie.
    """

    task_id: str
    started_at: str
    accepted: bool = False
    human_code_change: bool = False
    setup_minutes: float = 0.0
    plan_corrections: int = 0
    plan_correction_notes: tuple[str, ...] = ()
    manual_rescues: int = 0
    rescue_notes: tuple[str, ...] = ()
    completion_latency_minutes: float | None = None
    attempts: tuple[AttemptUsage, ...] = ()
    interventions: tuple[InterventionEvent, ...] = ()
    review_minutes: float = 0.0
    defects: int = 0
    rollbacks: int = 0
    touched_repos: tuple[str, ...] = ()
    blocked: str = ""

    def validate(self) -> None:
        if not self.task_id.strip():
            raise PilotError("task record needs a non-empty task_id")
        _parse_datetime(self.started_at)
        if self.setup_minutes < 0 or self.review_minutes < 0:
            raise PilotError(f"{self.task_id}: minutes cannot be negative")
        if self.plan_corrections < 0 or self.manual_rescues < 0 or self.defects < 0:
            raise PilotError(f"{self.task_id}: counters cannot be negative")
        if self.rollbacks < 0:
            raise PilotError(f"{self.task_id}: counters cannot be negative")
        if self.completion_latency_minutes is not None and self.completion_latency_minutes < 0:
            raise PilotError(f"{self.task_id}: completion latency cannot be negative")
        if self.blocked.strip() and self.accepted:
            raise PilotError(
                f"{self.task_id}: a blocked task cannot be accepted — the blocked "
                "reason is the outcome, and dropping it from the denominator would "
                "be the selection lie"
            )
        seen_attempts: set[str] = set()
        for attempt in self.attempts:
            if not attempt.attempt_id.strip():
                raise PilotError(f"{self.task_id}: attempt needs a non-empty attempt_id")
            if attempt.attempt_id in seen_attempts:
                raise PilotError(f"{self.task_id}: duplicate attempt id {attempt.attempt_id!r}")
            seen_attempts.add(attempt.attempt_id)
            if attempt.spend_usd is not None and attempt.spend_usd < 0:
                raise PilotError(f"{self.task_id}/{attempt.attempt_id}: spend cannot be negative")
        for event in self.interventions:
            event.validate()
        if len(self.plan_correction_notes) > self.plan_corrections:
            raise PilotError(
                f"{self.task_id}: more correction notes ({len(self.plan_correction_notes)}) "
                f"than corrections ({self.plan_corrections})"
            )
        if len(self.rescue_notes) > self.manual_rescues:
            raise PilotError(
                f"{self.task_id}: more rescue notes ({len(self.rescue_notes)}) "
                f"than rescues ({self.manual_rescues})"
            )


@dataclass(frozen=True)
class PilotMetrics:
    """The five metrics + reviewer load, folded against the frozen baseline.

    Every field is ``None`` when its inputs are missing — unknown, never
    a zero dressed up as measured (:attr:`notes` names every gap).  The
    definitions live in :data:`METRIC_DEFINITIONS` and ride the report:

    - ``autonomy_rate`` — accepted-without-human-code-change ÷ total;
    - ``cost_per_accepted_usd`` — ALL-attempt spend ÷ accepted count;
    - ``defect_rollback_rate`` — (defects + rollbacks) ÷ accepted count;
    - ``intervention_rate`` — tasks with ≥ 1 intervention ÷ total
      (steering counts);
    - ``cycle_time_vs_baseline`` — mean completion latency ÷ the frozen
      baseline (1.0 is parity);
    - ``reviewer_load_minutes_per_task`` — the capacity tax.
    """

    tasks_total: int = 0
    tasks_accepted: int = 0
    tasks_unaccepted: int = 0
    autonomy_rate: float | None = None
    total_cost_usd: float | None = None
    cost_per_accepted_usd: float | None = None
    defect_rollback_rate: float | None = None
    intervention_rate: float | None = None
    interventions_per_task: float | None = None
    mean_cycle_time_minutes: float | None = None
    baseline_cycle_time_minutes: float | None = None
    cycle_time_vs_baseline: float | None = None
    reviewer_load_minutes_per_task: float | None = None
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "tasks_total": self.tasks_total,
            "tasks_accepted": self.tasks_accepted,
            "tasks_unaccepted": self.tasks_unaccepted,
            "autonomy_rate": self.autonomy_rate,
            "total_cost_usd": self.total_cost_usd,
            "cost_per_accepted_usd": self.cost_per_accepted_usd,
            "defect_rollback_rate": self.defect_rollback_rate,
            "intervention_rate": self.intervention_rate,
            "interventions_per_task": self.interventions_per_task,
            "mean_cycle_time_minutes": self.mean_cycle_time_minutes,
            "baseline_cycle_time_minutes": self.baseline_cycle_time_minutes,
            "cycle_time_vs_baseline": self.cycle_time_vs_baseline,
            "reviewer_load_minutes_per_task": self.reviewer_load_minutes_per_task,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class PilotDiagnostics:
    """The preserved verbatim tracker state a stop leaves behind.

    ``snapshot`` is the records document EXACTLY as they were at stop
    time; ``pointer`` is its digest (what the report records); a
    preserved pilot refuses new task records — the state is retained
    for reading, NEVER silently retried.
    """

    stop_reason: str
    snapshot: Mapping[str, Any]
    pointer: str


class PilotTracker:
    """Records the pilot's per-task facts and folds them into metrics.

    Ordering is enforced, not trusted: the onboarding record must be
    stored before any task; the spec is digest-bound at construction and
    CANNOT be retargeted once a task is recorded (the J-curve
    pre-commitment — the measurement window is fixed pre-kickoff);
    a task that touched repos outside the data boundary records a scope
    violation automatically; a preserved (stopped) tracker refuses all
    further recording.
    """

    def __init__(self, spec: PilotSpec, onboarding: OnboardingRecord | None = None) -> None:
        spec.validate()
        self._spec = spec
        self._spec_digest = spec.frozen_digest
        self._onboarding: OnboardingRecord | None = None
        self._records: list[TaskRecord] = []
        self._record_ids: set[str] = set()
        self._violations: list[Violation] = []
        self._extensions: list[ExtensionRecord] = []
        self._diagnostics: PilotDiagnostics | None = None
        if onboarding is not None:
            self.record_onboarding(onboarding)

    # -- read-only surface -------------------------------------------------

    @property
    def spec(self) -> PilotSpec:
        return self._spec

    @property
    def spec_digest(self) -> str:
        return self._spec_digest

    @property
    def onboarding(self) -> OnboardingRecord | None:
        return self._onboarding

    @property
    def records(self) -> tuple[TaskRecord, ...]:
        return tuple(self._records)

    @property
    def violations(self) -> tuple[Violation, ...]:
        return tuple(self._violations)

    @property
    def extensions(self) -> tuple[ExtensionRecord, ...]:
        return tuple(self._extensions)

    @property
    def extensions_used(self) -> int:
        return len(self._extensions)

    @property
    def diagnostics(self) -> PilotDiagnostics | None:
        return self._diagnostics

    @property
    def first_task_started_at(self) -> datetime | None:
        """The earliest recorded task start (None before the first task)."""
        stamps = [_parse_datetime(record.started_at) for record in self._records]
        return min(stamps) if stamps else None

    # -- recording ---------------------------------------------------------

    def _refuse_if_stopped(self, action: str) -> None:
        if self._diagnostics is not None:
            raise PilotError(
                f"cannot {action}: this pilot stopped ({self._diagnostics.stop_reason}) — "
                "the tracker snapshot is preserved verbatim and is NEVER silently retried"
            )

    def record_onboarding(self, onboarding: OnboardingRecord) -> None:
        """Store the pre-onboarding record — only BEFORE any task, once."""
        self._refuse_if_stopped("record onboarding")
        if self._records:
            raise PilotError(
                "onboarding must be recorded BEFORE any task — the "
                "unsupported-features list is provably pre-task"
            )
        if self._onboarding is not None:
            raise PilotError("onboarding is recorded once; a second record is a new pilot")
        if onboarding.spec_digest != self._spec_digest:
            raise PilotError(
                "onboarding record is bound to a different spec "
                f"({onboarding.spec_digest[:12]}… != {self._spec_digest[:12]}…)"
            )
        _parse_datetime(onboarding.recorded_at)
        self._onboarding = onboarding

    def record_task(self, record: TaskRecord) -> None:
        """Record one task's facts; check scope; freeze the spec.

        A task whose ``touched_repos`` fall outside the spec's data
        boundary still records — the violation is recorded with it, and
        :func:`evaluate_stop` turns it into a stop with preserved
        diagnostics.  Suppressing the record would be the lie.
        """
        self._refuse_if_stopped(f"record task {record.task_id}")
        record.validate()
        if record.task_id in self._record_ids:
            raise PilotError(
                f"task {record.task_id!r} already recorded — one record per task; "
                "corrections belong in the record"
            )
        self._records.append(record)
        self._record_ids.add(record.task_id)
        for repo in record.touched_repos:
            if not self._spec.data_boundary.covers_repo(repo):
                self._violations.append(
                    Violation(
                        kind=VIOLATION_SCOPE,
                        detail=f"touched {repo!r} outside the data boundary",
                        task_id=record.task_id,
                    )
                )

    def record_authority_violation(self, kind: str, detail: str = "", task_id: str = "") -> None:
        """Record an authority violation (auto-merge attempt, production secret)."""
        self._refuse_if_stopped("record an authority violation")
        if kind not in _AUTHORITY_VIOLATIONS:
            raise PilotError(
                f"authority violation kind {kind!r} must be one of "
                f"{sorted(_AUTHORITY_VIOLATIONS)}; scope violations are recorded by record_task"
            )
        self._violations.append(Violation(kind=kind, detail=detail, task_id=task_id))

    def grant_extension(self, gap: str) -> None:
        """Grant the extend-once — a named gap, exactly once."""
        self._refuse_if_stopped("grant an extension")
        if not gap.strip():
            raise PilotError("extend-ONCE requires a NAMED gap (an unnamed extension is drift)")
        if self.extensions_used >= self._spec.exits.extension_limit:
            raise PilotError(
                "extend-once means once: the extension limit "
                f"({self._spec.exits.extension_limit}) is used — the exit is stop"
            )
        self._extensions.append(ExtensionRecord(gap=gap))

    def retarget_spec(self, spec: PilotSpec) -> None:
        """Swap in a different spec — refused once anything is recorded.

        The J-curve pre-commitment made structural: the measurement
        window (and the whole contract) is fixed BEFORE kickoff; a spec
        change after tasks (or the onboarding record) exist would move
        the goalposts the criteria are judged against.
        """
        if self._records or self._onboarding is not None:
            raise PilotError(
                "the spec is frozen for this tracker: the measurement window was fixed "
                "pre-kickoff and cannot be edited after tasks are recorded — start a new "
                "pilot (and a new spec digest) instead"
            )
        spec.validate()
        self._spec = spec
        self._spec_digest = spec.frozen_digest

    def preserve_diagnostics(self, reason: str) -> PilotDiagnostics:
        """Freeze the tracker snapshot verbatim; the pilot never records again.

        Idempotent-by-retention: a second call returns the SAME preserved
        diagnostics — the state at stop time is retained, not re-cut.
        """
        if self._diagnostics is not None:
            return self._diagnostics
        snapshot = self.snapshot_document()
        self._diagnostics = PilotDiagnostics(
            stop_reason=reason, snapshot=snapshot, pointer=_canonical_digest(snapshot)
        )
        return self._diagnostics

    # -- the fold ----------------------------------------------------------

    def metrics(self) -> PilotMetrics:
        """Fold the recorded tasks into the five metrics + reviewer load.

        The honesty rules: cost is known only when EVERY attempt of
        EVERY task carries a spend (an unaccepted attempt's spend
        counts; a missing one degrades the totals to unknown — never
        zero); cycle time runs over completed tasks only; every unknown
        is named in :attr:`PilotMetrics.notes`.
        """
        records = self._records
        notes: list[str] = []
        total = len(records)
        accepted = sum(1 for record in records if record.accepted)
        autonomous = sum(
            1 for record in records if record.accepted and not record.human_code_change
        )
        intervention_tasks = sum(1 for record in records if record.interventions)
        events = sum(len(record.interventions) for record in records)

        total_cost: float | None = 0.0
        for record in records:
            if not record.attempts:
                total_cost = None
                notes.append(f"{record.task_id}: no attempt usage recorded — cost unknown")
                continue
            for attempt in record.attempts:
                if attempt.spend_usd is None:
                    total_cost = None
                    notes.append(
                        f"{record.task_id}/{attempt.attempt_id}: attempt spend unrecorded "
                        "— cost unknown, never zero"
                    )
                elif total_cost is not None:
                    total_cost += attempt.spend_usd

        latencies = [
            record.completion_latency_minutes
            for record in records
            if record.completion_latency_minutes is not None
        ]
        if total and len(latencies) != total:
            notes.append(
                f"{total - len(latencies)} task(s) without a completion latency — "
                "cycle time runs over completed tasks only"
            )
        mean_cycle = round(sum(latencies) / len(latencies), 4) if latencies else None
        baseline_cycle = self._spec.baseline.cycle_time_minutes or None
        ratio = (
            round(mean_cycle / baseline_cycle, 4)
            if mean_cycle is not None and baseline_cycle
            else None
        )
        if not records:
            notes.append("no tasks recorded — every fold is unknown, not zero")
        defects_rollbacks = sum(record.defects + record.rollbacks for record in records)
        return PilotMetrics(
            tasks_total=total,
            tasks_accepted=accepted,
            tasks_unaccepted=total - accepted,
            autonomy_rate=round(autonomous / total, 4) if total else None,
            total_cost_usd=round(total_cost, 6) if total_cost is not None else None,
            cost_per_accepted_usd=(
                round(total_cost / accepted, 6) if total_cost is not None and accepted else None
            ),
            defect_rollback_rate=round(defects_rollbacks / accepted, 4) if accepted else None,
            intervention_rate=round(intervention_tasks / total, 4) if total else None,
            interventions_per_task=round(events / total, 4) if total else None,
            mean_cycle_time_minutes=mean_cycle,
            baseline_cycle_time_minutes=baseline_cycle,
            cycle_time_vs_baseline=ratio,
            reviewer_load_minutes_per_task=(
                round(sum(record.review_minutes for record in records) / total, 4)
                if total
                else None
            ),
            notes=tuple(notes),
        )

    def snapshot_document(self) -> dict[str, Any]:
        """The verbatim records document (what preservation retains)."""
        return {
            "spec_digest": self._spec_digest,
            "onboarding": (
                {
                    "recorded_at": self._onboarding.recorded_at,
                    "unsupported_features": list(self._onboarding.unsupported_features),
                    "spec_digest": self._onboarding.spec_digest,
                }
                if self._onboarding is not None
                else None
            ),
            "tasks": [_task_record_document(record) for record in self._records],
            "violations": [
                {"kind": v.kind, "detail": v.detail, "task_id": v.task_id} for v in self._violations
            ],
            "extensions": [{"gap": extension.gap} for extension in self._extensions],
        }


def _task_record_document(record: TaskRecord) -> dict[str, Any]:
    """One task record's document form (shared by snapshot and report)."""
    return {
        "task_id": record.task_id,
        "started_at": record.started_at,
        "accepted": record.accepted,
        "unaccepted": not record.accepted,
        "human_code_change": record.human_code_change,
        "setup_minutes": record.setup_minutes,
        "plan_corrections": record.plan_corrections,
        "plan_correction_notes": list(record.plan_correction_notes),
        "manual_rescues": record.manual_rescues,
        "rescue_notes": list(record.rescue_notes),
        "completion_latency_minutes": record.completion_latency_minutes,
        "attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "accepted": attempt.accepted,
                "spend_usd": attempt.spend_usd,
            }
            for attempt in record.attempts
        ],
        "interventions": [
            {"kind": event.kind, "note": event.note} for event in record.interventions
        ],
        "review_minutes": record.review_minutes,
        "defects": record.defects,
        "rollbacks": record.rollbacks,
        "touched_repos": list(record.touched_repos),
        "blocked": record.blocked,
    }


def task_record_from_document(doc: Mapping[str, Any]) -> TaskRecord:
    """Rebuild one :class:`TaskRecord` from its document form.

    The inverse of :func:`_task_record_document` — what a replayed
    report rebuilds its tracker from (the records document is the pilot's
    durable evidence, so the report must be reconstructible from it
    alone, byte-identically).  Unknown document fields are ignored
    (forward-compatibility for additive schema growth).
    """
    if not isinstance(doc, Mapping):
        raise PilotError("task record document is not an object")
    record = TaskRecord(
        task_id=str(doc.get("task_id") or ""),
        started_at=str(doc.get("started_at") or ""),
        accepted=bool(doc.get("accepted") or False),
        human_code_change=bool(doc.get("human_code_change") or False),
        setup_minutes=float(doc.get("setup_minutes") or 0.0),
        plan_corrections=int(doc.get("plan_corrections") or 0),
        plan_correction_notes=tuple(str(note) for note in doc.get("plan_correction_notes") or ()),
        manual_rescues=int(doc.get("manual_rescues") or 0),
        rescue_notes=tuple(str(note) for note in doc.get("rescue_notes") or ()),
        completion_latency_minutes=(
            float(doc["completion_latency_minutes"])
            if doc.get("completion_latency_minutes") is not None
            else None
        ),
        attempts=tuple(
            AttemptUsage(
                attempt_id=str(entry.get("attempt_id") or ""),
                accepted=bool(entry.get("accepted") or False),
                spend_usd=(
                    float(entry["spend_usd"]) if entry.get("spend_usd") is not None else None
                ),
            )
            for entry in doc.get("attempts") or ()
            if isinstance(entry, Mapping)
        ),
        interventions=tuple(
            InterventionEvent(kind=str(entry.get("kind") or ""), note=str(entry.get("note") or ""))
            for entry in doc.get("interventions") or ()
            if isinstance(entry, Mapping)
        ),
        review_minutes=float(doc.get("review_minutes") or 0.0),
        defects=int(doc.get("defects") or 0),
        rollbacks=int(doc.get("rollbacks") or 0),
        touched_repos=tuple(str(repo) for repo in doc.get("touched_repos") or ()),
        blocked=str(doc.get("blocked") or ""),
    )
    record.validate()
    return record


def record_blocked_task(tracker: PilotTracker, task_id: str, started_at: str, reason: str) -> None:
    """Record a task whose scenario genuinely could not run (R36-16).

    The honest alternative to dropping the task: it stays in every
    denominator (total tasks, the autonomy and intervention rates) with
    its named blocking reason, is never accepted, and carries no
    completion latency (the cycle-time fold runs over completed tasks
    only and will say so in its notes).
    """
    blocked_reason = reason.strip() or "blocked: unnamed reason"
    if not blocked_reason.startswith("blocked:"):
        blocked_reason = f"blocked: {blocked_reason}"
    tracker.record_task(TaskRecord(task_id=task_id, started_at=started_at, blocked=blocked_reason))


def usage_ledger_document(
    attempt_id: str,
    *,
    source: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    model_time_s: float | None = None,
    tool_call_count: int | None = None,
    latency_breakdown: Mapping[str, float | None] | None = None,
    spend_usd: float | None = None,
) -> dict[str, Any]:
    """One attempt's usage in the delivery-ledger shape (R36-17 / #276).

    The per-attempt receipt row ``reconcile_delivery`` folds — keyed to
    the attempt identity, every unknown an explicit ``null``, and the
    COST STATE labeled rather than implied: ``spend_usd`` of ``None``
    rides with ``cost_state: "unknown"`` ("unknown, never zero"), a
    known spend with ``cost_state: "known"``.  Token counters are the
    wire-observed counts when the vendor reports them (the lab vendor's
    ``tokenUsage`` frames) and ``None`` when nothing on the wire did.
    """
    if not attempt_id.strip():
        raise PilotError("usage ledger row needs a non-empty attempt_id")
    if not source.strip():
        raise PilotError(f"{attempt_id}: usage ledger row needs a named source")
    known_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    return {
        "attempt_id": attempt_id,
        "source": source,
        "spend_usd": spend_usd,
        "cost_state": "known" if spend_usd is not None else "unknown",
        "cost_note": ("spend unknown — never zero" if spend_usd is None else "spend recorded"),
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "total_known": known_tokens,
        },
        "model_time_s": model_time_s,
        "tool_call_count": tool_call_count,
        "latency_breakdown": dict(latency_breakdown or {}),
    }


def tracker_from_snapshot(
    spec: PilotSpec,
    snapshot: Mapping[str, Any],
    *,
    stop_reason: str = "",
) -> PilotTracker:
    """Rebuild a tracker from its records document — the replay entry point.

    Restores the onboarding record, every task record and the granted
    extensions through the SAME public recording API the live pilot
    used, so a report rebuilt from the records alone is byte-identical
    to the one the live run wrote (the determinism proof).  When
    ``stop_reason`` names the stop the live run hit, the rebuilt tracker
    re-preserves its diagnostics over the same state.

    Ordering note: scope violations regenerate from each record's
    touched repos as the records replay; authority violations replay
    after the tasks.  A live pilot that recorded an authority violation
    BETWEEN tasks replays it after them — the report content is
    identical, but that ordering's diagnostics pointer may differ (the
    runner's shape records violations only at task boundaries, where
    replay is exact).
    """
    if not isinstance(snapshot, Mapping):
        raise PilotError("tracker snapshot is not an object")
    digest = str(snapshot.get("spec_digest") or "")
    if digest != spec.frozen_digest:
        raise PilotError(
            f"snapshot is bound to a different spec ({digest[:12]}… != {spec.frozen_digest[:12]}…)"
        )
    tracker = PilotTracker(spec)
    onboarding = snapshot.get("onboarding")
    if isinstance(onboarding, Mapping):
        tracker.record_onboarding(
            OnboardingRecord(
                recorded_at=str(onboarding.get("recorded_at") or ""),
                unsupported_features=tuple(
                    str(feature) for feature in onboarding.get("unsupported_features") or ()
                ),
                spec_digest=str(onboarding.get("spec_digest") or ""),
            )
        )
    for entry in snapshot.get("tasks") or ():
        tracker.record_task(task_record_from_document(entry))
    for extension in snapshot.get("extensions") or ():
        if isinstance(extension, Mapping) and str(extension.get("gap") or "").strip():
            tracker.grant_extension(str(extension["gap"]))
    for violation in snapshot.get("violations") or ():
        if not isinstance(violation, Mapping):
            continue
        kind = str(violation.get("kind") or "")
        if kind not in _AUTHORITY_VIOLATIONS:
            continue  # scope violations regenerate from touched_repos
        tracker.record_authority_violation(
            kind, str(violation.get("detail") or ""), str(violation.get("task_id") or "")
        )
    if stop_reason.strip():
        tracker.preserve_diagnostics(stop_reason.strip())
    return tracker


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopDecision:
    """The evaluated stop condition: continue / extend-once / expand / stop.

    ``diagnostics_pointer`` is set exactly when the decision stopped the
    pilot — the preserved snapshot's digest, recorded in the report.
    """

    decision: str
    reasons: tuple[str, ...] = ()
    gap: str = ""
    met_criteria: tuple[str, ...] = ()
    unmet_criteria: tuple[str, ...] = ()
    unjudgeable_criteria: tuple[str, ...] = ()
    diagnostics_pointer: str = ""

    def as_document(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "gap": self.gap,
            "met_criteria": list(self.met_criteria),
            "unmet_criteria": list(self.unmet_criteria),
            "unjudgeable_criteria": list(self.unjudgeable_criteria),
            "diagnostics_pointer": self.diagnostics_pointer,
        }


def evaluate_stop(
    tracker: PilotTracker,
    spec: PilotSpec | None = None,
    *,
    as_of: date | str | None = None,
    named_gap: str = "",
) -> StopDecision:
    """Evaluate the pilot's stop conditions — ``continue_ | extend_once | expand | stop``.

    The check order (worst first, each documented):

    1. **Authority violations** (an attempted automatic merge, a used
       production secret) → instant :data:`DECISION_STOP` with the
       tracker snapshot preserved verbatim.
    2. **Scope violations** (a task touched outside the data boundary)
       → :data:`DECISION_STOP`, diagnostics preserved.
    3. **Before the named end date** → :data:`DECISION_CONTINUE`.  The
       measurement window was fixed pre-kickoff precisely so the pilot
       is not judged during the adjustment dip (the J-curve).
    4. **At/after the end date** — the 2-of-3 criteria math:
       ≥ 2 criteria met → :data:`DECISION_EXPAND` (the expand
       candidate); otherwise exactly one extend-once for a NAMED gap
       when the extension is still available →
       :data:`DECISION_EXTEND_ONCE`; otherwise :data:`DECISION_STOP`
       with preserved diagnostics.  Unjudgeable criteria (unknown
       metrics) count as not met and are named.
    """
    spec = spec or tracker.spec
    authority = [v for v in tracker.violations if v.kind in _AUTHORITY_VIOLATIONS]
    if authority:
        head = authority[0]
        reason = (
            f"authority violation ({head.kind}"
            f"{' — ' + head.detail if head.detail else ''}): the pilot stops now and the "
            "tracker snapshot is preserved verbatim"
        )
        diagnostics = tracker.preserve_diagnostics(reason)
        return StopDecision(
            decision=DECISION_STOP, reasons=(reason,), diagnostics_pointer=diagnostics.pointer
        )
    scope = [v for v in tracker.violations if v.kind == VIOLATION_SCOPE]
    if scope:
        head = scope[0]
        reason = (
            f"scope violation: task {head.task_id or '?'} {head.detail} — the data boundary "
            "is the pilot's edge; the tracker snapshot is preserved verbatim"
        )
        diagnostics = tracker.preserve_diagnostics(reason)
        return StopDecision(
            decision=DECISION_STOP, reasons=(reason,), diagnostics_pointer=diagnostics.pointer
        )

    decision_date = spec.decision_date
    when = _as_date(as_of) or decision_date
    if when < decision_date:
        return StopDecision(
            decision=DECISION_CONTINUE,
            reasons=(
                f"decision date {decision_date.isoformat()} not reached (as of "
                f"{when.isoformat()}) — {PILOT_DECISION_DATE_NOTE}",
            ),
        )

    metrics = tracker.metrics()
    met: list[str] = []
    unmet: list[str] = []
    unjudgeable: list[str] = []
    for criterion in spec.criteria:
        verdict = criterion.met_by(metrics)
        if verdict is None:
            unjudgeable.append(criterion.criterion_id)
        elif verdict:
            met.append(criterion.criterion_id)
        else:
            unmet.append(criterion.criterion_id)
    summary = (
        f"2-of-3 criteria by {decision_date.isoformat()}: met {met}, unmet {unmet}, "
        f"unjudgeable {unjudgeable}"
    )
    if len(met) >= 2:
        return StopDecision(
            decision=DECISION_EXPAND,
            reasons=(
                f"{summary} — ≥ 2 of 3 met: the expand candidate, gated on the first "
                "gate's single-writable-repo rule",
            ),
            met_criteria=tuple(met),
            unmet_criteria=tuple(unmet),
            unjudgeable_criteria=tuple(unjudgeable),
        )
    if named_gap.strip() and tracker.extensions_used < spec.exits.extension_limit:
        tracker.grant_extension(named_gap.strip())
        return StopDecision(
            decision=DECISION_EXTEND_ONCE,
            gap=named_gap.strip(),
            reasons=(
                f"{summary} — extended once for the named gap {named_gap.strip()!r} with an "
                "explicit decision date; a second extension is a stop",
            ),
            met_criteria=tuple(met),
            unmet_criteria=tuple(unmet),
            unjudgeable_criteria=tuple(unjudgeable),
        )
    reason = f"{summary} — the 2-of-3 rule was not met and no extension is available"
    diagnostics = tracker.preserve_diagnostics(reason)
    return StopDecision(
        decision=DECISION_STOP,
        reasons=(reason,),
        met_criteria=tuple(met),
        unmet_criteria=tuple(unmet),
        unjudgeable_criteria=tuple(unjudgeable),
        diagnostics_pointer=diagnostics.pointer,
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PilotReport:
    """The versioned report artifact (``forge.pilot.report/1``)."""

    document: Mapping[str, Any]

    @property
    def schema(self) -> str:
        return str(self.document.get("schema") or "")

    @property
    def digest(self) -> str:
        """Canonical digest — the determinism proof (same inputs, same bytes)."""
        return _canonical_digest(self.document)

    def write(self, path: Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.document, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )


def build_pilot_report(
    plan: PilotPlan,
    tracker: PilotTracker,
    decision: StopDecision | None = None,
    *,
    as_of: date | str | None = None,
) -> PilotReport:
    """Build the pilot report from the frozen plan and the recorded tracker.

    Enforced, not trusted:

    - the ONBOARDING record exists and its timestamp is STRICTLY before
      the first task start (the unsupported-features list is provably
      pre-task) — violated ordering raises :class:`PilotError`;
    - per-task outcomes list unaccepted tasks and operator rescues
      EXPLICITLY (``unaccepted: true``, rescues with their notes);
      plan tasks without a record appear as ``recorded: false`` — a
      missing record is a named gap, never silence;
    - the five metrics + reviewer load publish beside the frozen
      baseline and the pinned definitions;
    - the 2-of-3 verdict names met/unmet/unjudgeable criteria; the
      recommendation is the :func:`evaluate_stop` decision (evaluated at
      ``as_of`` or the named end date when no decision is passed);
    - capability readiness is decided PER LADDER STAGE: a stage is
      ``ready_to_support`` only when every one of its tasks ran, was
      accepted and needed zero manual rescues — everything else
      ``remains experimental``;
    - a stop carries its reason and the preserved diagnostics pointer.

    The build is deterministic: no clocks, no randomness — the same plan
    and tracker state produce byte-identical documents.
    """
    plan.validate()
    spec = plan.spec
    if tracker.spec_digest != spec.frozen_digest:
        raise PilotError(
            "tracker is bound to a different spec than the plan "
            f"({tracker.spec_digest[:12]}… != {spec.frozen_digest[:12]}…)"
        )
    onboarding = tracker.onboarding
    if onboarding is None:
        raise PilotError(
            "the report requires the pre-onboarding record — the unsupported-features "
            "list must be provably recorded before any task ran"
        )
    first_start = tracker.first_task_started_at
    ordering_ok = True
    if first_start is not None:
        ordering_ok = _parse_datetime(onboarding.recorded_at) < first_start
    if not ordering_ok:
        raise PilotError(
            "onboarding ordering violated: the onboarding record "
            f"({onboarding.recorded_at}) must precede the first task start "
            f"({first_start.isoformat() if first_start else '?'}) — the "
            "unsupported-features list cannot be recorded after tasks ran"
        )
    if decision is None:
        decision = evaluate_stop(tracker, spec, as_of=as_of)
    metrics = tracker.metrics()
    records_by_id = {record.task_id: record for record in tracker.records}

    tasks_section: dict[str, Any] = {}
    for task in plan.tasks:
        record = records_by_id.get(task.task_id)
        if record is None:
            tasks_section[task.task_id] = {
                "scenario": task.scenario,
                "group": task.group,
                "stage": task.stage,
                "owner": task.owner,
                "recorded": False,
                "note": "no tracker record — a missing record is a named gap, never silence",
            }
            continue
        entry = _task_record_document(record)
        entry.update(
            {
                "scenario": task.scenario,
                "group": task.group,
                "stage": task.stage,
                "owner": task.owner,
                "recorded": True,
                "attempts_count": len(record.attempts),
                "accepted_attempts": sum(1 for attempt in record.attempts if attempt.accepted),
            }
        )
        tasks_section[task.task_id] = entry

    groups_section: dict[str, Any] = {}
    for group in spec.review_groups:
        group_ids = {task.task_id for task in plan.tasks if task.group == group}
        group_tasks = [task for task in plan.tasks if task.group == group]
        group_records = [r for r in tracker.records if r.task_id in group_ids]
        groups_section[group] = {
            "stage": group_tasks[0].stage if group_tasks else "",
            "cadence": spec.review_cadence,
            "review_record": {
                "tasks_planned": len(group_tasks),
                "tasks_recorded": len(group_records),
                "tasks_accepted": sum(1 for r in group_records if r.accepted),
                "tasks_unaccepted": sum(1 for r in group_records if not r.accepted),
                "manual_rescues": sum(r.manual_rescues for r in group_records),
                "interventions": sum(len(r.interventions) for r in group_records),
                "review_minutes_total": round(sum(r.review_minutes for r in group_records), 4),
            },
        }

    criteria_section: list[dict[str, Any]] = []
    met_count = 0
    for criterion in spec.criteria:
        verdict = criterion.met_by(metrics)
        met_count += 1 if verdict is True else 0
        criteria_section.append(
            {
                "criterion_id": criterion.criterion_id,
                "statement": criterion.statement,
                "metric": criterion.metric,
                "op": criterion.op,
                "value": criterion.value,
                "method": criterion.method,
                "end_date": criterion.end_date,
                "measured": getattr(metrics, criterion.metric, None),
                "met": verdict,
            }
        )
    criteria_verdict = {
        "rule": "2-of-3 measurable criteria by the named end date",
        "met": met_count,
        "unmet_or_unjudgeable": len(spec.criteria) - met_count,
        "verdict": "met" if met_count >= 2 else "not_met",
    }

    readiness: dict[str, Any] = {}
    for stage in spec.ladder:
        stage_tasks = [task for task in plan.tasks if task.stage == stage.stage]
        stage_ids = {task.task_id for task in stage_tasks}
        stage_records = [r for r in tracker.records if r.task_id in stage_ids]
        accepted = sum(1 for r in stage_records if r.accepted)
        rescues = sum(r.manual_rescues for r in stage_records)
        ready = (
            bool(stage_tasks)
            and len(stage_records) == len(stage_tasks)
            and (accepted == len(stage_records) and rescues == 0)
        )
        readiness[stage.stage] = {
            "label": "ready_to_support" if ready else "experimental",
            "tasks": len(stage_tasks),
            "recorded": len(stage_records),
            "accepted": accepted,
            "manual_rescues": rescues,
            "reason": (
                "every task in the stage ran, was accepted and needed zero manual rescues"
                if ready
                else "the stage still shows unaccepted tasks, missing records or manual rescues"
            ),
        }

    diagnostics = tracker.diagnostics
    document = {
        "schema": REPORT_SCHEMA,
        "pilot_id": spec.pilot_id,
        "plan_id": plan.plan_id,
        "spec": {
            "schema": spec.schema,
            "digest": spec.frozen_digest,
            "window": {"start": spec.window_start, "end": spec.window_end},
            "decision_date": spec.decision_date.isoformat(),
        },
        "onboarding": {
            "recorded_at": onboarding.recorded_at,
            "unsupported_features": list(onboarding.unsupported_features),
            "spec_digest": onboarding.spec_digest,
            "ordering_onboarding_before_first_task": ordering_ok,
        },
        "tasks": tasks_section,
        "groups": groups_section,
        "metrics": {
            **metrics.to_json(),
            "definitions": dict(METRIC_DEFINITIONS),
        },
        "criteria": criteria_section,
        "criteria_verdict": criteria_verdict,
        "readiness": readiness,
        "decision": decision.as_document(),
        "recommendation": {
            "exit": decision.decision,
            "note": (
                "the only exits are expand / extend-once-for-a-named-gap / stop — "
                "each with documented learning"
            ),
        },
        "stop": (
            {
                "reason": diagnostics.stop_reason,
                "diagnostics_pointer": diagnostics.pointer,
                "retried": False,
                "note": "the tracker snapshot is preserved verbatim and is never retried",
            }
            if diagnostics is not None
            else None
        ),
        "provenance_labels": list(spec.provenance_labels),
        "limitations": list(LIMITATIONS),
    }
    return PilotReport(document)
