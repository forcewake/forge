"""The task-specific research-quality cohort (R32-14, review topics 3 §4 + 4 §4).

The review's finding: "the research pass produced text" is not evidence
that the research pass produced BETTER PLANS.  Speed and coverage
counters (calls made, tokens spent, findings minted) all Goodhart the
moment they become targets, and a synthetic benchmark dressed up as
partner evidence is the failure mode this module is built to avoid —
in four moves the research pass prescribes for every forge evaluation:

- **A small, task-specific cohort with SEEDED findings** — every task
  names its archetype's ground truth (the neighbor-repo dependency, the
  missing decision, the deep-file line, the intentionally irrelevant
  repository), so a grade measures whether the mode FOUND what the
  fixture says is there, never luck (:class:`CohortSpec`, the four
  :data:`ARCHETYPES`).
- **The runner is a PURE function over RECORDED artifacts** — research
  documents, :class:`~forge.adaptive.research_planner.ToolObservation`
  traces, plans and budgets per mode — replayable offline with no live
  LLM, no network and no production system.  Promotion reads recorded
  evidence; it never re-runs the thing being judged.
- **Five MERGED, non-redundant rubric dimensions** (correlated candidate
  dimensions are folded together, not accumulated — see
  :data:`RUBRIC_MERGE_NOTES`): evidence accuracy scored against ACTUAL
  snapshot CONTENT (a valid file:line whose bytes do not support the
  claim earns nothing — semantic, not syntactic), impacted-surface
  recall, unjustified assumptions (an invented default scores worse
  than a specific question), question quality, and the recorded human
  plan-correction estimate published BESIDE the quality score as its
  counterweight — never averaged into it.
- **Three-value promotion verdicts** — PASS / HOLD-with-expiry /
  ROLLBACK (:func:`promotion_verdict`).  The richer mode is promoted
  only when it improves useful plan outcomes under the agreed budget;
  incomplete evidence (missing runs, missing cost coverage, tampered
  digests, budget mismatches) routes to HOLD owned by a named human
  with an expiry, and a regression routes to ROLLBACK.  Held-back
  tasks are graded but reported SEPARATELY — they can never move the
  verdict.

Honest semantics are structural, not aspirational: a research document
that stopped on a budget keeps ``complete: false`` and its
``stopped_reason`` all the way into the report; failed and exhausted
attempts appear in the comparative report WITH their cost coverage;
token totals stay lower bounds while any call's usage is unknown; and
the report's notes carry the J-curve / Goodhart caveats the review
demands be pre-committed, not discovered.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from forge.adaptive.discovery_stage import repo_set_digest, validate_plan_citations
from forge.adaptive.research_planner import (
    LEXICAL_MODE,
    NONE_MODE,
    RESEARCH_HARNESS_MODE,
    RESEARCH_SCHEMA,
    ToolObservation,
)

__all__ = [
    "ARCHETYPES",
    "ARCHETYPE_AMBIGUOUS_REQUIREMENT",
    "ARCHETYPE_DEEP_FILE_EVIDENCE",
    "ARCHETYPE_IRRELEVANT_REPOSITORY",
    "ARCHETYPE_NEIGHBOR_DEPENDENCY",
    "ASSUMPTION_WEIGHT_SATURATION",
    "BASELINE_MODE",
    "CANDIDATE_MODE",
    "CLAIM_VERDICTS",
    "COHORT_MODES",
    "COHORT_SCHEMA",
    "CohortReport",
    "CohortRunner",
    "CohortSpec",
    "CohortSpecError",
    "CohortTask",
    "DEFAULT_RUBRICS",
    "DISCOVERY_MODE_OF",
    "INVENTED_DEFAULT_PENALTY",
    "MIN_COHORT_TASKS",
    "MODE_LEXICAL",
    "MODE_NONE",
    "MODE_RESEARCH",
    "MutationRecord",
    "PROMOTABLE_MODES",
    "PromotionPolicy",
    "RECORD_SCHEMA",
    "REPORT_NOTES",
    "REPORT_SCHEMA",
    "RUBRIC_MERGE_NOTES",
    "RecordedRun",
    "Rubric",
    "SCORING_DIMENSIONS",
    "SEMANTIC_TOKEN_COVERAGE",
    "SPURIOUS_SURFACE_PENALTY",
    "SEVERITY_WEIGHTS",
    "Snapshot",
    "SurfaceRef",
    "VERDICT_HOLD",
    "VERDICT_PASS",
    "VERDICT_ROLLBACK",
    "aggregate_mode",
    "apply_contract_mutation",
    "check_claim_support",
    "grade_run",
    "inject_valid_but_irrelevant_citation",
    "integrity_issues",
    "plan_digest",
    "promotion_verdict",
]

#: The schema stamp of a cohort spec document (versioned: bumping the tag
#: is how a breaking change to what a cohort MEANS stays distinguishable
#: from cohorts already published under the old shape).
COHORT_SCHEMA = "forge.research.cohort/1"

#: The schema stamp of the report artifact a replay produces.
REPORT_SCHEMA = "forge.research.cohort.report/1"

#: The schema stamp of one recorded run document.
RECORD_SCHEMA = "forge.research.cohort.run/1"

#: The floor on a shipped cohort's task count (the example cohort carries
#: more; a real partner cohort should not carry fewer).
MIN_COHORT_TASKS = 6

# ---------------------------------------------------------------------------
# The task archetypes — every cohort must exercise all four, and all four
# must appear among the PROMOTABLE (non-held-back) tasks, otherwise the
# promotion verdict would rest on a cohort that never tested what it claims.
# ---------------------------------------------------------------------------

#: The dependency/contract lives only in a NEIGHBOR repository — invisible
#: to any single-repo read, the cross-service coordination trap.
ARCHETYPE_NEIGHBOR_DEPENDENCY = "neighbor_dependency"

#: The requirement is AMBIGUOUS on purpose: a decision is missing, and the
#: graded behavior is a SPECIFIC question, not an invented default.
ARCHETYPE_AMBIGUOUS_REQUIREMENT = "ambiguous_requirement"

#: The relevant code window sits BEYOND the first line of a long file —
#: first-line reads and shallow greps must not earn deep-evidence credit.
ARCHETYPE_DEEP_FILE_EVIDENCE = "deep_file_evidence"

#: The authorized set contains an intentionally IRRELEVANT repository; a
#: plan that drags it into the impacted surface is expanding scope.
ARCHETYPE_IRRELEVANT_REPOSITORY = "irrelevant_repository"

ARCHETYPES: tuple[str, ...] = (
    ARCHETYPE_NEIGHBOR_DEPENDENCY,
    ARCHETYPE_AMBIGUOUS_REQUIREMENT,
    ARCHETYPE_DEEP_FILE_EVIDENCE,
    ARCHETYPE_IRRELEVANT_REPOSITORY,
)

# ---------------------------------------------------------------------------
# The modes compared on the SAME frozen snapshots under the SAME budget.
# ``research`` is the cohort spelling of the planner's ``research-harness``.
# ---------------------------------------------------------------------------

MODE_NONE = "none"
MODE_LEXICAL = "lexical"
MODE_RESEARCH = "research"

COHORT_MODES: tuple[str, ...] = (MODE_NONE, MODE_LEXICAL, MODE_RESEARCH)

#: Cohort mode -> the :func:`~forge.adaptive.research_planner.discovery_mode`
#: spelling the production planner uses for it.
DISCOVERY_MODE_OF: Mapping[str, str] = {
    MODE_NONE: NONE_MODE,
    MODE_LEXICAL: LEXICAL_MODE,
    MODE_RESEARCH: RESEARCH_HARNESS_MODE,
}

#: The promotion question is always "promote the CANDIDATE over the
#: BASELINE" — research over lexical (the incumbent default), with the
#: none column kept as the frozen floor for context.
CANDIDATE_MODE = MODE_RESEARCH
BASELINE_MODE = MODE_LEXICAL
PROMOTABLE_MODES: tuple[str, ...] = (BASELINE_MODE, CANDIDATE_MODE)

# The three-value verdicts (research topic 3 §4: Pass / Waived / Blocked
# transposed to eval gates as PROMOTE / HOLD / ROLLBACK).
VERDICT_PASS = "PASS"
VERDICT_HOLD = "HOLD"
VERDICT_ROLLBACK = "ROLLBACK"

# ---------------------------------------------------------------------------
# The rubric — five MERGED dimensions, four of them scored.
# ---------------------------------------------------------------------------

#: The scored dimensions (0..1, higher better except assumptions which is
#: derived from a penalized weight).  ``human_plan_correction_effort`` is
#: the fifth rubric dimension: recorded minutes published BESIDE the score
#: as the quality counterweight, never averaged into it.
SCORING_DIMENSIONS: tuple[str, ...] = (
    "evidence_accuracy",
    "impacted_surface_recall",
    "unjustified_assumptions",
    "question_quality",
)

#: The merge decisions — correlated candidate dimensions folded INTO a
#: kept dimension instead of accumulating as extra rows (research topic
#: 3 §4: "dimensions merged when they perfectly correlate").
RUBRIC_MERGE_NOTES: tuple[str, ...] = (
    "citation validity (file:line resolves) is folded INTO evidence_accuracy — "
    "a syntactically valid citation earns nothing unless the cited bytes "
    "support the claim, so validity alone is not a separate dimension",
    "scope precision (references into the task's irrelevant repositories) is "
    "folded INTO impacted_surface_recall as the spurious-surface penalty, not "
    "accumulated as a sixth dimension",
    "assumption COUNT and assumption SEVERITY merge into one "
    "unjustified_assumptions weight (severity-weighted, invented defaults "
    "penalized) — count-without-severity double-counts the same mistake",
    "correction minutes stay OUTSIDE the weighted overall: a speed/effort "
    "number is only ever published beside its quality counterweight "
    "(research topic 4 §4), never blended into it",
)

#: Reviewer-recorded severity weights for unjustified assumptions.
SEVERITY_WEIGHTS: Mapping[str, float] = {"high": 3.0, "medium": 2.0, "low": 1.0}

#: An invented default is worse than an unanswered question: it silently
#: bakes a decision into the plan (topic 3 §4's "separate blocker failures
#: from average quality scores" applied to assumptions).
INVENTED_DEFAULT_PENALTY = 2.0

#: The penalized assumption weight at which the dimension's score reaches 0.
ASSUMPTION_WEIGHT_SATURATION = 10.0

#: Fraction of the claim's identifier tokens that must appear in the cited
#: window for a SEMANTIC match (below this the citation is syntactic-only).
SEMANTIC_TOKEN_COVERAGE = 0.6

#: Each impacted-surface entry inside a task's irrelevant repositories
#: subtracts this from the recall score (floored at 0).
SPURIOUS_SURFACE_PENALTY = 0.25

#: The closed vocabulary of per-claim support verdicts.
CLAIM_VERDICTS: tuple[str, ...] = (
    "semantic_match",  # the cited window's actual bytes support the claim
    "syntactic_only",  # valid repo/path/line, but the bytes do not support it
    "invalid",  # the repo/path/line does not resolve inside the snapshot
    "unverified",  # no recorded snapshot to check the claim against
)

#: The report's standing caveats (J-curve / Goodhart pre-commitments).
REPORT_NOTES: tuple[str, ...] = (
    "Held-back tasks are graded and reported SEPARATELY; they never enter "
    "the aggregate means and can never move the promotion verdict.",
    "Exhaustion is never relabeled complete: a research pass stopped on a "
    "budget keeps complete=false and its stopped_reason into the report, "
    "and its partial findings stay partial.",
    "Failed and exhausted attempts appear in this report WITH their cost "
    "coverage; quality means run over graded plans only, cost totals over "
    "every attempt.",
    "Token totals stay lower bounds while any call's usage is unknown; "
    "unknown usage is counted, never silently re-totalled.",
    "Every cost number is published beside its quality counterweight; the "
    "none/lexical columns are the frozen baseline for this cohort, and a "
    "re-recorded baseline is a new cohort.",
    "Goodhart: the rubric grades recorded artifacts; once the cohort drives "
    "optimization, expect gaming — re-record a fresh cohort before a "
    "promotion decision that matters.",
    "J-curve: early candidate-mode runs are expected to dip (tool-call "
    "overhead, wider surfaces); judge promotion on the pre-committed "
    "window recorded in the spec, not the first week.",
    "The recorded artifacts this report replayed are EXAMPLES that exercise "
    "every grading path — not a real partner cohort; the verdict demonstrates "
    "the machinery, not evidence about production.",
)

_STOPWORDS = frozenset(
    {"the", "a", "an", "is", "are", "of", "to", "in", "and", "or", "for", "on", "at", "by", "be"}
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_VERSIONED_RE = re.compile(r"\bv(\d+)\b")
_WEIGHT_EPS = 0.01


class CohortSpecError(ValueError):
    """A cohort spec (or a recorded artifact shaped like one) is invalid."""


# ---------------------------------------------------------------------------
# Spec value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfaceRef:
    """One entry of a task's ground-truth impacted surface (repo + path)."""

    repo_key: str
    path: str

    def as_document(self) -> dict[str, str]:
        return {"repo": self.repo_key, "path": self.path}

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> SurfaceRef:
        return cls(repo_key=str(doc.get("repo") or ""), path=str(doc.get("path") or ""))


@dataclass(frozen=True)
class Rubric:
    """The reviewer rubric one task is graded under.

    The four weights blend the scored dimensions into ``overall``; they are
    the MERGE decisions above made concrete per archetype — an ambiguous
    task leans on questions and assumptions, a neighbor-dependency task on
    recall.  ``min_evidence_accuracy`` and ``max_invented_defaults`` are the
    per-task floors a promotable mode must respect.
    """

    accuracy_weight: float = 0.30
    recall_weight: float = 0.35
    assumptions_weight: float = 0.20
    questions_weight: float = 0.15
    min_evidence_accuracy: float = 0.75
    max_invented_defaults: int = 0

    def weights(self) -> dict[str, float]:
        return {
            "evidence_accuracy": float(self.accuracy_weight),
            "impacted_surface_recall": float(self.recall_weight),
            "unjustified_assumptions": float(self.assumptions_weight),
            "question_quality": float(self.questions_weight),
        }

    def validate(self) -> None:
        weights = self.weights()
        for name, value in weights.items():
            if value < 0:
                raise CohortSpecError(f"rubric weight {name} is negative ({value})")
        total = sum(weights.values())
        if abs(total - 1.0) > _WEIGHT_EPS:
            raise CohortSpecError(
                f"rubric weights must sum to 1.0 (±{_WEIGHT_EPS}), sum {total:.3f}"
            )
        if not 0.0 < self.min_evidence_accuracy <= 1.0:
            raise CohortSpecError(
                f"min_evidence_accuracy must be in (0, 1], got {self.min_evidence_accuracy}"
            )
        if self.max_invented_defaults < 0:
            raise CohortSpecError("max_invented_defaults must be >= 0")

    def as_document(self) -> dict[str, Any]:
        return {
            "accuracy_weight": self.accuracy_weight,
            "recall_weight": self.recall_weight,
            "assumptions_weight": self.assumptions_weight,
            "questions_weight": self.questions_weight,
            "min_evidence_accuracy": self.min_evidence_accuracy,
            "max_invented_defaults": self.max_invented_defaults,
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> Rubric:
        if not isinstance(doc, Mapping):
            return cls()
        kwargs = {
            key: doc[key]
            for key in (
                "accuracy_weight",
                "recall_weight",
                "assumptions_weight",
                "questions_weight",
                "min_evidence_accuracy",
                "max_invented_defaults",
            )
            if key in doc
        }
        return cls(**kwargs)


#: Per-archetype default rubrics — the merge made concrete.
DEFAULT_RUBRICS: Mapping[str, Rubric] = {
    ARCHETYPE_NEIGHBOR_DEPENDENCY: Rubric(
        accuracy_weight=0.30, recall_weight=0.40, assumptions_weight=0.20, questions_weight=0.10
    ),
    ARCHETYPE_AMBIGUOUS_REQUIREMENT: Rubric(
        accuracy_weight=0.20, recall_weight=0.15, assumptions_weight=0.30, questions_weight=0.35
    ),
    ARCHETYPE_DEEP_FILE_EVIDENCE: Rubric(
        accuracy_weight=0.40, recall_weight=0.35, assumptions_weight=0.15, questions_weight=0.10
    ),
    ARCHETYPE_IRRELEVANT_REPOSITORY: Rubric(
        accuracy_weight=0.30, recall_weight=0.45, assumptions_weight=0.15, questions_weight=0.10
    ),
}


@dataclass(frozen=True)
class CohortTask:
    """One cohort unit: frozen snapshot binding + statement + rubric.

    ``snapshot_digest`` is :func:`~forge.adaptive.discovery_stage.repo_set_digest`
    over the task's authorized multi-repo snapshot — the SAME authorization
    identity the discovery stage binds to, so a recorded run can be proved
    (or refused) to have run against what the task authorized.
    """

    task_id: str
    archetype: str
    statement: str
    snapshot_digest: str
    snapshot_ref: str
    rubric: Rubric
    held_back: bool = False
    expected_surface: tuple[SurfaceRef, ...] = ()
    missing_decision: str = ""
    missing_decision_terms: tuple[str, ...] = ()
    irrelevant_repos: tuple[str, ...] = ()

    def as_document(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "archetype": self.archetype,
            "statement": self.statement,
            "snapshot_digest": self.snapshot_digest,
            "snapshot_ref": self.snapshot_ref,
            "held_back": self.held_back,
            "rubric": self.rubric.as_document(),
            "expected_surface": [ref.as_document() for ref in self.expected_surface],
            "missing_decision": self.missing_decision,
            "missing_decision_terms": list(self.missing_decision_terms),
            "irrelevant_repos": list(self.irrelevant_repos),
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> CohortTask:
        if not isinstance(doc, Mapping):
            raise CohortSpecError("task entry is not an object")
        rubric_doc = doc.get("rubric")
        if rubric_doc is None:
            archetype = str(doc.get("archetype") or "")
            rubric = DEFAULT_RUBRICS.get(archetype, Rubric())
        else:
            rubric = Rubric.from_document(rubric_doc)
        return cls(
            task_id=str(doc.get("task_id") or ""),
            archetype=str(doc.get("archetype") or ""),
            statement=str(doc.get("statement") or ""),
            snapshot_digest=str(doc.get("snapshot_digest") or ""),
            snapshot_ref=str(doc.get("snapshot_ref") or ""),
            rubric=rubric,
            held_back=bool(doc.get("held_back") or False),
            expected_surface=tuple(
                SurfaceRef.from_document(entry)
                for entry in doc.get("expected_surface") or []
                if isinstance(entry, Mapping)
            ),
            missing_decision=str(doc.get("missing_decision") or ""),
            missing_decision_terms=tuple(
                str(term) for term in doc.get("missing_decision_terms") or []
            ),
            irrelevant_repos=tuple(str(repo) for repo in doc.get("irrelevant_repos") or []),
        )


@dataclass(frozen=True)
class PromotionPolicy:
    """The AGREED comparison terms, frozen into the spec at record time.

    ``budget`` is the one budget every mode's recorded runs must declare —
    comparability, not convenience: a run recorded under a different budget
    is a different experiment and forces HOLD.
    """

    owner: str
    hold_expires: str
    margin: float
    accuracy_floor: float
    budget: Mapping[str, Any]

    def as_document(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "hold_expires": self.hold_expires,
            "margin": self.margin,
            "accuracy_floor": self.accuracy_floor,
            "budget": dict(self.budget),
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> PromotionPolicy:
        entry = dict(doc or {})
        budget = dict(entry.get("budget") or {})
        return cls(
            owner=str(entry.get("owner") or ""),
            hold_expires=str(entry.get("hold_expires") or ""),
            margin=float(entry.get("margin") or 0.0),
            accuracy_floor=float(entry.get("accuracy_floor") or 0.0),
            budget=budget,
        )

    def validate(self) -> None:
        if not self.owner.strip():
            raise CohortSpecError("promotion owner must be a named human (HOLD routes to them)")
        try:
            date.fromisoformat(self.hold_expires)
        except ValueError as exc:
            raise CohortSpecError(
                f"promotion hold_expires must be an ISO date, got {self.hold_expires!r}"
            ) from exc
        if self.margin < 0:
            raise CohortSpecError("promotion margin must be >= 0")
        if not 0.0 < self.accuracy_floor <= 1.0:
            raise CohortSpecError("promotion accuracy_floor must be in (0, 1]")
        if int(self.budget.get("max_calls") or 0) < 1:
            raise CohortSpecError("promotion budget.max_calls must be >= 1")
        if float(self.budget.get("wall_seconds") or 0) < 1.0:
            raise CohortSpecError("promotion budget.wall_seconds must be >= 1.0")


@dataclass(frozen=True)
class CohortSpec:
    """A versioned, frozen cohort: the tasks, the policy, the replay terms."""

    cohort_id: str
    recorded_at: str
    promotion: PromotionPolicy
    tasks: tuple[CohortTask, ...] = ()
    schema: str = COHORT_SCHEMA

    @property
    def promotable_tasks(self) -> tuple[CohortTask, ...]:
        return tuple(task for task in self.tasks if not task.held_back)

    @property
    def held_back_tasks(self) -> tuple[CohortTask, ...]:
        return tuple(task for task in self.tasks if task.held_back)

    def validate(self) -> None:
        """Structural integrity (raises :class:`CohortSpecError`).

        Pins the contract: the schema tag, unique non-empty task ids, known
        archetypes with their archetype-specific ground truth (an ambiguous
        task MUST name its missing decision; an irrelevant-repository task
        MUST name its irrelevant repos), ALL FOUR archetypes present — and
        present among the promotable tasks — and at least one held-back
        task so the cohort keeps an unseen reserve.
        """
        if self.schema != COHORT_SCHEMA:
            raise CohortSpecError(f"spec schema {self.schema!r} is not {COHORT_SCHEMA!r}")
        if not self.cohort_id.strip():
            raise CohortSpecError("cohort_id must be non-empty")
        if not self.tasks:
            raise CohortSpecError("a cohort with no tasks measures nothing")
        self.promotion.validate()
        seen: set[str] = set()
        archetypes_seen: set[str] = set()
        promotable_archetypes: set[str] = set()
        for task in self.tasks:
            if not task.task_id.strip():
                raise CohortSpecError("every task needs a non-empty task_id")
            if task.task_id in seen:
                raise CohortSpecError(f"duplicate task id {task.task_id!r}")
            seen.add(task.task_id)
            if task.archetype not in ARCHETYPES:
                raise CohortSpecError(
                    f"{task.task_id}: unknown archetype {task.archetype!r}; "
                    f"the closed vocabulary is {list(ARCHETYPES)}"
                )
            if not task.statement.strip():
                raise CohortSpecError(f"{task.task_id}: empty task statement")
            if not task.snapshot_digest:
                raise CohortSpecError(f"{task.task_id}: missing snapshot digest binding")
            if not task.expected_surface:
                raise CohortSpecError(
                    f"{task.task_id}: no expected impacted surface — recall would grade nothing"
                )
            for ref in task.expected_surface:
                if not ref.repo_key or not ref.path:
                    raise CohortSpecError(f"{task.task_id}: malformed expected-surface entry")
            task.rubric.validate()
            if task.archetype == ARCHETYPE_AMBIGUOUS_REQUIREMENT and not task.missing_decision:
                raise CohortSpecError(
                    f"{task.task_id}: ambiguous_requirement task must name its missing decision"
                )
            if task.archetype == ARCHETYPE_IRRELEVANT_REPOSITORY and not task.irrelevant_repos:
                raise CohortSpecError(
                    f"{task.task_id}: irrelevant_repository task must name its irrelevant repos"
                )
            archetypes_seen.add(task.archetype)
            if not task.held_back:
                promotable_archetypes.add(task.archetype)
        missing = set(ARCHETYPES) - archetypes_seen
        if missing:
            raise CohortSpecError(f"archetypes without a task: {sorted(missing)}")
        missing_promotable = set(ARCHETYPES) - promotable_archetypes
        if missing_promotable:
            raise CohortSpecError(
                "archetypes present ONLY as held-back tasks (promotion would rest on a "
                f"cohort that never tested them): {sorted(missing_promotable)}"
            )
        if not self.held_back_tasks:
            raise CohortSpecError("cohort must hold back at least one task as an unseen reserve")

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "cohort_id": self.cohort_id,
            "recorded_at": self.recorded_at,
            "promotion": self.promotion.as_document(),
            "tasks": [task.as_document() for task in self.tasks],
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> CohortSpec:
        if not isinstance(doc, Mapping):
            raise CohortSpecError("spec document is not an object")
        tasks = tuple(CohortTask.from_document(entry) for entry in doc.get("tasks") or [])
        return cls(
            schema=str(doc.get("schema") or ""),
            cohort_id=str(doc.get("cohort_id") or ""),
            recorded_at=str(doc.get("recorded_at") or ""),
            promotion=PromotionPolicy.from_document(doc.get("promotion")),
            tasks=tasks,
        )

    @classmethod
    def load(cls, path: Path) -> CohortSpec:
        spec = cls.from_document(json.loads(Path(path).read_text(encoding="utf-8")))
        spec.validate()
        return spec


# ---------------------------------------------------------------------------
# The frozen snapshots a cohort task authorizes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """A recorded multi-repo snapshot: ``repo_key -> {ids, files}``.

    ``digest`` re-derives the dispatch authorization
    (:func:`repo_set_digest`) from the recorded bytes, so a recorded run's
    snapshot binding is CHECKED, never trusted.
    """

    repos: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return repo_set_digest(
            {key: dict(entry.get("files") or {}) for key, entry in self.repos.items()}
        )

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> Snapshot:
        repos_raw = doc.get("repos") if isinstance(doc, Mapping) else None
        repos: dict[str, dict[str, Any]] = {}
        for key, entry in (repos_raw or {}).items():
            if not isinstance(entry, Mapping):
                raise CohortSpecError(f"snapshot repo {key!r} is not an object")
            repos[str(key)] = {
                "repository_id": str(entry.get("repository_id") or ""),
                "source_oid": str(entry.get("source_oid") or ""),
                "files": {
                    str(path): str(content) for path, content in (entry.get("files") or {}).items()
                },
            }
        return cls(repos=repos)

    @classmethod
    def load(cls, path: Path) -> Snapshot:
        return cls.from_document(json.loads(Path(path).read_text(encoding="utf-8")))

    def files_of(self, repo_key: str) -> Mapping[str, str]:
        entry = self.repos.get(repo_key)
        return dict(entry.get("files") or {}) if entry is not None else {}

    def resolves(self, repo_key: str, path: str, line: int) -> bool:
        """Whether ``repo_key:path:line`` falls inside this snapshot (1-based)."""
        content = self.files_of(repo_key).get(path)
        if content is None:
            return False
        return 1 <= line <= len(content.splitlines())

    def window(self, repo_key: str, path: str, line: int, *, span: int = 2) -> list[str]:
        """The actual snapshot lines around ``line`` (bounded, 1-based)."""
        lines = self.files_of(repo_key).get(path, "").splitlines()
        start = max(0, line - 1 - span)
        stop = min(len(lines), line + span)
        return lines[start:stop]

    def with_file(self, repo_key: str, path: str, content: str) -> Snapshot:
        """A copy with ONE file's bytes replaced (the mutation hook's only write)."""
        repos: dict[str, dict[str, Any]] = {}
        for key, entry in self.repos.items():
            files = dict(entry.get("files") or {})
            if key == repo_key:
                files[path] = content
            repos[key] = {**entry, "files": files}
        return Snapshot(repos=repos)


# ---------------------------------------------------------------------------
# The recorded artifacts the runner replays
# ---------------------------------------------------------------------------


def _observations_of(doc: Mapping[str, Any]) -> tuple[ToolObservation, ...]:
    """Rebuild real :class:`ToolObservation` records from their JSON shape."""
    fields = {name for name in ToolObservation.__dataclass_fields__}
    rebuilt: list[ToolObservation] = []
    for entry in doc.get("observations") or []:
        if not isinstance(entry, Mapping):
            raise CohortSpecError("observation entry is not an object")
        kwargs = {
            key: (str(value) if key != "truncated" else bool(value))
            for key, value in entry.items()
            if key in fields
        }
        missing = fields - set(kwargs)
        if missing:
            raise CohortSpecError(
                f"observation missing field(s) {sorted(missing)}: {dict(entry)!r}"
            )
        rebuilt.append(ToolObservation(**kwargs))
    return tuple(rebuilt)


@dataclass(frozen=True)
class RecordedRun:
    """One recorded (task, mode) outcome: the runner's ONLY input.

    ``attempts`` carries EVERY attempt with its own cost — a first attempt
    that died on a gateway error stays in the record with its spend; the
    plan/research document/observations/reviewer grades belong to the
    FINAL attempt.  A run with no plan at all is a FAILED attempt: it is
    reported (with cost) and never graded.
    """

    task_id: str
    mode: str
    snapshot_digest: str
    budget: Mapping[str, Any]
    attempts: tuple[Mapping[str, Any], ...]
    research_document: Mapping[str, Any] | None
    observations: tuple[ToolObservation, ...]
    plan: Mapping[str, Any] | None
    reviewer: Mapping[str, Any]

    @property
    def outcome(self) -> str:
        """``graded`` when a plan exists, ``failed`` when it does not."""
        return "graded" if isinstance(self.plan, Mapping) else "failed"

    @property
    def final_attempt(self) -> Mapping[str, Any]:
        return self.attempts[-1] if self.attempts else {}

    @property
    def stopped_reason(self) -> str:
        """The FINAL attempt's honest stop ("" when it ran to completion)."""
        return str(self.final_attempt.get("stopped_reason") or "")

    @property
    def research_partial(self) -> bool:
        """Research that stopped on a budget — retained AS partial, never
        relabeled complete because a plan still got produced."""
        if self.mode != MODE_RESEARCH or self.research_document is None:
            return False
        return not bool(self.research_document.get("complete"))

    @property
    def attempts_stopped(self) -> int:
        """How many attempts stopped early (failure OR honest exhaustion) —
        each keeps its cost in the record either way."""
        return sum(1 for attempt in self.attempts if str(attempt.get("stopped_reason") or ""))

    def cost_within(self, budget: Mapping[str, Any]) -> bool:
        """Whether EVERY attempt stayed inside the agreed budget."""
        max_calls = int(budget.get("max_calls") or 0)
        wall = float(budget.get("wall_seconds") or 0.0)
        for attempt in self.attempts:
            cost = attempt.get("cost")
            if not isinstance(cost, Mapping):
                return False
            if int(cost.get("calls_proposed") or 0) > max_calls:
                return False
            if float(cost.get("wall_seconds_used") or 0.0) > wall:
                return False
        return True

    def cost_complete(self) -> bool:
        return bool(self.attempts) and all(
            isinstance(attempt.get("cost"), Mapping) for attempt in self.attempts
        )

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> RecordedRun:
        if not isinstance(doc, Mapping):
            raise CohortSpecError("recorded run is not an object")
        schema = str(doc.get("schema") or "")
        if schema and schema != RECORD_SCHEMA:
            raise CohortSpecError(f"recorded run schema {schema!r} is not {RECORD_SCHEMA!r}")
        plan = doc.get("plan")
        research = doc.get("research_document")
        if isinstance(research, Mapping) and str(research.get("schema") or "") != RESEARCH_SCHEMA:
            raise CohortSpecError(
                f"research document schema {research.get('schema')!r} is not {RESEARCH_SCHEMA!r}"
            )
        attempts = tuple(
            entry if isinstance(entry, Mapping) else {}
            for entry in doc.get("attempts") or [{"attempt": 1, "stopped_reason": ""}]
        )
        if not attempts:
            attempts = ({"attempt": 1, "stopped_reason": ""},)
        return cls(
            task_id=str(doc.get("task_id") or ""),
            mode=str(doc.get("mode") or ""),
            snapshot_digest=str(doc.get("snapshot_digest") or ""),
            budget=dict(doc.get("budget") or {}),
            attempts=attempts,
            research_document=dict(research) if isinstance(research, Mapping) else None,
            observations=_observations_of(doc),
            plan=dict(plan) if isinstance(plan, Mapping) else None,
            reviewer=dict(doc.get("reviewer") or {}),
        )

    @classmethod
    def load(cls, path: Path) -> RecordedRun:
        return cls.from_document(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class MutationRecord:
    """The recorded contract-mutation arm of one task.

    The runner does not trust ``mutated_snapshot_digest`` — it re-applies
    :func:`apply_contract_mutation` to the task's baseline snapshot and
    checks the digests agree.  ``plans`` holds the post-mutation plan per
    mode (the plan the mode produced once the contract moved); the runner
    digests them and compares against the baseline runs.
    """

    task_id: str
    mutated_snapshot_digest: str
    reviewer: Mapping[str, Any]
    plans: Mapping[str, Mapping[str, Any] | None]

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> MutationRecord:
        plans_raw = doc.get("plans") or {}
        plans = {
            str(mode): (dict(plan) if isinstance(plan, Mapping) else None)
            for mode, plan in plans_raw.items()
        }
        return cls(
            task_id=str(doc.get("task_id") or ""),
            mutated_snapshot_digest=str(doc.get("mutated_snapshot_digest") or ""),
            reviewer=dict(doc.get("reviewer") or {}),
            plans=plans,
        )


# ---------------------------------------------------------------------------
# Grading — semantic support, then the five merged dimensions
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall((text or "").lower())
        if len(token) >= 2 and token not in _STOPWORDS
    }


def check_claim_support(
    claim: Mapping[str, Any], snapshot: Snapshot | None, *, span: int = 2
) -> str:
    """The SEMANTIC support verdict for one plan claim.

    The claim carries the content it asserts (``asserted_content``) and the
    repo/path/line it leans on.  Validation is against CONTENT, not syntax:
    a citation whose file:line resolves (a "valid" citation by the syntactic
    half of NXT-06) still earns ``syntactic_only`` when the window's actual
    bytes do not carry the claim's identifier tokens.  ``unverified`` is
    reserved for claims no recorded snapshot can answer — never a pass.
    """
    repo_key = str(claim.get("repo") or "")
    path = str(claim.get("path") or "")
    line = int(claim.get("line") or 0)
    if snapshot is None or not snapshot.repos:
        return "unverified"
    if not snapshot.resolves(repo_key, path, line):
        return "invalid"
    asserted = _tokens(str(claim.get("asserted_content") or ""))
    if not asserted:
        return "syntactic_only"
    window_tokens = _tokens("\n".join(snapshot.window(repo_key, path, line, span=span)))
    coverage = len(asserted & window_tokens) / len(asserted)
    return "semantic_match" if coverage >= SEMANTIC_TOKEN_COVERAGE else "syntactic_only"


def _question_addresses(question_text: str, terms: Sequence[str]) -> bool:
    """Whether a question names the missing decision (any of its terms)."""
    lowered = (question_text or "").lower()
    return any(term.lower() in lowered for term in terms if term.strip())


def _plan_surface(plan: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The (repo, path) pairs the plan declares or its claims cite."""
    surface: list[tuple[str, str]] = []
    for entry in plan.get("surface") or []:
        if isinstance(entry, Mapping):
            surface.append((str(entry.get("repo") or ""), str(entry.get("path") or "")))
    for claim in plan.get("claims") or []:
        if isinstance(claim, Mapping):
            surface.append((str(claim.get("repo") or ""), str(claim.get("path") or "")))
    return surface


def grade_run(task: CohortTask, run: RecordedRun, snapshot: Snapshot | None) -> dict[str, Any]:
    """Grade ONE recorded run under the task's rubric.

    The recorded reviewer grades (claim importance, question specificity,
    assumption severity/invented-default, correction minutes) are INPUTS;
    the aggregates computed here are the module's own — including the
    semantic support verdicts, which are re-derived against the frozen
    snapshot bytes, never read off the recording.  A failed attempt (no
    plan) is graded as exactly that: honest fields plus cost, no dimension
    scores it does not have a plan for.
    """
    grade: dict[str, Any] = {
        "task_id": task.task_id,
        "mode": run.mode,
        "outcome": run.outcome,
        "attempts": [
            {
                "attempt": int(attempt.get("attempt") or index + 1),
                "stopped_reason": str(attempt.get("stopped_reason") or ""),
            }
            for index, attempt in enumerate(run.attempts)
        ],
        "attempts_stopped": run.attempts_stopped,
        "research": (
            {
                "present": run.research_document is not None,
                "schema": str((run.research_document or {}).get("schema") or ""),
                "complete": bool((run.research_document or {}).get("complete")),
                # The DOCUMENT's honest stop (an exhausted loop still produced
                # a plan; the attempt itself did not fail) — never relabeled.
                "stopped_reason": str((run.research_document or {}).get("stopped_reason") or "")
                or run.stopped_reason,
                "partial_retained_as_partial": run.research_partial,
                "observations": len(run.observations),
                "truncated_observations": sum(1 for obs in run.observations if obs.truncated),
                "failed_observations": sum(1 for obs in run.observations if obs.error),
            }
            if run.mode == MODE_RESEARCH
            else None
        ),
        "cost": _cost_summary(run),
        "budget": {"agreed": dict(run.budget), "within": run.cost_within(run.budget)},
    }
    if run.outcome == "failed" or not isinstance(run.plan, Mapping):
        grade["reason"] = run.stopped_reason or "no plan recorded"
        return grade

    plan = run.plan
    # Dimension 1 — evidence accuracy, scored against CONTENT.  Reviewer
    # importance decides which claims matter; the snapshot decides support.
    claim_grades: list[dict[str, Any]] = []
    important = 0
    verdict_counts = {verdict: 0 for verdict in CLAIM_VERDICTS}
    for claim in plan.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        verdict = check_claim_support(claim, snapshot)
        is_important = str(claim.get("importance") or "minor") == "important"
        important += int(is_important)
        verdict_counts[verdict] += 1
        claim_grades.append(
            {
                "claim_id": str(claim.get("claim_id") or ""),
                "verdict": verdict,
                "important": is_important,
            }
        )
    semantic_important = sum(
        1 for entry in claim_grades if entry["important"] and entry["verdict"] == "semantic_match"
    )
    accuracy = 1.0 if important == 0 else semantic_important / important
    grade["evidence_accuracy"] = {
        "score": round(accuracy, 4),
        "important_claims": important,
        "verdicts": verdict_counts,
        "claims": claim_grades,
    }

    # Dimension 2 — impacted-surface recall, with the irrelevant-repo
    # spurious-surface penalty folded in (merged, not a sixth dimension).
    expected = {(ref.repo_key, ref.path) for ref in task.expected_surface}
    declared = {pair for pair in _plan_surface(plan) if pair[0] and pair[1]}
    found = sorted(expected & declared)
    missed = sorted(expected - declared)
    spurious = sorted(pair for pair in declared - expected if pair[0] in set(task.irrelevant_repos))
    recall = len(found) / len(expected) if expected else 1.0
    score = max(0.0, recall - SPURIOUS_SURFACE_PENALTY * len(spurious))
    grade["impacted_surface_recall"] = {
        "score": round(score, 4),
        "recall": round(recall, 4),
        "found": [{"repo": repo, "path": path} for repo, path in found],
        "missed": [{"repo": repo, "path": path} for repo, path in missed],
        "spurious": [{"repo": repo, "path": path} for repo, path in spurious],
    }

    # Dimension 3 — unjustified assumptions: severity-weighted, invented
    # defaults penalized (a missing decision answered with an invented
    # default scores worse than one left as a specific question).
    by_severity: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    invented = 0
    details: list[dict[str, Any]] = []
    weight = 0.0
    for assumption in plan.get("assumptions") or []:
        if not isinstance(assumption, Mapping):
            continue
        severity = str(assumption.get("severity") or "low")
        if severity not in by_severity:
            severity = "low"
        by_severity[severity] += 1
        penalty = 0.0
        if bool(assumption.get("invented_default") or False):
            invented += 1
            penalty = INVENTED_DEFAULT_PENALTY
        weight += SEVERITY_WEIGHTS[severity] + penalty
        details.append(
            {
                "text": str(assumption.get("text") or "")[:200],
                "severity": severity,
                "invented_default": bool(assumption.get("invented_default") or False),
            }
        )
    grade["unjustified_assumptions"] = {
        "score": round(max(0.0, 1.0 - weight / ASSUMPTION_WEIGHT_SATURATION), 4),
        "weight": round(weight, 2),
        "count": sum(by_severity.values()),
        "by_severity": by_severity,
        "invented_defaults": invented,
        "detail": details,
    }

    # Dimension 4 — question quality: where a decision is missing, a
    # SPECIFIC question naming it is the graded behavior; generic
    # questions (and invented defaults, above) score against the mode.
    questions = [entry for entry in plan.get("questions") or [] if isinstance(entry, Mapping)]
    specific = sum(1 for entry in questions if bool(entry.get("specific") or False))
    addressed: bool | None = None
    if task.missing_decision:
        terms = task.missing_decision_terms or (task.missing_decision,)
        addressed = any(
            bool(entry.get("specific")) and _question_addresses(str(entry.get("text") or ""), terms)
            for entry in questions
        )
        score_q = (
            (0.7 + 0.3 * (specific / len(questions) if questions else 0.0)) if addressed else 0.0
        )
    else:
        score_q = 1.0 if not questions else specific / len(questions)
    grade["question_quality"] = {
        "score": round(score_q, 4),
        "total": len(questions),
        "specific": specific,
        "generic": len(questions) - specific,
        "missing_decision_addressed": addressed,
    }

    # Dimension 5 — human plan-correction effort: the recorded reviewer
    # estimate, published beside the score (the counterweight), never
    # blended into the overall.
    grade["human_plan_correction_effort"] = {
        "minutes": float(run.reviewer.get("correction_minutes") or 0.0),
        "severity": str(run.reviewer.get("correction_severity") or "unrecorded"),
        "recorded": "correction_minutes" in run.reviewer,
    }

    weights = task.rubric.weights()
    overall = sum(weights[dim] * float(grade[dim]["score"]) for dim in SCORING_DIMENSIONS)
    grade["overall"] = round(overall, 4)
    return grade


def _cost_summary(run: RecordedRun) -> dict[str, Any]:
    """Per-run cost coverage — attempts missing cost say so, never zero."""
    if not run.cost_complete():
        return {"coverage": "missing"}
    calls_proposed = 0
    calls_executed = 0
    wall = 0.0
    input_known = 0
    output_known = 0
    input_exact = True
    output_exact = True
    unknown_calls = 0
    for attempt in run.attempts:
        cost = dict(attempt.get("cost") or {})
        calls_proposed += int(cost.get("calls_proposed") or 0)
        calls_executed += int(cost.get("calls_executed") or 0)
        wall += float(cost.get("wall_seconds_used") or 0.0)
        tokens = cost.get("tokens") if isinstance(cost.get("tokens"), Mapping) else {}
        input_exact = input_exact and tokens.get("input") is not None
        output_exact = output_exact and tokens.get("output") is not None
        input_known += int(tokens.get("input_lower_bound") or tokens.get("input") or 0)
        output_known += int(tokens.get("output_lower_bound") or tokens.get("output") or 0)
        unknown_calls += int(tokens.get("unknown_usage_calls") or 0)
    return {
        "coverage": "recorded",
        "attempts": len(run.attempts),
        "calls_proposed": calls_proposed,
        "calls_executed": calls_executed,
        "wall_seconds_used": round(wall, 3),
        "tokens": {
            "input": input_known if input_exact else None,
            "output": output_known if output_exact else None,
            "input_lower_bound": input_known,
            "output_lower_bound": output_known,
            "unknown_usage_calls": unknown_calls,
        },
    }


# ---------------------------------------------------------------------------
# Aggregation, integrity, verdict
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def aggregate_mode(
    grades: Sequence[Mapping[str, Any]], runs: Sequence[RecordedRun]
) -> dict[str, Any]:
    """Aggregate ONE mode over the PROMOTABLE tasks' grades and runs.

    Quality means run over graded outcomes only (a failed attempt has no
    plan to grade); cost totals and failure counts run over EVERY attempt —
    failed and exhausted attempts keep their spend in the comparative view.
    """
    graded = [grade for grade in grades if grade.get("outcome") == "graded"]
    cost_complete = 0
    calls_proposed = 0
    calls_executed = 0
    wall = 0.0
    input_known = 0
    output_known = 0
    input_exact = True
    output_exact = True
    unknown_calls = 0
    budget_overruns: list[str] = []
    for run in runs:
        if run.cost_complete():
            cost_complete += 1
        for attempt in run.attempts:
            cost = attempt.get("cost")
            if not isinstance(cost, Mapping):
                continue
            calls_proposed += int(cost.get("calls_proposed") or 0)
            calls_executed += int(cost.get("calls_executed") or 0)
            wall += float(cost.get("wall_seconds_used") or 0.0)
            tokens = cost.get("tokens") if isinstance(cost.get("tokens"), Mapping) else {}
            input_exact = input_exact and tokens.get("input") is not None
            output_exact = output_exact and tokens.get("output") is not None
            input_known += int(tokens.get("input_lower_bound") or tokens.get("input") or 0)
            output_known += int(tokens.get("output_lower_bound") or tokens.get("output") or 0)
            unknown_calls += int(tokens.get("unknown_usage_calls") or 0)
        if not run.cost_within(run.budget):
            budget_overruns.append(run.task_id)
    stopped_reasons: dict[str, int] = {}
    complete = 0
    exhausted = 0
    if runs and runs[0].mode == MODE_RESEARCH:
        for run in runs:
            doc = run.research_document or {}
            if run.outcome == "failed":
                stopped_reasons[run.stopped_reason or "unknown"] = (
                    stopped_reasons.get(run.stopped_reason or "unknown", 0) + 1
                )
            elif bool(doc.get("complete")):
                complete += 1
            else:
                exhausted += 1
                reason = str(doc.get("stopped_reason") or "unknown")
                stopped_reasons[reason] = stopped_reasons.get(reason, 0) + 1
    return {
        "tasks_total": len(runs),
        "tasks_graded": len(graded),
        "attempts_stopped_total": sum(int(grade.get("attempts_stopped") or 0) for grade in grades),
        "evidence_accuracy_mean": _mean([g["evidence_accuracy"]["score"] for g in graded]),
        "impacted_surface_recall_mean": _mean(
            [g["impacted_surface_recall"]["score"] for g in graded]
        ),
        "unjustified_assumptions_weight_mean": _mean(
            [g["unjustified_assumptions"]["weight"] for g in graded]
        ),
        "question_quality_mean": _mean([g["question_quality"]["score"] for g in graded]),
        "correction_minutes_mean": _mean(
            [float(g["human_plan_correction_effort"].get("minutes") or 0.0) for g in graded]
        ),
        "invented_defaults_total": sum(
            int(g["unjustified_assumptions"]["invented_defaults"]) for g in graded
        ),
        "overall_mean": _mean([float(g.get("overall") or 0.0) for g in graded]),
        "research": (
            {
                "complete": complete,
                "exhausted": exhausted,
                "stopped_reasons": stopped_reasons,
            }
            if runs and runs[0].mode == MODE_RESEARCH
            else None
        ),
        "cost": {
            "coverage": "complete"
            if cost_complete == len(runs) and runs
            else ("missing" if cost_complete == 0 else "partial"),
            "tasks_with_cost": cost_complete,
            "calls_proposed_total": calls_proposed,
            "calls_executed_total": calls_executed,
            "wall_seconds_used_total": round(wall, 3),
            "tokens": {
                "input": input_known if input_exact else None,
                "output": output_known if output_exact else None,
                "input_lower_bound": input_known,
                "output_lower_bound": output_known,
                "unknown_usage_calls": unknown_calls,
            },
        },
        "budget_overruns": sorted(budget_overruns),
    }


def integrity_issues(
    spec: CohortSpec,
    runs: Mapping[str, Mapping[str, RecordedRun]],
    snapshots: Mapping[str, Snapshot],
    mutations: Mapping[str, MutationRecord] | None = None,
) -> dict[str, list[str]]:
    """Every reason the recorded evidence is incomplete or unbound.

    Empty lists mean the evidence binds: every promotable task has a
    recorded run per mode, each run's snapshot digest equals the task's
    (and the snapshot's bytes re-derive it), every attempt carries cost,
    every run declares the agreed budget, research-plan citations resolve
    to the research document's own findings, and every recorded mutation
    digest matches a re-applied mutation.  Any issue forces HOLD —
    promotion never leans on evidence it cannot bind.
    """
    binding: list[str] = []
    missing_runs: list[str] = []
    held_back_missing: list[str] = []
    cost_gaps: list[str] = []
    budget_mismatches: list[str] = []
    citation_violations: list[str] = []
    mutation_mismatches: list[str] = []
    for task in spec.tasks:
        snapshot = snapshots.get(task.snapshot_digest)
        if snapshot is None:
            binding.append(f"{task.task_id}: no recorded snapshot hashes to {task.snapshot_digest}")
        elif snapshot.digest != task.snapshot_digest:
            binding.append(
                f"{task.task_id}: snapshot bytes re-derive {snapshot.digest}, "
                f"task authorizes {task.snapshot_digest}"
            )
        task_runs = runs.get(task.task_id, {})
        for mode in COHORT_MODES:
            run = task_runs.get(mode)
            if run is None:
                entry = f"{task.task_id}/{mode}"
                (held_back_missing if task.held_back else missing_runs).append(entry)
                continue
            if run.snapshot_digest != task.snapshot_digest:
                binding.append(
                    f"{task.task_id}/{mode}: run bound to snapshot {run.snapshot_digest}, "
                    f"task authorizes {task.snapshot_digest}"
                )
            if not run.cost_complete():
                cost_gaps.append(f"{task.task_id}/{mode}: attempt without recorded cost")
            if dict(run.budget) != dict(spec.promotion.budget):
                budget_mismatches.append(
                    f"{task.task_id}/{mode}: budget {dict(run.budget)} != agreed "
                    f"{dict(spec.promotion.budget)}"
                )
            if run.mode == MODE_RESEARCH and isinstance(run.plan, Mapping):
                doc = run.research_document or {}
                known = {
                    str(finding.get("evidence_id"))
                    for finding in doc.get("findings") or []
                    if isinstance(finding, Mapping) and finding.get("evidence_id")
                }
                violations = validate_plan_citations(run.plan, known)
                if violations:
                    citation_violations.append(
                        f"{task.task_id}/{mode}: {'; '.join(violations[:3])}"
                    )
    for task_id, record in (mutations or {}).items():
        snapshot = snapshots.get(
            next(
                (task.snapshot_digest for task in spec.tasks if task.task_id == task_id),
                "",
            )
        )
        if snapshot is None:
            mutation_mismatches.append(f"{task_id}: mutation arm has no baseline snapshot")
            continue
        try:
            expected_digest = apply_contract_mutation(snapshot, task_id).digest
        except CohortSpecError as exc:
            mutation_mismatches.append(f"{task_id}: mutation does not bind ({exc})")
            continue
        if record.mutated_snapshot_digest != expected_digest:
            mutation_mismatches.append(
                f"{task_id}: recorded mutation digest {record.mutated_snapshot_digest} != "
                f"re-applied {expected_digest}"
            )
    return {
        "binding_violations": binding,
        "missing_runs": missing_runs,
        "held_back_missing_runs": held_back_missing,
        "cost_coverage_gaps": cost_gaps,
        "budget_mismatches": budget_mismatches,
        "citation_violations": citation_violations,
        "mutation_mismatches": mutation_mismatches,
    }


def promotion_verdict(
    policy: PromotionPolicy,
    aggregate: Mapping[str, Mapping[str, Any]],
    issues: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """The three-value promotion verdict over the recorded evidence.

    Ordering (worst-last-wins is deliberately NOT used; the order is):

    1. any integrity issue → HOLD — incomplete evidence is never a
       regression verdict, and never a promotion;
    2. candidate REGRESSIONS → ROLLBACK (overall below baseline, accuracy
       under the floor, or more invented defaults than the baseline);
    3. improvement below the agreed margin, or budget comparability
       broken → HOLD owned by ``policy.owner`` until ``hold_expires``;
    4. otherwise PASS — the richer mode improved useful plan outcomes
       under the agreed budget.
    """
    reasons: list[str] = []
    flat_issues = [f"{key}: {entry}" for key, entries in issues.items() for entry in entries]
    if flat_issues:
        return _verdict_document(
            VERDICT_HOLD,
            ["evidence incomplete — promotion never leans on evidence it cannot bind"]
            + flat_issues[:8],
            policy,
            aggregate,
        )
    baseline = aggregate.get(BASELINE_MODE) or {}
    candidate = aggregate.get(CANDIDATE_MODE) or {}
    base_overall = baseline.get("overall_mean")
    cand_overall = candidate.get("overall_mean")
    if base_overall is None or cand_overall is None:
        return _verdict_document(
            VERDICT_HOLD,
            ["no graded outcomes for the baseline/candidate comparison"],
            policy,
            aggregate,
        )
    if cand_overall < base_overall:
        reasons.append(
            f"{CANDIDATE_MODE} overall {cand_overall} below {BASELINE_MODE} {base_overall} "
            "— the richer mode did not improve useful plan outcomes"
        )
        return _verdict_document(VERDICT_ROLLBACK, reasons, policy, aggregate)
    cand_accuracy = candidate.get("evidence_accuracy_mean")
    if cand_accuracy is not None and cand_accuracy < policy.accuracy_floor:
        reasons.append(
            f"{CANDIDATE_MODE} evidence accuracy {cand_accuracy} under the agreed floor "
            f"{policy.accuracy_floor}"
        )
        return _verdict_document(VERDICT_ROLLBACK, reasons, policy, aggregate)
    cand_invented = int(candidate.get("invented_defaults_total") or 0)
    base_invented = int(baseline.get("invented_defaults_total") or 0)
    if cand_invented > base_invented:
        reasons.append(
            f"{CANDIDATE_MODE} invented {cand_invented} default(s) vs {BASELINE_MODE}'s "
            f"{base_invented} — more silent decisions is a regression, not a trade"
        )
        return _verdict_document(VERDICT_ROLLBACK, reasons, policy, aggregate)
    delta = round(cand_overall - base_overall, 4)
    if delta < policy.margin:
        reasons.append(
            f"improvement {delta:+.4f} below the agreed margin {policy.margin:+.4f} — "
            "not promotable, not a regression"
        )
        return _verdict_document(VERDICT_HOLD, reasons, policy, aggregate)
    overruns = list(candidate.get("budget_overruns") or [])
    if overruns:
        reasons.append(f"budget comparability broken — runs outside the agreed budget: {overruns}")
        return _verdict_document(VERDICT_HOLD, reasons, policy, aggregate)
    reasons.append(
        f"{CANDIDATE_MODE} overall {cand_overall} beats {BASELINE_MODE} {base_overall} "
        f"by {delta:+.4f} (>= margin {policy.margin:+.4f}) under the agreed budget, "
        f"accuracy {cand_accuracy} at/above the floor {policy.accuracy_floor}"
    )
    return _verdict_document(VERDICT_PASS, reasons, policy, aggregate)


def _verdict_document(
    verdict: str,
    reasons: Sequence[str],
    policy: PromotionPolicy,
    aggregate: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "verdict": verdict,
        "reasons": list(reasons),
        "candidate_mode": CANDIDATE_MODE,
        "baseline_mode": BASELINE_MODE,
    }
    if verdict == VERDICT_HOLD:
        document["hold_owner"] = policy.owner
        document["hold_expires"] = policy.hold_expires
        document["hold_semantics"] = (
            "HOLD routes to the named owner and EXPIRES on the recorded date; an expired "
            "HOLD without a recorded human decision is a ROLLBACK, never a silent pass"
        )
    document["compared"] = {
        mode: {
            "overall_mean": aggregate.get(mode, {}).get("overall_mean"),
            "evidence_accuracy_mean": aggregate.get(mode, {}).get("evidence_accuracy_mean"),
            "invented_defaults_total": aggregate.get(mode, {}).get("invented_defaults_total"),
        }
        for mode in PROMOTABLE_MODES
    }
    return document


# ---------------------------------------------------------------------------
# The runner (pure replay) and the report artifact
# ---------------------------------------------------------------------------


def plan_digest(plan: Mapping[str, Any]) -> str:
    """A stable digest over a plan's canonical JSON (mutation comparison)."""
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CohortRunner:
    """Replay a cohort's recorded artifacts into a report — nothing else.

    The runner never contacts a model, the network, or a production
    system: :meth:`run` is a pure function of ``(spec, snapshots, runs,
    mutations)`` and the same inputs always produce the same report.  The
    modes are compared on the SAME frozen snapshots under the SAME
    recorded budget, and every recorded artifact is re-validated (digests,
    budgets, cost coverage, citation resolution) before it can influence
    anything.
    """

    spec: CohortSpec
    snapshots: Mapping[str, Snapshot]
    runs: Mapping[str, Mapping[str, RecordedRun]]
    mutations: Mapping[str, MutationRecord]

    def __init__(
        self,
        spec: CohortSpec,
        snapshots: Mapping[str, Snapshot],
        runs: Mapping[str, Mapping[str, RecordedRun]],
        mutations: Mapping[str, MutationRecord] | None = None,
    ) -> None:
        spec.validate()
        unknown = set(runs) - {task.task_id for task in spec.tasks}
        if unknown:
            raise CohortSpecError(f"recorded runs for unknown tasks: {sorted(unknown)}")
        self.spec = spec
        self.snapshots = dict(snapshots)
        self.runs = {task: dict(mode_runs) for task, mode_runs in runs.items()}
        self.mutations = dict(mutations or {})

    @classmethod
    def from_directory(
        cls, spec_path: Path, snapshots_dir: Path, recorded_dir: Path
    ) -> CohortRunner:
        """Load a recorded cohort off disk (the offline replay entrypoint).

        Layout: ``spec_path`` (the cohort JSON), ``snapshots_dir/*.json``
        (snapshot blobs, keyed by their re-derived digest) and
        ``recorded_dir/<task_id>/<mode>.json`` plus optional
        ``recorded_dir/<task_id>/mutation.json`` per task.
        """
        spec = CohortSpec.load(Path(spec_path))
        snapshots: dict[str, Snapshot] = {}
        for path in sorted(Path(snapshots_dir).glob("*.json")):
            snapshot = Snapshot.load(path)
            snapshots[snapshot.digest] = snapshot
        runs: dict[str, dict[str, RecordedRun]] = {}
        mutations: dict[str, MutationRecord] = {}
        recorded_root = Path(recorded_dir)
        for task in spec.tasks:
            task_dir = recorded_root / task.task_id
            if not task_dir.is_dir():
                continue
            mode_runs: dict[str, RecordedRun] = {}
            for mode in COHORT_MODES:
                mode_path = task_dir / f"{mode}.json"
                if mode_path.is_file():
                    mode_runs[mode] = RecordedRun.load(mode_path)
            runs[task.task_id] = mode_runs
            mutation_path = task_dir / "mutation.json"
            if mutation_path.is_file():
                mutations[task.task_id] = MutationRecord.from_document(
                    json.loads(mutation_path.read_text(encoding="utf-8"))
                )
        return cls(spec, snapshots, runs, mutations)

    def grade_task(self, task: CohortTask) -> dict[str, dict[str, Any]]:
        snapshot = self.snapshots.get(task.snapshot_digest)
        grades: dict[str, dict[str, Any]] = {}
        for mode in COHORT_MODES:
            run = self.runs.get(task.task_id, {}).get(mode)
            if run is None:
                continue
            grades[mode] = grade_run(task, run, snapshot)
        return grades

    def mutation_section(self, task: CohortTask) -> dict[str, Any] | None:
        record = self.mutations.get(task.task_id)
        if record is None:
            return None
        snapshot = self.snapshots.get(task.snapshot_digest)
        try:
            recomputed = apply_contract_mutation(snapshot, task.task_id).digest if snapshot else ""
        except CohortSpecError:
            # The baseline snapshot does not carry the contract (a tampered or
            # foreign tree) — recorded as a mismatch, never a crash.
            recomputed = ""
        modes: dict[str, Any] = {}
        for mode in PROMOTABLE_MODES:
            baseline_run = self.runs.get(task.task_id, {}).get(mode)
            mutated_plan = record.plans.get(mode)
            entry: dict[str, Any] = {
                "digest_matches_reapplied_mutation": bool(
                    recomputed and record.mutated_snapshot_digest == recomputed
                ),
                "change_reason_substantive": bool(record.reviewer.get("change_reason_substantive")),
                "reviewer_notes": str(record.reviewer.get("notes") or ""),
            }
            if (
                isinstance(mutated_plan, Mapping)
                and baseline_run is not None
                and isinstance(baseline_run.plan, Mapping)
            ):
                entry["plan_changed"] = plan_digest(mutated_plan) != plan_digest(baseline_run.plan)
            else:
                entry["plan_changed"] = None
            modes[mode] = entry
        return {
            "task_id": task.task_id,
            "mutated_snapshot_digest": record.mutated_snapshot_digest,
            "reapplied_digest": recomputed,
            "modes": modes,
        }

    def run(self) -> CohortReport:
        """The pure replay: grade everything, aggregate, verdict."""
        task_sections: dict[str, Any] = {}
        held_back_sections: dict[str, Any] = {}
        for task in self.spec.tasks:
            grades = self.grade_task(task)
            section: dict[str, Any] = {
                "archetype": task.archetype,
                "snapshot_digest": task.snapshot_digest,
                "modes": grades,
            }
            mutation = self.mutation_section(task)
            if mutation is not None:
                section["mutation"] = mutation
            if task.held_back:
                held_back_sections[task.task_id] = section
            else:
                task_sections[task.task_id] = section
        aggregate: dict[str, Any] = {}
        for mode in COHORT_MODES:
            grades = [
                section["modes"][mode]
                for section in task_sections.values()
                if mode in section["modes"]
            ]
            mode_runs = [
                self.runs[task.task_id][mode]
                for task in self.spec.promotable_tasks
                if mode in self.runs.get(task.task_id, {})
            ]
            aggregate[mode] = aggregate_mode(grades, mode_runs)
        issues = integrity_issues(self.spec, self.runs, self.snapshots, self.mutations)
        document = {
            "schema": REPORT_SCHEMA,
            "cohort_id": self.spec.cohort_id,
            "spec_schema": self.spec.schema,
            "recorded_at": self.spec.recorded_at,
            "replay": {"pure_over_recorded": True, "live_calls": 0},
            "mode_spellings": dict(DISCOVERY_MODE_OF),
            "promotion_policy": self.spec.promotion.as_document(),
            "rubric_merge_notes": list(RUBRIC_MERGE_NOTES),
            "integrity": issues,
            "tasks": task_sections,
            "held_back": {
                "note": "graded but SEPARATE: held-back tasks never enter the aggregates "
                "and can never move the promotion verdict",
                "tasks": held_back_sections,
            },
            "aggregate": aggregate,
            "promotion": promotion_verdict(self.spec.promotion, aggregate, issues),
            "notes": list(REPORT_NOTES),
        }
        return CohortReport(document)


@dataclass(frozen=True)
class CohortReport:
    """The versioned report artifact a replay produces."""

    document: Mapping[str, Any]

    @property
    def schema(self) -> str:
        return str(self.document.get("schema") or "")

    @property
    def verdict(self) -> str:
        return str(self.document.get("promotion", {}).get("verdict") or "")

    def write(self, path: Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.document, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# The mutation + injection hooks
# ---------------------------------------------------------------------------


#: The contract-mutation registry for the shipped cohort: task id ->
#: (repo, path, old bytes, new bytes).  The mutation always moves the
#: CROSS-SERVICE contract (the bytes both services coordinate on), so a
#: plan that understood the contract has something to react to.
_MUTATION_REGISTRY: Mapping[str, tuple[str, str, str, str]] = {
    "RC-01-neighbor-expiry-event": (
        "billing",
        "src/events.py",
        "order.expired.v2",
        "order.expired.v3",
    ),
}


def apply_contract_mutation(snapshot: Snapshot, task_id: str) -> Snapshot:
    """Mutate the relevant cross-service contract in a COPY of *snapshot*.

    Registry tasks move their named contract bytes; any other task gets the
    deterministic fallback — the FIRST versioned token (``v<N>``) in the
    first (sorted) repo's first (sorted) file that carries one, bumped by
    one.  The original snapshot is never touched; the returned snapshot's
    digest always differs (or the function refuses — a no-op mutation
    would be a fixture bug, not a result).
    """
    if not isinstance(snapshot, Snapshot) or not snapshot.repos:
        raise CohortSpecError("apply_contract_mutation needs a non-empty snapshot")
    entry = _MUTATION_REGISTRY.get(task_id)
    if entry is not None:
        repo_key, path, old, new = entry
        content = snapshot.files_of(repo_key).get(path)
        if content is None or old not in content:
            raise CohortSpecError(
                f"mutation registry entry for {task_id!r} does not bind: "
                f"{repo_key}:{path} does not carry {old!r}"
            )
        mutated = snapshot.with_file(repo_key, path, content.replace(old, new))
        if mutated.digest == snapshot.digest:
            raise CohortSpecError(f"mutation for {task_id!r} changed nothing")
        return mutated
    for repo_key in sorted(snapshot.repos):
        for path in sorted(snapshot.files_of(repo_key)):
            content = snapshot.files_of(repo_key)[path]
            match = _VERSIONED_RE.search(content)
            if match is None:
                continue
            bumped = f"v{int(match.group(1)) + 1}"
            mutated = snapshot.with_file(
                repo_key, path, content[: match.start()] + bumped + content[match.end() :]
            )
            if mutated.digest == snapshot.digest:
                continue
            return mutated
    raise CohortSpecError(f"no cross-service contract token found to mutate for task {task_id!r}")


def inject_valid_but_irrelevant_citation(recorded_doc: Mapping[str, Any]) -> dict[str, Any]:
    """The adversarial probe: a syntactically VALID, semantically
    irrelevant citation added to a copy of a recorded research document.

    The injected finding cites a REAL line of a REAL file (bytes taken
    from the recorded read window itself, so the anchor resolves against
    the snapshot tree), chosen deterministically as the window's LAST
    line whose tokens do not overlap the document's own summary — i.e.
    content the investigation saw but that supports nothing.  The hook
    exists to pin the grading contract: leaning on the decoy is
    ``syntactic_only`` support and earns NO accuracy credit, because the
    accuracy dimension is scored against content, never against
    validity.  The input document is never mutated.
    """
    findings = [
        finding for finding in (recorded_doc.get("findings") or []) if isinstance(finding, Mapping)
    ]
    if not findings:
        raise CohortSpecError("cannot inject into a research document with no findings")
    anchor = next(
        (
            finding
            for finding in findings
            if len(str(finding.get("content") or "").splitlines()) >= 2
        ),
        findings[0],
    )
    summary_tokens = _tokens(str(recorded_doc.get("summary") or ""))
    lines = str(anchor.get("content") or "").splitlines()
    decoy_index = len(lines) - 1
    for index in range(len(lines) - 1, -1, -1):
        if not (_tokens(lines[index]) & summary_tokens):
            decoy_index = index
            break
    decoy_line = lines[decoy_index]
    decoy = {
        "repo_key": str(anchor.get("repo_key") or ""),
        "repository_id": str(anchor.get("repository_id") or ""),
        "source_oid": str(anchor.get("source_oid") or ""),
        "path": str(anchor.get("path") or ""),
        "line": int(anchor.get("line") or 1) + decoy_index,
        "kind": "injected_decoy",
        "detail": "valid-but-irrelevant citation (adversarial injection probe)",
        "text": decoy_line[:400],
        "content": decoy_line,
        "evidence_id": f"{str(anchor.get('evidence_id') or 'evidence')}:decoy",
    }
    document = dict(recorded_doc)
    document["findings"] = [dict(finding) for finding in findings] + [decoy]
    return document
