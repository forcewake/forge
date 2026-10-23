"""Bind plan revisions and executor inputs to current, consume-once human approval."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dataclass_field, replace
from fnmatch import fnmatchcase
from hashlib import sha256
from typing import Any, Iterable, Literal, Mapping, Protocol

from forge.adaptive.models import PlanRevision, PlanStep, WorkContract

__all__ = [
    "ACTIVE_PLAN_KEY",
    "ACTIVE_PLAN_SCHEMA",
    "ACTIVATION_SCHEMA",
    "ActivationRecord",
    "ActivationRefused",
    "ActivationSession",
    "ActivePlanState",
    "APPROVED_INPUT_KEY",
    "APPROVED_INPUT_SCHEMA",
    "APPROVED_SOURCE_REVISION",
    "APPROVED_SOURCE_SPEC",
    "APPROVED_SOURCE_SPEC_LEGACY",
    "ApprovedInput",
    "ArtifactReuseDecision",
    "CHECKPOINT_KIND",
    "CLARIFICATION_CLASS",
    "IN_SCOPE_CORRECTION_CLASS",
    "MATERIAL_CHANGE_CLASS",
    "CHECKPOINT_REUSE_DECISION_KEY",
    "DISCARD_DECISION",
    "DispatchPlanBinding",
    "DurableActivationOutcome",
    "EVIDENCE_KIND",
    "EXECUTOR_DIGEST_SCHEMA",
    "EXECUTOR_INPUT_FIELDS",
    "INVALIDATE_DECISION",
    "PENDING_PROPOSAL_KEY",
    "PENDING_PROPOSAL_SCHEMA",
    "PRESERVE_DECISION",
    "Question",
    "REUSE_DECISION_SCHEMA",
    "REVIEW_FEEDBACK_KEY",
    "REVIEW_FEEDBACK_SCHEMA",
    "REVIEW_OBLIGATION_SCHEMA",
    "ReviewFeedbackRefused",
    "ReviewFeedbackRequest",
    "REVISION_ACTIVATIONS_KEY",
    "REVISION_CONTENT_KEY",
    "REVISION_EXECUTOR_DIGEST_KEY",
    "REVISION_NOTE_MARKER",
    "ROUND_SEED_SCHEMA",
    "REQUEST_MR_CLOSED",
    "REQUEST_ROUND_ADMITTED",
    "REQUEST_ROUND_LIMIT",
    "REQUEST_TARGET_REFUSED",
    "RevisionDecision",
    "RevisionRebindRefused",
    "TacticalPolicy",
    "WorkArtifact",
    "WipReuseDecision",
    "activate_pending_revision",
    "activate_revision",
    "active_plan_document_of",
    "apply_tactical",
    "change_log",
    "classify_revision",
    "classify_review_feedback",
    "correction_decision_id",
    "correction_invalidation_set",
    "decide_wip_reuse",
    "decision_record",
    "dispatch_plan_binding",
    "executor_digest_document",
    "executor_input_digest",
    "fresh_session_brief",
    "head_binding_guard",
    "invalidation_set",
    "is_standing_guidance",
    "mark_review_feedback_request",
    "parse_tactical_policy",
    "parse_review_feedback_note",
    "plan_comment_revision_note",
    "plan_digest",
    "proposed_revision_identity",
    "read_active_plan",
    "read_wip_reuse_decision",
    "read_review_feedback_requests",
    "record_review_feedback_request",
    "referenced_paths_of",
    "refused_wip_reuse",
    "render_revision_brief",
    "review_correction_revision",
    "review_feedback_requests_of",
    "review_feedback_summary_section",
    "review_obligation_digest",
    "resolve_approved_input",
    "route_question",
    "stage_pending_revision",
    "stage_review_correction",
    "stage_standing_guidance_promotion",
    "stale_callback_guard",
    "standing_guidance_revision",
    "classic_spec_revision",
    "round_active_plan_seed",
    "transformation_kinds",
    "wip_artifacts_of_evidence",
]

#: Impact classes a tactical edit may carry.
_KNOWN_TACTICAL_IMPACTS = frozenset({"internal", "refactor", "docs", "tests", "cleanup"})

#: Declared compatibility-breaking impact classes — anything naming these is material by
#: declaration, no approval shortcut exists.
_MATERIAL_IMPACTS = frozenset({"migration", "schema", "api", "public-api", "event", "effect"})

#: The closed vocabulary of automatic plan transformations a contract's
#: ``tactical_revision_policy`` may pre-approve.
_TACTICAL_POLICY_OPERATIONS = frozenset(
    {"reorder", "edit_step", "reword", "add_step", "remove_step"}
)


def _delta_steps(old: PlanRevision, new: PlanRevision) -> list[PlanStep]:
    """The steps the new plan ADDS or CHANGES — the delta that carries risk."""
    old_by_id = {step.step_id: step for step in old.steps}
    return [step for step in new.steps if old_by_id.get(step.step_id) != step]


def transformation_kinds(old: PlanRevision, new: PlanRevision) -> frozenset[str]:
    """Name every automatic transformation the delta performs."""
    old_by_id = {step.step_id: step for step in old.steps}
    new_by_id = {step.step_id: step for step in new.steps}
    kinds: set[str] = set()
    surviving_old_order = [step.step_id for step in old.steps if step.step_id in new_by_id]
    surviving_new_order = [step.step_id for step in new.steps if step.step_id in old_by_id]
    if surviving_new_order != surviving_old_order:
        kinds.add("reorder")
    for step in new.steps:
        if step.step_id not in old_by_id:
            kinds.add("add_step")
        elif old_by_id[step.step_id] != step:
            kinds.add("edit_step")
    if any(step.step_id not in new_by_id for step in old.steps):
        kinds.add("remove_step")
    if new.summary != old.summary or list(new.assumptions) != list(old.assumptions):
        kinds.add("reword")
    return frozenset(kinds)


@dataclass(frozen=True)
class TacticalPolicy:
    """The contract's explicit pre-approval of automatic plan edits."""

    raw: str
    status: Literal["enabled", "disabled", "unset", "unknown"]
    allowed: frozenset[str] = frozenset()

    def permits(self, kinds: frozenset[str] | set[str]) -> bool:
        """True only when an ENABLED policy covers every requested kind."""
        return self.status == "enabled" and set(kinds) <= set(self.allowed)


def parse_tactical_policy(contract: WorkContract) -> TacticalPolicy:
    """Parse ``WorkContract.tactical_revision_policy`` fail closed."""
    raw = (contract.tactical_revision_policy or "").strip()
    if not raw:
        return TacticalPolicy(raw=raw, status="unset")
    tokens = [token.strip().lower() for token in raw.split(",") if token.strip()]
    if "disabled" in tokens:
        return TacticalPolicy(raw=raw, status="disabled")
    unknown = [token for token in tokens if token not in _TACTICAL_POLICY_OPERATIONS]
    if unknown:
        return TacticalPolicy(raw=raw, status="unknown")
    return TacticalPolicy(raw=raw, status="enabled", allowed=frozenset(tokens))


def classify_revision(old: PlanRevision, new: PlanRevision, contract: WorkContract) -> str:
    """Classify a proposed revision into the ChangeProposal vocabulary."""
    authorized_writes = {scope.repository_id for scope in contract.write_scope}
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

    delta_steps = _delta_steps(old, new)
    for step in delta_steps:
        if any(tag.strip().lower() in _MATERIAL_IMPACTS for tag in step.impact):
            return "material_migration"

    for step in delta_steps:
        declared = {tag.strip().lower() for tag in step.impact if tag.strip()}
        if not declared or not declared <= _KNOWN_TACTICAL_IMPACTS:
            return "decision_required"

    if not parse_tactical_policy(contract).permits(transformation_kinds(old, new)):
        return "decision_required"

    return "tactical_internal"


def change_log(old: PlanRevision, new: PlanRevision) -> list[str]:
    """One concise human line per step-objective delta, keyed by step_id."""
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
    """Land a tactical revision inside the pre-approved bounds."""
    classification = classify_revision(old, new, contract)
    if classification != "tactical_internal":
        raise ValueError(
            f"{classification} cannot land as a tactical edit — "
            "it requires an approved decision (raise a ChangeProposal)"
        )
    policy = parse_tactical_policy(contract)
    kinds = transformation_kinds(old, new)
    if not policy.permits(kinds):  # defense in depth: classify gates this too
        raise ValueError(
            f"tactical policy {policy.status!r} does not allow {sorted(kinds)} — "
            "a decision is required for these transformations"
        )

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
        "transformation_kinds": sorted(kinds),
        "tactical_policy": policy.raw,
    }
    return applied, event


@dataclass(frozen=True)
class RevisionDecision:
    """The durable compare-and-swap gate a material revision must pass."""

    decision_id: str
    work_id: str
    parent_revision: int
    proposed_revision_id: str
    proposed_digest: str
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
        """Record rejection under the same CAS fence as approval."""
        self._refuse_if_decided()
        self._refuse_stale_epoch(epoch)
        return replace(self, decided=True, decided_by=actor, decision="rejected")

    def expire(self) -> RevisionDecision:
        """Close a still-pending decision once its window has passed."""
        self._refuse_if_decided()
        return replace(self, decided=True, decision="expired")


def proposed_revision_identity(proposed: PlanRevision) -> str:
    """The stable identity string a decision names: ``plan_id#revision``."""
    return f"{proposed.plan_id}#{proposed.revision}"


class ActivationRefused(ValueError):
    """The typed refusal activation returns instead of consuming anything."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"activation refused [{code}] {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ActivePlanState:
    """What the caller asserts the world is RIGHT NOW for one work."""

    work_id: str
    plan_id: str
    active_revision: int
    work_contract_digest: str
    authorization_epoch: int
    publication_epoch: int = 0


@dataclass(frozen=True)
class ActivationRecord:
    """The ONE conditional transaction activation asks the store to run."""

    decision_id: str
    work_id: str
    plan_id: str
    parent_revision: int
    activated_revision: int
    activated_plan_digest: str
    work_contract_digest: str
    authorization_epoch: int
    publication_epoch: int


class ActivationSession(Protocol):
    """The durable-store seam activation runs its one transaction through."""

    def prior_activation(self, decision_id: str) -> ActivationRecord | None: ...

    def commit_activation(self, record: ActivationRecord) -> None: ...


def _binding_refusal(
    decision: RevisionDecision,
    proposed: PlanRevision,
    current: ActivePlanState,
) -> ActivationRefused | None:
    """The FULL binding-tuple guard, shared by every activation path."""
    if decision.work_id != proposed.work_id:
        return ActivationRefused(
            "work_mismatch",
            f"decision {decision.decision_id} is for work {decision.work_id!r}, "
            f"the proposal is for work {proposed.work_id!r}",
        )
    if proposed.work_id != current.work_id:
        return ActivationRefused(
            "current_work_mismatch",
            f"the proposal is for work {proposed.work_id!r}, "
            f"but the current work is {current.work_id!r}",
        )
    if proposed.plan_id != current.plan_id:
        return ActivationRefused(
            "plan_mismatch",
            f"the proposal is for plan {proposed.plan_id!r}, "
            f"but the active plan is {current.plan_id!r}",
        )
    if decision.proposed_revision_id != proposed_revision_identity(proposed):
        return ActivationRefused(
            "proposal_identity_mismatch",
            f"decision {decision.decision_id} names {decision.proposed_revision_id!r}, "
            f"the proposal is {proposed_revision_identity(proposed)!r}",
        )
    if decision.proposed_digest != plan_digest(proposed):
        return ActivationRefused(
            "proposed_digest_mismatch",
            f"decision {decision.decision_id} approved content {decision.proposed_digest}, "
            f"the proposal's canonical digest is {plan_digest(proposed)} — a changed proposal "
            "body is a different proposal and needs its own decision",
        )
    if decision.work_contract_digest != proposed.work_contract_digest:
        return ActivationRefused(
            "contract_digest_mismatch",
            f"decision {decision.decision_id} was judged under contract "
            f"{decision.work_contract_digest}, the proposal declares "
            f"{proposed.work_contract_digest}",
        )
    if decision.work_contract_digest != current.work_contract_digest:
        return ActivationRefused(
            "current_contract_mismatch",
            f"decision {decision.decision_id} was judged under contract "
            f"{decision.work_contract_digest}, the current contract is "
            f"{current.work_contract_digest} — the approval predates the contract change",
        )
    if decision.parent_revision != current.active_revision:
        return ActivationRefused(
            "parent_mismatch",
            f"decision {decision.decision_id} expects parent revision "
            f"{decision.parent_revision}, but revision {current.active_revision} is "
            "active — the approval predates newer work and must be re-requested",
        )
    if proposed.revision <= decision.parent_revision:
        return ActivationRefused(
            "revision_not_forward",
            "the proposed revision must follow the decision's parent revision",
        )
    if decision.authorization_epoch != current.authorization_epoch:
        return ActivationRefused(
            "stale_authorization_epoch",
            f"decision epoch {decision.authorization_epoch} != current epoch "
            f"{current.authorization_epoch}; the decision must be re-requested "
            "against the current epoch",
        )
    return None


def activate_revision(
    decision: RevisionDecision,
    proposed: PlanRevision,
    current: ActivePlanState,
    session: ActivationSession,
) -> PlanRevision:
    """Bind an APPROVED decision to its proposed revision — or refuse, typed."""
    if not decision.decided or decision.decision != "approved":
        state = decision.decision or "undecided"
        raise ActivationRefused(
            "not_approved",
            f"decision {decision.decision_id} is {state}; only approved decisions activate",
        )

    prior = session.prior_activation(decision.decision_id)
    if prior is not None:
        if (
            prior.activated_plan_digest == plan_digest(proposed)
            and prior.activated_revision == proposed.revision
            and prior.work_id == proposed.work_id
            and prior.plan_id == proposed.plan_id
        ):
            return proposed.model_copy(update={"parent_revision": prior.parent_revision})
        raise ActivationRefused(
            "decision_already_consumed",
            f"decision {decision.decision_id} already activated revision "
            f"{prior.activated_revision} of {prior.plan_id}; it cannot activate different content",
        )

    refusal = _binding_refusal(decision, proposed, current)
    if refusal is not None:
        raise refusal

    session.commit_activation(
        ActivationRecord(
            decision_id=decision.decision_id,
            work_id=proposed.work_id,
            plan_id=proposed.plan_id,
            parent_revision=decision.parent_revision,
            activated_revision=proposed.revision,
            activated_plan_digest=plan_digest(proposed),
            work_contract_digest=decision.work_contract_digest,
            authorization_epoch=current.authorization_epoch,
            publication_epoch=current.publication_epoch + 1,
        )
    )
    return proposed.model_copy(update={"parent_revision": decision.parent_revision})


@dataclass(frozen=True)
class Question:
    """A durable planning block — surfaced, never defaulted."""

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
        """Answer exactly once, from an authorized scope."""
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
        """True once ``now_iso`` reached ``expires_at``."""
        return bool(self.expires_at) and now_iso >= self.expires_at


def route_question(question: Question, child_specialists: list[str]) -> str:
    """Route a question to the PARENT coordinator — never to the children."""
    return "parent"


def plan_digest(revision: PlanRevision) -> str:
    """The stable digest of one revision's canonical JSON."""
    return sha256(revision.model_dump_json().encode("utf-8")).hexdigest()


def invalidation_set(
    old: PlanRevision, new: PlanRevision, evidence_bindings: dict[str, dict]
) -> dict:
    """Precisely partition evidence when a revision lands."""
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
    """True only when a native callback was minted against the ACTIVE plan."""
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
    """A durable decision record: the WHAT and the artifact-level WHY."""
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
    """Reconstruct a cold-start brief from durable artifacts alone."""
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


# The durable activation transaction
#
# ``activate_revision`` above is the domain door; this section is its durable twin.

#: Where each of the durable records lives inside ``FlowRun.evidence``.
REVISION_ACTIVATIONS_KEY = "revision_activations"
ACTIVE_PLAN_KEY = "active_plan"
PENDING_PROPOSAL_KEY = "revision_proposal"

#: the ACTIVE-plan document's field carrying the activated revision's own content
#: (``PlanRevision.model_dump``) — the bytes the activation CAS switched the digest over,
#: read back by :func:`resolve_approved_input` at every dispatch entry.
REVISION_CONTENT_KEY = "revision_content"

PENDING_PROPOSAL_SCHEMA = "forge.revision.proposal-pending/1"
ACTIVATION_SCHEMA = "forge.revision.activation/1"
ACTIVE_PLAN_SCHEMA = "forge.revision.active-plan/1"

#: SessionFactory alias (the same shape every runs/ service codes against: a zero-arg
#: callable yielding an async context manager over a session).
_RevisionSessionFactory = Any


@dataclass(frozen=True)
class DurableActivationOutcome:
    """What one:func:`activate_pending_revision` transaction established."""

    status: str
    record: ActivationRecord | None = None
    revision: PlanRevision | None = None
    code: str = ""
    reason: str = ""

    @property
    def activated(self) -> bool:
        return self.status == "activated"


def _activation_record_of(document: Any) -> ActivationRecord | None:
    if not isinstance(document, dict):
        return None
    try:
        return ActivationRecord(
            decision_id=str(document.get("decision_id") or ""),
            work_id=str(document.get("work_id") or ""),
            plan_id=str(document.get("plan_id") or ""),
            parent_revision=int(document.get("parent_revision") or 0),
            activated_revision=int(document.get("activated_revision") or 0),
            activated_plan_digest=str(document.get("activated_plan_digest") or ""),
            work_contract_digest=str(document.get("work_contract_digest") or ""),
            authorization_epoch=int(document.get("authorization_epoch") or 0),
            publication_epoch=int(document.get("publication_epoch") or 0),
        )
    except (TypeError, ValueError):
        return None


def _record_document(record: ActivationRecord) -> dict[str, Any]:
    return {
        "schema": ACTIVATION_SCHEMA,
        **{
            key: getattr(record, key)
            for key in (
                "decision_id",
                "work_id",
                "plan_id",
                "parent_revision",
                "activated_revision",
                "activated_plan_digest",
                "work_contract_digest",
                "authorization_epoch",
                "publication_epoch",
            )
        },
    }


def active_plan_document_of(
    current: ActivePlanState,
    record: ActivationRecord,
    *,
    revised_from_digest: str = "",
    revision_content: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The durable ACTIVE-plan pointer the dispatch leg reads."""
    document = {
        "schema": ACTIVE_PLAN_SCHEMA,
        "work_id": record.work_id,
        "plan_id": record.plan_id,
        "active_revision": record.activated_revision,
        "plan_digest": record.activated_plan_digest,
        "revised_from_digest": revised_from_digest,
        "work_contract_digest": record.work_contract_digest,
        "authorization_epoch": record.authorization_epoch,
        "publication_epoch": record.publication_epoch,
        "activated_by_decision": record.decision_id,
    }
    if revision_content is not None:
        document[REVISION_CONTENT_KEY] = dict(revision_content)
    return document


async def stage_pending_revision(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    decision: RevisionDecision,
    proposed: PlanRevision,
    current: ActivePlanState,
    *,
    old: PlanRevision | None = None,
) -> None:
    """Stage a PENDING material-revision decision for the human gate."""
    from forge.durable import FlowRun, Outbox

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise ActivationRefused("run_not_found", f"flow run {run_id!r} not found")
        merged = dict(run.evidence or {})
        merged[PENDING_PROPOSAL_KEY] = {
            "schema": PENDING_PROPOSAL_SCHEMA,
            "decision": {
                "decision_id": decision.decision_id,
                "work_id": decision.work_id,
                "parent_revision": decision.parent_revision,
                "proposed_revision_id": decision.proposed_revision_id,
                "proposed_digest": decision.proposed_digest,
                "work_contract_digest": decision.work_contract_digest,
                "authorization_epoch": decision.authorization_epoch,
            },
            "proposed": proposed.model_dump(),
            "current": {
                "work_id": current.work_id,
                "plan_id": current.plan_id,
                "active_revision": current.active_revision,
                "work_contract_digest": current.work_contract_digest,
                "authorization_epoch": current.authorization_epoch,
                "publication_epoch": current.publication_epoch,
            },
            **({"old": old.model_dump()} if old is not None else {}),
        }
        run.evidence = merged
        session.add(
            Outbox(
                flow_run_id=run_id,
                event_type="revision.proposal_staged",
                payload={
                    "run_id": run_id,
                    "decision_id": decision.decision_id,
                    "proposed_revision_id": decision.proposed_revision_id,
                },
            )
        )
        await session.commit()


async def read_active_plan(session_factory: _RevisionSessionFactory, run_id: str) -> dict | None:
    """The run's durable ACTIVE-plan document, or ``None`` when never activated."""
    from forge.durable import FlowRun

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            return None
        document = (run.evidence or {}).get(ACTIVE_PLAN_KEY)
        return dict(document) if isinstance(document, dict) else None


class _StagingActivationSession:
    """The in-transaction session double:func:`activate_revision` runs against."""

    def __init__(self, consumed: dict[str, ActivationRecord]) -> None:
        self._consumed = consumed
        self.staged: ActivationRecord | None = None

    def prior_activation(self, decision_id: str) -> ActivationRecord | None:
        return self._consumed.get(decision_id)

    def commit_activation(self, record: ActivationRecord) -> None:
        self.staged = record


def _refused(code: str, reason: str) -> DurableActivationOutcome:
    return DurableActivationOutcome(status="refused", code=code, reason=reason)


async def _rebind_gate_to_revision(session: Any, run_id: str, record: ActivationRecord) -> None:
    """Re-bind the run's human gate to the activated plan."""
    from sqlalchemy import func, select

    from forge.durable.models import GateApproval

    latest = (
        (
            await session.execute(
                select(GateApproval)
                .where(GateApproval.flow_run_id == run_id)
                .order_by(GateApproval.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if latest is None:
        return
    generation = (
        await session.execute(
            select(func.max(GateApproval.generation)).where(GateApproval.flow_run_id == run_id)
        )
    ).scalar_one()
    session.add(
        GateApproval(
            flow_run_id=run_id,
            generation=(generation + 1) if generation is not None else 0,
            plan_digest=record.activated_plan_digest,
            base_sha=latest.base_sha,
            policy_digest=latest.policy_digest,
            approver_user_id=latest.approver_user_id,
            source_event_id=f"approve-revision:{record.decision_id}"[:64],
            expires_at=latest.expires_at,
            spec_digest=latest.spec_digest,
            task_digest=latest.task_digest,
        )
    )


async def activate_pending_revision(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    decision_id: str,
    *,
    decided_by: str,
) -> DurableActivationOutcome:
    """Approve + activate the run's pending revision in ONE DB transaction."""
    from forge.durable import FlowRun, Outbox

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            return _refused("run_not_found", f"flow run {run_id!r} not found")
        evidence = dict(run.evidence or {})
        journal_raw = evidence.get(REVISION_ACTIVATIONS_KEY)
        journal_raw = journal_raw if isinstance(journal_raw, dict) else {}
        journal = {
            str(key): record
            for key, doc in journal_raw.items()
            if (record := _activation_record_of(doc)) is not None
        }

        prior = journal.get(decision_id)
        if prior is not None:
            return DurableActivationOutcome(
                status="already_active", record=prior, reason="decision already consumed"
            )

        pending = evidence.get(PENDING_PROPOSAL_KEY)
        if not isinstance(pending, dict):
            return _refused(
                "no_pending_proposal",
                f"run {run_id!r} has no pending revision decision to approve",
            )
        staged_id = str((pending.get("decision") or {}).get("decision_id") or "")
        if staged_id != decision_id:
            staged = staged_id or "unknown"
            return _refused(
                "stale_decision",
                f"decision {decision_id!r} is not the pending proposal (staged: {staged!r})",
            )
        active_raw = evidence.get(ACTIVE_PLAN_KEY)
        active_raw = active_raw if isinstance(active_raw, dict) else {}
        if not active_raw:
            return _refused(
                "no_active_plan",
                f"run {run_id!r} carries no durable active-plan state to guard against",
            )
        try:
            current = ActivePlanState(
                work_id=str(active_raw.get("work_id") or ""),
                plan_id=str(active_raw.get("plan_id") or ""),
                active_revision=int(active_raw.get("active_revision") or 0),
                work_contract_digest=str(active_raw.get("work_contract_digest") or ""),
                authorization_epoch=int(active_raw.get("authorization_epoch") or 0),
                publication_epoch=int(active_raw.get("publication_epoch") or 0),
            )
            decision = RevisionDecision(
                decision_id=staged_id,
                work_id=str((pending.get("decision") or {}).get("work_id") or ""),
                parent_revision=int((pending.get("decision") or {}).get("parent_revision") or 0),
                proposed_revision_id=str(
                    (pending.get("decision") or {}).get("proposed_revision_id") or ""
                ),
                proposed_digest=str((pending.get("decision") or {}).get("proposed_digest") or ""),
                work_contract_digest=str(
                    (pending.get("decision") or {}).get("work_contract_digest") or ""
                ),
                authorization_epoch=int(
                    (pending.get("decision") or {}).get("authorization_epoch") or 0
                ),
            )
            proposed = PlanRevision.model_validate(pending.get("proposed") or {})
            old_raw = pending.get("old")
            old_revision = (
                PlanRevision.model_validate(old_raw) if isinstance(old_raw, dict) else None
            )
        except (TypeError, ValueError) as exc:
            return _refused("malformed_pending_proposal", str(exc)[:200])

        try:
            approved = decision.approve(decided_by, current.authorization_epoch)
        except ValueError as exc:
            return _refused("approval_refused", str(exc)[:200])

        staging = _StagingActivationSession(journal)
        try:
            activated = activate_revision(approved, proposed, current, staging)
        except ActivationRefused as exc:
            return _refused(exc.code, exc.detail)
        record = staging.staged
        if record is None:  # pragma: no cover — activate_revision always commits on success
            return _refused("no_transaction", "the domain door staged no activation record")

        merged = dict(evidence)
        merged[REVISION_ACTIVATIONS_KEY] = {
            **journal_raw,
            record.decision_id: _record_document(record),
        }
        # Persist the approved bytes with the switched pointer so dispatch can reject
        # superseded digests and tampered content.
        merged[ACTIVE_PLAN_KEY] = active_plan_document_of(
            current,
            record,
            revised_from_digest=str(active_raw.get("plan_digest") or ""),
            revision_content=proposed.model_dump(),
        )
        merged.pop(PENDING_PROPOSAL_KEY, None)  # the decision is consumed
        # the WIP reuse decision lands in the SAME commit — which artifacts survive the
        # switch, and the checkpoint's explicit route.
        superseded_digest = str(active_raw.get("plan_digest") or "")
        artifacts, uncertainty = await _durable_wip_artifacts(
            session_factory, evidence, run_id=run_id, superseded_plan_digest=superseded_digest
        )
        reuse = decide_wip_reuse(old_revision, activated, artifacts)
        if uncertainty:
            # A checkpoint MAY exist behind an authority that could not be consulted — fail
            # closed to the explicit fresh attempt, never a silent preserve.
            reuse = replace(
                reuse,
                route=FRESH_ATTEMPT_ROUTE,
                route_reason=f"the checkpoint authority could not be consulted: {uncertainty}",
            )
        merged[CHECKPOINT_REUSE_DECISION_KEY] = reuse.document()
        run.evidence = merged
        # the durable row follows the switch — the plan digest the gate validates and the
        # dispatch claims is the ACTIVE plan's from here on, never the superseded comment's.
        run.plan_digest = record.activated_plan_digest
        await _rebind_gate_to_revision(session, run_id, record)
        session.add(
            Outbox(
                flow_run_id=run_id,
                event_type="revision.activated",
                payload={
                    "run_id": run_id,
                    "decision_id": record.decision_id,
                    "activated_revision": record.activated_revision,
                    "plan_id": record.plan_id,
                    "decided_by": decided_by,
                    "publication_epoch": record.publication_epoch,
                },
            )
        )
        # the reuse decision's own observability row — the route an operator (and the
        # dispatch fence) reads without opening the evidence blob.
        session.add(
            Outbox(
                flow_run_id=run_id,
                event_type="checkpoint.reuse_decision",
                payload={
                    "run_id": run_id,
                    "activated_revision": record.activated_revision,
                    "route": reuse.route,
                    "route_reason": reuse.route_reason[:200],
                    "preserved": len(reuse.preserved),
                    "invalidated": len(reuse.invalidated),
                    "discarded": len(reuse.discarded),
                },
            )
        )
        await session.commit()
        return DurableActivationOutcome(
            status="activated", record=record, revision=activated, reason="recorded"
        )


# Dispatch reads the active pointer so revised approval reaches the next executor input.

#: The plan-comment marker line the revision note rides under (same HTML-comment convention
#: as the brief-envelope markers: invisible when rendered, byte-stable in the raw body).
REVISION_NOTE_MARKER = "<!-- forge:plan:revision -->"


def plan_comment_revision_note(
    old_digest: str,
    new_digest: str,
    *,
    decided_by: str = "",
    decision_id: str = "",
) -> str:
    """The revised plan comment's footer."""
    lines = [REVISION_NOTE_MARKER, f"revised from {old_digest} to {new_digest}"]
    if decided_by or decision_id:
        lines.append(f"approved by {decided_by or 'unknown'} ({decision_id or 'unknown'})")
    return "\n".join(lines)


@dataclass(frozen=True)
class DispatchPlanBinding:
    """Which plan the next dispatch runs under."""

    status: str
    run_id: str
    plan_id: str = ""
    active_revision: int = 0
    plan_digest: str = ""
    revised_from_digest: str = ""
    code: str = ""
    reason: str = ""

    @property
    def dispatchable(self) -> bool:
        return self.status == "ok"

    def as_document(self) -> dict[str, Any]:
        """The audit shape the dispatch boundary freezes into the run's evidence: which plan the dispatched lane runs under, which digest it replaced, and — on refusal — the stable code."""
        return {
            "status": self.status,
            "plan_id": self.plan_id,
            "active_revision": self.active_revision,
            "plan_digest": self.plan_digest,
            "revised_from_digest": self.revised_from_digest,
            **({"code": self.code, "reason": self.reason[:200]} if self.code else {}),
        }


def _dispatch_refused(
    run_id: str,
    code: str,
    reason: str,
    *,
    active: str = "",
    revised_from: str = "",
) -> DispatchPlanBinding:
    return DispatchPlanBinding(
        status="refused",
        run_id=run_id,
        code=code,
        reason=reason,
        plan_digest=active,
        revised_from_digest=revised_from,
    )


async def dispatch_plan_binding(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    *,
    claimed_plan_digest: str = "",
) -> DispatchPlanBinding:
    """Bind the next /go dispatch to the ACTIVE plan — not the old comment."""
    active = await read_active_plan(session_factory, run_id)
    if active is None:
        return _dispatch_refused(
            run_id,
            "no_active_plan",
            f"run {run_id!r} has no durable active plan — nothing to dispatch under",
        )
    digest = str(active.get("plan_digest") or "")
    revised_from = str(active.get("revised_from_digest") or "")
    binding = DispatchPlanBinding(
        status="ok",
        run_id=run_id,
        plan_id=str(active.get("plan_id") or ""),
        active_revision=int(active.get("active_revision") or 0),
        plan_digest=digest,
        revised_from_digest=revised_from,
    )
    claimed = str(claimed_plan_digest or "").strip()
    if not claimed or claimed == digest:
        return binding
    if revised_from and claimed == revised_from:
        return _dispatch_refused(
            run_id,
            "stale_plan_digest",
            f"the /go carries plan digest {claimed}, which the approved revision"
            f" already replaced (active: {digest}) — dispatch must read the"
            " revised plan, not the superseded comment",
            active=digest,
            revised_from=revised_from,
        )
    return _dispatch_refused(
        run_id,
        "plan_digest_mismatch",
        f"the /go claims plan digest {claimed} but the active plan is {digest}"
        " — an unknown claim refuses like any other",
        active=digest,
        revised_from=revised_from,
    )


# Executor bindings fence superseded approval and preserve only compatible artifacts.

#: The closed artifact-kind vocabulary the reuse decision speaks.
CHECKPOINT_KIND = "checkpoint"
VERIFICATION_KIND = "verification"
EVIDENCE_KIND = "evidence"

#: The closed decision vocabulary.
PRESERVE_DECISION = "preserve"
INVALIDATE_DECISION = "invalidate"
DISCARD_DECISION = "discard"

#: The checkpoint's explicit route: keep the WIP, or start over on record.
PRESERVE_ROUTE = "preserve"
FRESH_ATTEMPT_ROUTE = "fresh_attempt"

#: Where the activation's reuse decision lives inside ``FlowRun.evidence``.
CHECKPOINT_REUSE_DECISION_KEY = "checkpoint_reuse_decision"
REUSE_DECISION_SCHEMA = "forge.checkpoint.reuse-decision/1"

#: Where the dispatch's executor-input identity lives in the evidence.
REVISION_EXECUTOR_DIGEST_KEY = "revision_executor_digest"
EXECUTOR_DIGEST_SCHEMA = "forge.revision.executor-digest/1"

#: The identity fields the executor-input digest is taken over — exactly the dispatch inputs
#: a native job receives, so any ledger that recorded the inputs can recompute the digest
#: and compare.
EXECUTOR_INPUT_FIELDS: tuple[str, ...] = (
    "run_id",
    "plan_digest",
    "envelope_digest",
    "spec_digest",
    "lane_resume_mode",
)


@dataclass(frozen=True)
class WorkArtifact:
    """One work artifact the reuse decision judges."""

    artifact_id: str
    kind: str = EVIDENCE_KIND
    step_id: str = ""
    applicability_digest: str = ""


@dataclass(frozen=True)
class ArtifactReuseDecision:
    """One artifact's verdict, with the durable reason a human can read."""

    artifact_id: str
    kind: str
    decision: str
    reason: str

    def document(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "decision": self.decision,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WipReuseDecision:
    """The partition a material revision's activation persists."""

    activated_revision: int
    plan_digest: str
    decisions: tuple[ArtifactReuseDecision, ...]
    route: str
    route_reason: str

    @property
    def preserved(self) -> tuple[str, ...]:
        return tuple(
            decision.artifact_id
            for decision in self.decisions
            if decision.decision == PRESERVE_DECISION
        )

    @property
    def invalidated(self) -> tuple[str, ...]:
        return tuple(
            decision.artifact_id
            for decision in self.decisions
            if decision.decision == INVALIDATE_DECISION
        )

    @property
    def discarded(self) -> tuple[str, ...]:
        return tuple(
            decision.artifact_id
            for decision in self.decisions
            if decision.decision == DISCARD_DECISION
        )

    def document(self) -> dict[str, Any]:
        """The durable shape frozen into the run's evidence."""
        return {
            "schema": REUSE_DECISION_SCHEMA,
            "activated_revision": self.activated_revision,
            "plan_digest": self.plan_digest,
            "route": self.route,
            "route_reason": self.route_reason,
            "artifacts": [decision.document() for decision in self.decisions],
        }


def decide_wip_reuse(
    old: PlanRevision | None,
    new: PlanRevision,
    artifacts: Iterable[WorkArtifact],
) -> WipReuseDecision:
    """Partition the work artifacts across a material revision's activation."""
    new_by_id = {step.step_id: step for step in new.steps}
    old_by_id = {step.step_id: step for step in old.steps} if old is not None else {}
    removed_ids = {step_id for step_id in old_by_id if step_id not in new_by_id}
    changed_ids = {step.step_id for step in new.steps if old_by_id.get(step.step_id) != step}
    declared_invalid = set(new.invalidated_step_ids)
    new_digest = plan_digest(new)

    decisions: list[ArtifactReuseDecision] = []
    for artifact in artifacts:
        decisions.append(
            _decide_artifact(
                artifact,
                old=old,
                new=new,
                removed_ids=removed_ids,
                changed_ids=changed_ids,
                declared_invalid=declared_invalid,
                new_digest=new_digest,
            )
        )

    checkpoint_decisions = [decision for decision in decisions if decision.kind == CHECKPOINT_KIND]
    if not checkpoint_decisions:
        route, reason = PRESERVE_ROUTE, "no workspace checkpoint on record"
    elif all(decision.decision == PRESERVE_DECISION for decision in checkpoint_decisions):
        route = PRESERVE_ROUTE
        reason = "compatible revision: {} — the checkpoint stands under the new plan".format(
            ", ".join(sorted(decision.artifact_id for decision in checkpoint_decisions))
        )
    else:
        failing = next(
            decision for decision in checkpoint_decisions if decision.decision != PRESERVE_DECISION
        )
        route = FRESH_ATTEMPT_ROUTE
        reason = failing.reason
    return WipReuseDecision(
        activated_revision=new.revision,
        plan_digest=new_digest,
        decisions=tuple(decisions),
        route=route,
        route_reason=reason,
    )


def _decide_artifact(
    artifact: WorkArtifact,
    *,
    old: PlanRevision | None,
    new: PlanRevision,
    removed_ids: set[str],
    changed_ids: set[str],
    declared_invalid: set[str],
    new_digest: str,
) -> ArtifactReuseDecision:
    """One artifact's verdict (the rules table of :func:`decide_wip_reuse`)."""
    if artifact.step_id:
        if artifact.step_id in removed_ids:
            return ArtifactReuseDecision(
                artifact.artifact_id,
                artifact.kind,
                DISCARD_DECISION,
                f"step {artifact.step_id} was removed by revision {new.revision}"
                " — the work is no longer pursued, the artifact leaves the active set",
            )
        if artifact.step_id in declared_invalid or artifact.step_id in changed_ids:
            why = (
                f"step {artifact.step_id} is named in revision {new.revision}'s invalidated steps"
                if artifact.step_id in declared_invalid
                else f"step {artifact.step_id} changed in revision {new.revision}"
            )
            return ArtifactReuseDecision(
                artifact.artifact_id,
                artifact.kind,
                INVALIDATE_DECISION,
                f"{why} — superseded (invalidated verification never auto-returns to passed)",
            )
        return ArtifactReuseDecision(
            artifact.artifact_id,
            artifact.kind,
            PRESERVE_DECISION,
            f"step {artifact.step_id} survived revision {new.revision} unchanged",
        )

    if artifact.kind == VERIFICATION_KIND:
        if artifact.applicability_digest and artifact.applicability_digest != new_digest:
            return ArtifactReuseDecision(
                artifact.artifact_id,
                artifact.kind,
                INVALIDATE_DECISION,
                "verified under the superseded plan digest "
                f"{artifact.applicability_digest[:12]}… — superseded by revision "
                f"{new.revision}; a fresh verification under the new plan is "
                "required (never an automatic return to passed)",
            )
        return ArtifactReuseDecision(
            artifact.artifact_id,
            artifact.kind,
            PRESERVE_DECISION,
            f"verified under the active plan of revision {new.revision}",
        )

    if artifact.kind == CHECKPOINT_KIND:
        if old is None:
            return ArtifactReuseDecision(
                artifact.artifact_id,
                artifact.kind,
                INVALIDATE_DECISION,
                "checkpoint compatibility is UNDECIDABLE — the superseded plan "
                "content is not durable, and an undecidable checkpoint is never "
                "silently preserved (fail closed)",
            )
        wip_steps = [step for step in old.steps if step.write_repository_id]
        wip_ids = {step.step_id for step in wip_steps}
        if wip_ids and wip_ids <= removed_ids:
            return ArtifactReuseDecision(
                artifact.artifact_id,
                artifact.kind,
                DISCARD_DECISION,
                "every write-carrying step the checkpoint's WIP anchored on was "
                f"removed by revision {new.revision} — the revision pursues "
                "different work",
            )
        churned = sorted(
            step_id
            for step_id in wip_ids
            if step_id in removed_ids or step_id in changed_ids or step_id in declared_invalid
        )
        if churned:
            return ArtifactReuseDecision(
                artifact.artifact_id,
                artifact.kind,
                INVALIDATE_DECISION,
                "write-carrying step(s) the checkpoint's WIP anchors on changed "
                f"or were invalidated by revision {new.revision}: {churned} — "
                "the restored bytes cannot stand under the new plan",
            )
        return ArtifactReuseDecision(
            artifact.artifact_id,
            artifact.kind,
            PRESERVE_DECISION,
            f"every write-carrying step survived revision {new.revision} "
            "byte-for-byte — the restored WIP stands under the new plan",
        )

    # A generic plan-scoped artifact: applicability decides, when declared.
    if artifact.applicability_digest and artifact.applicability_digest != new_digest:
        return ArtifactReuseDecision(
            artifact.artifact_id,
            artifact.kind,
            INVALIDATE_DECISION,
            "produced under the superseded plan digest "
            f"{artifact.applicability_digest[:12]}… — superseded by revision {new.revision}",
        )
    return ArtifactReuseDecision(
        artifact.artifact_id,
        artifact.kind,
        PRESERVE_DECISION,
        f"revision {new.revision} does not touch the artifact's applicability",
    )


def wip_artifacts_of_evidence(
    evidence: Mapping[str, Any], *, superseded_plan_digest: str
) -> list[WorkArtifact]:
    """The run's PLAN-SCOPED artifacts, read from its own durable evidence."""
    artifacts: list[WorkArtifact] = []
    continuation = evidence.get("continuation")
    checkpoint_id = (
        str(continuation.get("checkpoint_digest") or "") if isinstance(continuation, dict) else ""
    )
    if checkpoint_id:
        artifacts.append(
            WorkArtifact(
                artifact_id=checkpoint_id,
                kind=CHECKPOINT_KIND,
                applicability_digest=superseded_plan_digest,
            )
        )
    verification = evidence.get("verification")
    if isinstance(verification, dict):
        tested = str(verification.get("tested_oid") or "")
        artifacts.append(
            WorkArtifact(
                artifact_id=tested or "verification",
                kind=VERIFICATION_KIND,
                applicability_digest=superseded_plan_digest,
            )
        )
    return artifacts


async def _durable_wip_artifacts(
    session_factory: _RevisionSessionFactory,
    evidence: Mapping[str, Any],
    *,
    run_id: str,
    superseded_plan_digest: str,
) -> tuple[list[WorkArtifact], str]:
    """The artifacts + an uncertainty sentence, from the durable authorities."""
    artifacts = wip_artifacts_of_evidence(evidence, superseded_plan_digest=superseded_plan_digest)
    if any(artifact.kind == CHECKPOINT_KIND for artifact in artifacts):
        return artifacts, ""
    try:
        from forge.adaptive.checkpoint_repository import resolve_repository

        repository = resolve_repository(session_factory=session_factory)
        entry = await repository.entry(run_id)
    except Exception as exc:  # noqa: BLE001 — typed unavailable, never fatal here
        return artifacts, f"{type(exc).__name__}: {exc}"[:200]
    checkpoint_id = str((entry or {}).get("checkpoint_id") or "")
    if not checkpoint_id:
        return artifacts, ""
    artifacts.append(
        WorkArtifact(
            artifact_id=checkpoint_id,
            kind=CHECKPOINT_KIND,
            applicability_digest=superseded_plan_digest,
        )
    )
    return artifacts, ""


def _factory_of(session: Any) -> Any:
    """The zero-arg callable handing the repository THIS transaction's session."""
    return lambda: session


async def read_wip_reuse_decision(
    session_factory: _RevisionSessionFactory, run_id: str
) -> dict[str, Any] | None:
    """The run's persisted reuse-decision document, or ``None``."""
    from forge.durable import FlowRun

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        document = (
            (run.evidence or {}).get(CHECKPOINT_REUSE_DECISION_KEY) if run is not None else None
        )
    return dict(document) if isinstance(document, dict) else None


async def refused_wip_reuse(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    *,
    resume_mode: str,
) -> dict[str, Any] | None:
    """The recorded fresh-attempt route when THIS dispatch would reuse the checkpoint."""
    if resume_mode != "required":
        return None
    document = await read_wip_reuse_decision(session_factory, run_id)
    if document is not None and document.get("route") == FRESH_ATTEMPT_ROUTE:
        return document
    return None


def executor_input_digest(identity: Mapping[str, Any]) -> str:
    """The canonical digest of one dispatch's executor-input identity."""
    canonical = json.dumps(
        {name: str(identity.get(name) or "") for name in EXECUTOR_INPUT_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def executor_digest_document(
    *,
    run_id: str,
    plan_digest: str,
    active_revision: int,
    envelope_digest: str = "",
    spec_digest: str = "",
    lane_resume_mode: str = "",
    revised_from_digest: str = "",
    activated_by_decision: str = "",
) -> dict[str, Any]:
    """The ``revision.executor_digest`` evidence document."""
    identity = {
        "run_id": run_id,
        "plan_digest": plan_digest,
        "envelope_digest": envelope_digest,
        "spec_digest": spec_digest,
        "lane_resume_mode": lane_resume_mode,
    }
    return {
        "schema": EXECUTOR_DIGEST_SCHEMA,
        **identity,
        "active_revision": active_revision,
        "revised_from_digest": revised_from_digest,
        "activated_by_decision": activated_by_decision,
        "executor_input_digest": executor_input_digest(identity),
    }


# the ApprovedInput: the executor's brief binds to the ACTIVE revision's TEXT, not the
# superseded spec's
# The live
# counterexample (docs/evaluation/2026-09-25-combined-steering, cycle 1): the operator's
# approved revision renamed the public entrypoint (approach Y), the activation CAS switched
# identity AND digest, the WIP reuse preserved the renamed checkpoint — and the resumed lane
# REVERTED to approach X, because the GitLab dispatch still briefed it from the spec-frozen
# ``plan_summary``.

#: The approved-input document's schema (the versioned input contract the rollout note
#: names: legacy attempts carry no such section and resolve through the labeled adapter
#: below).
APPROVED_INPUT_SCHEMA = "forge.revision.approved-input/1"

#: Where the dispatch's resolved approved input lives in the evidence.
APPROVED_INPUT_KEY = "approved_input"

#: The resolution's source labels. ``revision`` — an activated revision's durable content IS
#: the brief source. ``spec`` — no revision was ever activated; the frozen spec's plan is
#: the brief (today's behavior, labeled). ``spec-legacy`` — a prior-version pointer carries
#: no revision content (persisted before): the EXPLICIT legacy adapter keeps the spec brief
#: under its own label, never a silent re-derivation.
APPROVED_SOURCE_REVISION = "revision"
APPROVED_SOURCE_SPEC = "spec"
APPROVED_SOURCE_SPEC_LEGACY = "spec-legacy"


class RevisionRebindRefused(ValueError):
    """The typed refusal approved-input resolution raises."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"approved-input resolution refused [{code}] {detail}")
        self.code = code
        self.detail = detail


def _bytes_digest(text: str) -> str:
    """sha256 over the exact UTF-8 bytes of one text field."""
    return sha256(text.encode("utf-8")).hexdigest()


def render_revision_brief(
    revision: PlanRevision,
    *,
    plan_digest_value: str = "",
    activated_by_decision: str = "",
    wip_reuse: Mapping[str, Any] | None = None,
    evidence_refs: Iterable[str] = (),
) -> str:
    """The executor brief rendered from one ACTIVE revision's content."""
    lines = [
        f"Implementation plan — active revision {revision.revision} of "
        f"{revision.plan_id}" + (f" (digest {plan_digest_value})" if plan_digest_value else ""),
    ]
    if activated_by_decision:
        lines.append(f"Activated by approved decision {activated_by_decision}.")
    lines.append("")
    if revision.summary.strip():
        lines.extend([revision.summary.strip(), ""])
    lines.append("Steps:")
    for step in revision.steps:
        suffix = f" [writes {step.write_repository_id}]" if step.write_repository_id else ""
        lines.append(f"{step.step_id}. {step.objective}{suffix}")
    reuse = dict(wip_reuse or {})
    if reuse:
        lines.append("")
        lines.append(
            f"WIP reuse route: {reuse.get('route') or 'unknown'} — "
            f"{str(reuse.get('route_reason') or '').strip()}"
        )
    preserved = [ref for ref in evidence_refs if ref]
    if preserved:
        lines.append(f"Preserved evidence: {', '.join(sorted(preserved))}")
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class ApprovedInput:
    """The ONE immutable executor-input record every dispatch resolves."""

    run_id: str
    task_title: str
    task_description: str
    plan_text: str
    source: str
    plan_id: str = ""
    work_id: str = ""
    active_revision: int = 0
    plan_digest: str = ""
    revised_from_digest: str = ""
    work_contract_digest: str = ""
    evidence_refs: tuple[str, ...] = ()
    allowed_writes: tuple[str, ...] = ()
    wip_reuse: Mapping[str, Any] = dataclass_field(default_factory=dict)
    activated_by_decision: str = ""

    @property
    def task(self) -> str:
        """The frozen task text — title and description, the envelope's pair."""
        return f"{self.task_title}\n{self.task_description}".strip()

    @property
    def revision_bound(self) -> bool:
        """True only when the ACTIVE revision's content is the brief source."""
        return self.source == APPROVED_SOURCE_REVISION

    @property
    def plan_text_digest(self) -> str:
        """sha256 over the brief's plan TEXT — the runner-consumed bytes."""
        return _bytes_digest(self.plan_text)

    def brief(self) -> str:
        """The executor brief — the record's plan TEXT, byte-exactly."""
        return self.plan_text

    def document(self) -> dict[str, Any]:
        """The durable shape persisted beside the native-start intent."""
        return {
            "schema": APPROVED_INPUT_SCHEMA,
            "run_id": self.run_id,
            "source": self.source,
            "task_title": self.task_title,
            "task_description": self.task_description,
            "plan_text": self.plan_text,
            "plan_text_digest": self.plan_text_digest,
            "plan_id": self.plan_id,
            "work_id": self.work_id,
            "active_revision": self.active_revision,
            "plan_digest": self.plan_digest,
            "revised_from_digest": self.revised_from_digest,
            "work_contract_digest": self.work_contract_digest,
            "evidence_refs": list(self.evidence_refs),
            "allowed_writes": list(self.allowed_writes),
            "wip_reuse": dict(self.wip_reuse),
            "activated_by_decision": self.activated_by_decision,
        }


async def resolve_approved_input(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    *,
    task_title: str,
    task_description: str,
    spec_plan_text: str,
    spec_plan_digest: str,
    allowed_writes: Iterable[str] = (),
) -> ApprovedInput:
    """Resolve the executor's approved input from durable state."""
    from forge.durable import FlowRun

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise RevisionRebindRefused("run_not_found", f"flow run {run_id!r} not found")
        evidence = run.evidence or {}
        active_raw = evidence.get(ACTIVE_PLAN_KEY)
        active = dict(active_raw) if isinstance(active_raw, dict) else None
        reuse_raw = evidence.get(CHECKPOINT_REUSE_DECISION_KEY)
        reuse = dict(reuse_raw) if isinstance(reuse_raw, dict) else {}

    evidence_refs = tuple(
        str(entry.get("artifact_id") or "")
        for entry in (reuse.get("artifacts") or [])
        if isinstance(entry, dict) and entry.get("decision") == PRESERVE_DECISION
    )
    shared = {
        "run_id": run_id,
        "task_title": str(task_title or ""),
        "task_description": str(task_description or ""),
        "evidence_refs": evidence_refs,
        "allowed_writes": tuple(allowed_writes),
        "wip_reuse": reuse,
    }
    if active is None:
        return ApprovedInput(
            **shared,
            plan_text=str(spec_plan_text or ""),
            source=APPROVED_SOURCE_SPEC,
            plan_digest=str(spec_plan_digest or ""),
        )

    content_raw = active.get(REVISION_CONTENT_KEY)
    if not isinstance(content_raw, dict) or not content_raw:
        # The prior-version pointer: identity without content.
        return ApprovedInput(
            **shared,
            plan_text=str(spec_plan_text or ""),
            source=APPROVED_SOURCE_SPEC_LEGACY,
            plan_id=str(active.get("plan_id") or ""),
            work_id=str(active.get("work_id") or ""),
            active_revision=int(active.get("active_revision") or 0),
            plan_digest=str(active.get("plan_digest") or ""),
            revised_from_digest=str(active.get("revised_from_digest") or ""),
            work_contract_digest=str(active.get("work_contract_digest") or ""),
            activated_by_decision=str(active.get("activated_by_decision") or ""),
        )

    try:
        revision = PlanRevision.model_validate(dict(content_raw))
    except Exception as exc:  # pydantic ValidationError — unreadable content
        raise RevisionRebindRefused(
            "content_unreadable",
            f"the active plan's revision content does not parse: {type(exc).__name__}",
        ) from exc
    pointer_digest = str(active.get("plan_digest") or "")
    content_digest = plan_digest(revision)
    if pointer_digest != content_digest:
        raise RevisionRebindRefused(
            "content_digest_mismatch",
            f"the durable revision content digests to {content_digest} but the "
            f"active-plan pointer says {pointer_digest} — the content and the "
            "identity the activation switched disagree",
        )
    revision_bound = ApprovedInput(
        **shared,
        plan_text=render_revision_brief(
            revision,
            plan_digest_value=pointer_digest,
            activated_by_decision=str(active.get("activated_by_decision") or ""),
            wip_reuse=reuse,
            evidence_refs=evidence_refs,
        ),
        source=APPROVED_SOURCE_REVISION,
        plan_id=str(active.get("plan_id") or "") or revision.plan_id,
        work_id=str(active.get("work_id") or "") or revision.work_id,
        active_revision=int(active.get("active_revision") or 0) or revision.revision,
        plan_digest=pointer_digest,
        revised_from_digest=str(active.get("revised_from_digest") or ""),
        work_contract_digest=str(active.get("work_contract_digest") or ""),
        activated_by_decision=str(active.get("activated_by_decision") or ""),
    )
    return revision_bound


# the standing-guidance promotion (the cycle-2 remedy)
#

#: The closed marker vocabulary that classifies an operator's guidance as STANDING (a
#: direction that must outlive the current turn).
_STANDING_GUIDANCE_MARKERS: frozenset[str] = frozenset(
    {
        "standing direction",
        "standing guidance",
        "from now on",
        "going forward",
        "for future turns",
        "for the remainder",
        "henceforth",
        "permanently",
        "always use",
        "always name",
    }
)


def is_standing_guidance(text: str) -> bool:
    """True only when *text* declares a STANDING direction."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _STANDING_GUIDANCE_MARKERS)


def standing_guidance_revision(
    active: PlanRevision, text: str, *, command_id: str = ""
) -> PlanRevision | None:
    """The PENDING proposal an accepted standing steer promotes to."""
    if not is_standing_guidance(text):
        return None
    provenance = f" (promoted from accepted guidance {command_id})" if command_id else ""
    folded = f"{active.summary.rstrip()}\n\nStanding direction{provenance}: {str(text).strip()}"
    return active.model_copy(
        update={
            "revision": active.revision + 1,
            "parent_revision": active.revision,
            "summary": folded,
        }
    )


async def stage_standing_guidance_promotion(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    text: str,
    *,
    command_id: str = "",
) -> str | None:
    """Promote accepted standing guidance into a PENDING revision."""
    from forge.durable import FlowRun

    if not is_standing_guidance(text):
        return None
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise RevisionRebindRefused("run_not_found", f"flow run {run_id!r} not found")
        active_raw = (run.evidence or {}).get(ACTIVE_PLAN_KEY)
        active = dict(active_raw) if isinstance(active_raw, dict) else None
    if active is None:
        return None
    content_raw = active.get(REVISION_CONTENT_KEY)
    if not isinstance(content_raw, dict) or not content_raw:
        return None  # a prior-version pointer — never a re-derivation
    try:
        revision = PlanRevision.model_validate(dict(content_raw))
    except Exception:
        return None  # unreadable content refuses; the operator re-raises the proposal
    proposed = standing_guidance_revision(revision, text, command_id=command_id)
    if proposed is None:  # pragma: no cover — the marker check above already passed
        return None
    if plan_digest(proposed) == plan_digest(revision):
        return None  # the fold changed nothing — nothing to approve
    decision = RevisionDecision(
        decision_id=f"rd-standing-{sha256(f'{run_id}:{command_id}:{plan_digest(proposed)}'.encode('utf-8')).hexdigest()[:16]}",
        work_id=revision.work_id,
        parent_revision=revision.revision,
        proposed_revision_id=proposed_revision_identity(proposed),
        proposed_digest=plan_digest(proposed),
        work_contract_digest=revision.work_contract_digest,
        authorization_epoch=int(active.get("authorization_epoch") or 0),
    )
    current = ActivePlanState(
        work_id=str(active.get("work_id") or revision.work_id),
        plan_id=str(active.get("plan_id") or revision.plan_id),
        active_revision=int(active.get("active_revision") or revision.revision),
        work_contract_digest=str(active.get("work_contract_digest") or ""),
        authorization_epoch=int(active.get("authorization_epoch") or 0),
        publication_epoch=int(active.get("publication_epoch") or 0),
    )
    await stage_pending_revision(session_factory, run_id, decision, proposed, current, old=revision)
    return decision.decision_id


# Bind follow-up edits to the current MR head and permitted scope.

#: The review-feedback request document's schema.
REVIEW_FEEDBACK_SCHEMA = "forge.review.feedback/1"

#: Where the run's review-feedback requests live inside ``FlowRun.evidence`` — a dict keyed
#: by the originating note id (the idempotency key).
REVIEW_FEEDBACK_KEY = "review_feedback_requests"

#: The closed classification vocabulary.
CLARIFICATION_CLASS = "clarification"
IN_SCOPE_CORRECTION_CLASS = "in-scope_correction"
MATERIAL_CHANGE_CLASS = "material_change"

#: The request's lifecycle states (the durable ``status`` field).
REQUEST_RECORDED = "recorded"
REQUEST_STAGED = "staged"
REQUEST_DISPATCHED = "dispatched"
REQUEST_STALE_HEAD = "stale_head"
REQUEST_MATERIALIZED = "material_proposal"
REQUEST_CLARIFICATION_OPEN = "clarification_open"
REQUEST_REFUSED_UNAUTHORIZED = "refused_unauthorized"
REQUEST_DELETED_DISCUSSION = "deleted_discussion"
REQUEST_CONFLICTING = "conflicting_correction"
REQUEST_WINDOW_CLOSED = "correction_window_closed"
#: the request admitted a linked post-readiness round — the round's child run carries the
#: correction; the parent's terminal record is untouched (the linkage lives in
#: ``review_rounds``).
REQUEST_ROUND_ADMITTED = "round_admitted"
#: the MR is merged/closed — eligibility refused, zero commits (the human decision already
#: ended the collaboration surface).
REQUEST_MR_CLOSED = "mr_closed"
#: the bounded round count is exhausted — the operator policy (FORGE_MAX_REVIEW_ROUNDS)
#: refuses further rounds on the lineage.
REQUEST_ROUND_LIMIT = "round_limit"
#: the lineage's collaboration target could not be resolved — an unresolved legacy topology,
#: or recorded handles that disagree (run MR vs round MR vs provider MR vs the target row).
REQUEST_TARGET_REFUSED = "collaboration_target_unresolved"
#: the MR surface is unreadable for a PERMISSION reason — the token was refused (HTTP 403)
#: on the read a feedback command's outcome depends on.
REQUEST_MR_FORBIDDEN = "mr_forbidden"
#: the collaboration surface is GONE — the MR was deleted (HTTP 404) on the read a feedback
#: command's outcome depends on.
REQUEST_MR_MISSING = "mr_missing"


class ReviewFeedbackRefused(ValueError):
    """The typed refusal the review-feedback legs raise."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"review feedback refused [{code}] {detail}")
        self.code = code
        self.detail = detail


class ReviewObservationRetryable(Exception):
    """A REQUIRED provider observation failed transiently."""

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        retry_after: float | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"review observation retryable [{reason}] {detail}")
        self.reason = reason
        self.detail = detail
        self.retry_after = retry_after
        self.status_code = status_code


def delivery_identity(request: "ReviewFeedbackRequest") -> tuple:
    """The identity a RETRIED delivery re-derives from its durable inputs."""
    return (
        request.note_id,
        request.run_id,
        request.discussion_id,
        request.mr_iid,
        request.actor,
        request.classification,
        request.text,
        request.referenced_paths,
    )


#: ``/fix <bounded description>`` — the correction request (a named edit).
_REVIEW_FIX_RE = re.compile(r"/fix\s+(.+)", re.IGNORECASE | re.DOTALL)
#: ``/ask <question>`` — the clarification (a question, no code request).
_REVIEW_ASK_RE = re.compile(r"/ask\s+(.+)", re.IGNORECASE | re.DOTALL)
#: A path token inside backticks (`` `src/app.py` ``) — the explicit scope claim a reviewer
#: makes when naming the edit's target.
_PATH_TOKEN_RE = re.compile(r"`([^`\n]+)`")


def parse_review_feedback_note(note_text: str) -> tuple[str, str] | None:
    """Parse a review-feedback note: ``(kind, body)`` or ``None``."""
    text = str(note_text or "").strip()
    fix = _REVIEW_FIX_RE.search(text)
    if fix is not None:
        return "fix", fix.group(1).strip()
    ask = _REVIEW_ASK_RE.search(text)
    if ask is not None:
        return "ask", ask.group(1).strip()
    return None


def referenced_paths_of(text: str) -> tuple[str, ...]:
    """The paths a reviewer explicitly claimed, in note order."""
    lowered = str(text or "")
    return tuple(
        dict.fromkeys(token.strip() for token in _PATH_TOKEN_RE.findall(lowered) if token.strip())
    )


def _path_inside_scope(path: str, allowed: Iterable[str]) -> bool:
    """Whether one claimed path falls inside the approved write scope."""
    claim = path.strip().strip("/")
    for entry in allowed:
        pattern = str(entry).strip().strip("/")
        if not pattern:
            continue
        if fnmatchcase(claim, pattern):
            return True
        has_wildcard = any(mark in pattern for mark in "*?[")
        if not has_wildcard and (claim == pattern or claim.startswith(pattern + "/")):
            return True
    return False


def classify_review_feedback(kind: str, text: str, allowed_paths: Iterable[str] = ()) -> str:
    """Classify one review-feedback note into the closed vocabulary."""
    if kind == "ask":
        return CLARIFICATION_CLASS
    if kind == "fix":
        claimed = referenced_paths_of(text)
        allowed = tuple(str(entry) for entry in allowed_paths)
        if claimed and all(_path_inside_scope(path, allowed) for path in claimed):
            return IN_SCOPE_CORRECTION_CLASS
        return MATERIAL_CHANGE_CLASS
    raise ReviewFeedbackRefused("unknown_kind", f"unknown review-feedback kind {kind!r}")


@dataclass(frozen=True)
class ReviewFeedbackRequest:
    """ONE immutable reviewer comment, durably recorded."""

    note_id: str
    run_id: str
    discussion_id: str
    mr_iid: int
    actor: str
    head_sha: str
    classification: str
    text: str
    referenced_paths: tuple[str, ...] = ()
    diff_context: str = ""
    created_at: str = ""
    decision_id: str = ""
    status: str = REQUEST_RECORDED
    #: The precise evidence invalidation the staging recorded (which items the correction
    #: supersedes, which survive) — applicability partitioned at staging time, re-readable
    #: from the record.
    invalidation: Mapping[str, Any] = dataclass_field(default_factory=dict)
    #: A conflicting note's refusal names the staged decision it lost to.
    conflict_with: str = ""

    def with_status(self, status: str, **updates: Any) -> "ReviewFeedbackRequest":
        """One lifecycle transition — a new record, never an in-place edit."""
        return replace(self, status=status, **updates)

    @property
    def code_requested(self) -> bool:
        """True only when the reviewer asked for a change (not a question)."""
        return self.classification != CLARIFICATION_CLASS

    def document(self) -> dict[str, Any]:
        """The durable shape frozen into the run's evidence."""
        return {
            "schema": REVIEW_FEEDBACK_SCHEMA,
            "note_id": self.note_id,
            "run_id": self.run_id,
            "discussion_id": self.discussion_id,
            "mr_iid": self.mr_iid,
            "actor": self.actor,
            "head_sha": self.head_sha,
            "classification": self.classification,
            "text": self.text,
            "referenced_paths": list(self.referenced_paths),
            "diff_context": self.diff_context,
            "created_at": self.created_at,
            "decision_id": self.decision_id,
            "status": self.status,
            "invalidation": dict(self.invalidation),
            "conflict_with": self.conflict_with,
        }


def _request_of_document(document: Mapping[str, Any]) -> ReviewFeedbackRequest | None:
    if not isinstance(document, dict):
        return None
    try:
        return ReviewFeedbackRequest(
            note_id=str(document.get("note_id") or ""),
            run_id=str(document.get("run_id") or ""),
            discussion_id=str(document.get("discussion_id") or ""),
            mr_iid=int(document.get("mr_iid") or 0),
            actor=str(document.get("actor") or ""),
            head_sha=str(document.get("head_sha") or ""),
            classification=str(document.get("classification") or ""),
            text=str(document.get("text") or ""),
            referenced_paths=tuple(str(path) for path in (document.get("referenced_paths") or [])),
            diff_context=str(document.get("diff_context") or ""),
            created_at=str(document.get("created_at") or ""),
            decision_id=str(document.get("decision_id") or ""),
            status=str(document.get("status") or REQUEST_RECORDED),
            invalidation=dict(document.get("invalidation") or {}),
            conflict_with=str(document.get("conflict_with") or ""),
        )
    except (TypeError, ValueError):
        return None


def review_feedback_requests_of(evidence: Mapping[str, Any]) -> dict[str, ReviewFeedbackRequest]:
    """The run's recorded review-feedback requests, keyed by note id."""
    raw = (evidence or {}).get(REVIEW_FEEDBACK_KEY)
    if not isinstance(raw, dict):
        return {}
    requests: dict[str, ReviewFeedbackRequest] = {}
    for key, document in raw.items():
        request = _request_of_document(document)
        if request is not None:
            requests[str(key)] = request
    return requests


async def read_review_feedback_requests(
    session_factory: _RevisionSessionFactory, run_id: str
) -> dict[str, ReviewFeedbackRequest]:
    """The run's durable review-feedback requests (or ``{}``)."""
    from forge.durable import FlowRun

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        evidence = run.evidence if run is not None else {}
    return review_feedback_requests_of(evidence or {})


async def _write_request(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    request: ReviewFeedbackRequest,
    *,
    patch: dict[str, Any] | None = None,
) -> None:
    """Persist ONE request (or its lifecycle transition) into the evidence."""
    from forge.durable import FlowRun, Outbox

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise ReviewFeedbackRefused("run_not_found", f"flow run {run_id!r} not found")
        merged = dict(run.evidence or {})
        section = dict(merged.get(REVIEW_FEEDBACK_KEY) or {})
        section[request.note_id] = request.document()
        merged[REVIEW_FEEDBACK_KEY] = section
        run.evidence = merged
        session.add(
            Outbox(
                flow_run_id=run_id,
                event_type="review_feedback.request",
                payload={
                    "run_id": run_id,
                    "note_id": request.note_id,
                    "classification": request.classification,
                    "status": request.status,
                    **(patch or {}),
                },
            )
        )
        await session.commit()


def _request_identity(request: ReviewFeedbackRequest) -> tuple:
    """The immutable identity fields — everything but lifecycle + clock."""
    return (
        request.note_id,
        request.run_id,
        request.discussion_id,
        request.mr_iid,
        request.actor,
        request.head_sha,
        request.classification,
        request.text,
        request.referenced_paths,
        request.diff_context,
    )


async def record_review_feedback_request(
    session_factory: _RevisionSessionFactory, run_id: str, request: ReviewFeedbackRequest
) -> ReviewFeedbackRequest:
    """Record the request ONCE — the note id is the idempotency key."""
    existing = await read_review_feedback_requests(session_factory, run_id)
    recorded = existing.get(request.note_id)
    if recorded is not None:
        if _request_identity(recorded) == _request_identity(request):
            return recorded
        raise ReviewFeedbackRefused(
            "note_id_conflict",
            f"note {request.note_id!r} already recorded a different request "
            f"(status {recorded.status!r}) — one comment is one request",
        )
    await _write_request(session_factory, run_id, request)
    return request


async def mark_review_feedback_request(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    note_id: str,
    status: str,
    **updates: Any,
) -> ReviewFeedbackRequest | None:
    """Apply ONE lifecycle transition to the recorded request (idempotent)."""
    existing = await read_review_feedback_requests(session_factory, run_id)
    recorded = existing.get(note_id)
    if recorded is None:
        return None
    updated = recorded.with_status(status, **updates)
    await _write_request(session_factory, run_id, updated, patch={"transition": status})
    return updated


def head_binding_guard(expected_head: str, actual_head: str) -> None:
    """Refuse, typed, when the MR head moved off the request's binding."""
    if not str(expected_head or "").strip():
        raise ReviewFeedbackRefused(
            "stale_head", "the request carries no head binding — refusing to dispatch blind"
        )
    if str(expected_head) != str(actual_head):
        raise ReviewFeedbackRefused(
            "stale_head",
            f"the MR head moved: the request bound {str(expected_head)[:12]}… "
            f"but the head is now {str(actual_head)[:12]}… — human edits are "
            "preserved; re-raise the correction against the current head",
        )


def correction_invalidation_set(
    expected_head: str, evidence_bindings: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Partition the run's evidence by APPLICABILITY to the corrected head."""
    invalidated: list[tuple[str, str]] = []
    preserved: list[str] = []
    for evidence_id, binding in evidence_bindings.items():
        applicability = str((binding or {}).get("applicability") or "")
        if applicability and applicability == expected_head:
            invalidated.append((evidence_id, f"bound to the corrected head {expected_head[:12]}…"))
        else:
            preserved.append(evidence_id)
    return {
        "expected_head": expected_head,
        "invalidated": invalidated,
        "preserved": preserved,
        "superseded": dict(invalidated),
    }


def correction_decision_id(run_id: str, note_id: str) -> str:
    """The staged correction's decision id — derived from the note identity."""
    digest = sha256(f"{run_id}:{note_id}".encode("utf-8")).hexdigest()[:16]
    return f"rd-review-{digest}"


def review_correction_revision(
    active: PlanRevision, request: ReviewFeedbackRequest
) -> PlanRevision:
    """The bounded input revision an in-scope correction stages."""
    provenance = f"discussion {request.discussion_id or 'unknown'}"
    lines = [
        f"Reviewer correction ({provenance}, note {request.note_id}, "
        f"head {request.head_sha[:12]}…): {request.text.strip()}"
    ]
    if request.referenced_paths:
        lines.append("Permitted paths: " + ", ".join(request.referenced_paths))
    if request.diff_context.strip():
        lines.append(f"Referenced diff: {request.diff_context.strip()}")
    lines.append(
        "Only this correction is permitted — preserve every other human edit on the branch."
    )
    folded = f"{active.summary.rstrip()}\n\n" + "\n".join(lines)
    return active.model_copy(
        update={
            "revision": active.revision + 1,
            "parent_revision": active.revision,
            "summary": folded,
        }
    )


async def _active_revision_content(
    session_factory: _RevisionSessionFactory, run_id: str
) -> tuple[PlanRevision, dict[str, Any]]:
    """The run's ACTIVE revision content + the pointer document it came from."""
    from forge.durable import FlowRun

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise ReviewFeedbackRefused("run_not_found", f"flow run {run_id!r} not found")
        active_raw = (run.evidence or {}).get(ACTIVE_PLAN_KEY)
        active = dict(active_raw) if isinstance(active_raw, dict) else None
    if active is None:
        raise ReviewFeedbackRefused(
            "no_active_plan",
            f"run {run_id!r} carries no durable active plan — a correction revises "
            "an ACTIVE revision, never the frozen spec",
        )
    content_raw = active.get(REVISION_CONTENT_KEY)
    if not isinstance(content_raw, dict) or not content_raw:
        raise ReviewFeedbackRefused(
            "content_unreadable",
            "the active-plan pointer predates the content join — the superseded "
            "revision's bytes are not re-derived",
        )
    try:
        revision = PlanRevision.model_validate(dict(content_raw))
    except Exception as exc:
        raise ReviewFeedbackRefused(
            "content_unreadable",
            f"the active plan's revision content does not parse: {type(exc).__name__}",
        ) from exc
    return revision, active


async def stage_review_correction(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    request: ReviewFeedbackRequest,
) -> str:
    """Stage the bounded correction through the EXISTING approval route."""
    if request.classification != IN_SCOPE_CORRECTION_CLASS:
        raise ReviewFeedbackRefused(
            "not_a_correction",
            f"classification {request.classification!r} stages nothing — only an "
            "in-scope correction becomes a bounded input revision",
        )
    revision, active = await _active_revision_content(session_factory, run_id)
    proposed = review_correction_revision(revision, request)
    if plan_digest(proposed) == plan_digest(
        revision
    ):  # pragma: no cover — the fold always adds text
        raise ReviewFeedbackRefused(
            "empty_correction", "the correction folded nothing into the revision"
        )
    decision_id = correction_decision_id(run_id, request.note_id)
    decision = RevisionDecision(
        decision_id=decision_id,
        work_id=revision.work_id,
        parent_revision=revision.revision,
        proposed_revision_id=proposed_revision_identity(proposed),
        proposed_digest=plan_digest(proposed),
        work_contract_digest=revision.work_contract_digest,
        authorization_epoch=int(active.get("authorization_epoch") or 0),
    )
    current = ActivePlanState(
        work_id=str(active.get("work_id") or revision.work_id),
        plan_id=str(active.get("plan_id") or revision.plan_id),
        active_revision=int(active.get("active_revision") or revision.revision),
        work_contract_digest=str(active.get("work_contract_digest") or ""),
        authorization_epoch=int(active.get("authorization_epoch") or 0),
        publication_epoch=int(active.get("publication_epoch") or 0),
    )
    await stage_pending_revision(session_factory, run_id, decision, proposed, current, old=revision)
    # The request is durably recorded BEFORE the staged mark (a caller that staged without
    # recording still lands ONE request — the note id remains the idempotency key either
    # way).
    await record_review_feedback_request(session_factory, run_id, request)
    await mark_review_feedback_request(
        session_factory,
        run_id,
        request.note_id,
        REQUEST_STAGED,
        decision_id=decision_id,
        invalidation=correction_invalidation_set(
            request.head_sha,
            {
                "review": {"applicability": request.head_sha},
                "verification": {"applicability": request.head_sha},
                "pipeline": {"applicability": request.head_sha},
            },
        ),
    )
    return decision_id


#: The closing review's obligation-identity schema: the document whose sha256 rides the
#: persisted review record beside its candidate sha.
REVIEW_OBLIGATION_SCHEMA = "forge.review.obligation/1"


def review_obligation_digest(
    *,
    plan_text: str,
    candidate_sha: str,
    requests: Iterable[ReviewFeedbackRequest] = (),
) -> str:
    """sha256 over the closing review's FULL obligation identity."""
    obligations = [
        {
            "note_id": request.note_id,
            "text": request.text,
            "head_sha": request.head_sha,
            "classification": request.classification,
            "decision_id": request.decision_id,
        }
        for request in sorted(requests, key=lambda request: request.note_id)
    ]
    document = {
        "schema": REVIEW_OBLIGATION_SCHEMA,
        "plan_text_digest": _bytes_digest(str(plan_text or "")),
        "candidate_sha": str(candidate_sha or ""),
        "obligations": obligations,
    }
    return sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def review_feedback_summary_section(
    requests: Mapping[str, ReviewFeedbackRequest],
    discussion_states: Mapping[str, bool],
    candidate_sha: str,
) -> str:
    """The ``**Review feedback:**`` block the final summary appends."""
    if not requests:
        return ""
    resolved: list[str] = []
    open_: list[str] = []
    for request in requests.values():
        line = (
            f"- {request.discussion_id or request.note_id} "
            f"({request.classification}, @{request.actor})"
        )
        if discussion_states.get(request.discussion_id, False):
            resolved.append(line)
        else:
            open_.append(line)
    lines = ["**Review feedback:**", f"- Tested candidate: `{candidate_sha}`"]
    if resolved:
        lines.append("Resolved discussions:")
        lines.extend(resolved)
    if open_:
        lines.append("Still-open discussions:")
        lines.extend(open_)
    if not resolved and not open_:  # pragma: no cover — requests exist above
        lines.append("- (no tracked discussions)")
    return "\n".join(lines)


# the linked post-readiness review round
# The verified
# gap: ``correction_window_closed`` answered every /fix after ``ready_for_human`` — while
# reviewers ask for corrections AFTER readiness is announced.

#: The round's seeded active-plan document schema (the seed is written by the round
#: admission itself — the authorized /fix + eligibility IS the activation authority,
#: recorded as ``activated_by_decision``).
ROUND_SEED_SCHEMA = "forge.revision.round-seed/1"

#: The closed set of spec document fields the classic adapter reads — the derivation's own
#: honesty bound: nothing outside these frozen, verified values (plus the accepted request)
#: shapes the round's plan.
_CLASSIC_ADAPTER_SPEC_FIELDS: tuple[str, ...] = (
    "task_title",
    "task_description",
    "plan_summary",
    "plan_digest",
    "policy_digest",
    "allowed_paths",
    "source_base_oid",
)


def _spec_digest_of(document: Mapping[str, Any]) -> str:
    """sha256 over the adapter's canonical projection of the spec document."""
    canonical = json.dumps(
        {name: document.get(name) for name in _CLASSIC_ADAPTER_SPEC_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def classic_spec_revision(
    *,
    work_id: str,
    spec: Mapping[str, Any],
    request: ReviewFeedbackRequest,
) -> PlanRevision:
    """The verified classic-run adapter: frozen spec → revision 1."""
    task_title = str(spec.get("task_title") or "").strip()
    plan_summary = str(spec.get("plan_summary") or "").strip()
    projection = _spec_digest_of(spec)
    base = PlanRevision(
        plan_id=f"spec-{projection[:16]}",
        work_id=work_id,
        revision=1,
        parent_revision=None,
        work_contract_digest=_spec_digest_of(
            {
                "policy_digest": spec.get("policy_digest"),
                "allowed_paths": spec.get("allowed_paths"),
                "task_digest": sha256(
                    f"{task_title}\n{spec.get('task_description') or ''}".encode("utf-8")
                ).hexdigest(),
            }
        ),
        snapshot_set_digest=sha256(
            f"{spec.get('source_base_oid') or ''}:{projection}".encode("utf-8")
        ).hexdigest(),
        summary=plan_summary or task_title,
        steps=[
            PlanStep(
                step_id="spec-plan",
                objective=plan_summary or task_title or "Implement the approved plan.",
            )
        ],
    )
    return review_correction_revision(base, request)


def round_active_plan_seed(
    revision: PlanRevision,
    *,
    revised_from_digest: str,
    decision_id: str,
    authorization_epoch: int = 1,
) -> dict[str, Any]:
    """The ACTIVE-plan document a review round's child run is seeded with."""
    return {
        "schema": ACTIVE_PLAN_SCHEMA,
        "seed": ROUND_SEED_SCHEMA,
        "work_id": revision.work_id,
        "plan_id": revision.plan_id,
        "active_revision": revision.revision,
        "plan_digest": plan_digest(revision),
        "revised_from_digest": revised_from_digest,
        "work_contract_digest": revision.work_contract_digest,
        "authorization_epoch": authorization_epoch,
        "publication_epoch": 1,
        "activated_by_decision": decision_id,
        REVISION_CONTENT_KEY: revision.model_dump(),
    }
