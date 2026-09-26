"""The adaptive revision lifecycle — the PLN epic remainder (review 05868e9).

``models.py`` carries the SHAPES (``WorkContract``, ``PlanRevision``,
``ChangeProposal`` ...); this module carries the RULES that keep a running
plan honest between approvals. The separation is the review's own: WHAT/WHY
are approved by a human once, HOW is revised by the agent many times — so
every rule here answers one question: may this revision land without going
back to the human?

- PLN-04 / NXT-20 — :func:`classify_revision` sorts a proposed revision
  into the ChangeProposal vocabulary with authority drawn from the
  CONTRACT alone (the old plan's write grants are lineage, never
  authority) and unknown materiality routed to ``decision_required`` —
  fail closed, never a guess of ``tactical_internal``;
  :func:`apply_tactical` reads the contract's
  ``tactical_revision_policy`` itself and lands only transformations the
  policy explicitly pre-approved — anything else raises and must go to
  the human gate as a decision request.
- PLN-05 / NXT-19 — material revisions land only through a
  compare-and-swap :class:`RevisionDecision` (a stale authorization
  epoch refuses), and :func:`activate_revision` is the single door from
  approval to plan: it guards the FULL binding tuple (work, plan,
  proposal identity, proposed-content digest, contract digest, expected
  active parent, authorization epoch) and then consumes the decision,
  switches the active revision, and bumps the publication fence in ONE
  conditional transaction — any mismatch is a typed refusal that
  consumes nothing.
- NEXT-20 — the journey does not end at activation:
  :func:`dispatch_plan_binding` is the /go dispatch leg's source of
  truth, reading the NEW active plan from :func:`read_active_plan`
  (never the old plan comment) so the next dispatch briefs the lane
  under the REVISED plan's digest; a stale /go still carrying the
  superseded digest refuses ``stale_plan_digest``. The switched
  durable pointer records the digest it replaced
  (``revised_from_digest``), and :func:`plan_comment_revision_note`
  renders the revised plan comment's "revised from <old-digest> to
  <new-digest>" footer from that pair.
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

#: Impact classes a tactical edit may carry. This is an ALLOWLIST, not a
#: blacklist: a declared class outside both this set and
#: ``_MATERIAL_IMPACTS`` is unknown materiality, and unknown goes to a
#: human decision — never to a silent tactical landing. Omitting the tag
#: altogether is equally unknown ("an impact tag was omitted" must never
#: be the reason a change passed).
_KNOWN_TACTICAL_IMPACTS = frozenset({"internal", "refactor", "docs", "tests", "cleanup"})

#: Declared compatibility-breaking impact classes — anything naming these
#: is material by declaration, no approval shortcut exists.
_MATERIAL_IMPACTS = frozenset({"migration", "schema", "api", "public-api", "event", "effect"})

#: The closed vocabulary of automatic plan transformations a contract's
#: ``tactical_revision_policy`` may pre-approve. Anything else in the
#: policy string makes the whole policy unknown (fail closed).
_TACTICAL_POLICY_OPERATIONS = frozenset(
    {"reorder", "edit_step", "reword", "add_step", "remove_step"}
)


def _delta_steps(old: PlanRevision, new: PlanRevision) -> list[PlanStep]:
    """The steps the new plan ADDS or CHANGES — the delta that carries risk.

    A step carried over byte-for-byte keeps the classification its
    original approval already paid for: the revision is judged by what
    it changes, not by re-litigating the whole plan (NXT-20: a genuine
    reorder/refactor succeeds without unnecessary reapproval).
    """
    old_by_id = {step.step_id: step for step in old.steps}
    return [step for step in new.steps if old_by_id.get(step.step_id) != step]


def transformation_kinds(old: PlanRevision, new: PlanRevision) -> frozenset[str]:
    """Name every automatic transformation the delta performs.

    The vocabulary is the closed set the tactical policy grants:
    ``reorder`` (surviving steps in a new order), ``edit_step`` (a
    surviving step modified — objective, deps, impact, references),
    ``reword`` (revision-level text: summary, assumptions), ``add_step``
    and ``remove_step``. Policy checks are set containment over these
    kinds, so an unlisted kind is a decision request by construction.
    """
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
    """The contract's explicit pre-approval of automatic plan edits.

    ``status`` is the fail-closed verdict on the policy string itself:
    ``enabled`` (every token recognized; ``allowed`` carries them),
    ``disabled`` (the contract said no), ``unset`` (the contract never
    declared one — silence is not consent), ``unknown`` (unrecognized
    token). Only ``enabled`` permits anything; every other status
    permits NOTHING, so :func:`classify_revision` routes the revision
    to a human decision instead of guessing.
    """

    raw: str
    status: Literal["enabled", "disabled", "unset", "unknown"]
    allowed: frozenset[str] = frozenset()

    def permits(self, kinds: frozenset[str] | set[str]) -> bool:
        """True only when an ENABLED policy covers every requested kind."""
        return self.status == "enabled" and set(kinds) <= set(self.allowed)


def parse_tactical_policy(contract: WorkContract) -> TacticalPolicy:
    """Parse ``WorkContract.tactical_revision_policy`` fail closed.

    The field is a plain string on the model (see models.py), so the
    grammar lives here: a comma-separated list of transformation kinds
    from :data:`_TACTICAL_POLICY_OPERATIONS`, or the single token
    ``disabled``. Empty means unset. ANY unrecognized token makes the
    policy unknown — an operator typo must narrow authority, never
    widen it.
    """
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
    """Classify a proposed revision into the ChangeProposal vocabulary.

    Authority for WRITES comes from the contract alone (NXT-20): the old
    plan's write repositories contribute nothing to the authorized set —
    the previous plan is a historical artifact, never a source of new
    authority, so a restrictive contract replacing a historically
    broader one narrows immediately. Precedence follows the blast
    radius, widest first:

    - ``material_scope`` — any new-plan step writes a repository the
      contract's write scope never authorized. New write surface is
      never an internal detail.
    - ``material_contract`` — the new steps stop referencing an
      acceptance id the old plan pursued (or the contract itself
      declares). Dropping pursuit of an approved criterion changes WHAT
      must result, not HOW; adding references is tactical, dropping
      never is.
    - ``material_migration`` — a step the revision ADDS OR CHANGES
      declares a compatibility-breaking impact (``migration``,
      ``schema``, ``api``, ``public-api``, ``event``, ``effect``).
      Carried-over steps are not re-litigated here.
    - ``decision_required`` — the materiality is UNKNOWN: a new or
      changed step declares no impact at all, or an impact class
      outside the known tactical vocabulary; or the delta performs a
      transformation kind the contract's tactical policy does not
      explicitly pre-approve. The classifier never guesses
      ``tactical_internal`` — uncertain goes to the human, fail closed.
    - ``tactical_internal`` — every guard passed AND every
      transformation kind in the delta is explicitly allowed by an
      ENABLED ``tactical_revision_policy``.
    """
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

    Tactical edits carry no approval notification — but that speed is
    only safe because TWO independent gates must agree, and
    ``apply_tactical`` checks both itself (NXT-20: it reads the
    contract's ``tactical_revision_policy`` directly, never the
    caller's say-so):

    - the classification must be ``tactical_internal`` — every material
      class AND every unknown materiality raises here; neither can ever
      slip through as a quiet tactical edit. The caller must raise a
      ChangeProposal / decision request for the human gate instead.
    - the policy must be ENABLED and cover every transformation kind
      the delta performs. A disabled, unset, or unrecognized policy
      permits no automatic revision at all.

    The applied revision carries its lineage explicitly: the next
    revision number, its parent, and the preserved-step linkage — every
    old step that survives into the new plan, merged with whatever the
    proposal already declared, so surviving WIP keeps its anchor. The
    event attaches the human-readable diff and the exact transformation
    kinds + policy under which it landed, so the audit trail shows not
    just WHAT changed but under WHICH pre-approval.
    """
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
    """The durable compare-and-swap gate a material revision must pass.

    An approval is only valid against the ``authorization_epoch`` it was
    granted in — the same fence ``ControlCommand.expected_execution_epoch``
    draws for commands. If the world has moved (contract re-approved, plan
    superseded, epoch bumped), a stale "yes" must refuse rather than land
    on top of a state it never saw. Frozen like everything durable here:
    every transition returns a new record, so the journal keeps both
    sides of every decision.

    NXT-19 widens the CAS slot from "an approval" to "an approval OF
    EXACTLY THIS proposed revision of exactly this work": the decision
    carries the proposal's identity (``proposed_revision_id``), its
    canonical content digest (``proposed_digest`` — a changed proposal
    body under the same revision number is a DIFFERENT proposal), the
    contract digest it was judged under, and the parent revision it
    expects to succeed. :func:`activate_revision` guards every one of
    these bindings before consuming anything.

    ``reject`` takes a ``reason`` but does not store it: the verdict is
    decision state, the reason is audit-trail material — it belongs in
    the durable command log beside the rejection, not in the CAS slot
    whose field set is the wire format shared with the coordinator.
    """

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


def proposed_revision_identity(proposed: PlanRevision) -> str:
    """The stable identity string a decision names: ``plan_id#revision``.

    The revision NUMBER alone is not an identity — two different works
    both have a "revision 2" — so the identity a
    :class:`RevisionDecision` binds to is the plan-qualified form.
    """
    return f"{proposed.plan_id}#{proposed.revision}"


class ActivationRefused(ValueError):
    """The typed refusal activation returns instead of consuming anything.

    ``code`` is a stable machine-readable reason (the observability
    ``revision.activation_rejected`` dimension); ``detail`` explains the
    mismatch for the audit trail. Subclassing ``ValueError`` keeps the
    raise-site ergonomics, but callers that need to distinguish "the
    world moved, re-request the decision" from "this decision never
    bound to this proposal" branch on ``code``, never on message text.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"activation refused [{code}] {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ActivePlanState:
    """What the caller asserts the world is RIGHT NOW for one work.

    The activation guards compare the decision's bindings against this
    state — it is the compare-and-swap "expected" side. The durable
    store behind :class:`ActivationSession` is the authoritative one;
    this record is the snapshot the guard runs against, and the session
    re-checks the same expectations inside its conditional commit.
    """

    work_id: str
    plan_id: str
    active_revision: int
    work_contract_digest: str
    authorization_epoch: int
    publication_epoch: int = 0


@dataclass(frozen=True)
class ActivationRecord:
    """The ONE conditional transaction activation asks the store to run.

    A single all-or-nothing commit that consumes the decision, switches
    the active revision, and bumps the publication fence — there is no
    intermediate state in which any one of the three happened alone, so
    a crash between consumption and the switch is unrepresentable. The
    record is also the durable audit entry: replaying the same decision
    finds it via :meth:`ActivationSession.prior_activation` and gets the
    prior outcome back with no new effects.
    """

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
    """The durable-store seam activation runs its one transaction through.

    ``prior_activation`` answers "was this decision already consumed, and
    what did it activate". ``commit_activation`` applies the record
    CONDITIONALLY — the store must refuse (not partially apply) if the
    world no longer matches the guards' expectations, the same
    compare-and-swap the in-memory checks perform; what "condition"
    means concretely (SQL ``WHERE active_revision = ...`` and friends)
    is the store's to implement, the domain's to demand.
    """

    def prior_activation(self, decision_id: str) -> ActivationRecord | None: ...

    def commit_activation(self, record: ActivationRecord) -> None: ...


def _binding_refusal(
    decision: RevisionDecision,
    proposed: PlanRevision,
    current: ActivePlanState,
) -> ActivationRefused | None:
    """The FULL binding-tuple guard, shared by every activation path (NXT-19).

    Returns the typed refusal for the FIRST violated binding, or ``None``
    when the decision binds to exactly this proposal of exactly this
    work under exactly the world *current* describes. Both
    :func:`activate_revision` (the in-memory domain door) and
    :func:`activate_pending_revision` (the durable transaction) run THIS
    guard — one rule set, two doors, no drift.
    """
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
    """Bind an APPROVED decision to its proposed revision — or refuse, typed.

    NXT-19: an approval authorizes exactly ONE activation, and the guard
    checks the FULL binding tuple before anything changes:

    - the decision is approved (never pending, rejected, or expired);
    - ``decision.work_id`` == the proposal's work == the CURRENT work —
      a decision for work A can never activate work B's proposal;
    - ``decision.proposed_revision_id`` == the proposal's identity and
      ``decision.proposed_digest`` == the proposal's canonical digest —
      a changed proposal body under the same revision number refuses;
    - ``decision.work_contract_digest`` == the proposal's contract
      digest == the CURRENT contract digest;
    - ``decision.parent_revision`` == the currently active revision, and
      the proposal's revision follows it — an approval whose parent has
      been superseded fails rather than overwriting newer work;
    - ``decision.authorization_epoch`` == the current epoch.

    Any mismatch raises :class:`ActivationRefused` BEFORE the session is
    touched: the decision is NOT consumed, the active revision stays,
    and the caller re-requests the decision against the world as it now
    is. When every binding holds, consumption, the active-revision
    switch, and the publication-epoch fence land as ONE conditional
    transaction (:class:`ActivationRecord` through
    ``session.commit_activation``) — a crash between them is impossible
    by construction, and the returned immutable revision is the identity
    every downstream dispatch must use. Replaying an already-consumed
    decision returns its prior outcome with no new effects; replaying
    DIFFERENT content under a consumed decision refuses.
    """
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


# ---------------------------------------------------------------------------
# The durable activation transaction (R28-18)
# ---------------------------------------------------------------------------
#
# ``activate_revision`` above is the domain door; this section is its
# durable twin. The review's finding: the conditional ActivationSession
# contract was correct but every session was in-memory, so an
# /approve-revision that arrived through the REAL command ingress had
# nothing durable to transact against — the next dispatch could not read
# the ACTIVE revision from the record. Here the transaction's backing
# store is the ``FlowRun.evidence`` blob (the same runs/ pattern the
# discovery stage and the work package use): one ``revision_activations``
# journal keyed by decision id, one ``active_plan`` pointer the dispatch
# leg reads, and one ``revision_proposal`` slot holding the PENDING
# decision a human approves through the ingress. Record → verify binding
# tuple → switch active → commit happen inside ONE database transaction:
# the session double below STAGES the activation while the guard runs,
# and the evidence write + the outbox row + the single ``commit()`` land
# together or not at all — a failure between decision consumption and
# the revision switch cannot leave partial state.

#: Where each of the durable records lives inside ``FlowRun.evidence``.
REVISION_ACTIVATIONS_KEY = "revision_activations"
ACTIVE_PLAN_KEY = "active_plan"
PENDING_PROPOSAL_KEY = "revision_proposal"

#: Q39-02 (#321): the ACTIVE-plan document's field carrying the activated
#: revision's own content (``PlanRevision.model_dump()``) — the bytes the
#: activation CAS switched the digest over, read back by
#: :func:`resolve_approved_input` at every dispatch entry.
REVISION_CONTENT_KEY = "revision_content"

PENDING_PROPOSAL_SCHEMA = "forge.revision.proposal-pending/1"
ACTIVATION_SCHEMA = "forge.revision.activation/1"
ACTIVE_PLAN_SCHEMA = "forge.revision.active-plan/1"

#: SessionFactory alias (the same shape every runs/ service codes against:
#: a zero-arg callable yielding an async context manager over a session).
_RevisionSessionFactory = Any


@dataclass(frozen=True)
class DurableActivationOutcome:
    """What one :func:`activate_pending_revision` transaction established.

    ``status`` is ``activated`` (this call switched the active revision),
    ``already_active`` (the decision was consumed by an earlier delivery —
    the prior record comes back, nothing new applied) or ``refused``
    (the typed refusal, carrying the domain ``code``). ``revision`` is
    the activated :class:`PlanRevision` for the first two statuses.
    """

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
    """The durable ACTIVE-plan pointer the dispatch leg reads.

    The document carries the switched revision, its canonical digest
    (what :func:`stale_callback_guard` compares late native callbacks
    against), and the NEW publication epoch — one durable answer to
    "which revision is active", replacing the in-memory function call.

    NEXT-20: ``revised_from_digest`` records the digest of the plan this
    one REPLACED (the prior active document's own digest; empty for a
    first activation, or when the prior pointer carried no digest). The
    dispatch leg uses it to refuse a stale ``/go`` that still carries the
    SUPERSEDED plan's digest, and the revised plan comment renders its
    "revised from <old> to <new>" note from this pair — the journey from
    human decision to changed execution is one durable document.

    Q39-02 (#321): ``revision_content`` is the activated revision's own
    ``model_dump()`` — the BYTES the activation CAS switched the digest
    over. Without them the durable pointer names a revision the dispatch
    can identify but never re-read: identity → CONTENT is the join this
    field closes (the live counterexample's gap — the activation switched
    the digest while the executor kept receiving the superseded brief).
    Additive: pointers persisted before Q39-02 carry no such field and
    parse unchanged (the dispatch resolves them through the explicit
    legacy adapter, never a re-derivation).
    """
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
    """Stage a PENDING material-revision decision for the human gate (R28-18).

    The proposal-emitter leg: the UNDECIDED decision (bound to the exact
    proposed content and the world it was judged against), the proposed
    revision and the then-current plan state land in the run's evidence
    under ONE key with an outbox row — so the /approve-revision ingress
    approves something DURABLE, and a restart finds the pending proposal
    exactly as it was staged. Staging OVERWRITES a prior pending
    proposal of the same run (one live proposal per work at a time); the
    prior decision, if never consumed, simply expires unused.

    R36-13 (#272): *old* optionally stages the SUPERSEDED revision's
    content beside the proposal. The activation's WIP reuse decision
    needs the old steps to decide checkpoint compatibility POSITIVELY;
    without them the decision fails closed (an undecidable checkpoint is
    never silently preserved). The staged bytes are audit material only —
    the binding guards below never read them.
    """
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
    """The run's durable ACTIVE-plan document, or ``None`` when never activated.

    The dispatch leg's source of truth (R28-18): which revision is
    active is read from the durable record, not from an in-memory
    activation function. ``plan_digest`` in the document is the identity
    :func:`stale_callback_guard` fences late callbacks with.
    """
    from forge.durable import FlowRun

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            return None
        document = (run.evidence or {}).get(ACTIVE_PLAN_KEY)
        return dict(document) if isinstance(document, dict) else None


class _StagingActivationSession:
    """The in-transaction session double :func:`activate_revision` runs against.

    ``prior_activation`` answers from the journal AS READ IN THIS
    TRANSACTION (a decision consumed by an earlier delivery is found
    before the domain guard runs); ``commit_activation`` only STAGES the
    record — the caller writes the evidence, the active-plan switch and
    the outbox row and commits ONCE. Nothing durable happens unless that
    single ``commit()`` runs, which is the "one DB operation" the
    review demands.
    """

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
    """Re-bind the run's human gate to the activated plan (R32-11).

    The /approve-revision IS a human approval of the revised content, so
    the gate the next ``/go`` consumes must bind the NEW plan digest — a
    fresh approval generation copying the superseded round's window,
    base, policy and spec bindings (only the plan digest and the source
    identity move). Runs without a gate (a revision world that never
    opened one) rebind nothing: there is no approval surface to move,
    and the dispatch's ``stale_plan_digest`` fence still guards the
    boundary. Called INSIDE the activation transaction — the rebind
    commits with the switch or not at all.
    """
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
    """Approve + activate the run's pending revision in ONE DB transaction.

    The /approve-revision persistence leg (R28-17's twin for revisions).
    Inside a single session/commit:

    1. **record** — the evidence blob is read: the activations journal,
       the pending proposal slot and the durable ACTIVE-plan pointer;
    2. **verify binding tuple** — the pending decision is approved
       against the DURABLE current epoch, then the shared
       :func:`_binding_refusal` guard checks work/plan/proposal
       identity/content-digest/contract/parent/epoch against the
       durable ACTIVE-plan state — never the stash the proposal came
       with (a parent revision that moved since staging refuses);
    3. **switch active** — the activation record joins the journal, the
       ``active_plan`` pointer flips to the activated revision with its
       digest and the bumped publication epoch, and the pending slot is
       CONSUMED (removed);
    4. **commit** — one ``commit()`` carries the evidence write and the
       ``revision.activated`` outbox row, or nothing at all.

    Idempotent by decision id: a redelivered approval finds the consumed
    decision in the journal and returns ``already_active`` with the
    prior record — one revision switch, one continuation, no matter how
    many deliveries arrive. Every refusal consumes nothing.

    R36-13 (#272): the SAME transaction also persists the WIP reuse
    decision (:func:`decide_wip_reuse`) under
    :data:`CHECKPOINT_REUSE_DECISION_KEY` with a
    ``checkpoint.reuse_decision`` outbox row — the per-artifact
    preserve/invalidate/discard verdict plus the checkpoint's ROUTE
    (``preserve`` or an explicit ``fresh_attempt`` with its reason), so
    the dispatch boundary can refuse a ``required`` resume whose
    checkpoint the activation already routed away from the reuse.
    """
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
        # NEXT-20: the switched pointer remembers the digest it replaced,
        # so the next dispatch can refuse a /go still carrying the OLD
        # plan and the revised plan comment can say what it revised.
        # Q39-02 (#321): the same switch persists the activated revision's
        # CONTENT — the exact ``proposed`` bytes the guard digested (the
        # staged shape, not the returned copy whose parent was rewritten),
        # so a later dispatch resolves the approved TEXT from the same
        # source the CAS switched, and a tampered payload fails the
        # digest comparison at resolution instead of dispatching quietly.
        merged[ACTIVE_PLAN_KEY] = active_plan_document_of(
            current,
            record,
            revised_from_digest=str(active_raw.get("plan_digest") or ""),
            revision_content=proposed.model_dump(),
        )
        merged.pop(PENDING_PROPOSAL_KEY, None)  # the decision is consumed
        # R36-13 (#272): the WIP reuse decision lands in the SAME commit —
        # which artifacts survive the switch, and the checkpoint's explicit
        # route. Computed from the superseded digest the pointer carried and
        # the run's own durable records; an unreachable checkpoint authority
        # routes fresh_attempt (fail closed), never a silent preserve.
        superseded_digest = str(active_raw.get("plan_digest") or "")
        artifacts, uncertainty = await _durable_wip_artifacts(
            session_factory, evidence, run_id=run_id, superseded_plan_digest=superseded_digest
        )
        reuse = decide_wip_reuse(old_revision, activated, artifacts)
        if uncertainty:
            # A checkpoint MAY exist behind an authority that could not be
            # consulted — fail closed to the explicit fresh attempt, never
            # a silent preserve.
            reuse = replace(
                reuse,
                route=FRESH_ATTEMPT_ROUTE,
                route_reason=f"the checkpoint authority could not be consulted: {uncertainty}",
            )
        merged[CHECKPOINT_REUSE_DECISION_KEY] = reuse.document()
        run.evidence = merged
        # R32-11: the durable row follows the switch — the plan digest the
        # gate validates and the dispatch claims is the ACTIVE plan's from
        # here on, never the superseded comment's. The same transaction
        # also re-binds the human gate to the revised digest (a fresh
        # generation copying the superseded round's window/policy): the
        # /approve-revision WAS the human approval of the new content, so
        # the next /go consumes a decision bound to the plan it will run,
        # while a /go still carrying the OLD digest refuses at the
        # dispatch boundary (``stale_plan_digest``).
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
        # R36-13 (#272): the reuse decision's own observability row — the
        # route an operator (and the dispatch fence) reads without opening
        # the evidence blob.
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


# ---------------------------------------------------------------------------
# NEXT-20 — the dispatch leg: from human decision to changed execution
# ---------------------------------------------------------------------------
#
# The review's finding: "/approve-revision exists, but the journey from
# decision to changed plan isn't complete." Activation switches the
# durable pointer; the DISPATCH side must then read THAT pointer — never
# the old plan comment — so the next /go briefs the lane under the
# REVISED plan. Three pieces close the journey:
#
# - :func:`dispatch_plan_binding` — the /go seam: which plan the next
#   dispatch runs under, read from ``read_active_plan()`` (the durable
#   record). The binding's ``plan_digest`` is what the brief envelope is
#   built from, and a /go whose claimed digest is the SUPERSESED plan's
#   refuses ``stale_plan_digest`` (carrying the old comment's digest is
#   exactly the drift the fence exists for);
# - :func:`plan_comment_revision_note` — the revised plan comment's
#   footer: "revised from <old-digest> to <new-digest>", rendered from
#   the durable document's ``revised_from_digest``/``plan_digest`` pair;
# - the ``revised_from_digest`` field itself (see
#   :func:`active_plan_document_of` above), written by the same
#   activation transaction that switched the pointer.

#: The plan-comment marker line the revision note rides under (same
#: HTML-comment convention as the brief-envelope markers: invisible when
#: rendered, byte-stable in the raw body).
REVISION_NOTE_MARKER = "<!-- forge:plan:revision -->"


def plan_comment_revision_note(
    old_digest: str,
    new_digest: str,
    *,
    decided_by: str = "",
    decision_id: str = "",
) -> str:
    """The revised plan comment's footer (NEXT-20).

    The exact sentence the review demanded — ``revised from <old-digest>
    to <new-digest>`` — under a machine-extractable marker, with the
    human decision's provenance when the caller has it. The digests come
    from the durable active-plan document (``revised_from_digest`` and
    ``plan_digest``), so the note cannot drift from the record it
    describes.
    """
    lines = [REVISION_NOTE_MARKER, f"revised from {old_digest} to {new_digest}"]
    if decided_by or decision_id:
        lines.append(f"approved by {decided_by or 'unknown'} ({decision_id or 'unknown'})")
    return "\n".join(lines)


@dataclass(frozen=True)
class DispatchPlanBinding:
    """Which plan the next dispatch runs under (NEXT-20).

    ``status`` is ``ok`` (the binding names the ACTIVE plan — its
    ``plan_digest`` is the digest the brief envelope must be built from)
    or ``refused`` with a stable ``code``: ``no_active_plan`` (nothing
    durable to dispatch under), ``stale_plan_digest`` (the /go carries
    the SUPERSEDED plan's digest — the revision replaced it) or
    ``plan_digest_mismatch`` (a digest that is neither the active nor
    the superseded one — an unknown claim refuses like any other).
    """

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
        """The audit shape the dispatch boundary freezes into the run's
        evidence (R32-11): which plan the dispatched lane runs under,
        which digest it replaced, and — on refusal — the stable code."""
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
    """Bind the next /go dispatch to the ACTIVE plan — not the old comment.

    Reads the durable active-plan record via :func:`read_active_plan`
    (never the plan comment the approval thread may still show), and:

    - with no claim, returns the binding the caller briefs the lane
      under: the active revision, its canonical digest (the brief
      envelope's plan identity), and the digest it replaced;
    - with a claimed digest (a /go echoing the plan it was told about),
      the claim must equal the ACTIVE digest. A claim equal to
      ``revised_from_digest`` — the plan an approved revision just
      superseded — refuses ``stale_plan_digest``, naming both digests:
      the dispatch must re-read the revised plan, not run the stale
      one. Any other mismatch refuses ``plan_digest_mismatch``.

    Refusals consume nothing and change nothing — the caller re-issues
    the /go without a claim (or with the digest the durable record
    names).
    """
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


# ---------------------------------------------------------------------------
# R36-13 (#272) — proving the revision through the next executor input
# ---------------------------------------------------------------------------
#
# The review's finding: activation switches the durable pointer and the
# dispatch reads it — but the customer guarantee is about the WORK: the
# next REAL executor input carries the approved revision, compatible WIP
# survives the switch, and superseded authority cannot reactivate. Two
# pieces close that proof:
#
# - the WIP REUSE DECISION — when a material revision activates, ONE
#   persisted verdict partitions the run's work artifacts into
#   preserve / invalidate / discard and names the checkpoint's explicit
#   ROUTE (``preserve`` or ``fresh_attempt`` + reason). Not every
#   checkpoint is reusable; an undecidable one is never silently
#   preserved. The dispatch boundary reads the route and refuses a
#   ``required`` resume whose checkpoint was routed away.
# - the EXECUTOR-INPUT DIGEST — the identity the next dispatch actually
#   sends (run id + the ACTIVE plan digest + the brief envelope + the
#   spec + the resume mode), hashed canonically so the native ledger's
#   server-side fingerprint and the run's ``revision.executor_digest``
#   evidence must AGREE — the "exact digest recorded" acceptance.


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

#: The identity fields the executor-input digest is taken over — exactly
#: the dispatch inputs a native job receives, so any ledger that recorded
#: the inputs can recompute the digest and compare.
EXECUTOR_INPUT_FIELDS: tuple[str, ...] = (
    "run_id",
    "plan_digest",
    "envelope_digest",
    "spec_digest",
    "lane_resume_mode",
)


@dataclass(frozen=True)
class WorkArtifact:
    """One work artifact the reuse decision judges.

    ``kind`` is the closed vocabulary above. ``step_id`` scopes the
    artifact to one plan step (empty for plan-scoped artifacts — the
    workspace checkpoint, the run's verification verdict).
    ``applicability_digest`` is the plan digest the artifact was
    PRODUCED under — the applicability axis: an artifact is only as
    valid as the plan it was made for, and a revision that changed the
    plan changes what the artifact applies to.
    """

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
    """The partition a material revision's activation persists.

    ``route`` is the CHECKPOINT's explicit route: ``preserve`` (the WIP
    stands — a resume after the revision restores the exact bytes under
    the NEW plan) or ``fresh_attempt`` (with the reason — an explicit
    fresh attempt, never a silent reuse). ``decisions`` carries every
    artifact's own verdict; invalidated artifacts are SUPERSEDED, never
    deleted, and an invalidated verification never auto-returns to
    passed — only a fresh verification under the new plan can.
    """

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
    """Partition the work artifacts across a material revision's activation.

    The rules, per artifact — kind first, then applicability:

    - a STEP-SCOPED artifact whose step the new plan REMOVED is
      ``discard``ed (the work is no longer pursued — the artifact leaves
      the active set, still durable);
    - a step-scoped artifact whose step CHANGED or the revision NAMES in
      ``invalidated_step_ids`` is ``invalidate``d (superseded — its
      authority is withdrawn, its bytes stay);
    - a VERIFICATION artifact is only valid under the exact plan it
      verified: an applicability digest that is not the NEW plan's
      digest invalidates it. An invalidated verification NEVER
      auto-returns to passed — only a fresh verification does;
    - a CHECKPOINT artifact (plan-scoped workspace WIP) survives only
      when compatibility is PROVEN: with the superseded content staged,
      every write-carrying step the WIP anchors on must survive the
      revision byte-for-byte and un-invalidated. All such steps removed
      discards the checkpoint (the revision pursues different work);
      any such step changed or invalidated invalidates it; without the
      superseded content the decision FAILS CLOSED to invalidate — an
      undecidable checkpoint is never silently preserved.

    The ROUTE is the checkpoint verdict spelled for the dispatch
    boundary: ``preserve`` keeps the resume path standing; anything
    else is an explicit ``fresh_attempt`` with the reason.
    """
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
    """The run's PLAN-SCOPED artifacts, read from its own durable evidence.

    - the ``continuation`` document's pinned ``checkpoint_digest`` (the
      exact checkpoint a retry decision bound) — a CHECKPOINT artifact;
    - the ``verification`` fragment (the unified R02 verdict shape) — a
      VERIFICATION artifact whose applicability is the plan digest it
      was recorded under.

    Both carry ``applicability_digest=superseded_plan_digest``: they
    were produced under the plan the revision replaces.
    """
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
    """The artifacts + an uncertainty sentence, from the durable authorities.

    The run's own evidence contributes the plan-scoped artifacts; the
    CHECKPOINT AUTHORITY (the one composition point upload and resume
    share, resolved over the caller's factory so it opens its OWN short
    read session — never the activation's transaction) contributes the
    run's CURRENT held checkpoint when the evidence pins none. An
    authority that cannot be consulted answers ``(artifacts,
    uncertainty)`` — the caller routes fresh_attempt, never a silent
    preserve.
    """
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
    """The zero-arg callable handing the repository THIS transaction's session.

    The activation's artifact read shares the activation's transaction:
    an ``async with factory() as session`` over this closure re-enters
    the session the activation already holds (AsyncSession is its own
    async context manager), so the reuse decision and the switch commit
    together or not at all.
    """
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
    """The recorded fresh-attempt route when THIS dispatch would reuse the checkpoint.

    Returns the reuse-decision document when the dispatch carries the
    ``required`` resume contract (the mandated WIP restore) and the
    activation routed that checkpoint to an explicit ``fresh_attempt``
    — the silent reuse this fence exists to prevent. ``None`` in every
    other case: no recorded decision, a ``preserve`` route, or a
    dispatch that restores nothing (``fresh``) or discards on the
    operator's explicit instruction (``restart`` — the way out this
    fence points at).
    """
    if resume_mode != "required":
        return None
    document = await read_wip_reuse_decision(session_factory, run_id)
    if document is not None and document.get("route") == FRESH_ATTEMPT_ROUTE:
        return document
    return None


def executor_input_digest(identity: Mapping[str, Any]) -> str:
    """The canonical digest of one dispatch's executor-input identity.

    Hashed over EXACTLY :data:`EXECUTOR_INPUT_FIELDS` — the identity
    inputs a native job receives — canonicalized field-sorted, so the
    producer (the dispatch), the receiver's ledger (the native server's
    recorded inputs) and the auditor (the run's evidence) all compute
    the same digest over the same bytes, and any disagreement is drift
    by construction.
    """
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
    """The ``revision.executor_digest`` evidence document (R36-13).

    Freezes WHAT the dispatched executor runs under: the ACTIVE plan's
    digest (never the superseded comment's), the revision number, the
    digest it replaced, and the executor-input digest recomputable from
    the native ledger's recorded inputs.
    """
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


# ---------------------------------------------------------------------------
# Q39-02 (#321) — the ApprovedInput: the executor's brief binds to the
# ACTIVE revision's TEXT, not the superseded spec's
# ---------------------------------------------------------------------------
#
# The live counterexample (docs/evaluation/2026-09-25-combined-steering,
# cycle 1): the operator's approved revision renamed the public entrypoint
# (approach Y), the activation CAS switched identity AND digest, the WIP
# reuse preserved the renamed checkpoint — and the resumed lane REVERTED to
# approach X, because the GitLab dispatch still briefed it from the
# spec-frozen ``plan_summary``. The run needed a SECOND manual standing
# steer to survive. Identity → CONTENT is the missing join, closed in three
# pieces:
#
# - :class:`ApprovedInput` — ONE immutable, frozen record of everything the
#   dispatched executor may run under: the task, the ACTIVE plan TEXT, the
#   revision identity/digest, the work-contract digest, the evidence the
#   reuse decision preserved, the allowed writes, and the exact WIP reuse
#   decision. Resolved at EVERY dispatch entry by
#   :func:`resolve_approved_input`; a run with no active revision keeps
#   today's behavior under an explicit ``source: spec`` label.
# - the brief envelope — the GitLab ``FORGE_PLAN`` bytes are GENERATED from
#   the resolved record (:meth:`ApprovedInput.brief`), so the brief is a
#   pure function of durable state; the dispatch persists the record and
#   its executor-input digest beside the native-start intent, and the
#   three-way digest discipline (evidence == the native ledger's recorded
#   inputs == the bytes the runner consumes) proves the rebind end to end.
# - :func:`stage_standing_guidance_promotion` — the #313 cycle-2 remedy
#   formalized: accepted STANDING guidance promotes into durable revision
#   content through the EXISTING approval route (a staged pending proposal
#   a human must ``/approve-revision``); a next-turn hint that was never
#   approved NEVER expands authority.

#: The approved-input document's schema (the versioned input contract the
#: rollout note names: legacy attempts carry no such section and resolve
#: through the labeled adapter below).
APPROVED_INPUT_SCHEMA = "forge.revision.approved-input/1"

#: Where the dispatch's resolved approved input lives in the evidence.
APPROVED_INPUT_KEY = "approved_input"

#: The resolution's source labels. ``revision`` — an activated revision's
#: durable content IS the brief source. ``spec`` — no revision was ever
#: activated; the frozen spec's plan is the brief (today's behavior,
#: labeled). ``spec-legacy`` — a prior-version pointer carries no revision
#: content (persisted before Q39-02): the EXPLICIT legacy adapter keeps the
#: spec brief under its own label, never a silent re-derivation.
APPROVED_SOURCE_REVISION = "revision"
APPROVED_SOURCE_SPEC = "spec"
APPROVED_SOURCE_SPEC_LEGACY = "spec-legacy"


class RevisionRebindRefused(ValueError):
    """The typed refusal approved-input resolution raises (Q39-02).

    ``code`` is the stable machine-readable reason (the
    ``revision.rebind_refused{reason}`` observability dimension); the
    dispatch parks the run BEFORE any provider I/O — never a fallback to
    the superseded brief.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"approved-input resolution refused [{code}] {detail}")
        self.code = code
        self.detail = detail


def _bytes_digest(text: str) -> str:
    """sha256 over the exact UTF-8 bytes of one text field (A03's shape)."""
    return sha256(text.encode("utf-8")).hexdigest()


def render_revision_brief(
    revision: PlanRevision,
    *,
    plan_digest_value: str = "",
    activated_by_decision: str = "",
    wip_reuse: Mapping[str, Any] | None = None,
    evidence_refs: Iterable[str] = (),
) -> str:
    """The executor brief rendered from one ACTIVE revision's content.

    A pure function of durable material only — the revision's summary and
    step objectives, the digest the activation CAS switched, the WIP reuse
    route, and the preserved evidence references. Deterministic by
    construction, so two workers resolving the same durable state render
    byte-identical briefs and the brief's content digest is reproducible.
    """
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
    """The ONE immutable executor-input record every dispatch resolves.

    Every field comes from durable state read at the dispatch entry — the
    frozen task text, the ACTIVE revision's identity, digest and rendered
    TEXT (or the spec's plan under an explicit ``source`` label), the
    work-contract digest the activation guarded, the evidence the WIP
    reuse decision preserved, the allowed writes, and the reuse decision
    itself. A restarted worker resolving the same durable rows
    reconstructs the identical record — there is no clock, no counter and
    no session memory in it.
    """

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
        """The executor brief — the record's plan TEXT, byte-exactly.

        The revision-bound record's text was RENDERED at resolution (a
        pure function of the durable revision content — see
        :func:`render_revision_brief`); the spec sources carry the frozen
        spec's plan text byte-identically (today's behavior — the labels
        live in the persisted document, never in changed legacy bytes).
        The dispatch never re-renders: brief == the resolved record.
        """
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
    """Resolve the executor's approved input from durable state (Q39-02).

    Called at EVERY dispatch entry (start, retry, revival, repair
    re-dispatch). One read; three honest outcomes:

    - **no active revision** — the frozen spec's plan is the brief,
      labeled ``source: spec`` (today's behavior, explicit);
    - **an active revision with durable content** — the content must
      re-parse and its canonical digest must equal the pointer's
      ``plan_digest`` (the digest the activation CAS switched); any
      mismatch is a typed :class:`RevisionRebindRefused` — fail closed,
      never a quiet fallback to the superseded brief. The rendered
      revision text becomes the brief, labeled ``source: revision``;
    - **an active revision whose pointer predates Q39-02** (no content
      field) — the EXPLICIT legacy adapter: the spec's plan stays the
      brief under the label ``source: spec-legacy``. The superseded
      revision's bytes are NOT re-derived from anything.

    The WIP reuse decision (when the activation persisted one) rides the
    record verbatim; its preserved artifacts are the evidence references.
    """
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
        # The prior-version pointer: identity without content. The legacy
        # adapter keeps the spec brief under its own label — the record
        # can never pretend to know the superseded revision's bytes.
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


# ---------------------------------------------------------------------------
# Q39-02 (#321) — the standing-guidance promotion (the #313 cycle-2 remedy)
# ---------------------------------------------------------------------------

#: The closed marker vocabulary that classifies an operator's guidance as
#: STANDING (a direction that must outlive the current turn). A steer with
#: none of these markers is a next-turn hint: it may shape the live turn,
#: never the durable plan. The classification lives HERE (with the rest of
#: the revision rules), not in the steering channel — the channel delivers,
#: the revision gate owns authority.
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
    """True only when *text* declares a STANDING direction.

    Fail closed by vocabulary: an unmarked instruction is a next-turn hint
    — it NEVER promotes, so nothing that was not explicitly standing can
    expand the durable plan's authority.
    """
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _STANDING_GUIDANCE_MARKERS)


def standing_guidance_revision(
    active: PlanRevision, text: str, *, command_id: str = ""
) -> PlanRevision | None:
    """The PENDING proposal an accepted standing steer promotes to.

    The promoted revision keeps every STEP byte-identical (so the WIP the
    checkpoint holds stays compatible — :func:`decide_wip_reuse` sees no
    churn) and absorbs the standing text into the SUMMARY with its
    provenance. The revision number follows the active one. ``None`` when
    the text is not standing guidance: a next-turn hint promotes to
    NOTHING. The return value is a PROPOSAL — it carries no authority
    until a human approves it through the existing route.
    """
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
    """Promote accepted standing guidance into a PENDING revision (Q39-02).

    The #313 cycle-2 remedy, formalized: the operator's channel for a
    direction that must outlive a continuation is the DURABLE PLAN, not a
    repeated steer. This leg resolves the active revision's durable
    content and stages the promoted proposal through the EXISTING
    :func:`stage_pending_revision` route — the human gate still decides
    (``/approve-revision`` still owns the activation CAS). Returns the
    staged decision id, or ``None`` when the text is not standing
    guidance (nothing staged, nothing expanded) or the run carries no
    resolvable active revision content (the promotion never guesses: an
    identity-only pointer or an unreadable payload refuses rather than
    re-deriving plan bytes).
    """
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


# ---------------------------------------------------------------------------
# Q39-13 (#332) — post-MR review feedback as a new bounded revision on the
# CURRENT candidate
# ---------------------------------------------------------------------------
#
# The review's most practical next scenario: a reviewer names a specific
# edit on the Draft MR; the request binds to the CURRENT MR head; human
# changes are preserved; only the permitted thing is fixed; the affected
# checks rerun; a new reviewable result comes back. The workflow does not
# end at the first Draft MR — but reviewer feedback needs the SAME
# exact-input + authority discipline as initial implementation, so this
# section EXTENDS the revision machinery above (no new controller):
#
# - :class:`ReviewFeedbackRequest` — ONE immutable, frozen record per
#   reviewer comment (``forge.review.feedback/1``), keyed durably by the
#   originating note id: one reviewer comment → ONE durable request,
#   however many times the webhook replays. The record binds the request
#   to the CURRENT MR head (``head_sha``), the originating discussion, the
#   authorized actor and the approved work scope.
# - :func:`classify_review_feedback` — the closed three-way
#   classification: ``clarification`` (a question — answered, no
#   dispatch), ``in-scope_correction`` (a bounded input revision) and
#   ``material_change`` (the EXISTING material-revision approval route —
#   never a permission expansion). Fail closed like every other gate
#   here: a correction whose scope cannot be PROVEN inside the approved
#   write scope is material, never a guessed in-scope edit.
# - :func:`stage_review_correction` — the bounded correction: the active
#   revision with the correction folded into its SUMMARY (steps
#   byte-identical, so the held WIP stays compatible), staged through the
#   EXISTING :func:`stage_pending_revision` human gate. The correction
#   becomes the active revision TEXT through the existing activation CAS,
#   so the #321 ApprovedInput machinery briefs the next executor with the
#   correction + the referenced diff context + the current head — never an
#   obsolete source version.
# - :func:`head_binding_guard` — the head fence: the request records the
#   MR head at request time; at the correction's dispatch the expected
#   head is re-checked, and an unexpected head move is a typed
#   ``stale_head`` conflict (human edits preserved, never force-
#   overwritten).
# - :func:`correction_invalidation_set` — precise evidence invalidation on
#   the applicability axis: only the evidence produced under the head the
#   correction targets is superseded; everything else is preserved. The
#   required checks + the review rerun for the NEW candidate before any
#   readiness claim (the sha-binding replay guards own that in the lane).

#: The review-feedback request document's schema.
REVIEW_FEEDBACK_SCHEMA = "forge.review.feedback/1"

#: Where the run's review-feedback requests live inside ``FlowRun.evidence``
#: — a dict keyed by the originating note id (the idempotency key).
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
#: R40-02 (#338): the request admitted a linked post-readiness round —
#: the round's child run carries the correction; the parent's terminal
#: record is untouched (the linkage lives in ``review_rounds``).
REQUEST_ROUND_ADMITTED = "round_admitted"
#: R40-02 (#338): the MR is merged/closed — eligibility refused, zero
#: commits (the human decision already ended the collaboration surface).
REQUEST_MR_CLOSED = "mr_closed"
#: R40-02 (#338): the bounded round count is exhausted — the operator
#: policy (FORGE_MAX_REVIEW_ROUNDS) refuses further rounds on the lineage.
REQUEST_ROUND_LIMIT = "round_limit"


class ReviewFeedbackRefused(ValueError):
    """The typed refusal the review-feedback legs raise (Q39-13).

    ``code`` is the stable machine-readable reason (the
    ``review_feedback.stale_head`` observability dimension and friends);
    ``detail`` explains the mismatch for the audit trail. Callers branch
    on ``code``, never on message text — the same contract
    :class:`ActivationRefused` established for activations.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"review feedback refused [{code}] {detail}")
        self.code = code
        self.detail = detail


#: ``/fix <bounded description>`` — the correction request (a named edit).
_REVIEW_FIX_RE = re.compile(r"/fix\s+(.+)", re.IGNORECASE | re.DOTALL)
#: ``/ask <question>`` — the clarification (a question, no code request).
_REVIEW_ASK_RE = re.compile(r"/ask\s+(.+)", re.IGNORECASE | re.DOTALL)
#: A path token inside backticks (`` `src/app.py` ``) — the explicit scope
#: claim a reviewer makes when naming the edit's target. Deliberately
#: narrow: a bare word is not a path claim, so an unscoped correction
#: fails closed to the material route instead of guessing a scope.
_PATH_TOKEN_RE = re.compile(r"`([^`\n]+)`")


def parse_review_feedback_note(note_text: str) -> tuple[str, str] | None:
    """Parse a review-feedback note: ``(kind, body)`` or ``None``.

    ``kind`` is ``fix`` (a named edit — the bounded correction request)
    or ``ask`` (a question — the clarification). Every other note shape
    is NOT review feedback (``None``): the ingress ignores it exactly
    like an unparseable ``/go``.
    """
    text = str(note_text or "").strip()
    fix = _REVIEW_FIX_RE.search(text)
    if fix is not None:
        return "fix", fix.group(1).strip()
    ask = _REVIEW_ASK_RE.search(text)
    if ask is not None:
        return "ask", ask.group(1).strip()
    return None


def referenced_paths_of(text: str) -> tuple[str, ...]:
    """The paths a reviewer explicitly claimed, in note order.

    Only backticked tokens count (``rename `src/entry.py` to ...``) — an
    explicit scope claim, not an inference. A correction naming no path
    has an UNPROVABLE scope and can never classify in-scope.
    """
    lowered = str(text or "")
    return tuple(
        dict.fromkeys(token.strip() for token in _PATH_TOKEN_RE.findall(lowered) if token.strip())
    )


def _path_inside_scope(path: str, allowed: Iterable[str]) -> bool:
    """Whether one claimed path falls inside the approved write scope.

    The SAME matcher the lane's changeset validation uses (fnmatch
    globs — ``*`` spans ``/``), plus the directory-prefix reading for a
    plain entry (``src`` / ``src/`` covers ``src/app.py``): the reviewer
    classification and the enforcement boundary must agree on what the
    frozen ``allowed_paths`` mean, or the gate would approve edits the
    writer refuses (or vice versa). An empty allowed scope authorizes
    nothing.
    """
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
    """Classify one review-feedback note into the closed vocabulary.

    - ``ask`` → :data:`CLARIFICATION_CLASS` — a question. Answered,
      routed to the approvers; NEVER a dispatch (reviewer-only recovery
      consumes no implementation budget).
    - ``fix`` → the correction must PROVE its scope: every explicitly
      claimed path inside the approved write scope →
      :data:`IN_SCOPE_CORRECTION_CLASS`. No path claimed at all (the
      scope is unknowable) or any claimed path outside the approved
      scope → :data:`MATERIAL_CHANGE_CLASS` — the EXISTING
      material-revision approval route. Out-of-scope is NEVER a
      permission expansion: the bounded lane simply refuses to widen the
      write surface a human approved.
    """
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
    """ONE immutable reviewer comment, durably recorded (Q39-13).

    Every field is durable, provider-side material read at the ingress:
    the originating note id (the idempotency key — one reviewer comment
    → ONE request despite webhook replay), the discussion the note lives
    in, the CURRENT MR head at request time, the authorized actor, the
    classification, the requested text and the explicitly claimed paths,
    and the referenced diff context (the diff-position paths a
    positioned note carries). ``status`` is the request's lifecycle; the
    record never mutates in place — transitions return a new record, and
    the durable store keeps both sides.
    """

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
    #: The precise evidence invalidation the staging recorded (which
    #: items the correction supersedes, which survive) — applicability
    #: partitioned at staging time, re-readable from the record.
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
    """The immutable identity fields — everything but lifecycle + clock.

    A replayed delivery re-derives the SAME identity (the same note, the
    same bindings, the same classification); the lifecycle ``status`` and
    ``created_at`` legitimately move with time and must never turn a
    replay into a conflict.
    """
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
    """Record the request ONCE — the note id is the idempotency key.

    A replayed delivery of the SAME comment returns the recorded request
    unchanged (no second request, no second outbox row — the replay's
    fresh clock reading does not make it a different comment). A
    DIFFERENT request under an already-used note id refuses
    ``note_id_conflict`` — the identity is the provider's note, and one
    note is one request.
    """
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
    """Refuse, typed, when the MR head moved off the request's binding.

    The request recorded the CURRENT MR head at request time; the
    correction's dispatch re-checks it and any head move — a human edit
    landed between request and publication — is the ``stale_head``
    conflict: human edits are preserved, NEVER force-overwritten. The
    reviewer re-raises the correction against the new head.
    """
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
    """Partition the run's evidence by APPLICABILITY to the corrected head.

    The applicability axis the reuse machinery already speaks: an
    evidence item carries the head (candidate sha) it was produced
    under. The correction targeting ``expected_head`` invalidates ONLY
    the items produced under that head (the review + the verification of
    the candidate being corrected — superseded, never deleted, and an
    invalidated verification never auto-returns to passed); everything
    else is preserved as-is. "The plan changed somewhere" is never
    grounds to drop evidence — the correction must NAME its victims.
    """
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
    """The staged correction's decision id — derived from the note identity.

    Deterministic by construction: a replayed delivery re-derives the
    SAME decision id, so the note's identity and the staged proposal's
    identity can never drift apart.
    """
    digest = sha256(f"{run_id}:{note_id}".encode("utf-8")).hexdigest()[:16]
    return f"rd-review-{digest}"


def review_correction_revision(
    active: PlanRevision, request: ReviewFeedbackRequest
) -> PlanRevision:
    """The bounded input revision an in-scope correction stages (Q39-13).

    Every STEP stays byte-identical — the correction rewords the
    revision's own guidance, so the WIP the checkpoint holds remains
    compatible (:func:`decide_wip_reuse` sees no churn) and only the
    permitted thing changes. The correction rides the SUMMARY with its
    full provenance: the originating discussion + note, the head the
    request is bound to, the permitted change the reviewer named, and
    the referenced diff context — the exact material the next executor's
    brief renders (:func:`render_revision_brief`), so the agent sees the
    REFERENCED DIFF and the CURRENT HEAD, never an obsolete source
    version. The return value is a PROPOSAL: it carries no authority
    until a human approves it through the existing activation route.
    """
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
    """The run's ACTIVE revision content + the pointer document it came from.

    The correction never re-derives plan bytes: an identity-only pointer
    (a prior-version document without content) or an unreadable payload
    refuses — the operator re-raises the proposal instead.
    """
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
    """Stage the bounded correction through the EXISTING approval route.

    Resolves the active revision's durable content, folds the correction
    in (:func:`review_correction_revision`), records the precise evidence
    invalidation (:func:`correction_invalidation_set`), and stages the
    proposal through :func:`stage_pending_revision` — the SAME human gate
    ``/approve-revision`` owns. Nothing dispatches here: the correction
    becomes the active revision TEXT only when a human approves it, and
    the next dispatch reads it through the #321 ApprovedInput machinery.
    """
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
    # The request is durably recorded BEFORE the staged mark (a caller
    # that staged without recording still lands ONE request — the note id
    # remains the idempotency key either way).
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


def review_feedback_summary_section(
    requests: Mapping[str, ReviewFeedbackRequest],
    discussion_states: Mapping[str, bool],
    candidate_sha: str,
) -> str:
    """The ``**Review feedback:**`` block the final summary appends.

    Names the resolved and the still-open discussions (each line carries
    the discussion id, the request's classification and its actor) and
    the TESTED candidate the summary covers. A repair that passes its
    tests while a required discussion stays unresolved is NOT ready —
    this section is what makes that honest instead of implied. Empty
    string when the run recorded no review feedback (the legacy comment
    shape stays byte-identical).
    """
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


# ---------------------------------------------------------------------------


# R40-02 (#338) — the linked post-readiness review round
# ---------------------------------------------------------------------------
#
# The verified gap: ``correction_window_closed`` answered every /fix after
# ``ready_for_human`` — while reviewers ask for corrections AFTER readiness
# is announced. The round this section shapes is the review's own model:
#
#     Delivery 1 — immutable, ready.
#         ↓ human requests a change
#     Review round 2: current MR head + request + scope + budget + approval
#         ↓
#     New candidate, new checks, new review.
#
# The terminal record is never reopened: the round is a NEW linked work
# unit (its own FlowRun, budget, base head and execution generation), and
# everything here is PURE — the derivation of the round's active-plan
# representation from durable material, with the service owning the I/O:
#
# - :func:`classic_spec_revision` — the VERIFIED adapter for ordinary
#   classic runs that never staged an adaptive revision (no
#   ``active_plan`` pointer): the initial approved input is DERIVED, never
#   guessed, from exactly two sources — the digest-verified frozen RunSpec
#   document and the accepted request. A parent that DID stage a revision
#   chains from its durable content instead (the #337 world); nothing else
#   contributes to the round's plan.
# - :func:`round_active_plan_seed` — the ACTIVE-plan document the round's
#   child run is seeded with: the folded correction revision as CONTENT,
#   the parent's (or adapter's) digest as ``revised_from_digest``, and the
#   round's deterministic decision identity as the activation authority.
#   :func:`resolve_approved_input` then briefs the round's executor from
#   this document at every dispatch entry — the #321 join, unchanged.

#: The round's seeded active-plan document schema (the seed is written by
#: the round admission itself — the authorized /fix + eligibility IS the
#: activation authority, recorded as ``activated_by_decision``).
ROUND_SEED_SCHEMA = "forge.revision.round-seed/1"

#: The closed set of spec document fields the classic adapter reads — the
#: derivation's own honesty bound: nothing outside these frozen, verified
#: values (plus the accepted request) shapes the round's plan.
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
    """The verified classic-run adapter: frozen spec → revision 1 (R40-02).

    An ORDINARY classic run never staged a PlanRevision — its approved
    input is the frozen RunSpec — so a post-readiness round has no active
    revision to fold the correction into. This adapter derives the initial
    usable representation from EXACTLY two durable sources:

    - the digest-verified frozen spec document (the same bytes the gate
      approved; the caller loads it through the verified spec read, never
      a live re-read), and
    - the accepted request (the classified, authorized /fix).

    The derivation is deterministic and total: one step carrying the
    frozen plan objective (the plan the approver saw, not a re-plan), the
    spec's task text as the revision summary, digests derived from the
    adapter's canonical projection of the spec, and the correction then
    folded through the EXISTING :func:`review_correction_revision` (steps
    byte-identical to the derived base, the correction riding the summary
    with its full provenance). The returned revision is a PROPOSAL-shaped
    base + fold: it carries no authority until the round admission seeds
    it as the child's active plan under the round's decision identity.
    """
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
    """The ACTIVE-plan document a review round's child run is seeded with.

    The same shape :func:`active_plan_document_of` writes for an activated
    revision, minted here for the round's OWN work unit: the folded
    correction revision is the CONTENT (so :func:`resolve_approved_input`
    digests and briefs from it — the #321 join), ``revised_from_digest``
    records what the round supersedes (the parent's active digest, or the
    classic adapter's derived base for a spec-run), and the decision id is
    the round's deterministic correction identity — the audit name the
    admission, the request lifecycle and the seeded pointer all share.
    """
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
