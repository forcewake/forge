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

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Literal, Protocol

from forge.adaptive.models import PlanRevision, PlanStep, WorkContract

__all__ = [
    "ACTIVE_PLAN_KEY",
    "ACTIVE_PLAN_SCHEMA",
    "ACTIVATION_SCHEMA",
    "ActivationRecord",
    "ActivationRefused",
    "ActivationSession",
    "ActivePlanState",
    "DispatchPlanBinding",
    "DurableActivationOutcome",
    "PENDING_PROPOSAL_KEY",
    "PENDING_PROPOSAL_SCHEMA",
    "Question",
    "REVISION_ACTIVATIONS_KEY",
    "REVISION_NOTE_MARKER",
    "RevisionDecision",
    "TacticalPolicy",
    "activate_pending_revision",
    "activate_revision",
    "active_plan_document_of",
    "apply_tactical",
    "change_log",
    "classify_revision",
    "decision_record",
    "dispatch_plan_binding",
    "fresh_session_brief",
    "invalidation_set",
    "parse_tactical_policy",
    "plan_comment_revision_note",
    "plan_digest",
    "proposed_revision_identity",
    "read_active_plan",
    "route_question",
    "stage_pending_revision",
    "stale_callback_guard",
    "transformation_kinds",
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
) -> dict[str, Any]:
    """The durable ACTIVE-plan pointer the dispatch leg reads.

    The document carries the switched revision, its canonical digest
    (what :func:`stale_callback_guard` compares late native callbacks
    against), and the NEW publication epoch — one durable answer to
    "which revision is active", replacing the in-memory function call.

    NEXT-20: ``revised_from_digest`` records the digest of the plan this
    one REPLACED (the prior active document's own digest; empty for a
    first activation, or when the prior pointer carried no digest). The
    dispatch leg uses it to refuse a stale ``/go`` that still carries
    the SUPERSEDED plan's digest, and the revised plan comment renders
    its "revised from <old> to <new>" note from this pair — the journey
    from human decision to changed execution is one durable document.
    """
    return {
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


async def stage_pending_revision(
    session_factory: _RevisionSessionFactory,
    run_id: str,
    decision: RevisionDecision,
    proposed: PlanRevision,
    current: ActivePlanState,
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
        merged[ACTIVE_PLAN_KEY] = active_plan_document_of(
            current,
            record,
            revised_from_digest=str(active_raw.get("plan_digest") or ""),
        )
        merged.pop(PENDING_PROPOSAL_KEY, None)  # the decision is consumed
        run.evidence = merged
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


def _dispatch_refused(run_id: str, code: str, reason: str) -> DispatchPlanBinding:
    return DispatchPlanBinding(status="refused", run_id=run_id, code=code, reason=reason)


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
        )
    return _dispatch_refused(
        run_id,
        "plan_digest_mismatch",
        f"the /go claims plan digest {claimed} but the active plan is {digest}"
        " — an unknown claim refuses like any other",
    )
