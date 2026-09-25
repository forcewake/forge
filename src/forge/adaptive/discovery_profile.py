"""R38-11 — the customer-scale discovery PLANNING PROFILE machinery.

The live fixture scenario (R37-09 / :mod:`scripts.run_discovery_live`)
proved the read-many/write-one boundary on ONE authored task: the
decisive neighbor window found, the decoy excluded, the unauthorized
read refused. What it did NOT prove is that the same bargain survives
CONTACT SCALE — several repositories, several decisive facts at
different depths, a large irrelevant repository trying to eat the
budget, and a discovery stage whose findings implementation does not
pay for again. This module is that machinery, deliberately independent
of any one scenario:

- **The observation cache** (:class:`ObservationCache`) — repeated reads
  of the same immutable source (repository + OID + path) under ONE
  policy scope REUSE the verified observation; the underlying reader is
  never re-paid. The policy scope is part of the key, so a cached
  observation can never leak across scopes (:meth:`ObservationCache
  .assert_no_cross_scope_leak`), and the cache rides the discovery
  record (:func:`attach_observation_cache` /
  :func:`cache_from_record`) so a restart resumes with its
  observations intact instead of re-reading them.
- **The exhaustion taxonomies** (:func:`classify_investigation`) — one
  bounded classification over an investigation's terminal state:
  ``completed``, ``exhausted_budget`` (visible to the approver WITH the
  retained findings — never a complete-system-understanding claim),
  ``truncated_output``, ``invalid_synthesis`` and ``confused_policy``
  (the conflicting-current-vs-obsolete case). Each class carries its
  OWN bounded :class:`Recovery`: exhaustion surfaces and asks, a
  truncated window gets exactly one continuation read, an invalid
  synthesis gets exactly one re-synthesis under schema validation, and
  a policy conflict becomes an explicit QUESTION carrying BOTH
  citations (:func:`conflict_question`) — never a silent pick.
- **Budget-suited synthesis** (:class:`SynthesisBudgetProfile`,
  :func:`validate_plan_synthesis`) — a per-mode output-budget profile
  (the reasoning-heavy route reserves MORE of the plan budget for the
  model's reasoning behavior), and a structural validation that NEVER
  silently changes plan meaning: a plan that fails validation is
  returned UNMODIFIED with its errors and routes to the
  ``invalid_synthesis`` recovery — nothing is coerced, defaulted or
  repaired.
- **The carry-forward** (:class:`ImplementationCarryForward`) — the
  discovery findings rendered into the implementation brief's evidence
  section: the plan's facts / assumptions / questions with their
  citations, verifiable inside the plan text and consumable by the lane
  through the APPROVED brief bytes
  (:mod:`forge.harnesses.brief_envelope`) — the findings are carried
  forward, not re-paid at implementation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from forge.harnesses.brief_envelope import (
    build_brief_envelope,
    extract_approved_sections,
    render_approved_sections,
    verify_brief_envelope,
)

__all__ = [
    "CARRY_FORWARD_BEGIN",
    "CARRY_FORWARD_END",
    "CARRY_FORWARD_SCHEMA",
    "CarryForwardEntry",
    "CachedObservation",
    "CLASSIFICATION_VALUES",
    "CLASS_COMPLETED",
    "CLASS_CONFUSED_POLICY",
    "CLASS_EXHAUSTED_BUDGET",
    "CLASS_INVALID_SYNTHESIS",
    "CLASS_TRUNCATED_OUTPUT",
    "ConflictSide",
    "ImplementationCarryForward",
    "InvestigationOutcome",
    "OBSERVATION_CACHE_SCHEMA",
    "ObservationCache",
    "PLAN_SYNTHESIS_SCHEMA",
    "PolicyConflict",
    "RECOVERY_CONTINUATION_READ",
    "RECOVERY_EXPLICIT_QUESTION",
    "RECOVERY_NONE",
    "RECOVERY_RE_SYNTHESIS",
    "RECOVERY_SURFACE_AND_ASK",
    "RECOVERY_VALUES",
    "Recovery",
    "SYNTHESIS_BUDGET_MODES",
    "SYNTHESIS_BUDGET_PROFILES",
    "SynthesisBudgetProfile",
    "SynthesisValidation",
    "TaxonomyVerdict",
    "attach_observation_cache",
    "brief_envelope_seam",
    "budget_profile_for",
    "cache_from_record",
    "carry_forward",
    "classify_investigation",
    "conflict_question",
    "planning_scope",
    "render_carry_forward_section",
    "review_scope",
    "validate_plan_synthesis",
    "verify_carry_forward",
]

# ---------------------------------------------------------------------------
# Schema stamps
# ---------------------------------------------------------------------------

#: The schema stamp of a serialized observation cache (the discovery
#: record's ``observation_cache`` section).
OBSERVATION_CACHE_SCHEMA = "forge.discovery.observation-cache/1"

#: The schema stamp of the implementation carry-forward document.
CARRY_FORWARD_SCHEMA = "forge.discovery.carry-forward/1"

#: The schema stamp a plan synthesis is validated against.
PLAN_SYNTHESIS_SCHEMA = "forge.discovery.plan-synthesis/1"

# ---------------------------------------------------------------------------
# The observation cache — verified reuse of immutable reads, scope-isolated
# ---------------------------------------------------------------------------

#: Delimiters around the rendered carry-forward section — the same
#: survival property as every other delimited forge section: the block
#: passes prompt/plan assembly unchanged and can be inspected alone.
CARRY_FORWARD_BEGIN = "<<<FORGE_DISCOVERY_CARRY_FORWARD"
CARRY_FORWARD_END = "FORGE_DISCOVERY_CARRY_FORWARD>>>"


def planning_scope(authorized_set_digest: str) -> str:
    """The policy scope of the PLANNING consumer over one authorized set."""
    return f"planning:{str(authorized_set_digest)[:12]}"


def review_scope(authorized_set_digest: str) -> str:
    """The policy scope of the REVIEW consumer over one authorized set."""
    return f"review:{str(authorized_set_digest)[:12]}"


@dataclass(frozen=True)
class CachedObservation:
    """One VERIFIED read of an immutable source under one policy scope.

    The identity is the issue's own: which repository, at which frozen
    OID, which path, read under which policy scope. ``content`` is the
    verified full content as the reader returned it; windows are sliced
    from it, so a second window of the same immutable source under the
    same scope is also reuse, not a re-pay.
    """

    repository: str
    source_oid: str
    path: str
    policy_scope: str
    content: str

    def window(self, offset: int = 0, length: int | None = None) -> str:
        """The requested slice of the verified content."""
        end = len(self.content) if length is None else min(len(self.content), offset + length)
        return self.content[max(0, offset) : end]

    def as_document(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "source_oid": self.source_oid,
            "path": self.path,
            "policy_scope": self.policy_scope,
            "content": self.content,
        }


@dataclass(frozen=True)
class _ServedRecord:
    """One serve action — the evidence the isolation assertion walks."""

    action: str  # "hit" | "miss"
    repository: str
    source_oid: str
    path: str
    policy_scope: str


class ObservationCache:
    """Cache of verified observations, keyed by (repository, OID, path,
    policy scope).

    The point is NOT text similarity: two different files with equal
    bytes under different identities stay different cache entries, and
    the SAME immutable file under a DIFFERENT policy scope is a MISS
    with its own fresh verified read — the scope is part of the
    identity, so an observation verified for one authorization can
    never serve another. Repeated reads under one scope reuse the
    verified bytes and never touch the underlying reader again.

    The cache serializes through :meth:`as_document` /
    :meth:`from_document` so it can ride a discovery record: a restart
    restores the observations and resumes without re-paying them.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str, str], CachedObservation] = {}
        self._served: list[_ServedRecord] = []
        self.hits = 0
        self.misses = 0
        #: How many times the UNDERLYING reader actually ran (misses
        #: only — the reuse economy's numerator is hits/lookups).
        self.underlying_reads = 0

    # -- lookup -----------------------------------------------------------

    def _key(
        self, repository: str, source_oid: str, path: str, policy_scope: str
    ) -> tuple[str, str, str, str]:
        if not all(str(value).strip() for value in (repository, source_oid, path, policy_scope)):
            raise ValueError(
                "an observation cache key needs non-empty repository, source_oid,"
                " path and policy_scope"
            )
        return (
            str(repository),
            str(source_oid),
            str(path),
            str(policy_scope),
        )

    def get(
        self, repository: str, source_oid: str, path: str, policy_scope: str
    ) -> CachedObservation | None:
        """The cached observation for the EXACT key, or None (no leak by
        construction: a different scope is a different key)."""
        return self._entries.get(self._key(repository, source_oid, path, policy_scope))

    def observe(
        self,
        reader: Callable[[str], str],
        *,
        repository: str,
        source_oid: str,
        path: str,
        policy_scope: str,
        offset: int = 0,
        length: int | None = None,
    ) -> CachedObservation:
        """Read *path* through *reader* ONCE per (repository/OID/path/scope).

        A miss executes the reader and stores the verified observation;
        a hit returns the stored bytes WITHOUT the reader. The returned
        observation's :meth:`CachedObservation.window` slice is what the
        caller asked for — windows never re-pay either.
        """
        key = self._key(repository, source_oid, path, policy_scope)
        cached = self._entries.get(key)
        if cached is None:
            self.misses += 1
            self.underlying_reads += 1
            cached = CachedObservation(
                repository=key[0],
                source_oid=key[1],
                path=key[2],
                policy_scope=key[3],
                content=str(reader(path)),
            )
            self._entries[key] = cached
            self._served.append(_ServedRecord("miss", key[0], key[1], key[2], key[3]))
        else:
            self.hits += 1
            self._served.append(_ServedRecord("hit", key[0], key[1], key[2], key[3]))
        return cached

    # -- the isolation assertion ------------------------------------------

    def scopes_of(self, repository: str, source_oid: str, path: str) -> tuple[str, ...]:
        """Every policy scope an immutable source was cached under."""
        prefix = self._key(repository, source_oid, path, "@")
        return tuple(
            sorted(
                entry.policy_scope
                for entry in self._entries.values()
                if (entry.repository, entry.source_oid, entry.path) == prefix[:3]
            )
        )

    def assert_no_cross_scope_leak(self) -> None:
        """Assert the cache never served an observation across scopes.

        Walks the complete serve log: every HIT must have been served by
        an entry whose own policy scope equals the requested one, and no
        (repository, OID, path) identity may map two scopes onto one
        entry. Raises :class:`AssertionError` with the offending record.
        """
        by_key = {
            (entry.repository, entry.source_oid, entry.path, entry.policy_scope): entry
            for entry in self._entries.values()
        }
        for record in self._served:
            served = by_key.get(
                (record.repository, record.source_oid, record.path, record.policy_scope)
            )
            if served is None:
                raise AssertionError(
                    f"cross-scope leak: a {record.action} under scope"
                    f" {record.policy_scope!r} resolved to no entry of its own"
                )
            if served.policy_scope != record.policy_scope:
                raise AssertionError(
                    f"cross-scope leak: scope {record.policy_scope!r} was served an"
                    f" observation verified under {served.policy_scope!r}"
                    f" ({record.repository}/{record.path})"
                )
        # No identity may share ONE entry across scopes (defensive: the
        # map is keyed with the scope, so this holds by construction —
        # the assertion documents and checks the invariant).
        seen: dict[int, str] = {}
        for entry in self._entries.values():
            identity = id(entry)
            scope = seen.setdefault(identity, entry.policy_scope)
            if scope != entry.policy_scope:
                raise AssertionError(
                    f"cross-scope leak: one cached observation serves scopes"
                    f" {scope!r} and {entry.policy_scope!r}"
                )

    # -- the record round-trip ---------------------------------------------

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "underlying_reads": self.underlying_reads,
        }

    def as_document(self) -> dict[str, Any]:
        """The cache as it rides the discovery record (restart resumption)."""
        return {
            "schema": OBSERVATION_CACHE_SCHEMA,
            "stats": dict(self.stats),
            "entries": [entry.as_document() for entry in self._entries.values()],
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ObservationCache:
        """Restore a cache from its recorded form.

        Refuses a foreign schema loudly. The restored cache serves hits
        for every recorded entry without any reader — resumption is
        reuse, not re-payment.
        """
        if not isinstance(document, Mapping) or document.get("schema") != OBSERVATION_CACHE_SCHEMA:
            raise ValueError(
                f"an observation cache document must carry schema {OBSERVATION_CACHE_SCHEMA!r}"
            )
        cache = cls()
        for entry in document.get("entries") or []:
            if not isinstance(entry, Mapping):
                continue
            observation = CachedObservation(
                repository=str(entry.get("repository") or ""),
                source_oid=str(entry.get("source_oid") or ""),
                path=str(entry.get("path") or ""),
                policy_scope=str(entry.get("policy_scope") or ""),
                content=str(entry.get("content") or ""),
            )
            key = cache._key(
                observation.repository,
                observation.source_oid,
                observation.path,
                observation.policy_scope,
            )
            cache._entries[key] = observation
        return cache


def attach_observation_cache(record: dict[str, Any], cache: ObservationCache) -> dict[str, Any]:
    """Ride the cache on a discovery record (in place, for persistence)."""
    record["observation_cache"] = cache.as_document()
    return record


def cache_from_record(record: Mapping[str, Any]) -> ObservationCache | None:
    """The cache a discovery record carried (None when it never rode).

    The restart path: a record-bearing cache is restored with its
    verified observations, so the resuming run REUSES them instead of
    re-reading the immutable sources.
    """
    section = record.get("observation_cache") if isinstance(record, Mapping) else None
    if not isinstance(section, Mapping):
        return None
    return ObservationCache.from_document(section)


# ---------------------------------------------------------------------------
# The exhaustion taxonomies — one bounded classification, one bounded recovery
# ---------------------------------------------------------------------------

CLASS_COMPLETED = "completed"
CLASS_EXHAUSTED_BUDGET = "exhausted_budget"
CLASS_TRUNCATED_OUTPUT = "truncated_output"
CLASS_INVALID_SYNTHESIS = "invalid_synthesis"
CLASS_CONFUSED_POLICY = "confused_policy"

#: The closed classification vocabulary.
CLASSIFICATION_VALUES = (
    CLASS_COMPLETED,
    CLASS_EXHAUSTED_BUDGET,
    CLASS_TRUNCATED_OUTPUT,
    CLASS_INVALID_SYNTHESIS,
    CLASS_CONFUSED_POLICY,
)

RECOVERY_NONE = "none"
RECOVERY_SURFACE_AND_ASK = "surface_and_ask"
RECOVERY_CONTINUATION_READ = "continuation_read"
RECOVERY_RE_SYNTHESIS = "re_synthesis"
RECOVERY_EXPLICIT_QUESTION = "explicit_question"

#: The closed recovery vocabulary (each bound to exactly one class).
RECOVERY_VALUES = (
    RECOVERY_NONE,
    RECOVERY_SURFACE_AND_ASK,
    RECOVERY_CONTINUATION_READ,
    RECOVERY_RE_SYNTHESIS,
    RECOVERY_EXPLICIT_QUESTION,
)


@dataclass(frozen=True)
class ConflictSide:
    """One side of a policy conflict, WITH its citation."""

    revision: str
    line: int
    content: str

    def as_document(self) -> dict[str, Any]:
        return {"revision": self.revision, "line": self.line, "content": self.content}


@dataclass(frozen=True)
class PolicyConflict:
    """A CURRENT-vs-OBSOLETE policy pair found in one neighbor file.

    Both sides carry their exact citation (path + line + quoted bytes);
    the honest product of a conflict is :func:`conflict_question` — an
    explicit question with BOTH citations — never a silent pick of one
    side.
    """

    repository: str
    source_oid: str
    path: str
    current: ConflictSide
    obsolete: ConflictSide

    def as_document(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "source_oid": self.source_oid,
            "path": self.path,
            "current": self.current.as_document(),
            "obsolete": self.obsolete.as_document(),
        }

    def citations(self) -> tuple[dict[str, Any], ...]:
        both = (
            {"repository": self.repository, "path": self.path, **self.current.as_document()},
            {"repository": self.repository, "path": self.path, **self.obsolete.as_document()},
        )
        return both


def conflict_question(conflict: PolicyConflict) -> str:
    """The explicit question a policy conflict becomes — both citations.

    The question names the repository, the file, BOTH line numbers and
    BOTH quoted values, so the approver decides from evidence. It is
    the only sanctioned resolution: picking a side silently is the
    failure mode this taxonomy exists to prevent.
    """
    return (
        f"Which policy is current for {conflict.repository}/{conflict.path}: the"
        f" {conflict.current.revision} value at line {conflict.current.line}"
        f" ({conflict.current.content.strip()!r}) or the"
        f" {conflict.obsolete.revision} value at line {conflict.obsolete.line}"
        f" ({conflict.obsolete.content.strip()!r})? Both are present in the"
        " neighbor; the plan does not pick silently."
    )


@dataclass(frozen=True)
class Recovery:
    """The BOUNDED recovery attached to exactly one classification.

    Bounds are counts, not vibes: ``max_extra_reads`` /
    ``max_extra_syntheses`` are the recovery's whole spend authority
    (exhaustion asks an approver for ZERO further reads; a truncated
    window earns exactly ONE continuation read; an invalid synthesis
    earns exactly ONE re-synthesis under schema validation).
    """

    kind: str
    detail: str
    max_extra_reads: int = 0
    max_extra_syntheses: int = 0
    question: str = ""
    citations: tuple[Mapping[str, Any], ...] = ()
    continuation: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind not in RECOVERY_VALUES:
            raise ValueError(f"unknown recovery kind {self.kind!r}")
        if self.kind == RECOVERY_EXPLICIT_QUESTION and not self.question:
            raise ValueError("an explicit-question recovery must carry its question")
        if self.kind == RECOVERY_CONTINUATION_READ and self.continuation is None:
            raise ValueError("a continuation recovery must name its continuation window")

    def as_document(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "max_extra_reads": self.max_extra_reads,
            "max_extra_syntheses": self.max_extra_syntheses,
            "question": self.question,
            "citations": [dict(citation) for citation in self.citations],
            "continuation": dict(self.continuation) if self.continuation else None,
        }


@dataclass(frozen=True)
class InvestigationOutcome:
    """The terminal state of one investigation, pre-classification.

    Deliberately small: the classifier consumes what the run ACTUALLY
    established, not prose. ``synthesis`` participates only when a plan
    synthesis was attempted; ``conflicts`` are the unresolved
    current-vs-obsolete pairs the run found (a conflict resolved by an
    operator answer is no longer carried here).
    """

    declared_done: bool
    stopped_reason: str = ""
    truncated_observations: int = 0
    synthesis: SynthesisValidation | None = None
    conflicts: tuple[PolicyConflict, ...] = ()

    def as_document(self) -> dict[str, Any]:
        return {
            "declared_done": self.declared_done,
            "stopped_reason": self.stopped_reason,
            "truncated_observations": self.truncated_observations,
            "synthesis": (self.synthesis.as_document() if self.synthesis is not None else None),
            "conflicts": [conflict.as_document() for conflict in self.conflicts],
        }


@dataclass(frozen=True)
class TaxonomyVerdict:
    """One classification plus its OWN bounded recovery."""

    classification: str
    recovery: Recovery
    detail: str
    #: True when the retained findings stay visible to the approver —
    #: every non-``completed`` class keeps them; nothing discards them.
    retained_findings_visible: bool = True
    #: ALWAYS False: no classification ever licenses a complete-system-
    #: understanding claim, ``completed`` included (it means the bounded
    #: investigation finished, not that the system is understood).
    complete_understanding_claimed: bool = False

    @property
    def honest_summary(self) -> str:
        if self.classification == CLASS_COMPLETED:
            return "bounded investigation complete; findings carry forward"
        return f"investigation partial ({self.classification}); retained findings visible"

    def as_document(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "recovery": self.recovery.as_document(),
            "detail": self.detail,
            "retained_findings_visible": self.retained_findings_visible,
            "complete_understanding_claimed": self.complete_understanding_claimed,
            "honest_summary": self.honest_summary,
        }


def classify_investigation(outcome: InvestigationOutcome) -> TaxonomyVerdict:
    """Classify one terminal investigation state, with its own recovery.

    Precedence (documented, deterministic):

    1. an unresolved policy conflict dominates everything — a confused
       policy with a perfect budget is still confused;
    2. an attempted-and-failed synthesis — the research may be fine, the
       PRODUCT is not usable as a plan;
    3. a recorded stop reason — the budget ended the run;
    4. truncated observations without a stop — the run paused on a cut
       window and a continuation read is the bounded next step;
    5. a declared ``done`` with none of the above — completed;
    6. anything else — a run that neither stopped nor finished is
       surfaced honestly as exhausted, never as complete.
    """
    if outcome.conflicts:
        conflict = outcome.conflicts[0]
        question = conflict_question(conflict)
        return TaxonomyVerdict(
            classification=CLASS_CONFUSED_POLICY,
            recovery=Recovery(
                kind=RECOVERY_EXPLICIT_QUESTION,
                detail=(
                    "conflicting current/obsolete policies found — ask with BOTH"
                    " citations; never pick a side silently"
                ),
                question=question,
                citations=tuple(
                    citation for one in outcome.conflicts for citation in one.citations()
                ),
            ),
            detail=(
                f"{len(outcome.conflicts)} unresolved policy conflict(s), first in"
                f" {conflict.repository}/{conflict.path}"
            ),
        )
    if outcome.synthesis is not None and not outcome.synthesis.ok:
        return TaxonomyVerdict(
            classification=CLASS_INVALID_SYNTHESIS,
            recovery=Recovery(
                kind=RECOVERY_RE_SYNTHESIS,
                detail=(
                    "the plan synthesis failed schema validation — ONE bounded"
                    " re-synthesis with validation; the failed plan is kept"
                    " unmodified, never silently repaired"
                ),
                max_extra_syntheses=1,
            ),
            detail=("synthesis invalid: " + "; ".join(outcome.synthesis.errors[:4])),
        )
    if outcome.stopped_reason:
        return TaxonomyVerdict(
            classification=CLASS_EXHAUSTED_BUDGET,
            recovery=Recovery(
                kind=RECOVERY_SURFACE_AND_ASK,
                detail=(
                    "the budget stopped the research — surface the exhaustion and"
                    " the RETAINED findings to the approver and ask; no further"
                    " reads are authorized by this recovery"
                ),
                question=(
                    f"research stopped early ({outcome.stopped_reason}) — approve"
                    " another budget, narrow the task, or proceed on the retained"
                    " findings; the plan will not claim complete understanding"
                ),
            ),
            detail=f"stopped_reason={outcome.stopped_reason}",
        )
    if outcome.truncated_observations > 0:
        return TaxonomyVerdict(
            classification=CLASS_TRUNCATED_OUTPUT,
            recovery=Recovery(
                kind=RECOVERY_CONTINUATION_READ,
                detail=(
                    f"{outcome.truncated_observations} observation(s) were cut —"
                    " ONE bounded continuation read of the exact window"
                ),
                max_extra_reads=1,
                continuation={
                    "note": "continue reading the truncated window where it was cut",
                    "reads": 1,
                },
            ),
            detail=(
                f"{outcome.truncated_observations} truncated observation(s) pending a continuation"
            ),
        )
    if outcome.declared_done:
        return TaxonomyVerdict(
            classification=CLASS_COMPLETED,
            recovery=Recovery(kind=RECOVERY_NONE, detail="carry the findings forward"),
            detail="the investigation declared done with no pending cut or conflict",
        )
    return TaxonomyVerdict(
        classification=CLASS_EXHAUSTED_BUDGET,
        recovery=Recovery(
            kind=RECOVERY_SURFACE_AND_ASK,
            detail=(
                "the run neither finished nor recorded a stop — surface it and"
                " the retained findings honestly; never present it as complete"
            ),
            question=(
                "the investigation ended without a done declaration or a recorded"
                " stop reason — approve a re-run or proceed on the retained"
                " findings; no complete-understanding claim is made"
            ),
        ),
        detail="no done declaration and no recorded stop reason",
    )


# ---------------------------------------------------------------------------
# Budget-suited synthesis — per-mode reserves, validation that never rewrites
# ---------------------------------------------------------------------------

#: The closed budget-mode vocabulary.
SYNTHESIS_BUDGET_MODES = ("reasoning-heavy", "standard")


@dataclass(frozen=True)
class SynthesisBudgetProfile:
    """The output-budget profile one capture mode synthesizes plans under.

    ``reasoning_reserve_tokens`` is the part of the plan budget reserved
    for the model's own reasoning behavior BEFORE plan content — the
    reasoning-heavy route needs the LARGER reserve or its JSON gets cut
    mid-emission (exactly what the live attempts in R37-09 showed at
    3000/6000 tokens).
    """

    mode: str
    plan_max_tokens: int
    reasoning_reserve_tokens: int
    research_max_tokens_per_call: int

    def __post_init__(self) -> None:
        if self.mode not in SYNTHESIS_BUDGET_MODES:
            raise ValueError(
                f"unknown synthesis budget mode {self.mode!r}"
                f" (expected one of {SYNTHESIS_BUDGET_MODES})"
            )
        if int(self.plan_max_tokens) < 1 or int(self.reasoning_reserve_tokens) < 0:
            raise ValueError("token budgets must be positive (reserve >= 0)")
        if int(self.reasoning_reserve_tokens) >= int(self.plan_max_tokens):
            raise ValueError("the reasoning reserve must leave room for plan content")
        if int(self.research_max_tokens_per_call) < 1:
            raise ValueError("research_max_tokens_per_call must be >= 1")

    @property
    def plan_content_tokens(self) -> int:
        """The tokens left for plan CONTENT after the reasoning reserve."""
        return int(self.plan_max_tokens) - int(self.reasoning_reserve_tokens)

    def as_document(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "plan_max_tokens": int(self.plan_max_tokens),
            "reasoning_reserve_tokens": int(self.reasoning_reserve_tokens),
            "plan_content_tokens": self.plan_content_tokens,
            "research_max_tokens_per_call": int(self.research_max_tokens_per_call),
        }


#: The per-mode profiles. The reasoning-heavy route reserves MORE (the
#: live attempts' truncation history priced this), and its per-call
#: research cap matches the R37-09 caps that finally emitted full JSON.
SYNTHESIS_BUDGET_PROFILES: dict[str, SynthesisBudgetProfile] = {
    "reasoning-heavy": SynthesisBudgetProfile(
        mode="reasoning-heavy",
        plan_max_tokens=12000,
        reasoning_reserve_tokens=4000,
        research_max_tokens_per_call=3000,
    ),
    "standard": SynthesisBudgetProfile(
        mode="standard",
        plan_max_tokens=8000,
        reasoning_reserve_tokens=800,
        research_max_tokens_per_call=3000,
    ),
}


def budget_profile_for(mode: str) -> SynthesisBudgetProfile:
    """The profile for *mode* — unknown modes REFUSE (fail closed).

    An unrecognized model-behavior mode must never silently fall back
    to the smaller reserve: that is how a reasoning model's JSON gets
    cut mid-emission again.
    """
    profile = SYNTHESIS_BUDGET_PROFILES.get(str(mode))
    if profile is None:
        raise ValueError(
            f"no synthesis budget profile for mode {mode!r}"
            f" (known: {sorted(SYNTHESIS_BUDGET_PROFILES)})"
        )
    return profile


@dataclass(frozen=True)
class SynthesisValidation:
    """The verdict over one parsed plan synthesis — verdict, not repair.

    ``raw`` is the ORIGINAL parsed mapping even when validation fails:
    a failed validation never returns a cleaned, defaulted or coerced
    plan. The only sanctioned consumer of a failure is the
    ``invalid_synthesis`` recovery (one bounded re-synthesis).
    """

    ok: bool
    errors: tuple[str, ...]
    raw: Mapping[str, Any] | None

    @property
    def classification(self) -> str:
        return CLASS_INVALID_SYNTHESIS if not self.ok else CLASS_COMPLETED

    @property
    def recovery(self) -> Recovery:
        if self.ok:
            return Recovery(kind=RECOVERY_NONE, detail="no recovery needed")
        return Recovery(
            kind=RECOVERY_RE_SYNTHESIS,
            detail=(
                "ONE bounded re-synthesis under schema validation; the invalid"
                " plan is kept unmodified — meaning is never silently changed"
            ),
            max_extra_syntheses=1,
        )

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SYNTHESIS_SCHEMA,
            "ok": self.ok,
            "errors": list(self.errors),
            "recovery": self.recovery.as_document(),
            "raw_kept_unmodified": self.raw is not None,
        }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_plan_synthesis(parsed: Mapping[str, Any] | None) -> SynthesisValidation:
    """Structurally validate a parsed plan synthesis WITHOUT changing it.

    The shape: ``steps``/``claims``/``questions``/``assumptions``/
    ``write_targets`` lists; every claim a mapping with non-empty
    ``repo``/``path`` and integer ``line_start <= line_end``; every
    question/assumption/write target a string; at least one write
    target. Any deviation is an ERROR — nothing is coerced, dropped or
    defaulted, and the failed document rides back unchanged as
    ``raw`` so the recovery can show exactly what was wrong.
    """
    if not isinstance(parsed, Mapping):
        return SynthesisValidation(False, ("the synthesis is not a JSON object",), None)
    errors: list[str] = []
    for key in ("steps", "claims", "questions", "assumptions", "write_targets"):
        if not isinstance(parsed.get(key), list):
            errors.append(f"{key} must be a list")
    if errors:
        return SynthesisValidation(False, tuple(errors), parsed)
    for index, step in enumerate(parsed["steps"], start=1):
        if not isinstance(step, Mapping):
            errors.append(f"step {index} must be an object")
    for index, claim in enumerate(parsed["claims"], start=1):
        if not isinstance(claim, Mapping):
            errors.append(f"claim {index} must be an object")
            continue
        if not str(claim.get("repo") or "").strip():
            errors.append(f"claim {index} needs a non-empty repo")
        if not str(claim.get("path") or "").strip():
            errors.append(f"claim {index} needs a non-empty path")
        start, end = claim.get("line_start"), claim.get("line_end")
        if not _is_int(start) or not _is_int(end) or start < 1 or end < start:
            errors.append(f"claim {index} needs integer line_start <= line_end (>= 1)")
    for key in ("questions", "assumptions", "write_targets"):
        for index, entry in enumerate(parsed[key], start=1):
            if not isinstance(entry, str) or not entry.strip():
                errors.append(f"{key} entry {index} must be a non-empty string")
    if not parsed["write_targets"]:
        errors.append("write_targets must name the one writable target")
    return SynthesisValidation(ok=not errors, errors=tuple(errors), raw=parsed)


# ---------------------------------------------------------------------------
# The carry-forward — findings into the implementation brief, paid once
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CarryForwardEntry:
    """One carried finding: a fact with its citation, an assumption, or a
    question the implementation must not silently decide."""

    evidence_id: str
    kind: str  # "fact" | "assumption" | "question"
    text: str
    repository: str = ""
    path: str = ""
    line_start: int = 0
    line_end: int = 0
    source_oid: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("fact", "assumption", "question"):
            raise ValueError(f"unknown carry-forward entry kind {self.kind!r}")
        if not str(self.evidence_id).strip() or not str(self.text).strip():
            raise ValueError("a carry-forward entry needs an id and text")

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "text": self.text,
        }
        if self.kind == "fact":
            document.update(
                {
                    "repository": self.repository,
                    "path": self.path,
                    "line_start": self.line_start,
                    "line_end": self.line_end,
                    "source_oid": self.source_oid,
                }
            )
        return document


@dataclass(frozen=True)
class ImplementationCarryForward:
    """The discovery findings in the shape the implementation brief eats.

    Built ONCE from the approved plan (:func:`carry_forward`): the
    plan's evidence section is what the lane consumes — through the
    approved brief bytes (:func:`brief_envelope_seam`) — so
    implementation receives the findings instead of paying for a second
    discovery.
    """

    discovery_id: str
    snapshot_digest: str
    entries: tuple[CarryForwardEntry, ...] = field(default=())

    @property
    def facts(self) -> tuple[CarryForwardEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == "fact")

    @property
    def assumptions(self) -> tuple[CarryForwardEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == "assumption")

    @property
    def questions(self) -> tuple[CarryForwardEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == "question")

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": CARRY_FORWARD_SCHEMA,
            "discovery_id": self.discovery_id,
            "snapshot_digest": self.snapshot_digest,
            "counts": {
                "facts": len(self.facts),
                "assumptions": len(self.assumptions),
                "questions": len(self.questions),
            },
            "entries": [entry.as_document() for entry in self.entries],
        }


def carry_forward(
    plan: Mapping[str, Any],
    *,
    discovery_id: str,
    snapshot_digest: str,
) -> ImplementationCarryForward:
    """Mint the carry-forward from the plan's OWN evidence section.

    Facts come from the plan's claims (their citations ride verbatim),
    assumptions and questions from the plan's lists. Nothing is
    re-derived from the repositories: what discovery established is
    what implementation receives.
    """
    entries: list[CarryForwardEntry] = []
    for claim in plan.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        entries.append(
            CarryForwardEntry(
                evidence_id=str(claim.get("claim_id") or f"cf{len(entries) + 1}"),
                kind="fact",
                text=str(claim.get("text") or ""),
                repository=str(claim.get("repo") or ""),
                path=str(claim.get("path") or ""),
                line_start=int(claim.get("line_start") or 0),
                line_end=int(claim.get("line_end") or 0),
                source_oid=str(claim.get("source_oid") or ""),
            )
        )
    for assumption in plan.get("assumptions") or []:
        entries.append(
            CarryForwardEntry(
                evidence_id=f"a{len([e for e in entries if e.kind == 'assumption']) + 1}",
                kind="assumption",
                text=str(assumption),
            )
        )
    for question in plan.get("questions") or []:
        entries.append(
            CarryForwardEntry(
                evidence_id=f"q{len([e for e in entries if e.kind == 'question']) + 1}",
                kind="question",
                text=str(question),
            )
        )
    return ImplementationCarryForward(
        discovery_id=str(discovery_id),
        snapshot_digest=str(snapshot_digest),
        entries=tuple(entries),
    )


def render_carry_forward_section(
    carry: ImplementationCarryForward, *, max_chars: int = 3000
) -> str:
    """The delimited carry-forward section, bounded to *max_chars*.

    Renders the canonical document inside the delimiters; under budget
    the entries list drops from the END with an explicit ``truncated``
    marker — the section never silently loses an entry it still counts.
    """
    document = carry.as_document()

    def _render() -> str:
        body = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return f"{CARRY_FORWARD_BEGIN}\n{body}\n{CARRY_FORWARD_END}"

    section = _render()
    dropped = 0
    while len(section) > max_chars and document["entries"]:
        dropped += 1
        document["entries"].pop()
        document["counts"] = {
            "facts": sum(1 for entry in document["entries"] if entry["kind"] == "fact"),
            "assumptions": sum(1 for entry in document["entries"] if entry["kind"] == "assumption"),
            "questions": sum(1 for entry in document["entries"] if entry["kind"] == "question"),
        }
        section = _render()
    if dropped:
        document["truncated"] = True
        document["dropped_entries"] = dropped
        section = _render()
    return section


def _delimited_section(text: str, begin: str, end: str) -> str | None:
    start = text.find(begin)
    if start == -1:
        return None
    stop = text.find(end, start)
    if stop == -1:
        return None
    return text[start + len(begin) : stop]


def verify_carry_forward(carry: ImplementationCarryForward, plan_text: str) -> bool:
    """True when the plan text carries EVERY carried entry's evidence id.

    The section inside the plan text is parsed back and each entry's
    ``evidence_id`` must resolve in it — the plan's evidence section is
    what implementation consumes, so an entry that fell out of the plan
    text is a carry-forward that did not happen.
    """
    body = _delimited_section(plan_text, CARRY_FORWARD_BEGIN, CARRY_FORWARD_END)
    if body is None:
        return False
    try:
        document = json.loads(body)
    except ValueError:
        return False
    ids = {
        str(entry.get("evidence_id"))
        for entry in document.get("entries") or []
        if isinstance(entry, Mapping)
    }
    return all(entry.evidence_id in ids for entry in carry.entries) and bool(carry.entries)


def brief_envelope_seam(
    carry: ImplementationCarryForward,
    plan_text: str,
    *,
    run_id: str,
    task_title: str,
    task_description: str,
    spec_digest: str,
) -> dict[str, Any]:
    """Prove the seam: the lane consumes the findings through the APPROVED
    brief bytes, not through a second discovery.

    Renders the approved plan-comment sections over *plan_text* (which
    carries the carry-forward section), freezes them into the A03 brief
    envelope, then walks the lane's own path — extract the approved
    sections, re-verify the envelope digest — and checks the extracted
    plan bytes still contain the carry-forward section. When
    ``verified`` is True the findings reach implementation exactly once:
    inside the approved plan the lane already verifies.
    """
    envelope = build_brief_envelope(
        run_id=run_id,
        task_title=task_title,
        task_description=task_description,
        plan_text=plan_text,
        spec_digest=spec_digest,
    )
    comment = render_approved_sections(
        task_title=task_title, task_description=task_description, plan_text=plan_text
    )
    try:
        title, description, extracted_plan = extract_approved_sections(comment)
        verify_brief_envelope(
            envelope["envelope_digest"],
            run_id=run_id,
            task_title=title,
            task_description=description,
            plan_text=extracted_plan,
            spec_digest=spec_digest,
        )
    except ValueError as exc:  # BriefEnvelopeError is a ValueError
        return {"verified": False, "reason": f"{type(exc).__name__}: {exc}"}
    section_present = CARRY_FORWARD_BEGIN in extracted_plan and verify_carry_forward(
        carry, extracted_plan
    )
    return {
        "verified": bool(section_present),
        "envelope_digest": envelope["envelope_digest"],
        "plan_text_digest": envelope["plan_text_digest"],
        "lane_extraction": "plan bytes carry the carry-forward section",
        "reason": "" if section_present else "the extracted plan bytes lost the section",
    }
