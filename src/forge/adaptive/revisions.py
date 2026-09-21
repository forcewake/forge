"""The adaptive revision lifecycle — the PLN epic remainder (review 05868e9).

``models.py`` carries the SHAPES (``WorkContract``, ``PlanRevision``,
``ChangeProposal`` ...); this module carries the RULES that keep a running
plan honest between approvals. The separation is the review's own: WHAT/WHY
are approved by a human once, HOW is revised by the agent many times — so
every rule here answers one question: may this revision land without going
back to the human?

- PLN-04 — :func:`classify_revision` sorts a proposed revision into the
  ChangeProposal vocabulary; :func:`apply_tactical` lands the pre-approved
  class and NOTHING else (a material change raises — it can never slip
  through as a quiet tactical edit).
- PLN-05 — material revisions land only through a compare-and-swap
  :class:`RevisionDecision` (a stale authorization epoch refuses), and
  :func:`activate_revision` is the single door from approval to plan;
  activation fences the old publication rights.
- PLN-06 — :class:`Question` makes blocker questions durable planning
  state (answered only by an actor with the required scope, never
  defaulted), and :func:`route_question` sends them to the PARENT
  coordinator — never fanned out independently to every subagent.
- PLN-07 — :func:`invalidation_set` invalidates evidence PRECISELY (the
  steps the new revision names, the digests that no longer match) and
  marks it superseded rather than deleting it;
  :func:`stale_callback_guard` keeps a stale native callback from
  advancing the active revision.
- PLN-08 — :func:`decision_record` writes WHAT was chosen with its
  durable WHY (alternatives, evidence, assumptions) and refuses
  secret-looking values; :func:`fresh_session_brief` reconstructs a
  cold-start brief from durable artifacts alone — no session memory, no
  hidden reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256

from forge.adaptive.models import PlanRevision, WorkContract

__all__ = [
    "Question",
    "RevisionDecision",
    "activate_revision",
    "apply_tactical",
    "change_log",
    "classify_revision",
    "decision_record",
    "fresh_session_brief",
    "invalidation_set",
    "plan_digest",
    "route_question",
    "stale_callback_guard",
]


def classify_revision(old: PlanRevision, new: PlanRevision, contract: WorkContract) -> str:
    """Classify a proposed revision into the ChangeProposal vocabulary.

    Precedence follows the blast radius, widest first — a revision that
    trips two classes reports the one a human must see first:

    - ``material_scope`` — any NEW step writes a repository that neither
      the old plan's steps nor the contract's write scope authorized.
      New write surface is never an internal detail.
    - ``material_contract`` — the new steps stop referencing an
      acceptance id the old plan pursued (or the contract itself
      declares). Dropping pursuit of an approved criterion changes WHAT
      must result, not HOW; adding references is tactical, dropping
      never is.
    - ``material_migration`` — any new step's impact names ``migration``
      or ``schema`` (case-insensitive): the compatibility invariants the
      contract protects live exactly there.
    - ``tactical_internal`` — everything else: reordering, rewording,
      adding steps inside the authorized write scope.
    """
    authorized_writes = {scope.repository_id for scope in contract.write_scope}
    authorized_writes.update(
        step.write_repository_id for step in old.steps if step.write_repository_id
    )
    for step in new.steps:
        if step.write_repository_id and step.write_repository_id not in authorized_writes:
            return "material_scope"

    required_acceptance = {ref for step in old.steps for ref in step.acceptance_refs}
    required_acceptance.update(
        str(entry["id"])
        for entry in contract.acceptance
        if isinstance(entry, dict) and "id" in entry
    )
    referenced_acceptance = {ref for step in new.steps for ref in step.acceptance_refs}
    if required_acceptance - referenced_acceptance:
        return "material_contract"

    for step in new.steps:
        if any(tag.strip().lower() in ("migration", "schema") for tag in step.impact):
            return "material_migration"

    return "tactical_internal"


def change_log(old: PlanRevision, new: PlanRevision) -> list[str]:
    """One concise human line per step-objective delta, keyed by step_id.

    Old steps walk first (changed, then removed, in old order); genuinely
    new steps follow in new order — a stable, scannable diff for the
    ``tactical_applied`` event.
    """
    old_by_id = {step.step_id: step for step in old.steps}
    new_by_id = {step.step_id: step for step in new.steps}
    lines: list[str] = []
    for step in old.steps:
        replacement = new_by_id.get(step.step_id)
        if replacement is None:
            lines.append(f"removed {step.step_id}: {step.objective}")
        elif replacement.objective != step.objective:
            lines.append(f"changed {step.step_id}: {step.objective} -> {replacement.objective}")
    for step in new.steps:
        if step.step_id not in old_by_id:
            lines.append(f"added {step.step_id}: {step.objective}")
    return lines


def apply_tactical(
    old: PlanRevision, new: PlanRevision, contract: WorkContract
) -> tuple[PlanRevision, dict]:
    """Land a tactical revision inside the pre-approved bounds.

    Tactical edits carry no approval notification — the contract's
    ``tactical_revision_policy`` pre-approved them — but that speed is
    only safe because the material classes can NEVER pass through here:
    anything :func:`classify_revision` calls material raises, and the
    caller must raise a ChangeProposal for the human gate instead.

    The applied revision carries its lineage explicitly: the next
    revision number, its parent, and the preserved-step linkage — every
    old step that survives into the new plan, merged with whatever the
    proposal already declared, so surviving WIP keeps its anchor.
    """
    if classify_revision(old, new, contract) != "tactical_internal":
        raise ValueError("material change requires approval")

    new_step_ids = {step.step_id for step in new.steps}
    surviving = [step.step_id for step in old.steps if step.step_id in new_step_ids]
    preserved = list(dict.fromkeys([*new.preserved_step_ids, *surviving]))
    applied = new.model_copy(
        update={
            "revision": old.revision + 1,
            "parent_revision": old.revision,
            "preserved_step_ids": preserved,
        }
    )
    event = {
        "schema": "forge.revision.event/1",
        "kind": "tactical_applied",
        "revision": applied.revision,
        "parent": old.revision,
        "change_log": change_log(old, new),
    }
    return applied, event


@dataclass(frozen=True)
class RevisionDecision:
    """The durable compare-and-swap gate a material revision must pass.

    An approval is only valid against the ``authorization_epoch`` it was
    granted in — the same fence ``ControlCommand.expected_execution_epoch``
    draws for commands. If the world has moved (contract re-approved, plan
    superseded, epoch bumped), a stale "yes" must refuse rather than land
    on top of a state it never saw. Frozen like everything durable here:
    every transition returns a new record, so the journal keeps both
    sides of every decision.

    ``reject`` takes a ``reason`` but does not store it: the verdict is
    decision state, the reason is audit-trail material — it belongs in
    the durable command log beside the rejection, not in the CAS slot
    whose field set is the wire format shared with the coordinator.
    """

    decision_id: str
    work_id: str
    parent_revision: int
    proposed_revision_id: str
    work_contract_digest: str
    authorization_epoch: int
    decided: bool = False
    decided_by: str = ""
    #: "" | "approved" | "rejected" | "expired" — "" means still pending.
    decision: str = ""

    def _refuse_if_decided(self) -> None:
        if self.decided or self.decision:
            state = self.decision or "decided"
            raise ValueError(f"decision {self.decision_id} is already {state}")

    def _refuse_stale_epoch(self, epoch: int) -> None:
        if epoch != self.authorization_epoch:
            raise ValueError(
                f"stale authorization epoch {epoch} != {self.authorization_epoch}; "
                "the decision must be re-requested against the current epoch"
            )

    def approve(self, actor: str, epoch: int) -> RevisionDecision:
        """Record approval — only once, and only in the granting epoch."""
        self._refuse_if_decided()
        self._refuse_stale_epoch(epoch)
        return replace(self, decided=True, decided_by=actor, decision="approved")

    def reject(self, actor: str, epoch: int, reason: str) -> RevisionDecision:
        """Record rejection under the same CAS fence as approval.

        Rejection leaves the proposal unactivated: the previous revision
        and its last usable checkpoint remain the honest state — work is
        never rolled back, it just refuses to advance.
        """
        self._refuse_if_decided()
        self._refuse_stale_epoch(epoch)
        return replace(self, decided=True, decided_by=actor, decision="rejected")

    def expire(self) -> RevisionDecision:
        """Close a still-pending decision once its window has passed.

        Expiry is terminal — a late approval must not resurrect a
        decision that already expired; the proposer re-asks in the
        current epoch. ``decided`` marks the slot as closed to further
        transitions; ``decision`` carries the outcome.
        """
        self._refuse_if_decided()
        return replace(self, decided=True, decision="expired")


def activate_revision(decision: RevisionDecision, proposed: PlanRevision) -> PlanRevision:
    """Bind an APPROVED decision to its proposed revision.

    The only door from approval to active plan: rejected, expired, and
    still-pending decisions all leave the proposal unactivated, so the
    parent revision (and its last usable checkpoint) remains the honest
    state.

    Activation also FENCES the old publication rights: the revision the
    decision approved supersedes its parent, and any publication
    authorization minted against the parent dies with it — the caller
    bumps its publication epoch as part of applying the activation.
    """
    if not decision.decided or decision.decision != "approved":
        state = decision.decision or "undecided"
        raise ValueError(
            f"decision {decision.decision_id} is {state}; only approved decisions activate"
        )
    if proposed.revision <= decision.parent_revision:
        raise ValueError("proposed revision must follow the decision's parent revision")
    return proposed.model_copy(update={"parent_revision": decision.parent_revision})


@dataclass(frozen=True)
class Question:
    """A durable planning block — surfaced, never defaulted.

    A question the plan cannot proceed without is durable STATE, not a
    prompt to guess at: while ``answered`` is false the planner must
    block on it (or route it), never silently pick a default answer.
    Answers come from an actor whose scope is in ``required_actor_scope``
    (when non-empty) — scope identity is the authority, the free
    ``actor`` string is only provenance. ``expires_at`` lets an
    unanswered question time out instead of blocking forever; expiry is
    the caller's clock to declare.
    """

    question_id: str
    work_id: str
    reason: str
    options: tuple[str, ...] = ()
    free_text_allowed: bool = True
    required_actor_scope: tuple[str, ...] = ()
    revision_context: int = 1
    answer: str = ""
    answered_by: str = ""
    answered: bool = False
    expires_at: str = ""

    def answer_question(self, text: str, actor: str, scope: str) -> Question:
        """Answer exactly once, from an authorized scope.

        An empty answer refuses — an unfilled answer is exactly the
        silent default this class exists to prevent. When ``options`` is
        non-empty and ``free_text_allowed`` is false the options are a
        closed set and anything outside them refuses.
        """
        if self.answered:
            raise ValueError(
                f"question {self.question_id} already answered by {self.answered_by!r}"
            )
        if not text.strip():
            raise ValueError("refusing to record an empty answer — questions are never defaulted")
        if self.required_actor_scope and scope not in self.required_actor_scope:
            raise ValueError(
                f"scope {scope!r} may not answer {self.question_id}; "
                f"required one of {list(self.required_actor_scope)}"
            )
        if self.options and not self.free_text_allowed and text not in self.options:
            raise ValueError(f"answer must be one of the closed options {list(self.options)}")
        return replace(self, answer=text, answered_by=actor, answered=True)

    def is_expired(self, now_iso: str) -> bool:
        """True once ``now_iso`` reached ``expires_at``.

        ISO-8601 strings in one consistent format compare
        lexicographically, so string comparison IS chronological
        comparison; a question without a deadline never expires.
        """
        return bool(self.expires_at) and now_iso >= self.expires_at


def route_question(question: Question, child_specialists: list[str]) -> str:
    """Route a question to the PARENT coordinator — never to the children.

    The failure this prevents: a coordinator broadcasting an open
    question to every child specialist and receiving N confident,
    contradictory answers, each indistinguishable from authority.
    ``child_specialists`` is deliberately a parameter so the
    non-destination is explicit — the parent MAY consult them while
    gathering evidence, but the question's destination, and the only
    place an answer acquires authority, is the parent.
    """
    return "parent"


def plan_digest(revision: PlanRevision) -> str:
    """The stable digest of one revision's canonical JSON.

    Evidence binds to the plan it was produced under, revision number
    included — a renumbered plan is a different plan even with identical
    steps, which is exactly why stale evidence must be re-marked.
    """
    return sha256(revision.model_dump_json().encode("utf-8")).hexdigest()


def invalidation_set(
    old: PlanRevision, new: PlanRevision, evidence_bindings: dict[str, dict]
) -> dict:
    """Precisely partition evidence when a revision lands.

    An evidence item is invalidated ONLY for a stated reason — its step
    is named in ``new.invalidated_step_ids``, or its ``plan_digest`` no
    longer matches the incoming revision (``plan_digest(new)`` is the
    string every binding is compared against). Everything else is
    preserved as-is: re-verification is expensive, so "the plan changed
    somewhere" is never grounds to drop evidence — the change must NAME
    its victims.

    Invalidated evidence is marked SUPERSEDED, not deleted: superseded
    evidence stays durable (auditable, re-usable if a later revision
    returns to that step); only its authority is withdrawn.

    ``old`` is retained for the revision-pair symmetry every other
    transition in this module takes (and the lineage the journal
    records beside the partition); the rules themselves reference only
    the new revision's declarations and digest.
    """
    new_digest = plan_digest(new)
    invalidated_step_ids = set(new.invalidated_step_ids)
    invalidated: list[tuple[str, str]] = []
    preserved: list[str] = []
    for evidence_id, binding in evidence_bindings.items():
        step_id = binding.get("step_id")
        if step_id in invalidated_step_ids:
            invalidated.append((evidence_id, f"step {step_id} invalidated"))
        elif binding.get("plan_digest") != new_digest:
            invalidated.append((evidence_id, "plan digest mismatch"))
        else:
            preserved.append(evidence_id)
    return {
        "invalidated": invalidated,
        "preserved": preserved,
        "superseded": dict(invalidated),
    }


def stale_callback_guard(callback_plan_digest: str, active_plan_digest: str) -> bool:
    """True only when a native callback was minted against the ACTIVE plan.

    A verification runner from a superseded revision finishing late must
    not advance, publish, or attach evidence to the revision that
    replaced it — digest inequality is the cheap fence. A blank callback
    digest is never trusted to match, even against a blank active one.
    """
    return bool(callback_plan_digest) and callback_plan_digest == active_plan_digest


_LEAK_MARKERS = ("sk-", "PRIVATE")


def _leaks_secret(value: str) -> bool:
    return any(marker in value for marker in _LEAK_MARKERS)


def decision_record(
    decision_id: str,
    question_id: str,
    alternatives: list[str],
    chosen: str,
    evidence_ids: list[str],
    assumptions: list[str],
    approver: str,
) -> dict:
    """A durable decision record: the WHAT and the artifact-level WHY.

    The record survives the session that made it, so it carries only
    durable material — the alternatives considered, the chosen one, the
    evidence and assumptions it rests on, the approving actor. Model
    reasoning is deliberately absent: a reconstructed session must not
    inherit (or leak) another session's chain of thought, and any value
    that looks like a credential or a private note refuses outright.
    """
    tainted = []
    if _leaks_secret(chosen):
        tainted.append("chosen")
    tainted.extend(
        f"alternatives[{index}]"
        for index, alternative in enumerate(alternatives)
        if _leaks_secret(alternative)
    )
    if tainted:
        raise ValueError(f"decision records carry no secrets or private material: {tainted}")
    return {
        "schema": "forge.decision.record/1",
        "decision_id": decision_id,
        "question_id": question_id,
        "alternatives": list(alternatives),
        "chosen": chosen,
        "evidence_ids": list(evidence_ids),
        "assumptions": list(assumptions),
        "approver": approver,
    }


def fresh_session_brief(
    contract: WorkContract,
    revision: PlanRevision,
    decisions: list[dict],
    checkpoint_summary: str | None,
) -> dict:
    """Reconstruct a cold-start brief from durable artifacts alone.

    A revived or fresh session never inherits another session's memory —
    it rebuilds its context from the approved contract, the active plan
    revision, the chosen decisions, and the last checkpoint summary.
    Everything in the brief is durable, human-auditable material: the
    objective and write scope from the contract, the per-step objectives
    from the plan, and only the CHOSEN alternatives from the decision
    records (their deliberation is not context).
    """
    return {
        "schema": "forge.session.brief/1",
        "objective": contract.objective,
        "write_scope": [
            {"repository_id": scope.repository_id, "paths": list(scope.paths)}
            for scope in contract.write_scope
        ],
        "plan_summary": revision.summary,
        "step_objectives": {step.step_id: step.objective for step in revision.steps},
        "decisions": [record["chosen"] for record in decisions],
        "checkpoint": checkpoint_summary,
    }
